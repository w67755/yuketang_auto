"""雨课堂课件 / PPT 自动观看与漏页定向补看模块。

核心场景（对应雨课堂“同学你没有好好观看喔 / 您还有以下页面未看”校验机制）：
1. 每页课件需停留足够时长，雨课堂才会记录该页为“已看”。
2. 本模块执行流程：
   - 第一轮（全量逐页浏览）：从第 1 页翻到最后一页，每页按配置的随机区间
     `[ppt_dwell_min, ppt_dwell_max]` 停留，期间发送轻量页面交互并实时响应停止信号；
   - 翻完后自动检查结束页是否出现“您还有以下页面未看”提示或侧边栏仍存在未读标记；
   - 若存在未看页码，点击「继续观看」（如有），然后按页码逐一跳转并**加长停留时间**
     进行定向补看，最多循环 `ppt_retry_rounds` 轮（默认 2 轮）。
"""

from __future__ import annotations

import random
import re
import time
from typing import Iterable, List, Optional, Tuple

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from core.browser import interruptible_sleep
from core.selectors import SELECTORS
from tasks.base import BaseTask, TaskKind, TaskResult, TaskStatus


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
    """从结束页提示文本或未看标签列表中提取去重且升序排列的未看页码。

    示例输入：
    - page_text: "同学你没有好好观看喔 您还有以下页面未看 18 52 54 57 64 65 继续观看"
    - badge_texts: ["18", "52", "54", "57", "64", "65"]
    返回：[18, 52, 54, 57, 64, 65]
    """
    pages: set[int] = set()

    # 1. 优先解析显式未看页码标签元素（最准确）
    for raw in badge_texts:
        cleaned = (raw or "").strip()
        if re.fullmatch(r"\d{1,4}", cleaned):
            num = int(cleaned)
            if 1 <= num <= 2000:
                pages.add(num)

    # 2. 再从“以下页面未看 / 未观看页面 / 未看页”之后的文本段落提取页码
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
    """负责单份课件/PPT 的逐页随机停留与未看页码自动补看。"""

    def execute(self, title: str) -> TaskResult:
        driver = self.browser.ensure_started()
        self.browser.inject_anti_blur_now()
        self.logger.info(
            "【课件】开始观看：%s（每页随机停留 %.1f~%.1f 秒，最多补看 %d 轮）",
            title,
            self.config.ppt_dwell_min,
            self.config.ppt_dwell_max,
            self.config.ppt_retry_rounds,
        )

        # 如果进入时正好停留在“您还有以下页面未看”结果页，先直接尝试补看
        initial_missed = self._detect_unviewed_pages(driver)
        if initial_missed:
            self.logger.info("【课件】进入页面即检测到 %d 个未看页码：%s，直接启动定向补看。", len(initial_missed), initial_missed)
            return self._run_remediation_loop(driver, title, initial_missed)

        # 第一轮：全量逐页观看
        slide_elements = self._find_slide_items(driver)
        counter = self._read_page_counter(driver)

        if slide_elements:
            ok = self._walk_by_slide_list(driver, title, slide_elements)
        elif counter is not None:
            ok = self._walk_by_next_action(driver, title, total_pages=counter[1], start_page=counter[0])
        else:
            # 兜底：适用于竖向长图/PDF 滚动式课件
            ok = self._walk_by_vertical_scroll(driver, title)

        if not ok:
            return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

        # 翻完后稍等 2 秒让页面结算“是否全部观看”
        if not interruptible_sleep(2.0, self.stop_event):
            return TaskResult(title, TaskKind.PPT, TaskStatus.STOPPED, "用户手动停止")

        missed_pages = self._detect_unviewed_pages(driver)
        if not missed_pages:
            self.logger.info("【课件】《%s》所有页面已观看完成，无漏看页码。", title)
            return TaskResult(title, TaskKind.PPT, TaskStatus.COMPLETED, "课件全量观看完成")

        return self._run_remediation_loop(driver, title, missed_pages)

    def _walk_by_slide_list(
        self,
        driver: WebDriver,
        title: str,
        initial_slides: List[WebElement],
    ) -> bool:
        """通过左侧/底部缩略图列表逐页点击并随机停留。"""
        total = len(initial_slides)
        self.logger.info("【课件】检测到缩略图列表，共 %d 页，开始逐页停留...", total)

        for idx in range(total):
            if self.stop_event.is_set():
                return False

            # 每页重新定位元素，防止 SPA 局部重渲染导致 StaleElementReferenceException
            slides = self._find_slide_items(driver)
            if idx >= len(slides):
                break
            slide = slides[idx]

            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", slide)
                driver.execute_script("arguments[0].click();", slide)
            except WebDriverException:
                self._step_next_page(driver)

            dwell = self._pick_dwell_seconds(extra=0.0)
            self.logger.info("【课件】《%s》第 %d/%d 页，停留 %.1f 秒...", title, idx + 1, total, dwell)
            if not self._dwell_with_keepalive(driver, dwell):
                return False

        return True

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
                # 在最后一页额外翻一次，触发部分雨课堂版本的完成结算页
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
        """针对“您还有以下页面未看”进行加长停留的定向补看循环（最多重试 N 轮）。"""
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

            # 若当前停在“同学你没有好好观看喔”结果页，先点击「继续观看」回到课件视图
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

            # 补看完毕后翻至末页触发状态刷新，再次检测是否仍有未看页
            self._trigger_completion_check(driver)
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
        """综合页面提示语、未看页码标签与侧边栏未读标记，返回未看页码列表。"""
        badge_texts: List[str] = []
        for selector in SELECTORS.ppt_unviewed_badge_selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, selector):
                    if el.is_displayed():
                        badge_texts.append(el.text or "")
            except WebDriverException:
                continue

        # 额外检查侧边栏中带有明确“未看/unread”类名的缩略图项
        slides = self._find_slide_items(driver)
        for idx, slide in enumerate(slides, start=1):
            try:
                cls = (slide.get_attribute("class") or "").lower()
                text = (slide.text or "").strip()
                if any(flag in cls for flag in ("unread", "unviewed", "not-read")) or "未看" in text:
                    badge_texts.append(str(idx))
            except WebDriverException:
                continue

        try:
            body_text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        except WebDriverException:
            body_text = ""

        return parse_unviewed_pages(body_text, badge_texts)

    def _click_continue_watching_if_present(self, driver: WebDriver) -> bool:
        """若页面存在“继续观看”按钮，点击返回课件展示区。"""
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
        """精准跳转到指定页码（1-based）：优先点缩略图，其次点未看页码按钮，最后按方向键步进。"""
        # 方式 1：点击结束页或页码条中直接标有该数字的按钮
        try:
            xpath = f"//*[self::button or self::span or self::div or self::li][normalize-space()='{page_no}']"
            for el in driver.find_elements(By.XPATH, xpath):
                if el.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(0.35)
                    return True
        except WebDriverException:
            pass

        # 方式 2：按索引点击缩略图列表第 page_no - 1 项
        slides = self._find_slide_items(driver)
        if 1 <= page_no <= len(slides):
            try:
                target = slides[page_no - 1]
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
                driver.execute_script("arguments[0].click();", target)
                time.sleep(0.35)
                return True
            except WebDriverException:
                pass

        # 方式 3：基于当前页码计数用上一页/下一页步进逼近
        counter = self._read_page_counter(driver)
        if counter is not None:
            current, _ = counter
            diff = page_no - current
            steps = min(abs(diff), 80)
            for _ in range(steps):
                if self.stop_event.is_set():
                    return False
                if diff > 0:
                    self._step_next_page(driver)
                else:
                    self._step_prev_page(driver)
                time.sleep(0.2)
            return True
        return False

    def _trigger_completion_check(self, driver: WebDriver) -> None:
        """补看完毕后跳到最后一页并尝试再翻一次，促使页面刷新完成统计。"""
        slides = self._find_slide_items(driver)
        if slides:
            try:
                driver.execute_script("arguments[0].click();", slides[-1])
                time.sleep(0.5)
            except WebDriverException:
                pass
        self._step_next_page(driver)

    def _find_slide_items(self, driver: WebDriver) -> List[WebElement]:
        """查找可见的课件页缩略图/页码节点列表。"""
        for selector in SELECTORS.ppt_slide_item_selectors:
            try:
                items = [el for el in driver.find_elements(By.CSS_SELECTOR, selector) if el.is_displayed()]
                # 至少 2 个节点才认为是有效的多页列表，避免误命中单个容器
                if len(items) >= 2:
                    return items
            except WebDriverException:
                continue
        return []

    def _read_page_counter(self, driver: WebDriver) -> Optional[Tuple[int, int]]:
        """读取页面上类似 '18 / 70' 的当前页与总页数。"""
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
        """翻到下一页：优先点击下一页按钮，否则发送键盘右方向键。"""
        for selector in SELECTORS.ppt_next_button_selectors:
            try:
                for btn in driver.find_elements(By.CSS_SELECTOR, selector):
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        return
            except WebDriverException:
                continue
        try:
            ActionChains(driver).send_keys(Keys.ARROW_RIGHT).perform()
        except WebDriverException:
            pass

    def _step_prev_page(self, driver: WebDriver) -> None:
        """翻到上一页：优先点击上一页按钮，否则发送键盘左方向键。"""
        for selector in SELECTORS.ppt_prev_button_selectors:
            try:
                for btn in driver.find_elements(By.CSS_SELECTOR, selector):
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        return
            except WebDriverException:
                continue
        try:
            ActionChains(driver).send_keys(Keys.ARROW_LEFT).perform()
        except WebDriverException:
            pass

    def _pick_dwell_seconds(self, extra: float = 0.0) -> float:
        """按配置区间生成拟人化随机停留时长。"""
        base = random.uniform(self.config.ppt_dwell_min, self.config.ppt_dwell_max)
        return round(base + max(0.0, extra), 1)

    def _dwell_with_keepalive(self, driver: WebDriver, seconds: float) -> bool:
        """在当前课件页停留指定时长，期间执行轻量活跃触发防止页面休眠。"""
        half = seconds / 2.0
        if not interruptible_sleep(half, self.stop_event):
            return False
        try:
            driver.execute_script("window.dispatchEvent(new Event('mousemove'));")
        except WebDriverException:
            pass
        return interruptible_sleep(seconds - half, self.stop_event)
