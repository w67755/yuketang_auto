"""雨课堂课件 / PPT 自动观看与漏页定向补看模块。

针对雨课堂网页版课件页（如 `/v2/web/studentCards/...`）的关键机制做了专门适配：
1. 左侧缩略图栏采用**虚拟滚动（Virtual Scroll）**：DOM 中任意时刻只渲染约 10~12 个可见页，
   因此绝不能按 DOM 数组下标 `slides[i]` 盲点（否则滚动后节点复用会导致跳页、漏页和提前结束）。
2. 正确做法——按真实页码 `1 -> total_pages` 顺序驱动：
   - 优先从顶栏「学习进度：done/total」（如 `10/25`、`0/75`）读取真实总页数；
   - 严格按页码 `1, 2, 3, ..., total_pages` 依次在左侧虚拟列表中定向滚动查找对应页码缩略图；
   - 若该页缩略图已带「已完成」标记，直接跳过节省时间；
   - 否则点击该页并按配置的随机区间 `[ppt_dwell_min, ppt_dwell_max]` 停留；
3. 精准完成校验与漏页定向补看：
   - 翻完后同时检查顶栏「学习进度：done/total」是否达到 `done >= total`，以及是否出现
     「您还有以下页面未看」提示；
   - 若仍有未完成页，自动延长停留时长定向补看未完成页（最多 `ppt_retry_rounds` 轮）。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from core.browser import interruptible_sleep
from core.selectors import SELECTORS
from tasks.base import BaseTask, TaskKind, TaskResult, TaskStatus

# 在左侧虚拟滚动缩略图栏中按精确页码查找并定向滚动的 JS
# 返回结构：{ found, completed, active, element, minPage, maxPage, moved, hasSidebar }
LOCATE_VIRTUAL_SLIDE_JS = r"""
const targetPage = Number(arguments[0]);
const doScroll = Boolean(arguments[1]);

// 1. 定位左侧缩略图区域内的可滚动容器（位于视口左侧 40% 以内，且可竖向滚动）
function findLeftScrollContainer() {
  const all = Array.from(document.querySelectorAll('div, ul, section, aside'));
  let best = null;
  let bestScore = -1;
  for (const el of all) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 80 || rect.width > window.innerWidth * 0.42) continue;
    if (rect.height < 200 || rect.left > window.innerWidth * 0.30) continue;
    if (el.scrollHeight <= el.clientHeight + 10) continue;
    const style = window.getComputedStyle(el);
    if (!['auto', 'scroll', 'overlay'].includes(style.overflowY)) continue;
    const imgCount = el.querySelectorAll('img, canvas').length;
    const score = imgCount * 100 + rect.height;
    if (score > bestScore) {
      bestScore = score;
      best = el;
    }
  }
  return best;
}

// 2. 在容器内提取每个缩略图卡片对应的真实页码（1-based）、完成态与激活态
function collectThumbItems(scope) {
  const root = scope || document;
  const raw = Array.from(
    root.querySelectorAll(
      '[class*="slide"], [class*="thumb"], [class*="ppt-item"], [class*="page-item"], [class*="card-item"], li, .swiper-slide'
    )
  );
  const byPage = new Map();
  for (const el of raw) {
    const rect = el.getBoundingClientRect();
    // 缩略图项位于页面左侧栏，高度合理，避免选到整个大容器或单行文字
    if (rect.left > window.innerWidth * 0.35) continue;
    if (rect.width < 60 || rect.width > window.innerWidth * 0.38) continue;
    if (rect.height < 40 || rect.height > 240) continue;

    // 必须包含缩略图图片或画布，过滤掉“全部 / 未完成”等导航标签
    if (!el.querySelector('img, canvas')) continue;

    // 跳过包裹多个缩略图的父容器
    const childImgs = el.querySelectorAll('img, canvas').length;
    if (childImgs > 3) continue;

    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const numMatch = text.match(/\b(\d{1,4})\b/);
    if (!numMatch) continue;
    const pageNo = Number(numMatch[1]);
    if (pageNo < 1 || pageNo > 2000) continue;

    const cls = (el.className || '').toString().toLowerCase();
    const unread = text.includes('未读') || text.includes('未看') || text.includes('未完成') || cls.includes('unread') || cls.includes('unviewed');
    const completed = !unread && (text.includes('已完成') || cls.includes('finish') || cls.includes('complete'));
    const active = cls.includes('active') || cls.includes('selected') || cls.includes('current');

    // 同一页码优先保留面积更紧凑的内层卡片元素
    const prev = byPage.get(pageNo);
    const area = rect.width * rect.height;
    if (!prev || area < prev.area) {
      byPage.set(pageNo, { pageNo, element: el, completed, unread, active, area });
    }
  }
  const arr = Array.from(byPage.values()).sort((a, b) => a.pageNo - b.pageNo);
  // 标准版雨课堂课件页：未看页标有紫色“未读”角标，已看页无角标；若列表中存在“未读”项，则无“未读”项即为已完成！
  if (arr.some((it) => it.unread)) {
    for (const it of arr) {
      if (!it.unread) it.completed = true;
    }
  }
  return arr;
}

