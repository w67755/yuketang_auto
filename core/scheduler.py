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
    return TaskKind.UNKNOWN


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

        # 否则视为课程目录 / 学习日志列表页：展开章节并依次扫描未完成任务
        self.logger.info("正在扫描《%s》中的未完成任务点...", course_label)
        self._expand_collapsed_chapters(driver)
        interruptible_sleep(1.5, self.stop_event)

        course_list_handle = driver.current_window_handle
        course_list_url = driver.current_url

        # 用“已尝试过的任务指纹集合”防止同一异常任务陷入死循环
        attempted_keys: set[str] = set()
        max_rounds = 200

        for _ in range(max_rounds):
            if self.stop_event.is_set():
                break

            # 确保每次扫描都回到课程目录主标签页
            self.browser.close_extra_tabs(course_list_handle)
            if driver.current_url != course_list_url:
                driver.get(course_list_url)
                if not interruptible_sleep(2.5, self.stop_event):
                    break
                self._expand_collapsed_chapters(driver)

            next_candidate = self._find_next_pending_task(driver, attempted_keys)
            if next_candidate is None:
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
                # 若 URL 未变化，再宽容等待 1.5 秒以适配 SPA 弹窗型课件/视频
                interruptible_sleep(1.5, self.stop_event)

            self.browser.inject_anti_blur_now()
            actual_kind = self._inspect_current_page_kind(driver)
            if actual_kind == TaskKind.UNKNOWN:
                actual_kind = guessed_kind

            result = self._execute_single_task(actual_kind, title=task_title)
            summary.record(result)

            # 任务完成后清理临时标签页，回到课程列表继续下一项
            self.browser.close_extra_tabs(course_list_handle)
            interruptible_sleep(1.0, self.stop_event)

    def _expand_collapsed_chapters(self, driver: WebDriver) -> None:
        """展开折叠的章节目录，确保所有子任务节点都能被扫描到。"""
        for selector in SELECTORS.chapter_expand_selectors:
            try:
                headers = driver.find_elements(By.CSS_SELECTOR, selector)
                for header in headers:
                    if header.is_displayed():
                        driver.execute_script("arguments[0].click();", header)
                        time.sleep(0.15)
            except WebDriverException:
                continue

    def _find_next_pending_task(
        self,
        driver: WebDriver,
        attempted_keys: set[str],
    ) -> Optional[Tuple[str, str, TaskKind, object]]:
        """在当前课程目录 DOM 中找到下一个尚未完成且未被尝试过的任务项。"""
        for selector in SELECTORS.task_item_selectors:
            try:
                items = driver.find_elements(By.CSS_SELECTOR, selector)
            except WebDriverException:
                continue

            for idx, item in enumerate(items):
                try:
                    if not item.is_displayed():
                        continue
                    raw_text = " ".join((item.text or "").split())
                    if not raw_text or len(raw_text) < 2:
                        continue
                except StaleElementReferenceException:
                    continue

                # 跳过明确标注“已完成 / 100% / 已提交”的条目
                if any(done_kw in raw_text for done_kw in SELECTORS.completed_keywords):
                    continue

                guessed_kind = classify_task_text(raw_text)
                if not self._is_kind_enabled(guessed_kind):
                    continue

                short_title = raw_text[:45]
                task_key = f"{selector}#{idx}:{short_title}"
                if task_key in attempted_keys:
                    continue

                return task_key, short_title, guessed_kind, item
        return None

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
