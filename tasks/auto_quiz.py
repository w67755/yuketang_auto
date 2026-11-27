"""雨课堂习题 / 测试自动作答与提交模块（接入 DeepSeek API）。

处理流程：
1. 扫描当前页面的题目容器（兼容单页单题与整卷多题模式）；
2. 提取每道题的题型（单选 / 多选 / 判断 / 填空 / 简答）、题干与选项列表；
3. 调用 `DeepSeekClient.ask(...)` 获取答案列表；
4. 按题型自动点击选项或填入文本（派发 Vue 兼容的 input/change 事件，确保前端状态同步）；
5. 若配置了 `auto_submit_quiz=True`，全部题目填答完毕后自动点击「提交」并处理确认弹窗。
"""

from __future__ import annotations

import re
import time
from typing import List, Tuple

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from core.browser import interruptible_sleep
from core.selectors import SELECTORS
from tasks.base import BaseTask, TaskKind, TaskResult, TaskStatus
from tasks.deepseek_client import DeepSeekClient


def detect_question_type(raw_text: str, option_count: int, input_count: int) -> str:
    """结合题目文本关键词与 DOM 控件特征推断题型。"""
    if "多选" in raw_text:
        return "多选题"
    if "判断" in raw_text:
        return "判断题"
    if "单选" in raw_text:
        return "单选题"
    if "填空" in raw_text:
        return "填空题"
    if any(kw in raw_text for kw in ("简答", "论述", "主观", "分析题")):
        return "简答题"

    # 若题干未显式标明题型，依据页面控件形态兜底判断
    if option_count == 2:
        return "判断题"
    if option_count >= 3:
        return "单选题"
    if input_count >= 2:
        return "填空题"
    if input_count == 1:
        return "简答题"
    return "单选题"


def match_option_indices(answers: List[str], option_texts: List[str]) -> List[int]:
    """将 DeepSeek 返回的答案列表精准映射到选项索引（0-based）。

    支持三种匹配形式：
    1. 直接返回选项字母，例如 ["A"]、["A", "C"]、["AC"]；
    2. 判断题同义词匹配，例如 "正确"/"对"/"T" vs "错误"/"错"/"F"；
    3. 选项正文包含或归一化相等匹配。
    """
    matched: List[int] = []
    normalized_options = [re.sub(r"\s+", "", opt).lower() for opt in option_texts]

    # 展开类似 "AC" 的连续字母答案
    expanded_answers: List[str] = []
    for ans in answers:
        clean = ans.strip()
        if re.fullmatch(r"[A-Ha-h]{2,6}", clean):
            expanded_answers.extend(list(clean.upper()))
        else:
            expanded_answers.append(clean)

    true_synonyms = {"正确", "对", "是", "true", "t", "√"}
    false_synonyms = {"错误", "错", "否", "false", "f", "×"}

    for ans in expanded_answers:
        clean = ans.strip()
        clean_norm = re.sub(r"\s+", "", clean).lower()

        # 策略 1：单字母直接按 A=0, B=1 ... 映射
        letter_match = re.match(r"^([A-Ha-h])(?:[.\、\s]|$)", clean)
        if letter_match:
            idx = ord(letter_match.group(1).upper()) - ord("A")
            if 0 <= idx < len(option_texts) and idx not in matched:
                matched.append(idx)
                continue

        # 策略 2：判断题同义词匹配
        if clean_norm in true_synonyms or clean_norm in false_synonyms:
            target_set = true_synonyms if clean_norm in true_synonyms else false_synonyms
            found_judge = False
            for idx, opt_norm in enumerate(normalized_options):
                if any(word in opt_norm for word in target_set):
                    if idx not in matched:
                        matched.append(idx)
                    found_judge = True
                    break
            if found_judge:
                continue

        # 策略 3：剥离选项前缀（如 "A."、"B、"）后做正文匹配
        for idx, opt_norm in enumerate(normalized_options):
            opt_body = re.sub(r"^[a-h][.\、:：)]*", "", opt_norm)
            if clean_norm and (clean_norm == opt_body or clean_norm in opt_norm or opt_body in clean_norm):
                if idx not in matched:
                    matched.append(idx)
                break

    return matched


