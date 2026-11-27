"""雨课堂视频自动观看模块（稳健模式）。

关键设计要点：
1. 绝不注入超高倍速（如 16x），仅使用雨课堂原生支持的 1.0x / 1.5x / 2.0x，
   优先点击播放器自带倍速菜单触发合法心跳事件，保证观看时长 100% 计入。
2. 播放期间持续监测：
   - 页面是否已标注「已完成 / 100%」；
   - `<video>` 是否被意外暂停（若暂停自动恢复）；
   - `currentTime` 是否长时间停滞（自动尝试重新点击播放，连续卡死则安全失败并留存诊断）。
3. 支持在主文档与单层 iframe 内定位 `<video>` 元素。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from core.browser import interruptible_sleep
from core.selectors import SELECTORS
from tasks.base import BaseTask, TaskKind, TaskResult, TaskStatus


class VideoWatcher(BaseTask):
    """负责单节视频任务的播放控制与完成判定。"""

    def execute(self, title: str) -> TaskResult:
        driver = self.browser.ensure_started()
        self.browser.inject_anti_blur_now()
        self.logger.info("【视频】开始处理：%s（目标倍速：%sx）", title, self.config.video_speed)

        # 1. 先检查页面上是否已经显示“已完成”
        if self._is_page_marked_completed(driver):
            self.logger.info("【视频】《%s》已显示完成，跳过。", title)
            return TaskResult(title, TaskKind.VIDEO, TaskStatus.SKIPPED, "页面已标注已完成")

        # 2. 等待 <video> 元素加载（兼容主文档或嵌套 iframe）
        if not self._locate_video_context(driver, wait_seconds=15):
            return TaskResult(title, TaskKind.VIDEO, TaskStatus.FAILED, "未在页面中找到 <video> 播放器元素")

        # 3. 静音并开启播放
        if self.config.video_mute:
            self._ensure_muted(driver)
        self._ensure_playing(driver)
        interruptible_sleep(1.5, self.stop_event)

        # 4. 切换原生倍速（优先点击播放器倍速控件）
        self._apply_native_speed(driver, self.config.video_speed)

        # 5. 持续监控播放进度，直到视频播完或页面显示 100% 完成
        last_time = -1.0
        stalled_cycles = 0
        last_log_ts = 0.0

        while not self.stop_event.is_set():
            state = self._read_video_state(driver)
            if state is None:
                return TaskResult(title, TaskKind.VIDEO, TaskStatus.FAILED, "视频元素意外丢失或窗口已关闭")

            current = state["currentTime"]
            duration = state["duration"]
            paused = state["paused"]
            ended = state["ended"]

            # 每 15 秒打印一次进度，避免日志刷屏
            now = time.monotonic()
            if now - last_log_ts >= 15.0:
                percent = (current / duration * 100.0) if duration > 0 else 0.0
                self.logger.info(
                    "【视频】《%s》播放进度：%.1fs / %.1fs (%.1f%%) | 倍速 %.1fx",
                    title,
                    current,
                    duration,
                    percent,
                    state["playbackRate"],
                )
                last_log_ts = now

            # 完成条件：video.ended 为真、剩余不足 1 秒，或雨课堂页面已显示 100% / 已完成
            if ended or (duration > 5 and current >= duration - 1.0):
                # 播完后多等 3 秒，确保最后一次心跳包成功上报到服务器
                self.logger.info("【视频】《%s》播放结束，等待 3 秒确保心跳上报完成...", title)
                interruptible_sleep(3.0, self.stop_event)
                return TaskResult(title, TaskKind.VIDEO, TaskStatus.COMPLETED, "视频播放完成")

            if self._is_page_marked_completed(driver) and current > 5:
                self.logger.info("【视频】《%s》页面进度已达 100%%，提前结束。", title)
                return TaskResult(title, TaskKind.VIDEO, TaskStatus.COMPLETED, "页面进度已达 100%")

            # 防失焦/防暂停恢复
            if paused:
                self.logger.debug("【视频】检测到视频暂停，自动恢复播放...")
                self._ensure_playing(driver)

            # 卡顿/缓冲停滞检测（每轮休眠 3 秒，连续 10 轮 = 30 秒未推进则尝试强恢复，20 轮判失败）
            if abs(current - last_time) < 0.2:
                stalled_cycles += 1
                if stalled_cycles in (4, 8, 12):
                    self.logger.warning("【视频】检测到播放进度停滞，尝试重新触发播放...")
                    self._ensure_playing(driver)
                elif stalled_cycles >= 20:
                    return TaskResult(title, TaskKind.VIDEO, TaskStatus.FAILED, "视频连续 60 秒无进度推进（可能网络卡顿或存在弹窗阻断）")
            else:
                stalled_cycles = 0
                last_time = current

            if not interruptible_sleep(3.0, self.stop_event):
                return TaskResult(title, TaskKind.VIDEO, TaskStatus.STOPPED, "用户手动停止")

        return TaskResult(title, TaskKind.VIDEO, TaskStatus.STOPPED, "用户手动停止")

    def _locate_video_context(self, driver: WebDriver, wait_seconds: int = 15) -> bool:
        """在主文档或第一层 iframe 中查找 `<video>` 并切入对应上下文。"""
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                driver.switch_to.default_content()
                if driver.find_elements(By.TAG_NAME, "video"):
                    return True

                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                for frame in iframes:
                    try:
                        driver.switch_to.default_content()
                        driver.switch_to.frame(frame)
                        if driver.find_elements(By.TAG_NAME, "video"):
                            return True
                    except WebDriverException:
                        continue
            except WebDriverException:
                return False
            interruptible_sleep(1.0, self.stop_event)

        try:
            driver.switch_to.default_content()
        except WebDriverException:
            pass
        return False

    def _is_page_marked_completed(self, driver: WebDriver) -> bool:
        """检查雨课堂视频页顶栏/状态栏是否已明确显示完成。"""
        for selector in SELECTORS.video_progress_text_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    text = (el.text or "").strip()
                    if any(kw in text for kw in ("已完成", "100%", "完成度：100", "完成度: 100")):
                        return True
            except WebDriverException:
                continue
        return False

    def _ensure_muted(self, driver: WebDriver) -> None:
        """将 video 设为静音，避免后台刷课时出声。"""
        try:
            driver.execute_script(
                "const v = document.querySelector('video'); if (v) { v.muted = true; v.volume = 0; }"
            )
        except WebDriverException:
            pass

    def _ensure_playing(self, driver: WebDriver) -> None:
        """优先点击播放器播放按钮，再以 video.play() 兜底。"""
        for selector in SELECTORS.video_play_button_selectors:
            try:
                btns = driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in btns:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        break
            except WebDriverException:
                continue

        try:
            driver.execute_script(
                """
                const v = document.querySelector('video');
                if (v && v.paused && !v.ended) {
                  v.muted = true;
                  const p = v.play();
                  if (p && p.catch) p.catch(() => {});
                }
                """
            )
        except WebDriverException:
            pass

    def _apply_native_speed(self, driver: WebDriver, target_speed: str) -> None:
        """优先通过雨课堂原生倍速菜单切换倍速；仅允许 1.0/1.5/2.0。"""
        if target_speed == "1.0":
            return

        # 兼容界面上可能显示的 "2.0X" / "2X" / "2.0x" / "1.5X" 等写法
        numeric = float(target_speed)
        accepted_labels = {
            f"{numeric:.1f}x",
            f"{numeric:g}x",
            f"{numeric:.1f}倍",
            f"{numeric:g}倍",
        }

        # 1. 先尝试点击原生倍速菜单按钮展开列表
        for btn_sel in SELECTORS.video_speed_button_selectors:
            try:
                btns = driver.find_elements(By.CSS_SELECTOR, btn_sel)
                for btn in btns:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(0.4)
                        break
            except WebDriverException:
                continue

        # 2. 点击对应的原生倍速选项
        for opt_sel in SELECTORS.video_speed_option_selectors:
            try:
                options = driver.find_elements(By.CSS_SELECTOR, opt_sel)
                for opt in options:
                    text = (opt.text or "").strip().lower().replace(" ", "")
                    if text in accepted_labels:
                        driver.execute_script("arguments[0].click();", opt)
                        self.logger.info("【视频】已通过原生播放器切换至 %sx 倍速。", target_speed)
                        return
            except WebDriverException:
                continue

        # 3. 若播放器未暴露倍速 UI，则在合法上限（<= 2.0）内设置 playbackRate
        safe_rate = min(2.0, max(1.0, numeric))
        try:
            driver.execute_script(
                "const v = document.querySelector('video'); if (v) v.playbackRate = arguments[0];",
                safe_rate,
            )
            self.logger.info("【视频】未找到原生倍速菜单，已安全设置播放速率为 %.1fx。", safe_rate)
        except WebDriverException:
            self.logger.warning("【视频】倍速设置未生效，将保持默认 1.0x 播放。")

    def _read_video_state(self, driver: WebDriver) -> Optional[Dict[str, Any]]:
        """读取 `<video>` 实时播放状态。"""
        try:
            return driver.execute_script(
                """
                const v = document.querySelector('video');
                if (!v) return null;
                return {
                  paused: Boolean(v.paused),
                  ended: Boolean(v.ended),
                  currentTime: Number(v.currentTime || 0),
                  duration: Number(v.duration || 0),
                  playbackRate: Number(v.playbackRate || 1),
                };
                """
            )
        except WebDriverException:
            return None
