"""Selenium 4 浏览器生命周期管理与防失焦保护模块。

核心职责：
1. 自动启动 Edge（默认）或 Chrome，借助 Selenium 4 内置 Selenium Manager 免去手动下载驱动。
2. 挂载独立 `user-data-dir` 持久化登录态，避免每次启动重复微信扫码。
3. 注入页面级防失焦/切屏暂停脚本，并关闭浏览器后台标签节流，确保视频与课件计分稳定。
4. 提供支持随时响应停止信号的 `interruptible_sleep` 辅助函数。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.remote.webdriver import WebDriver

from app.config import AppConfig

# 防失焦保护脚本：阻止雨课堂通过 visibilitychange / blur 事件在切窗口时暂停视频
ANTI_BLUR_JS = r"""
(() => {
  if (window.__yktAntiBlurInstalled) return;
  window.__yktAntiBlurInstalled = true;
  try {
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
  } catch (e) {}

  const blockedEvents = new Set(['visibilitychange', 'webkitvisibilitychange', 'blur', 'pagehide']);
  const origAddDoc = document.addEventListener.bind(document);
  document.addEventListener = function(type, listener, options) {
    if (blockedEvents.has(type)) return;
    return origAddDoc(type, listener, options);
  };
  const origAddWin = window.addEventListener.bind(window);
  window.addEventListener = function(type, listener, options) {
    if (blockedEvents.has(type)) return;
    return origAddWin(type, listener, options);
  };
})();
"""


# Windows 电源执行状态标志（与视频播放器同原理，阻止无人值守时进入屏保或系统睡眠）
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def set_system_awake(awake: bool) -> None:
    """开启或释放 Windows 防休眠/防屏保状态（非 Windows 平台安全忽略）。"""
    if sys.platform != "win32":
        return
    try:
        flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if awake else ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def interruptible_sleep(seconds: float, stop_event: threading.Event, step: float = 0.25) -> bool:
    """可随时被停止信号打断的休眠函数。

    返回值：
    - True：正常休眠完成；
    - False：休眠期间收到停止信号，提前结束。
    """
    end_time = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end_time:
        if stop_event.is_set():
            return False
        remaining = end_time - time.monotonic()
        time.sleep(min(step, max(0.0, remaining)))
    return not stop_event.is_set()


class BrowserManager:
    """封装 WebDriver 的启动、复用、防失焦注入与安全关闭。"""

    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.driver: Optional[WebDriver] = None

    def is_alive(self) -> bool:
        """检查当前托管的浏览器窗口是否依然存活。"""
        if self.driver is None:
            return False
        try:
            _ = self.driver.window_handles
            return True
        except WebDriverException:
            self.driver = None
            return False

    def ensure_started(self) -> WebDriver:
        """若浏览器尚未启动或已关闭，则按当前配置拉起新实例。"""
        if self.is_alive() and self.driver is not None:
            return self.driver

        profile_dir: Path = self.config.resolved_profile_dir()
        profile_dir.mkdir(parents=True, exist_ok=True)

        browser_type = self.config.browser.lower()
        self.logger.info(
            "正在启动 %s 浏览器（持久化目录：%s）...",
            browser_type.upper(),
            profile_dir,
        )

        if browser_type == "chrome":
            driver = self._start_chrome(profile_dir)
        else:
            driver = self._start_edge(profile_dir)

        driver.set_page_load_timeout(45)
        # 必须设为 0：多选择器回退查找时若开启隐式等待，每个未命中选择器都会白白阻塞数秒，导致整体极慢
        driver.implicitly_wait(0)
        self.driver = driver

        # 注册新文档加载前自动执行的防失焦脚本（CDP 协议，Edge 与 Chrome 均支持）
        self._install_cdp_anti_blur(driver)
        self.logger.info("浏览器启动成功。")
        return driver

    def _apply_common_options(self, options: EdgeOptions | ChromeOptions, profile_dir: Path) -> None:
        """写入 Edge / Chrome 通用的防节流、持久化配置与自动播放参数。"""
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--start-maximized")
        # 允许视频静音自动播放，避免浏览器拦截 play() 调用
        options.add_argument("--autoplay-policy=no-user-gesture-required")
        # 禁用后台标签页定时器节流，防止最小化或切换窗口时视频/课件心跳中断
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        # 降低自动化特征干扰
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)
        # 避免脚本异常退出后误关闭用户手动操作的页面（仅在我们显式 quit 时关闭）
        options.add_experimental_option("detach", True)

    def _start_edge(self, profile_dir: Path) -> WebDriver:
        options = EdgeOptions()
        self._apply_common_options(options, profile_dir)
        if self.config.driver_path:
            service = EdgeService(executable_path=self.config.driver_path)
            return webdriver.Edge(service=service, options=options)
        return webdriver.Edge(options=options)

    def _start_chrome(self, profile_dir: Path) -> WebDriver:
        options = ChromeOptions()
        self._apply_common_options(options, profile_dir)
        if self.config.driver_path:
            service = ChromeService(executable_path=self.config.driver_path)
            return webdriver.Chrome(service=service, options=options)
        return webdriver.Chrome(options=options)

    def _install_cdp_anti_blur(self, driver: WebDriver) -> None:
        """通过 Chrome DevTools Protocol 在每个新文档加载前注入防失焦脚本。"""
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": ANTI_BLUR_JS},
            )
        except Exception as exc:
            self.logger.debug("CDP 防失焦脚本预注入失败（将在页面内按需回退注入）：%s", exc)

    def inject_anti_blur_now(self) -> None:
        """在当前活动页面立即注入一次防失焦脚本（兼容已打开页面）。"""
        if not self.is_alive() or self.driver is None:
            return
        try:
            self.driver.execute_script(ANTI_BLUR_JS)
        except Exception:
            pass

    def switch_to_newest_window(self) -> None:
        """若点击任务打开了新标签页，切换到最新打开的窗口。"""
        if self.driver is None:
            return
        handles = self.driver.window_handles
        if handles:
            self.driver.switch_to.window(handles[-1])

    def close_extra_tabs(self, keep_handle: str) -> None:
        """关闭任务过程中打开的临时标签页，并切回课程目录主标签页。"""
        if self.driver is None:
            return
        for handle in list(self.driver.window_handles):
            if handle != keep_handle:
                try:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
                except WebDriverException:
                    pass
        if keep_handle in self.driver.window_handles:
            self.driver.switch_to.window(keep_handle)
        elif self.driver.window_handles:
            self.driver.switch_to.window(self.driver.window_handles[0])

    def close(self) -> None:
        """安全关闭受控浏览器进程。"""
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            finally:
                self.driver = None
