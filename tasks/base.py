"""任务执行器通用抽象与结果模型。

统一所有任务（视频、课件、习题）的输入与返回契约，
使调度器能够以一致的方式记录统计、处理停止信号与导出异常诊断。
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from app.config import AppConfig
from core.browser import BrowserManager


class TaskKind(str, Enum):
    """学习任务类型枚举。"""

    VIDEO = "视频"
    PPT = "课件"
    QUIZ = "习题"
    UNKNOWN = "未知"


class TaskStatus(str, Enum):
    """任务执行终态枚举。"""

    COMPLETED = "已完成"
    SKIPPED = "已跳过"
    FAILED = "失败"
    STOPPED = "已中止"


@dataclass
class TaskResult:
    """单个学习任务的执行结果记录。"""

    title: str
    kind: TaskKind
    status: TaskStatus
    message: str = ""


class BaseTask(ABC):
    """所有学习任务执行器的基类。"""

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

    @abstractmethod
    def execute(self, title: str) -> TaskResult:
        """执行当前页面对应的学习任务。"""