const container = findLeftScrollContainer();
const items = collectThumbItems(container);
const hasSidebar = Boolean(container) || items.length >= 2;

if (!items.length) {
  return { found: false, hasSidebar, minPage: null, maxPage: null, moved: false };
}

const minPage = items[0].pageNo;
const maxPage = items[items.length - 1].pageNo;
const hit = items.find((it) => it.pageNo === targetPage);

if (hit) {
  return {
    found: true,
    hasSidebar,
    pageNo: hit.pageNo,
    completed: hit.completed,
    active: hit.active,
    element: hit.element,
    minPage,
    maxPage,
    moved: false,
  };
}

let moved = false;
if (doScroll && container) {
  const before = container.scrollTop;
  const step = Math.max(180, Math.floor(container.clientHeight * 0.55));
  if (targetPage > maxPage) {
    container.scrollTop = Math.min(container.scrollHeight, before + step);
  } else if (targetPage < minPage) {
    container.scrollTop = Math.max(0, before - step);
  }
  moved = Math.abs(container.scrollTop - before) > 2;
}

return { found: false, hasSidebar, minPage, maxPage, moved };
"""


def parse_study_progress(text: str) -> Optional[Tuple[int, int]]:
    """从雨课堂顶栏文本（如 '学习进度：10/25'、'学习进度: 0/75'）解析 (已看页数, 总页数)。"""
    if not text:
        return None
    match = re.search(r"学习进度[:：\s]*(\d{1,4})\s*/\s*(\d{1,4})", text)
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    if total <= 0 or done < 0 or done > total:
        return None
    return done, total


def parse_page_counter(text: str) -> Optional[Tuple[int, int]]:
    """从页码文本（如 '18 / 70'、'第 5/32 页'）中解析 (当前页, 总页数)。"""
    if not text:
        return None
    match = re.search(r"(\d{1,4})\s*/\s*(\d{1,4})", text)
    if not match:
        return None
    current, total = int(match.group(1)), int(match.group(2))
    if total <= 0 or current <= 0 or current > total:
        return None
    return current, total


def parse_unviewed_pages(page_text: str, badge_texts: Iterable[str] = ()) -> List[int]:
    """从结束页提示文本或未看标签列表中提取去重且升序排列的未看页码。"""
    pages: set[int] = set()

    for raw in badge_texts:
        cleaned = (raw or "").strip()
        if re.fullmatch(r"\d{1,4}", cleaned):
            num = int(cleaned)
            if 1 <= num <= 2000:
                pages.add(num)

    if page_text:
        pattern = re.search(
            r"(?:以下页面未看|未观看页面|未看页面|未读页面)[:：\s]*([\d\s,，、]+)",
            page_text,
        )
        if pattern:
            for token in re.findall(r"\d{1,4}", pattern.group(1)):
                num = int(token)
                if 1 <= num <= 2000:
                    pages.add(num)

    return sorted(pages)


class PptWatcher(BaseTask):
    """负责单份课件/PPT 的按页码顺序停留与未看页自动补看。"""

    def execute(self, title: str) -> TaskResult:
        driver = self.browser.ensure_started()
        self.browser.inject_anti_blur_now()
        # 给 SPA 课件页留 1.5 秒加载顶栏与左侧虚拟缩略图列表
        if not interruptible_sleep(1.5, self.stop_event):
            return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

        current_url = (driver.current_url or "").lower()
        if "/studycontent" in current_url or "/studentlog/" in current_url:
            return TaskResult(title, TaskKind.PPT, TaskStatus.FAILED, "当前仍停留在课程列表页，未成功打开课件详情页")

        self.logger.info(
            "【课件】开始观看：%s（每页随机停留 %.1f~%.1f 秒，最多补看 %d 轮）",
            title,
            self.config.ppt_dwell_min,
            self.config.ppt_dwell_max,
            self.config.ppt_retry_rounds,
        )

        # 1. 若顶栏已显示学习进度达成 100%（例如 25/25），直接跳过
        progress = self._read_study_progress(driver)
        if progress is not None:
            done, total = progress
            self.logger.info("【课件】当前顶栏学习进度：%d/%d", done, total)
            if done >= total:
                self.logger.info("【课件】《%s》进度已满（%d/%d），无需重复观看。", title, done, total)
                return TaskResult(title, TaskKind.PPT, TaskStatus.SKIPPED, f"学习进度已满 {done}/{total}")
            # 半程课件（如 70/75）：优先切换到左侧栏「未完成」标签页，只精准补刷剩余的几页！
            if 0 < done < total and self._watch_via_unfinished_tab(driver, title, extra_dwell=0.0):
                after_fast = self._read_study_progress(driver)
                if after_fast and after_fast[0] >= after_fast[1]:
                    self.logger.info("【课件】《%s》通过「未完成」筛选页快速补齐至 %d/%d！", title, after_fast[0], after_fast[1])
                    return TaskResult(title, TaskKind.PPT, TaskStatus.COMPLETED, f"学习进度已达 {after_fast[0]}/{after_fast[1]}")

        # 2. 若进入时停在“您还有以下页面未看”结果页，先直接触发定向补看
        initial_missed = self._detect_unviewed_pages(driver)
        if initial_missed:
            self.logger.info("【课件】进入页面即检测到 %d 个未看页码：%s，直接启动定向补看。", len(initial_missed), initial_missed)
            return self._run_remediation_loop(driver, title, initial_missed)

        # 3. 优先走「按真实页码顺序驱动虚拟滚动缩略图栏」（彻底杜绝虚拟列表跳页/漏页）
        total_pages = self._resolve_total_pages(driver, title, progress)
        probe = self._locate_virtual_slide(driver, target_page=1, do_scroll=False)

        if total_pages is not None and probe.get("hasSidebar"):
            ok = self._walk_virtual_sidebar_by_page_number(driver, title, total_pages, extra_dwell=0.0)
        elif total_pages is not None:
            counter = self._read_page_counter(driver)
            start_p = counter[0] if counter else 1
            ok = self._walk_by_next_action(driver, title, total_pages=total_pages, start_page=start_p)
        else:
            ok = self._walk_by_vertical_scroll(driver, title)

        if not ok:
            return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

        if not interruptible_sleep(1.5, self.stop_event):
            return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

        # 4. 校验完成情况：同时核对“您还有以下页面未看”提示与顶栏“学习进度：done/total”
        missed_pages = self._detect_unviewed_pages(driver)
        if missed_pages:
            return self._run_remediation_loop(driver, title, missed_pages)

        after_progress = self._read_study_progress(driver)
        if after_progress is not None:
            done, total = after_progress
            if done >= total:
                self.logger.info("【课件】《%s》学习进度已达 %d/%d，全部观看完成！", title, done, total)
                return TaskResult(title, TaskKind.PPT, TaskStatus.COMPLETED, f"学习进度已达 {done}/{total}")

            # 若仍有未完成页面，通过加长停留的补看轮次仅重看未标记「已完成」的页码
            return self._run_virtual_remediation_loop(driver, title, total)

        self.logger.info("【课件】《%s》全部页面已浏览完毕。", title)
        return TaskResult(title, TaskKind.PPT, TaskStatus.COMPLETED, "课件全量观看完成")

    # ------------------------------------------------------------------
    # 核心：按页码 1..total 顺序遍历虚拟滚动缩略图（绝不跳页）
    # ------------------------------------------------------------------
    def _watch_via_unfinished_tab(self, driver: WebDriver, title: str, extra_dwell: float = 0.0) -> bool:
        """在雨课堂课件页点击左侧「未完成」筛选标签，仅对剩余未读页精准停留（如 70/75 只需刷 5 页）。"""
        try:
            clicked_tab = driver.execute_script(
                r"""
                const nodes = Array.from(document.querySelectorAll('span, div, li, a, button'));
                for (const el of nodes) {
                  const r = el.getBoundingClientRect();
                  if (r.width <= 0 || r.height <= 0 || r.left > window.innerWidth * 0.35 || r.top > 220) continue;
                  const txt = (el.innerText || '').replace(/\s+/g, '').trim();
                  if (txt === '未完成' || /^未完成\(\d+\)$/.test(txt)) {
                    el.click();
                    return true;
                  }
                }
                return false;
                """
            )
        except WebDriverException:
            return False

        if not clicked_tab:
            return False

        self.logger.info("【课件】已切换至左侧「未完成」标签页，仅针对未读页面精准停留...")
        interruptible_sleep(0.8, self.stop_event)
        visited_in_tab: set[int] = set()

        for _ in range(120):
            if self.stop_event.is_set():
                return False

            prog = self._read_study_progress(driver)
            if prog and prog[0] >= prog[1]:
                break

            try:
                next_slide = driver.execute_script(
                    r"""
                    const visited = new Set(arguments[0]);
                    const raw = Array.from(
                      document.querySelectorAll('[class*="slide"], [class*="thumb"], [class*="ppt-item"], [class*="page-item"], [class*="card-item"], li')
                    );
                    for (const el of raw) {
                      const r = el.getBoundingClientRect();
                      if (r.left > window.innerWidth * 0.35 || r.width < 60 || r.height < 40 || r.height > 240) continue;
                      if (!el.querySelector('img, canvas')) continue;
                      if (el.querySelectorAll('img, canvas').length > 3) continue;
                      const m = ((el.innerText || '').trim()).match(/\b(\d{1,4})\b/);
                      const pageNo = m ? Number(m[1]) : -1;
                      if (pageNo > 0 && !visited.has(pageNo)) {
                        el.click();
                        return pageNo;
                      }
                    }
                    return null;
                    """,
                    list(visited_in_tab),
                )
            except WebDriverException:
                next_slide = None

            if next_slide is None:
                break

            visited_in_tab.add(int(next_slide))
            dwell = self._pick_dwell_seconds(extra=extra_dwell)
            self.logger.info("【课件】《%s》补刷未完成第 %d 页，停留 %.1f 秒...", title, int(next_slide), dwell)
            if not self._dwell_with_keepalive(driver, dwell):
                return False

        # 切回「全部」标签页以便后续常规检查
        try:
            driver.execute_script(
                r"""
                for (const el of document.querySelectorAll('span, div, li, a, button')) {
                  const r = el.getBoundingClientRect();
                  if (r.width > 0 && r.left < window.innerWidth * 0.35 && r.top < 220 && (el.innerText || '').trim() === '全部') {
                    el.click();
                    break;
                  }
                }
                """
            )
        except WebDriverException:
            pass
        interruptible_sleep(0.5, self.stop_event)
        return len(visited_in_tab) > 0

    def _walk_virtual_sidebar_by_page_number(
        self,
        driver: WebDriver,
        title: str,
        total_pages: int,
        extra_dwell: float = 0.0,
    ) -> bool:
        """按真实页码 1 -> total_pages 逐页定位左侧虚拟列表并停留。"""
        self.logger.info("【课件】启用精确页码顺序模式，共 %d 页（跳过已标「已完成」页）...", total_pages)
        skipped_done = 0

        for page_no in range(1, total_pages + 1):
            if self.stop_event.is_set():
                return False

            info = self._scroll_until_page_found(driver, page_no)
            if not info.get("found"):
                # 若个别页在虚拟列表中未定位到，用方向键步进兜底并停留
                self.logger.warning("【课件】未在左侧栏定位到第 %d/%d 页，使用翻页键步进...", page_no, total_pages)
                self._step_next_page(driver)
                dwell = self._pick_dwell_seconds(extra=extra_dwell)
                if not self._dwell_with_keepalive(driver, dwell):
                    return False
                continue

            if info.get("completed"):
                skipped_done += 1
                continue

            # 点击精确页码对应的缩略图（不调用 scrollIntoView 避免打乱虚拟列表位置）
            el = info.get("element")
            try:
                driver.execute_script("arguments[0].click();", el)
            except WebDriverException:
                self._step_next_page(driver)

            dwell = self._pick_dwell_seconds(extra=extra_dwell)
            self.logger.info("【课件】《%s》第 %d/%d 页，停留 %.1f 秒...", title, page_no, total_pages, dwell)
            if not self._dwell_with_keepalive(driver, dwell):
                return False

        if skipped_done > 0:
            self.logger.info("【课件】本轮已自动跳过 %d 个已完成的页面。", skipped_done)
        return True

    def _run_virtual_remediation_loop(self, driver: WebDriver, title: str, total_pages: int) -> TaskResult:
        """当顶栏显示 done < total 时，按轮次加长停留时间只补看仍未带「已完成」标记的页面。"""
        max_rounds = self.config.ppt_retry_rounds
        for round_idx in range(1, max_rounds + 1):
            if self.stop_event.is_set():
                return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

            progress = self._read_study_progress(driver)
            if progress and progress[0] >= progress[1]:
                return TaskResult(title, TaskKind.PPT, TaskStatus.COMPLETED, f"学习进度已达 {progress[0]}/{progress[1]}")

            extra_sec = self.config.ppt_retry_extra_seconds * round_idx
            self.logger.warning(
                "【课件】《%s》当前进度 %s，启动第 %d/%d 轮未完成页补看（每页额外 +%.1f 秒）...",
                title,
                f"{progress[0]}/{progress[1]}" if progress else "未满",
                round_idx,
                max_rounds,
                extra_sec,
            )
            if not self._watch_via_unfinished_tab(driver, title, extra_dwell=extra_sec):
                ok = self._walk_virtual_sidebar_by_page_number(driver, title, total_pages, extra_dwell=extra_sec)
                if not ok:
                    return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

            interruptible_sleep(1.5, self.stop_event)
            after = self._read_study_progress(driver)
            if after and after[0] >= after[1]:
                self.logger.info("【课件】《%s》第 %d 轮补看后进度已满（%d/%d）！", title, round_idx, after[0], after[1])
                return TaskResult(title, TaskKind.PPT, TaskStatus.COMPLETED, f"补看后进度已达 {after[0]}/{after[1]}")

        final_prog = self._read_study_progress(driver)
        prog_str = f"{final_prog[0]}/{final_prog[1]}" if final_prog else "未知"
        msg = f"经过 {max_rounds} 轮补看后学习进度仍为 {prog_str}"
        self.logger.error("【课件】《%s》%s", title, msg)
        return TaskResult(title, TaskKind.PPT, TaskStatus.FAILED, msg)

    def _scroll_until_page_found(self, driver: WebDriver, target_page: int, max_scrolls: int = 35) -> Dict[str, Any]:
        """在左侧虚拟滚动栏中按方向逐步滚动，直到精确页码进入 DOM。"""
        for _ in range(max_scrolls):
            if self.stop_event.is_set():
                return {"found": False}
            info = self._locate_virtual_slide(driver, target_page=target_page, do_scroll=True)
            if info.get("found"):
                return info
            if not info.get("moved"):
                # 已经滚到顶部或底部仍未找到，再重试一次确认渲染
                time.sleep(0.25)
                return self._locate_virtual_slide(driver, target_page=target_page, do_scroll=False)
            time.sleep(0.18)
        return {"found": False}

    def _locate_virtual_slide(self, driver: WebDriver, target_page: int, do_scroll: bool) -> Dict[str, Any]:
        try:
            res = driver.execute_script(LOCATE_VIRTUAL_SLIDE_JS, target_page, do_scroll)
            return res if isinstance(res, dict) else {"found": False}
        except WebDriverException:
            return {"found": False}

    def _resolve_total_pages(
        self,
        driver: WebDriver,
        title: str,
        progress: Optional[Tuple[int, int]],
    ) -> Optional[int]:
        """综合顶栏学习进度、任务标题中的 X/Y 以及页码计数器确定真实总页数。"""
        if progress is not None:
            return progress[1]

        counter = self._read_page_counter(driver)
        if counter is not None:
            return counter[1]

        # 从任务标题兜底提取（例如 “第五讲 ... 0/75 学习进度”）
        match = re.search(r"\d{1,4}\s*/\s*(\d{1,4})", title or "")
        if match:
            total = int(match.group(1))
            if total > 0:
                return total
        return None

    def _read_study_progress(self, driver: WebDriver) -> Optional[Tuple[int, int]]:
        """读取顶栏「学习进度：X/Y」。"""
        try:
            text = driver.execute_script("return document.body ? document.body.innerText.slice(0, 600) : '';") or ""
            return parse_study_progress(text)
        except WebDriverException:
            return None

    # ------------------------------------------------------------------
    # 兼容其他雨课堂版本：翻页按钮步进 / 竖向滚动 / 结束页未看提示补看
    # ------------------------------------------------------------------
    def _walk_by_next_action(
        self,
        driver: WebDriver,
        title: str,
        total_pages: int,
        start_page: int = 1,
    ) -> bool:
        """通过“下一页按钮 / 方向键”按页码计数器逐页翻看。"""
        self.logger.info("【课件】检测到页码计数 %d/%d，开始逐页翻看...", start_page, total_pages)
        current_page = start_page

        for step in range(total_pages):
            if self.stop_event.is_set():
                return False

            dwell = self._pick_dwell_seconds(extra=0.0)
            self.logger.info("【课件】《%s》第 %d/%d 页，停留 %.1f 秒...", title, current_page, total_pages, dwell)
            if not self._dwell_with_keepalive(driver, dwell):
                return False

            if current_page >= total_pages:
                self._step_next_page(driver)
                break

            self._step_next_page(driver)
            interruptible_sleep(0.5, self.stop_event)

            updated = self._read_page_counter(driver)
            current_page = updated[0] if updated else (current_page + 1)
            if step >= total_pages + 3:
                break

        return True

    def _walk_by_vertical_scroll(self, driver: WebDriver, title: str) -> bool:
        """兜底策略：无分页控件的图文/PDF 课件，按视口分段滚动并停留。"""
        self.logger.info("【课件】未检测到分页控件，启用分段滚动阅读模式：《%s》", title)
        try:
            scroll_height = int(driver.execute_script("return document.body.scrollHeight || 0;") or 0)
            viewport_height = int(driver.execute_script("return window.innerHeight || 800;") or 800)
        except WebDriverException:
            return True

        steps = max(1, min(60, (scroll_height + viewport_height - 1) // max(300, viewport_height)))
        for idx in range(steps):
            if self.stop_event.is_set():
                return False
            y = int(idx * viewport_height * 0.85)
            try:
                driver.execute_script("window.scrollTo(0, arguments[0]);", y)
            except WebDriverException:
                pass
            dwell = self._pick_dwell_seconds(extra=0.0)
            self.logger.info("【课件】《%s》滚动阅读第 %d/%d 屏，停留 %.1f 秒...", title, idx + 1, steps, dwell)
            if not self._dwell_with_keepalive(driver, dwell):
                return False
        return True

    def _run_remediation_loop(
        self,
        driver: WebDriver,
        title: str,
        initial_missed: List[int],
    ) -> TaskResult:
        """针对“您还有以下页面未看”结果页进行加长停留的定向补看循环（最多重试 N 轮）。"""
        missed_pages = initial_missed
        max_rounds = self.config.ppt_retry_rounds

        for round_idx in range(1, max_rounds + 1):
            if self.stop_event.is_set():
                return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

            extra_sec = self.config.ppt_retry_extra_seconds * round_idx
            self.logger.warning(
                "【课件】《%s》第 %d/%d 轮漏页补看，待补看页码：%s（每页额外 +%.1f 秒）",
                title,
                round_idx,
                max_rounds,
                missed_pages,
                extra_sec,
            )

            self._click_continue_watching_if_present(driver)
            interruptible_sleep(1.0, self.stop_event)

            for page_no in missed_pages:
                if self.stop_event.is_set():
                    return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

                jumped = self._jump_to_page(driver, page_no)
                dwell = self._pick_dwell_seconds(extra=extra_sec)
                self.logger.info(
                    "【课件】补看第 %d 页（跳转=%s），加长停留 %.1f 秒...",
                    page_no,
                    "成功" if jumped else "翻页尝试",
                    dwell,
                )
                if not self._dwell_with_keepalive(driver, dwell):
                    return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

            self._step_next_page(driver)
            if not interruptible_sleep(2.0, self.stop_event):
                return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

            missed_pages = self._detect_unviewed_pages(driver)
            if not missed_pages:
                self.logger.info("【课件】《%s》第 %d 轮补看后全部页面均已通过校验！", title, round_idx)
                return TaskResult(title, TaskKind.PPT, TaskStatus.COMPLETED, f"经过 {round_idx} 轮补看后全部完成")

        msg = f"经过 {max_rounds} 轮定向补看后，仍有未看页码：{missed_pages}"
        self.logger.error("【课件】《%s》%s", title, msg)
        return TaskResult(title, TaskKind.PPT, TaskStatus.FAILED, msg)

    def _detect_unviewed_pages(self, driver: WebDriver) -> List[int]:
        """检测页面是否存在“您还有以下页面未看”提示与未看页码标签。"""
        badge_texts: List[str] = []
        for selector in SELECTORS.ppt_unviewed_badge_selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, selector):
                    if el.is_displayed():
                        badge_texts.append(el.text or "")
            except WebDriverException:
                continue

        try:
            body_text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        except WebDriverException:
            body_text = ""

        return parse_unviewed_pages(body_text, badge_texts)

    def _click_continue_watching_if_present(self, driver: WebDriver) -> bool:
        for label in SELECTORS.ppt_continue_button_texts:
            try:
                xpath = f"//*[self::button or self::a or self::div or self::span][normalize-space()='{label}']"
                for el in driver.find_elements(By.XPATH, xpath):
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        return True
            except WebDriverException:
                continue
        return False

    def _jump_to_page(self, driver: WebDriver, page_no: int) -> bool:
        """精准跳转到指定页码（优先通过虚拟侧边栏精确定位）。"""
        info = self._scroll_until_page_found(driver, page_no)
        if info.get("found") and info.get("element") is not None:
            try:
                driver.execute_script("arguments[0].click();", info["element"])
                time.sleep(0.3)
                return True
            except WebDriverException:
                pass

        try:
            xpath = f"//*[self::button or self::span or self::div or self::li][normalize-space()='{page_no}']"
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(0.3)
                    return True
        except WebDriverException:
            pass
        return False

    def _read_page_counter(self, driver: WebDriver) -> Optional[Tuple[int, int]]:
        for selector in SELECTORS.ppt_counter_selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, selector):
                    if el.is_displayed():
                        parsed = parse_page_counter(el.text or "")
                        if parsed is not None:
                            return parsed
            except WebDriverException:
                continue
        return None

    def _step_next_page(self, driver: WebDriver) -> None:
        for selector in SELECTORS.ppt_next_button_selectors:
            try:
                for btn in driver.find_elements(By.CSS_SELECTOR, selector):
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        return
            except WebDriverException:
                continue
        try:
            ActionChains(driver).send_keys(Keys.ARROW_DOWN).perform()
        except WebDriverException:
            pass

    def _pick_dwell_seconds(self, extra: float = 0.0) -> float:
        base = random.uniform(self.config.ppt_dwell_min, self.config.ppt_dwell_max)
        return round(base + max(0.0, extra), 1)

    def _dwell_with_keepalive(self, driver: WebDriver, seconds: float) -> bool:
        half = seconds / 2.0
        if not interruptible_sleep(half, self.stop_event):
            return False
        try:
            driver.execute_script("window.dispatchEvent(new Event('mousemove'));")
        except WebDriverException:
            pass
        return interruptible_sleep(seconds - half, self.stop_event)
