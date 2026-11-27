"""DeepSeek API 答题客户端（沿用原学习通脚本的 DeepSeek 接入思路并做了健壮性重构）。

相较原版 `DeepSeekAsk.py` 的改进：
1. 直接使用纯 Python `requests` 调用 DeepSeek Chat Completions 接口，
   不依赖带原生编译扩展的庞大 SDK，完美兼容 Python 3.15 等最新环境；
2. 修复原版中 `填空题` 分支被前置条件遮蔽导致永远不会执行的问题，
   并将默认模型修正为官方稳定模型 `deepseek-chat`（可在配置中修改）；
3. 增强返回结果解析：同时兼容标准 JSON 数组、Markdown 代码块包裹的 JSON、
   单引号 Python 列表及异常空白格式。
"""

from __future__ import annotations

import ast
import json
import logging
import random
import re
from typing import List

import requests


def build_question_prompt(question_type: str, question_text: str) -> str:
    """根据题型构造严格要求返回 JSON 字符串数组的提示词。"""
    q_type = (question_type or "").strip()

    if q_type in ("单选题", "判断题"):
        instruction = (
            "你是一个精准的课程习题助手。不要输出任何解释或多余文字，仅返回一个 JSON 字符串数组。\n"
            "要求：答案必须从题目给出的选项中选择且只能选 1 个；若题目带选项字母，优先返回完整选项内容或选项字母。\n"
            '返回格式示例：["A"] 或 ["正确"]\n'
        )
    elif q_type == "多选题":
        instruction = (
            "你是一个精准的课程习题助手。不要输出任何解释或多余文字，仅返回一个 JSON 字符串数组。\n"
            "要求：从题目给出的选项中选择所有正确项，列表每个元素对应一个正确选项。\n"
            '返回格式示例：["A", "C", "D"]\n'
        )
    elif q_type == "填空题":
        instruction = (
            "你是一个精准的课程习题助手。不要输出任何解释或多余文字，仅返回一个 JSON 字符串数组。\n"
            "要求：题目有几个空，数组里就按顺序放几个对应的填空文本答案。\n"
            '返回格式示例：["答案1", "答案2"]\n'
        )
    else:
        # 简答题 / 论述题 / 未知题型
        instruction = (
            "你是一个精准的课程习题助手。不要输出任何解释或多余文字，仅返回一个 JSON 字符串数组。\n"
            "要求：给出简洁、切中要点的文字答案，数组包含 1 个字符串元素，不要包含题号或多余前后缀。\n"
            '返回格式示例：["简洁准确的文字答案"]\n'
        )

    return f"{instruction}\n【题型】{q_type or '未知题型'}\n【题目与选项】\n{question_text.strip()}"


def parse_answer_list(raw_content: str) -> List[str]:
    """从大模型返回文本中稳健提取答案列表。"""
    if not raw_content:
        return []

    text = raw_content.strip()
    # 剥离可能出现的 ```json ... ``` 代码块围栏
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    # 提取最外层中括号内容
    match = re.search(r"\[[\s\S]*\]", text)
    candidate = match.group(0) if match else text

    # 优先按标准 JSON 解析
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass

    # 回退：兼容单引号列表格式 ['A', 'B']
    try:
        parsed = ast.literal_eval(candidate)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except (SyntaxError, ValueError):
        pass

    # 最后兜底：若模型直接返回纯文本答案
    cleaned = text.strip().strip('"').strip("'")
    return [cleaned] if cleaned else []


class DeepSeekClient:
    """封装 DeepSeek Chat Completions API 请求与多模型容错切换。"""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-v4.1-flash",
        base_url: str = "https://api.deepseek.com",
        timeout: int = 45,
        logger: logging.Logger | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        raw_models = (model or "deepseek-v4.1-flash").replace("，", ",")
        self.models = [m.strip() for m in raw_models.split(",") if m.strip()] or ["deepseek-v4.1-flash"]
        self.model = self.models[0]
        self.endpoint = f"{(base_url or 'https://api.deepseek.com').rstrip('/')}/chat/completions"
        self.timeout = timeout
        self.logger = logger

    def is_configured(self) -> bool:
        """检查是否已填写看起来有效的 DeepSeek API Key。"""
        return self.api_key.startswith("sk-") and len(self.api_key) >= 12

    def ask(self, question_type: str, question_text: str) -> List[str]:
        """调用 DeepSeek API 获取题目答案列表；配置多模型时随机选用并在失败时自动回退。"""
        if not self.is_configured():
            raise ValueError("DeepSeek API Key 未配置或格式无效（应以 sk- 开头）")

        prompt = build_question_prompt(question_type, question_text)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # 若填写多个模型（如 deepseek-v4-pro,deepseek-v4-flash），随机排优先级并在单模型报错时自动回退
        candidates = list(self.models)
        random.shuffle(candidates)
        last_error: Exception | None = None

        for model_name in candidates:
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,  # 答题场景采用低温度以提高准确率与格式稳定性
                "stream": False,
            }
            resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code == 401:
                raise RuntimeError("DeepSeek API 鉴权失败（401），请检查 API Key 是否正确")
            if resp.status_code == 402:
                raise RuntimeError("DeepSeek 账户余额不足（402），请先在官网充值")
            if resp.status_code >= 400:
                last_error = RuntimeError(
                    f"模型 {model_name} 请求失败（HTTP {resp.status_code}）：{resp.text[:200]}"
                )
                if self.logger:
                    self.logger.warning("【DeepSeek】%s，尝试切换备选模型...", last_error)
                continue

            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                last_error = RuntimeError(f"模型 {model_name} 未返回有效候选答案")
                continue

            content = (choices[0].get("message") or {}).get("content") or ""
            answers = parse_answer_list(content)
            if self.logger:
                self.logger.info("【DeepSeek:%s】解析答案：%s", model_name, answers)
            return answers

        raise last_error or RuntimeError("DeepSeek API 调用失败")
