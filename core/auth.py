"""雨课堂登录态校验（微信扫码）与课程导航模块。

设计说明：
- 雨课堂主要通过微信扫码登录；由于浏览器已启用持久化 user-data-dir，
  首次扫码后 Cookie 会自动保留，后续启动可直接跳过扫码步骤。
- 选课支持双模式：
  1. 接管模式：用户已在浏览器里手动点进目标课程目录页或单个任务页，脚本直接接管；
  2. 课程名模式：根据配置里的课程关键词，在首页课程卡片中自动匹配并点击进入。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from app.config import AppConfig
from core.browser import BrowserManager, interruptible_sleep
from core.selectors import SELECTORS


def is_on_login_page(driver: WebDriver) -> bool:
    """判断当前页面是否处于登录/扫码等待状态。"""
    try:
        url = (driver.current_url or "").lower()
    except WebDriverException:
        return True

    if any(kw in url for kw in SELECTORS.login_url_keywords):
        return True

    # 补充检查页面是否展示了明显的扫码登录提示
    try:
        body_text = (driver.execute_script("return document.body ? document.body.innerText : '';") or "")
        if "微信扫码登录" in body_text or "请使用微信扫一扫" in body_text:
            return True
    except WebDriverException:
        pass
    return False


def is_inside_course_or_task_page(url: str) -> bool:
    """判断当前 URL 是否已经位于某个具体课程目录或视频/课件/习题页面内。"""
    lower = (url or "").lower()
    all_keywords = (
        SELECTORS.course_page_url_keywords
        + SELECTORS.video_url_keywords
        + SELECTORS.ppt_url_keywords
        + SELECTORS.quiz_url_keywords
    )
    return any(kw.lower() in lower for kw in all_keywords)


def ensure_logged_in(
    browser: BrowserManager,
    config: AppConfig,
    stop_event: threading.Event,
    logger: logging.Logger,
    wait_timeout: int = 300,
) -> bool:
    """确保浏览器已打开目标雨课堂站点并完成登录。"""
    driver = browser.ensure_started()
    current_url = (driver.current_url or "").lower()
    target_host = config.base_url.replace("https://", "").replace("http://", "").split("/")[0].lower()

    # 若当前不在目标域名（例如刚启动在 data:, 空白页），先打开雨课堂入口
    if target_host not in current_url:
        logger.info("正在打开雨课堂入口：%s", config.base_url)
        driver.get(config.base_url)
        if not interruptible_sleep(2.5, stop_event):
            return False

    if not is_on_login_page(driver):
        logger.info("检测到有效登录态，免扫码直接继续。")
        browser.inject_anti_blur_now()
        return True

    logger.info("当前需要登录：请在弹出的浏览器窗口中使用微信扫码登录雨课堂（最多等待 %d 秒）...", wait_timeout)
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if stop_event.is_set():
            logger.info("收到停止信号，取消等待登录。")
            return False
        if not is_on_login_page(driver):
            logger.info("微信扫码登录成功！登录态已自动持久化到配置目录。")
            interruptible_sleep(2.0, stop_event)
            browser.inject_anti_blur_now()
            return True
        if not interruptible_sleep(1.5, stop_event):
            return False

    logger.error("等待扫码登录超时（%d 秒），请重试。", wait_timeout)
    return False


def _try_click_my_courses_tab(driver: WebDriver, logger: logging.Logger) -> None:
    """在首页尝试切换到“我听的课”标签页，确保学生课程列表可见。"""
    for text in SELECTORS.my_courses_tab_texts:
        try:
            candidates = driver.find_elements(
                By.XPATH,
                f"//*[self::a or self::button or self::span or self::div][normalize-space()='{text}']",
            )
            for el in candidates:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    logger.debug("已切换课程标签：%s", text)
                    time.sleep(1.0)
                    return
        except WebDriverException:
            continue


def enter_course_by_name(
    browser: BrowserManager,
    config: AppConfig,
    course_name: str,
    stop_event: threading.Event,
    logger: logging.Logger,
) -> bool:
    """返回雨课堂首页并根据课程名称关键词查找并进入对应课程。"""
    driver = browser.ensure_started()
    logger.info("正在查找课程：《%s》", course_name)

    # 先回到首页课程列表
    driver.get(config.base_url)
    if not interruptible_sleep(2.5, stop_event):
        return False

    _try_click_my_courses_tab(driver, logger)
    if not interruptible_sleep(1.5, stop_event):
        return False

    # 策略 1：优先在已知课程卡片选择器中匹配名称
    target_el = _find_course_element(driver, course_name)
    if target_el is None:
        # 策略 2：页面向下滚动一次以触发懒加载课程卡片后再查找
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
        except WebDriverException:
            pass
        interruptible_sleep(1.0, stop_event)
        target_el = _find_course_element(driver, course_name)

    if target_el is None:
        logger.warning("未在课程列表中找到《%s》，请确认课程名称或改用手动进课接管模式。", course_name)
        return False

    before_handles = set(driver.window_handles)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target_el)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", target_el)
    except WebDriverException as exc:
        logger.error("点击课程《%s》失败：%s", course_name, exc)
        return False

    if not interruptible_sleep(2.5, stop_event):
        return False

    # 若点击后在新标签页打开，自动切到新标签页
    after_handles = set(driver.window_handles)
    new_handles = list(after_handles - before_handles)
    if new_handles:
        driver.switch_to.window(new_handles[-1])
        interruptible_sleep(1.5, stop_event)

    browser.inject_anti_blur_now()
    logger.info("已进入课程《%s》，当前页面：%s", course_name, driver.current_url)
    return True


def _find_course_element(driver: WebDriver, course_name: str) -> Optional[object]:
    """在课程卡片及可见节点中按忽略空格的包含关系查找课程元素（兼容“形势与政策7”匹配“形势与政策 7”）。"""
    keyword = "".join(course_name.split()).lower()
    if not keyword:
        return None

    for selector in SELECTORS.course_card_selectors:
        try:
            cards = driver.find_elements(By.CSS_SELECTOR, selector)
            for card in cards:
                card_norm = "".join((card.text or "").split()).lower()
                if card.is_displayed() and keyword in card_norm:
                    return card
        except WebDriverException:
            continue

    # 兜底：遍历可见卡片/标题元素，忽略中间空白后匹配
    try:
        nodes = driver.find_elements(By.CSS_SELECTOR, "h1, h2, h3, h4, p, span, div")
        best_node = None
        best_len = 10**9
        for node in nodes:
            if not node.is_displayed():
                continue
            raw = (node.text or "").strip()
            norm = "".join(raw.split()).lower()
            if keyword in norm and len(norm) < best_len:
                best_node = node
                best_len = len(norm)
        if best_node is not None:
            return best_node
    except WebDriverException:
        pass
    return None