class QuizSolver(BaseTask):
    """负责单份习题/测验的题目识别、DeepSeek 作答与自动提交。"""

    def execute(self, title: str) -> TaskResult:
        driver = self.browser.ensure_started()
        self.browser.inject_anti_blur_now()

        # 双重安全锁：即使开启了 AI 做题，遇到论文/心得/报告等重要主观考核任务也强制跳过
        page_head_text = ""
        try:
            page_head_text = driver.execute_script("return document.body ? document.body.innerText.slice(0, 600) : '';") or ""
        except WebDriverException:
            pass
        if any(kw in (title or "") or kw in page_head_text for kw in SELECTORS.protected_essay_keywords):
            msg = f"检测到论文/心得类重要主观考核任务（{title}），安全保护已拦截，绝不自动作答或提交。"
            self.logger.warning("【安全保护】%s", msg)
            return TaskResult(title, TaskKind.QUIZ, TaskStatus.SKIPPED, msg)

        self.logger.info("【做题】开始处理习题：%s", title)

        client = DeepSeekClient(
            api_key=self.config.deepseek_api_key,
            model=self.config.deepseek_model,
            base_url=self.config.deepseek_base_url,
            logger=self.logger,
        )
        if not client.is_configured():
            msg = "未配置有效的 DeepSeek API Key，已跳过自动做题。"
            self.logger.warning("【做题】%s", msg)
            return TaskResult(title, TaskKind.QUIZ, TaskStatus.SKIPPED, msg)

        # 检查是否已提交过，避免重复操作
        if self._is_quiz_already_submitted(driver):
            self.logger.info("【做题】《%s》已提交，跳过。", title)
            return TaskResult(title, TaskKind.QUIZ, TaskStatus.SKIPPED, "该习题已完成提交")

        containers = self._find_question_containers(driver)
        if not containers:
            return TaskResult(title, TaskKind.QUIZ, TaskStatus.FAILED, "未在页面中找到可识别的题目容器")

        self.logger.info("【做题】共识别到 %d 道题目，开始依次调用 DeepSeek 求解...", len(containers))
        solved_count = 0

        for idx, container in enumerate(containers, start=1):
            if self.stop_event.is_set():
                return TaskResult(title, TaskKind.QUIZ, TaskStatus.STOPPED, "用户手动停止")

            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", container)
                time.sleep(0.3)
            except WebDriverException:
                pass

            q_type, prompt_text, option_els, input_els = self._extract_question(container)
            if not prompt_text:
                self.logger.warning("【做题】第 %d 题题干为空，跳过。", idx)
                continue

            self.logger.info("【做题】正在求解第 %d/%d 题（%s）...", idx, len(containers), q_type)
            try:
                answers = client.ask(q_type, prompt_text)
            except Exception as exc:
                return TaskResult(title, TaskKind.QUIZ, TaskStatus.FAILED, f"第 {idx} 题调用 DeepSeek 失败：{exc}")

            if not answers:
                self.logger.warning("【做题】第 %d 题未能解析出有效答案，跳过该题。", idx)
                continue

            if self._fill_answer(driver, q_type, answers, option_els, input_els):
                solved_count += 1
            interruptible_sleep(0.8, self.stop_event)

        if solved_count == 0:
            return TaskResult(title, TaskKind.QUIZ, TaskStatus.FAILED, "未能成功作答任何题目")

        if self.config.auto_submit_quiz:
            submitted = self._submit_quiz(driver)
            if submitted:
                self.logger.info("【做题】《%s》已作答 %d/%d 题并完成自动提交。", title, solved_count, len(containers))
                return TaskResult(title, TaskKind.QUIZ, TaskStatus.COMPLETED, f"已作答 {solved_count}/{len(containers)} 题并提交")
            return TaskResult(title, TaskKind.QUIZ, TaskStatus.FAILED, f"已作答 {solved_count}/{len(containers)} 题，但未找到提交按钮")

        return TaskResult(title, TaskKind.QUIZ, TaskStatus.COMPLETED, f"已作答 {solved_count}/{len(containers)} 题（未开启自动提交）")

    def _is_quiz_already_submitted(self, driver: WebDriver) -> bool:
        try:
            text = driver.execute_script("return document.body ? document.body.innerText.slice(0, 800) : '';") or ""
            return any(kw in text for kw in ("已提交", "查看解析", "得分：", "总分："))
        except WebDriverException:
            return False

    def _find_question_containers(self, driver: WebDriver) -> List[WebElement]:
        for selector in SELECTORS.quiz_question_container_selectors:
            try:
                found = [el for el in driver.find_elements(By.CSS_SELECTOR, selector) if el.is_displayed()]
                if found:
                    return found
            except WebDriverException:
                continue
        return []

    def _extract_question(
        self, container: WebElement
    ) -> Tuple[str, str, List[WebElement], List[WebElement]]:
        """从单个题目容器中提取题型、完整题干+选项文本、选项节点与输入框节点。"""
        raw_text = " ".join((container.text or "").split())

        option_els: List[WebElement] = []
        for selector in SELECTORS.quiz_option_selectors:
            try:
                found = [el for el in container.find_elements(By.CSS_SELECTOR, selector) if el.is_displayed()]
                if found:
                    option_els = found
                    break
            except WebDriverException:
                continue

        input_els: List[WebElement] = []
        for selector in SELECTORS.quiz_input_selectors:
            try:
                input_els.extend(
                    el for el in container.find_elements(By.CSS_SELECTOR, selector) if el.is_displayed()
                )
            except WebDriverException:
                continue

        q_type = detect_question_type(raw_text, len(option_els), len(input_els))

        # 拼接清晰的题目+选项文本供 DeepSeek 阅读
        lines = [raw_text]
        if option_els:
            lines.append("可选选项列表：")
            for idx, opt in enumerate(option_els):
                letter = chr(ord("A") + idx)
                lines.append(f"{letter}. {' '.join((opt.text or '').split())}")

        return q_type, "\n".join(lines), option_els, input_els

    def _fill_answer(
        self,
        driver: WebDriver,
        q_type: str,
        answers: List[str],
        option_els: List[WebElement],
        input_els: List[WebElement],
    ) -> bool:
        """按题型将答案回填至网页控件。"""
        # 客观选择题（单选 / 多选 / 判断）
        if option_els:
            option_texts = [" ".join((el.text or "").split()) for el in option_els]
            indices = match_option_indices(answers, option_texts)
            if not indices:
                self.logger.warning("【做题】未能将答案 %s 匹配到选项 %s", answers, option_texts)
                return False

            # 单选/判断仅点击首个匹配项
            if q_type in ("单选题", "判断题"):
                indices = indices[:1]

            for idx in indices:
                try:
                    driver.execute_script("arguments[0].click();", option_els[idx])
                    time.sleep(0.2)
                except WebDriverException:
                    continue
            return True

        # 填空题 / 简答题（需触发 input & change 事件，兼容 Vue 双向绑定）
        if input_els:
            for idx, field in enumerate(input_els):
                value = answers[idx] if idx < len(answers) else answers[-1]
                try:
                    tag = (field.tag_name or "").lower()
                    if tag in ("input", "textarea"):
                        driver.execute_script(
                            """
                            const el = arguments[0], val = arguments[1];
                            el.focus();
                            el.value = val;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            """,
                            field,
                            value,
                        )
                    else:
                        driver.execute_script(
                            """
                            const el = arguments[0], val = arguments[1];
                            el.focus();
                            el.innerText = val;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            """,
                            field,
                            value,
                        )
                except WebDriverException:
                    continue
            return True

        return False

    def _submit_quiz(self, driver: WebDriver) -> bool:
        """点击提交按钮并自动确认可能弹出的二次确认对话框。"""
        clicked_submit = self._click_button_by_texts(driver, SELECTORS.quiz_submit_button_texts)
        if not clicked_submit:
            return False

        interruptible_sleep(1.2, self.stop_event)
        # 若弹出“确认提交？”对话框则自动点击确认
        self._click_button_by_texts(driver, SELECTORS.quiz_confirm_button_texts)
        interruptible_sleep(1.5, self.stop_event)
        return True

    def _click_button_by_texts(self, driver: WebDriver, texts: Tuple[str, ...]) -> bool:
        for text in texts:
            try:
                xpath = f"//*[self::button or self::a or self::div or self::span][normalize-space()='{text}']"
                for el in driver.find_elements(By.XPATH, xpath):
                    if el.is_displayed():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        driver.execute_script("arguments[0].click();", el)
                        return True
            except WebDriverException:
                continue
        return False
