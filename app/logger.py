"""统一日志系统：同时支持终端输出、本地日志文件归档与 GUI 实时展示。

使用队列（queue.Queue）在后台工作线程与 Tkinter 主线程之间安全传递日志消息，
避免在子线程中直接操作 GUI 控件导致卡顿或崩溃。
"""

from __future__ import annotations

import logging
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from app.config import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "logs"

# 日志消息元组：(级别名, 格式化后的文本)
LogMessage = Tuple[str, str]


class QueueLogHandler(logging.Handler):
    """将日志推送到线程安全队列，供桌面 GUI 定时拉取并渲染彩色文本。"""

    def __init__(self, log_queue: "queue.Queue[LogMessage]") -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.log_queue.put_nowait((record.levelname, msg))
        except Exception:
            # 日志输出绝不能反向导致业务线程异常
            pass


def setup_logger(
    name: str = "yuketang",
    log_queue: Optional["queue.Queue[LogMessage]"] = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """初始化并返回全局共享日志器（幂等调用，避免重复挂载 Handler）。"""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not logger.handlers:
        # 1. 终端输出 Handler
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        # 2. 按天切分的本地日志文件 Handler
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # 3. 如传入 GUI 队列，替换旧的 QueueLogHandler 以避免重复推送
    if log_queue is not None:
        logger.handlers = [
            h for h in logger.handlers if not isinstance(h, QueueLogHandler)
        ]
        queue_handler = QueueLogHandler(log_queue)
        queue_handler.setFormatter(formatter)
        logger.addHandler(queue_handler)

    return logger
