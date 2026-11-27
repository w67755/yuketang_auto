"""课程任务扫描与顺序调度引擎。

核心调度流程：
1. 确保浏览器已启动并完成微信扫码登录；
2. 根据配置决定入口：
   - 若开启「接管当前页面」且用户已经停留在课程目录或单个任务页，直接接管；
   - 否则依次按 `course_names` 进入每门课程；
   - 若两者皆未指定，在首页温和提示用户点进目标课程页后自动接管。
3. 扫描页面内未完成的任务点，识别其类型（视频 / 课件 PPT / 习题），
   按用户勾选的功能开关顺序派发至对应执行器；
4. 单个任务抛出异常时，自动保存截图与 DOM 诊断摘要并安全跳过，继续执行后续任务。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from app.config import AppConfig
from core.auth import ensure_logged_in, enter_course_by_name, is_inside_course_or_task_page
from core.browser import BrowserManager, interruptible_sleep
from core.diagnostics import capture_diagnostics
from core.selectors import SELECTORS
from tasks.auto_quiz import QuizSolver
from tasks.base import TaskKind, TaskResult, TaskStatus
from tasks.watch_ppt import PptWatcher
from tasks.watch_video import VideoWatcher


@dataclass
class RunSummary:
    """一次运行结束后的任务统计汇总。"""

    completed: int = 0
    skipped: int = 0
    failed: int = 0
    details: List[TaskResult] = field(default_factory=list)

    def record(self, result: TaskResult) -> None:
        self.details.append(result)
        if result.status == TaskStatus.COMPLETED:
            self.completed += 1
        elif result.status == TaskStatus.SKIPPED:
            self.skipped += 1
        elif result.status == TaskStatus.FAILED:
            self.failed += 1


def detect_page_task_kind(url: str, page_text: str = "", has_video: bool = False) -> TaskKind:
    """根据当前页面 URL、正文特征与 video 标签综合判断页面任务类型。"""
    lower_url = (url or "").lower()

    if any(kw in lower_url for kw in SELECTORS.quiz_url_keywords):
        return TaskKind.QUIZ
    if any(kw in lower_url for kw in SELECTORS.ppt_url_keywords):
        return TaskKind.PPT
    if any(kw in lower_url for kw in SELECTORS.video_url_keywords):
        return TaskKind.VIDEO

    if has_video:
        return TaskKind.VIDEO

    # 依据页面显著正文特征回退判断
    if "您还有以下页面未看" in page_text or "继续观看" in page_text:
        return TaskKind.PPT
    if "提交答案" in page_text or "交卷" in page_text or "单选题" in page_text:
        return TaskKind.QUIZ
    return TaskKind.UNKNOWN


def strip_datetime_noise(text: str) -> str:
    """移除文本中的日期/时间片段，避免 “2027-01-31/23:59” 被误识别为 “31/23” 的进度分数。"""
    if not text:
        return ""
    cleaned = re.sub(r"\d{4}-\d{1,2}-\d{1,2}(?:\s*/\s*|\s+)?(?:\d{1,2}:\d{2}(?::\d{2})?)?", " ", text)
    cleaned = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", " ", cleaned)
    return cleaned


def is_progress_fraction_completed(text: str) -> bool:
    """判断任务卡片文本中的“分子/分母”进度是否已达 100%（例如 '69/69 学习进度'、'36/36'、'1/1'）。

    注意：会先剔除截止日期（如 '请在2027-01-31/23:59前完成学习'），防止日期被误判为已完成进度。
    """
    if not text:
        return False
    for match in re.finditer(r"(\d{1,4})\s*/\s*(\d{1,4})", strip_datetime_noise(text)):
        done, total = int(match.group(1)), int(match.group(2))
        if total > 0 and done >= total:
            return True
    return False


def is_collapsible_bundle_text(text: str) -> bool:
    """判断是否为折叠合集卡片头（例如 '视频学习 等 包含3章，0小节，共计9个学习单元 展开 进行中'）。"""
    if not text:
        return False
    return bool(re.search(r"包含\s*\d+\s*章.*学习单元", text))


def is_protected_essay_task(text: str) -> bool:
    """判断是否为论文/心得/报告等需要人工撰写的重要考核任务（强制保护，绝不自动作答或误交）。"""
    if not text:
        return False
    return any(kw in text for kw in SELECTORS.protected_essay_keywords)


def classify_task_text(text: str) -> TaskKind:
    """根据课程目录中任务条目的标题与角标文本初步判定任务类型。"""
    if not text:
        return TaskKind.UNKNOWN
    if any(kw in text for kw in SELECTORS.quiz_type_keywords):
        return TaskKind.QUIZ
    if any(kw in text for kw in SELECTORS.ppt_type_keywords):
        return TaskKind.PPT
    if any(kw in text for kw in SELECTORS.video_type_keywords):
        return TaskKind.VIDEO
    # 标准版雨课堂的课件卡片常以图标标注“课件”，正文呈现为 “第X讲... 0/75 学习进度”
    if "学习进度" in text and re.search(r"\d+\s*/\s*\d+", text):
        return TaskKind.PPT
    return TaskKind.UNKNOWN


def task_priority_rank(kind: TaskKind, task_order: str) -> int:
    """根据配置的优先顺序返回任务排序权重（值越小越优先执行）。"""
    if task_order == "ppt_first":
        order_map = {TaskKind.PPT: 0, TaskKind.VIDEO: 1, TaskKind.QUIZ: 2, TaskKind.UNKNOWN: 3}
    else:
        order_map = {TaskKind.VIDEO: 0, TaskKind.PPT: 1, TaskKind.QUIZ: 2, TaskKind.UNKNOWN: 3}
    return order_map.get(kind, 3)


class CourseScheduler:
    """雨课堂单浏览器顺序调度器（视频 + 课件 + DeepSeek 做题）。"""

    def __init__(
        self,
        config: AppConfig,
        browser: BrowserManager,
        logger: logging.Logger,
        stop_event: threading.Event,
    ) -> None:
        self.config = config
        self.browser = browser
        self.logger = logger
        self.stop_event = stop_event

        # 初始化三类核心任务执行器
        self.video_watcher = VideoWatcher(config, browser, logger, stop_event)
        self.ppt_watcher = PptWatcher(config, browser, logger, stop_event)
        self.quiz_solver = QuizSolver(config, browser, logger, stop_event)

    def run(self) -> RunSummary:
        """执行完整刷课流程并返回汇总报告。"""
        summary = RunSummary()
        if not ensure_logged_in(self.browser, self.config, self.stop_event, self.logger):
            return summary

        driver = self.browser.ensure_started()
        current_url = driver.current_url or ""

        # 模式 1：优先接管用户已经手动打开的课程页或单个学习任务页
        if self.config.takeover_current_page and is_inside_course_or_task_page(current_url):
            self.logger.info("检测到当前已处于课程/任务页面，启动一键接管：%s", current_url)
            self._process_current_page(summary)
            return summary

        # 模式 2：按配置里的课程名列表依次自动进入并处理
        if self.config.course_names:
            for course_name in self.config.course_names:
                if self.stop_event.is_set():
                    break
                if enter_course_by_name(self.browser, self.config, course_name, self.stop_event, self.logger):
                    self._process_current_page(summary, course_label=course_name)
                else:
                    if self.config.save_diagnostics_on_error:
                        capture_diagnostics(driver, f"course_not_found_{course_name}", "未找到课程", self.logger)
            return summary

        # 模式 3：未填课程名且当前仍在首页，等待用户手动点进目标课程页后自动接管
        self.logger.info("未填写课程名称：请在浏览器中手动点击进入要刷的课程页面，脚本将自动检测并接管...")
        while not self.stop_event.is_set():
            if not self.browser.is_alive():
                self.logger.warning("浏览器窗口已关闭，任务终止。")
                return summary
            if is_inside_course_or_task_page(driver.current_url or ""):
                self.logger.info("已检测到进入课程页面，开始自动接管：%s", driver.current_url)
                interruptible_sleep(2.0, self.stop_event)
                self._process_current_page(summary)
                return summary
            interruptible_sleep(1.5, self.stop_event)

        return summary

    def _process_current_page(self, summary: RunSummary, course_label: str = "当前课程") -> None:
        """判断当前页是单个任务页还是课程目录页，分别执行对应逻辑。"""
        driver = self.browser.ensure_started()
        self.browser.inject_anti_blur_now()

        # 先检测当前页本身是否已经是具体的视频/课件/习题页
        direct_kind = self._inspect_current_page_kind(driver)
        if direct_kind != TaskKind.UNKNOWN:
            self.logger.info("当前页面为单个【%s】任务页，直接开始执行。", direct_kind.value)
            result = self._execute_single_task(direct_kind, title=driver.title or direct_kind.value)
            summary.record(result)
            return

        # 否则视为课程目录 / 学习日志列表页：
        # 注意：标准版雨课堂中，「视频/MOOC章节」通常位于「学习内容」iframe（/pro/lms/.../studycontent），
        # 而「课件」位于「学习日志」（/v2/web/studentLog/...）。此处按 task_order 自动串联两个区域！
        student_log_url = driver.current_url or ""
        study_content_url = self._detect_study_content_url(driver)

        if study_content_url and "/studycontent" not in student_log_url.lower():
            phases: List[Tuple[str, str]] = []
            # 根据用户设置的优先顺序编排「学习内容（视频/章节）」与「学习日志（课件）」的执行次序
            need_study_content = self.config.enable_video or self.config.enable_quiz
            need_student_log = self.config.enable_ppt or self.config.enable_video or self.config.enable_quiz

            if self.config.task_order == "ppt_first":
                if need_student_log:
                    phases.append((f"{course_label} · 学习日志（课件）", student_log_url))
                if need_study_content:
                    phases.append((f"{course_label} · 学习内容（视频/章节）", study_content_url))
            else:
                if need_study_content:
                    phases.append((f"{course_label} · 学习内容（视频/章节）", study_content_url))
                if need_student_log:
                    phases.append((f"{course_label} · 学习日志（课件）", student_log_url))

            self.logger.info(
                "检测到该课程同时包含「学习内容（视频/章节）」与「学习日志（课件）」，将按配置顺序依次处理：%s",
                " -> ".join(name for name, _ in phases),
            )
            for phase_name, phase_url in phases:
                if self.stop_event.is_set():
                    break
                if driver.current_url != phase_url:
                    driver.get(phase_url)
                    if not interruptible_sleep(2.5, self.stop_event):
                        break
                self._scan_and_run_list_page(summary, course_label=phase_name)
            return

        self._scan_and_run_list_page(summary, course_label=course_label)

    def _detect_study_content_url(self, driver: WebDriver) -> Optional[str]:
        """提取雨课堂课程页中指向「学习内容（/pro/lms/.../studycontent）」的同源 URL。"""
        for selector in SELECTORS.study_content_iframe_selectors:
            try:
                for frame in driver.find_elements(By.CSS_SELECTOR, selector):
                    src = (frame.get_attribute("src") or "").strip()
                    if src and "studycontent" in src.lower():
                        return src
            except WebDriverException:
                continue
        return None

    def _scan_and_run_list_page(self, summary: RunSummary, course_label: str) -> None:
        """在单个列表页（学习内容页或学习日志页）内按优先级扫描并依次完成未完成任务。"""
        driver = self.browser.ensure_started()
        self.logger.info("正在扫描《%s》中的未完成任务点...", course_label)
        interruptible_sleep(2.0, self.stop_event)
        self._expand_collapsed_chapters(driver)
        interruptible_sleep(1.2, self.stop_event)

        course_list_handle = driver.current_window_handle
        course_list_url = driver.current_url

        # 用“已尝试过的任务指纹集合”防止同一异常任务陷入死循环
        attempted_keys: set[str] = set()
        max_rounds = 200

        for _ in range(max_rounds):
            if self.stop_event.is_set():
                break

            # 确保每次扫描都回到当前列表主标签页
            self.browser.close_extra_tabs(course_list_handle)
            if driver.current_url != course_list_url:
                driver.get(course_list_url)
                if not interruptible_sleep(2.5, self.stop_event):
                    break
                self._expand_collapsed_chapters(driver)

            next_candidate = self._find_next_pending_task(driver, attempted_keys)
            if next_candidate is None:
                if not attempted_keys:
                    self.logger.warning(
                        "在《%s》当前页面未识别到未完成任务点（可能全部已完成，或该页 DOM 选择器需适配）。",
                        course_label,
                    )
                    if self.config.save_diagnostics_on_error:
                        capture_diagnostics(
                            driver,
                            f"zero_tasks_{course_label}",
                            "课程页面未识别到任何待处理任务点",
                            self.logger,
                        )
                else:
                    self.logger.info("《%s》所有符合条件的未完成任务点已处理完毕！", course_label)
                break

            task_key, task_title, guessed_kind, element = next_candidate
            attempted_keys.add(task_key)
            self.logger.info("准备进入任务：%s（预判类型：%s）", task_title, guessed_kind.value)

            before_handles = set(driver.window_handles)
            before_url = driver.current_url

            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                time.sleep(0.4)
                driver.execute_script("arguments[0].click();", element)
            except WebDriverException as exc:
                self.logger.error("点击任务《%s》失败：%s", task_title, exc)
                if self.config.save_diagnostics_on_error:
                    capture_diagnostics(driver, f"click_failed_{task_title}", str(exc), self.logger)
                summary.record(TaskResult(task_title, guessed_kind, TaskStatus.FAILED, str(exc)))
                continue

            if not interruptible_sleep(2.5, self.stop_event):
                break

            # 判断是新标签页打开还是当前页跳转
            new_handles = list(set(driver.window_handles) - before_handles)
            if new_handles:
                driver.switch_to.window(new_handles[-1])
                interruptible_sleep(1.5, self.stop_event)
            elif driver.current_url == before_url:
                interruptible_sleep(1.5, self.stop_event)

            self.browser.inject_anti_blur_now()
            actual_kind = self._inspect_current_page_kind(driver)
            if actual_kind == TaskKind.UNKNOWN:
                actual_kind = guessed_kind

            result = self._execute_single_task(actual_kind, title=task_title)
            summary.record(result)

            # 任务完成后清理临时标签页，回到列表继续下一项
            self.browser.close_extra_tabs(course_list_handle)
            interruptible_sleep(1.0, self.stop_event)

    def _expand_collapsed_chapters(self, driver: WebDriver) -> None:
        """展开折叠的章节目录与学习日志中的「展开」合集卡片，确保所有子任务节点可见。"""
        for selector in SELECTORS.chapter_expand_selectors:
            try:
                headers = driver.find_elements(By.CSS_SELECTOR, selector)
                for header in headers:
                    if header.is_displayed():
                        driver.execute_script("arguments[0].click();", header)
                        time.sleep(0.15)
            except WebDriverException:
                continue

        # 展开学习日志中类似 “视频学习 等 包含3章，共计9个学习单元 ⌄ 展开” 的合集按钮
        try:
            expand_btns = driver.find_elements(
                By.XPATH,
                "//*[self::span or self::a or self::button or self::div][normalize-space()='展开']",
            )
            for btn in expand_btns:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.35)
        except WebDriverException:
            pass

    def _find_next_pending_task(
        self,
        driver: WebDriver,
        attempted_keys: set[str],
    ) -> Optional[Tuple[str, str, TaskKind, object]]:
        """在当前列表 DOM 中收集未完成任务，并按 `task_order` 优先级返回最应先执行的一项。"""
        # 0. 若当前在 /pro/lms/.../studycontent（学习内容章节总览页），走精准右侧内容总览扫描器
        #    严格限定在 section.section-fr 内，绝不会误点左侧目录栏（#chapter-section）或顶部导航栏
        if "/studycontent" in (driver.current_url or "").lower():
            study_item = self._find_next_studycontent_task(driver, attempted_keys)
            if study_item is not None:
                return study_item

        candidates: List[Tuple[int, int, str, str, TaskKind, object]] = []
        seq = 0

        # 1. 先按已知任务卡片选择器收集候选
        for selector in SELECTORS.task_item_selectors:
            try:
                items = driver.find_elements(By.CSS_SELECTOR, selector)
            except WebDriverException:
                continue

            for item in items:
                try:
                    if not item.is_displayed():
                        continue
                    raw_text = " ".join((item.text or "").split())
                    if not raw_text or len(raw_text) < 4 or len(raw_text) > 500:
                        continue
                except StaleElementReferenceException:
                    continue

                if raw_text in SELECTORS.ignored_nav_texts:
                    continue
                if is_collapsible_bundle_text(raw_text) or is_protected_essay_task(raw_text):
                    continue
                if any(done_kw in raw_text for done_kw in SELECTORS.completed_keywords):
                    continue
                if is_progress_fraction_completed(raw_text):
                    continue

                guessed_kind = classify_task_text(raw_text)
                if not self._is_kind_enabled(guessed_kind):
                    continue

                short_title = raw_text[:45]
                stable_title = re.sub(r"\d+\s*/\s*\d+\s*学习进度", "", short_title).strip()
                task_key = f"{selector}:{stable_title}"
                if task_key in attempted_keys:
                    continue

                rank = task_priority_rank(guessed_kind, self.config.task_order)
                candidates.append((rank, seq, task_key, short_title, guessed_kind, item))
                seq += 1

            # 首个有效选择器已找到候选时即可按优先级排序返回，避免重复匹配嵌套子节点
            if candidates:
                candidates.sort(key=lambda c: (c[0], c[1]))
                _, _, task_key, short_title, guessed_kind, item = candidates[0]
                return task_key, short_title, guessed_kind, item

        # 2. 语义兜底：针对动态类名页面按最小包含块匹配并按优先级排序（严格排除导航栏与左侧目录）
        try:
            fallback_nodes = driver.execute_script(
                """
                const kws = ['课件', 'PPT', '视频', '录播', '习题', '作业', '测验', '预习', '未完成', '未读', '未看', '未开始', '进行中', '讲'];
                const done = ['已完成', '已读', '已看完', '100%', '已提交', '已作答'];
                const candidates = Array.from(document.querySelectorAll('div, li, section, article, a'));
                const matched = [];
                for (const el of candidates) {
                  if (el.closest('#chapter-section, .section-fl, .rain-tabs__nav, .classroom-header-base')) continue;
                  const rect = el.getBoundingClientRect();
                  if (rect.width < 120 || rect.height < 28 || rect.height > 260) continue;
                  const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (txt.length < 6 || txt.length > 260) continue;
                  if (done.some((d) => txt.includes(d))) continue;
                  if (!kws.some((k) => txt.includes(k))) continue;
                  const hasChildMatch = Array.from(el.children).some((c) => {
                    const ct = (c.innerText || '').replace(/\\s+/g, ' ').trim();
                    return ct.length >= 6 && kws.some((k) => ct.includes(k)) && c.getBoundingClientRect().height >= 28;
                  });
                  if (!hasChildMatch) matched.push(el);
                }
                return matched.slice(0, 40);
                """
            ) or []
            for idx, node in enumerate(fallback_nodes):
                raw_text = " ".join((node.text or "").split())
                if not raw_text or raw_text in SELECTORS.ignored_nav_texts:
                    continue
                if is_collapsible_bundle_text(raw_text) or is_protected_essay_task(raw_text) or is_progress_fraction_completed(raw_text):
                    continue
                guessed_kind = classify_task_text(raw_text)
                if not self._is_kind_enabled(guessed_kind):
                    continue
                short_title = raw_text[:45]
                stable_title = re.sub(r"\d+\s*/\s*\d+\s*学习进度", "", short_title).strip()
                task_key = f"semantic:{stable_title}"
                if task_key in attempted_keys:
                    continue
                rank = task_priority_rank(guessed_kind, self.config.task_order)
                candidates.append((rank, idx, task_key, short_title, guessed_kind, node))

            if candidates:
                candidates.sort(key=lambda c: (c[0], c[1]))
                _, _, task_key, short_title, guessed_kind, item = candidates[0]
                return task_key, short_title, guessed_kind, item
        except WebDriverException:
            pass

        return None

    def _find_next_studycontent_task(
        self,
        driver: WebDriver,
        attempted_keys: set[str],
    ) -> Optional[Tuple[str, str, TaskKind, object]]:
        """精准扫描「学习内容（/pro/lms/.../studycontent）」右侧内容总览中的未完成任务行。

        原理：
        - 在 `section.section-fr` 内定位所有带蓝色未完成状态标签（`未开始`、`进行中(...)`、`已观看...`）的行；
        - 结合每行的 SVG/图标类名与状态文本区分「视频 / 课件 / 习题」，并按 `task_order` 优先级返回首项。
        """
        try:
            rows = driver.execute_script(
                r"""
                const root = document.querySelector('section.section-fr, .section-fr') || document.body;
                const statusNodes = Array.from(root.querySelectorAll('[class*="blue-color"], .el-tooltip.item, span, div'))
                  .filter((el) => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0 || rect.height > 48) return false;
                    const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
                    if (!txt || txt.length > 24) return false;
                    return txt === '未开始' || /^进行中/.test(txt) || /^已观看/.test(txt) || txt === '未完成' || txt === '未提交';
                  });

                const seen = new Set();
                const results = [];
                for (const badge of statusNodes) {
                  // 向上查找对应的单条任务行容器（宽度 >= 300px，高度在 28~130px 之间）
                  let row = badge.parentElement;
                  let picked = null;
                  while (row && row !== root && row !== document.body) {
                    const r = row.getBoundingClientRect();
                    const t = (row.innerText || '').replace(/\s+/g, ' ').trim();
                    if (r.width >= 300 && r.height >= 28 && r.height <= 130 && t.length > (badge.innerText || '').trim().length + 2) {
                      picked = row;
                      // 继续向上看一层是否仍是同一行外框（避免只点到右侧状态栏）
                      const parent = row.parentElement;
                      if (parent && parent !== root) {
                        const pr = parent.getBoundingClientRect();
                        if (pr.height <= 130 && pr.width >= 300 && (parent.innerText || '').replace(/\s+/g, ' ').trim().length < 220) {
                          picked = parent;
                        }
                      }
                      break;
                    }
                    row = row.parentElement;
                  }
                  if (!picked || seen.has(picked)) continue;
                  seen.add(picked);

                  const rowText = (picked.innerText || '').replace(/\s+/g, ' ').trim();
                  const statusText = (badge.innerText || '').replace(/\s+/g, ' ').trim();
                  // 收集该行内图标的 SVG use/href 或 icon class，用于区分视频图标 vs P 课件图标 vs 习题图标
                  const iconHints = [];
                  for (const useEl of picked.querySelectorAll('use')) {
                    iconHints.push(useEl.getAttribute('xlink:href') || useEl.getAttribute('href') || '');
                  }
                  for (const iconEl of picked.querySelectorAll('i, svg')) {
                    const cls = typeof iconEl.className === 'string' ? iconEl.className : (iconEl.className && iconEl.className.baseVal) || '';
                    if (cls) iconHints.push(cls);
                  }
                  for (const img of picked.querySelectorAll('img')) {
                    iconHints.push(img.getAttribute('src') || '');
                  }
                  // 行首图标外层 HTML 片段（用于区分“视频图标”与“P 课件图标”）
                  const firstIcon = picked.querySelector('[class*="icon"], i, svg, img');
                  if (firstIcon) iconHints.push((firstIcon.outerHTML || '').slice(0, 240));
                  // 优先把行内主标题区域作为可点击目标（点击标题最稳，不会误触提示 tooltip）
                  const clickTarget = picked.querySelector('[class*="title"], [class*="name"], .leaf-detail, a') || picked;
                  results.push({
                    element: clickTarget,
                    text: rowText,
                    status: statusText,
                    iconHint: iconHints.join(' ').toLowerCase(),
                  });
                }
                return results;
                """
            ) or []
        except WebDriverException:
            return None

        candidates: List[Tuple[int, int, str, str, TaskKind, object]] = []
        for idx, row in enumerate(rows):
            raw_text = (row.get("text") or "").strip()
            status_text = (row.get("status") or "").strip()
            icon_hint = (row.get("iconHint") or "").lower()
            element = row.get("element")
            if not raw_text or element is None:
                continue
            if any(done_kw in raw_text for done_kw in SELECTORS.completed_keywords):
                continue
            if is_protected_essay_task(raw_text):
                continue
            if is_progress_fraction_completed(raw_text):
                continue

            # 结合图标特征 + 状态文本精准判定该行是视频、课件还是习题
            guessed_kind = self._classify_studycontent_row(raw_text, status_text, icon_hint)
            if not self._is_kind_enabled(guessed_kind):
                continue

            # 去掉末尾状态词生成稳定任务指纹，避免进度从“未开始”变“进行中”后重复进入
            clean_title = re.sub(r"请在\d{4}-\d{2}-\d{2}.*?完成学习", "", raw_text)
            clean_title = clean_title.replace(status_text, "").strip()[:45]
            task_key = f"studycontent:{guessed_kind.value}:{clean_title}"
            if task_key in attempted_keys:
                continue

            rank = task_priority_rank(guessed_kind, self.config.task_order)
            candidates.append((rank, idx, task_key, f"{clean_title} [{status_text}]", guessed_kind, element))

        if not candidates:
            return None

        candidates.sort(key=lambda c: (c[0], c[1]))
        _, _, task_key, display_title, guessed_kind, element = candidates[0]
        return task_key, display_title, guessed_kind, element

    def _classify_studycontent_row(self, raw_text: str, status_text: str, icon_hint: str) -> TaskKind:
        """结合雨课堂 studycontent 行内图标与状态文字识别任务类型。"""
        # 1. 带分数进度（如 “进行中(70/75)”）明确是课件/PPT
        if re.search(r"\(\s*\d+\s*/\s*\d+\s*\)", status_text):
            return TaskKind.PPT
        # 2. 图标类名/SVG 名称特征（先判课件，再判视频；避免 "display"/"document" 等通用词误命中）
        if any(k in icon_hint for k in ("ppt", "kejian", "slide", "courseware")):
            return TaskKind.PPT
        if any(k in icon_hint for k in ("shipin", "video", "mooc-video")):
            return TaskKind.VIDEO
        if any(k in icon_hint for k in ("zuoye", "homework", "exam", "quiz", "xiti")):
            return TaskKind.QUIZ
        # 3. 文本关键词回退（如 “小论文” / “心得体会” 归为习题/作业类，不误当视频）
        if any(k in raw_text for k in ("小论文", "心得", "作业", "习题", "测验", "考试")):
            return TaskKind.QUIZ
        guessed = classify_task_text(raw_text)
        if guessed != TaskKind.UNKNOWN:
            return guessed
        # 4. 在“视频学习”章节下的“未开始”且无 X/Y 分数的普通讲次，默认判为视频
        if status_text == "未开始" or status_text.startswith("已观看"):
            return TaskKind.VIDEO
        return TaskKind.UNKNOWN

    def _is_kind_enabled(self, kind: TaskKind) -> bool:
        """检查用户是否在配置中启用了对应类型的任务。"""
        if kind == TaskKind.VIDEO:
            return self.config.enable_video
        if kind == TaskKind.PPT:
            return self.config.enable_ppt
        if kind == TaskKind.QUIZ:
            return self.config.enable_quiz
        # 对目录标题无法直接判断类型的节点，只要任一功能开启则放行进入后再精确识别
        return self.config.enable_video or self.config.enable_ppt or self.config.enable_quiz

    def _inspect_current_page_kind(self, driver: WebDriver) -> TaskKind:
        """进入任务页面后，结合 URL、video 标签与正文进行二次精准判定。"""
        try:
            url = driver.current_url or ""
            has_video = bool(driver.find_elements(By.TAG_NAME, "video"))
            body_text = (driver.execute_script("return document.body ? document.body.innerText.slice(0, 1500) : '';") or "")
            return detect_page_task_kind(url, body_text, has_video=has_video)
        except WebDriverException:
            return TaskKind.UNKNOWN

    def _execute_single_task(self, kind: TaskKind, title: str) -> TaskResult:
        """将具体任务派发给对应执行器，统一兜底异常并自动留存诊断信息。"""
        if kind == TaskKind.VIDEO and not self.config.enable_video:
            return TaskResult(title, kind, TaskStatus.SKIPPED, "用户未启用看视频功能")
        if kind == TaskKind.PPT and not self.config.enable_ppt:
            return TaskResult(title, kind, TaskStatus.SKIPPED, "用户未启用看课件功能")
        if kind == TaskKind.QUIZ and not self.config.enable_quiz:
            return TaskResult(title, kind, TaskStatus.SKIPPED, "用户未启用 AI 做题功能")

        executor_map: Dict[TaskKind, object] = {
            TaskKind.VIDEO: self.video_watcher,
            TaskKind.PPT: self.ppt_watcher,
            TaskKind.QUIZ: self.quiz_solver,
        }
        executor = executor_map.get(kind)
        if executor is None:
            msg = f"未能识别任务页面类型（{title}），已跳过并导出诊断。"
            self.logger.warning(msg)
            if self.config.save_diagnostics_on_error:
                capture_diagnostics(self.browser.driver, f"unknown_{title}", msg, self.logger)
            return TaskResult(title, TaskKind.UNKNOWN, TaskStatus.SKIPPED, msg)

        try:
            result: TaskResult = executor.execute(title)
            if result.status == TaskStatus.FAILED and self.config.save_diagnostics_on_error:
                capture_diagnostics(self.browser.driver, f"failed_{kind.value}_{title}", result.message, self.logger)
            return result
        except Exception as exc:
            self.logger.exception("执行任务《%s》时发生未预期异常：%s", title, exc)
            if self.config.save_diagnostics_on_error:
                capture_diagnostics(self.browser.driver, f"exception_{title}", str(exc), self.logger)
            return TaskResult(title, kind, TaskStatus.FAILED, str(exc))
