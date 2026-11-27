"""异常现场留存与页面 DOM 诊断导出模块。

作用：
- 当脚本遇到未识别页面结构、补看未通过、视频卡死或 API 异常时，自动保存：
  1. `screenshot.png`：当前浏览器可见区域截图；
  2. `dom_summary.json`：页面 URL、标题、iframe、video 状态、按钮与疑似课件/任务元素摘要。
- 同时支持在 GUI 上点击「页面结构诊断」手动导出，便于快速校准 `core/selectors.py`。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from selenium.webdriver.remote.webdriver import WebDriver

from app.config import PROJECT_ROOT

DIAGNOSTICS_ROOT = PROJECT_ROOT / "logs" / "diagnostics"

# 在目标页面中采集精简结构摘要的 JS（不保存敏感 Cookie，仅输出 DOM 结构与可见文本）
DOM_SUMMARY_JS = r"""
return (() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const videos = Array.from(document.querySelectorAll('video')).map((v, idx) => ({
    index: idx,
    src: v.currentSrc || v.src || '',
    paused: v.paused,
    ended: v.ended,
    muted: v.muted,
    playbackRate: v.playbackRate,
    currentTime: Number((v.currentTime || 0).toFixed(2)),
    duration: Number((v.duration || 0).toFixed(2)),
  }));

  const iframes = Array.from(document.querySelectorAll('iframe')).map((f, idx) => ({
    index: idx,
    id: f.id || '',
    className: f.className || '',
    src: (f.src || '').slice(0, 200),
  }));

  const interactive = Array.from(
    document.querySelectorAll('button, a, [role="button"], [role="tab"], li, section, article, [class*="card"], [class*="activity"], [class*="log"], [class*="unit"], [class*="item"], [class*="page"], [class*="slide"]')
  )
    .filter(visible)
    .map((el) => ({
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      className: typeof el.className === 'string' ? el.className.slice(0, 140) : '',
      text: clean(el.innerText || el.textContent),
    }))
    .filter((item) => item.text.length > 0)
    .slice(0, 120);

  const bodyText = clean(document.body ? document.body.innerText : '').slice(0, 1000);

  return {
    url: location.href,
    title: document.title,
    videos,
    iframes,
    interactive_sample_count: interactive.length,
    interactive_samples: interactive.slice(0, 80),
    body_text_preview: bodyText,
  };
})();
"""


def _sanitize_tag(tag: str) -> str:
    """将诊断标签清洗为安全的 Windows 文件夹名。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]+", "_", tag).strip("_")
    return cleaned[:40] or "snapshot"


def capture_diagnostics(
    driver: Optional[WebDriver],
    tag: str,
    reason: str = "",
    logger: Optional[logging.Logger] = None,
) -> Optional[Path]:
    """导出当前页面的截图与 DOM 结构摘要，返回诊断目录路径。"""
    if driver is None:
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = DIAGNOSTICS_ROOT / f"{timestamp}_{_sanitize_tag(tag)}"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "tag": tag,
        "reason": reason,
    }

    # 1. 保存当前页面截图
    screenshot_path = out_dir / "screenshot.png"
    try:
        driver.save_screenshot(str(screenshot_path))
        summary["screenshot"] = screenshot_path.name
    except Exception as exc:
        summary["screenshot_error"] = str(exc)

    # 2. 采集页面结构摘要（包含 video / iframe / 可点击节点样例）
    try:
        driver.switch_to.default_content()
        dom_info = driver.execute_script(DOM_SUMMARY_JS)
        summary["dom"] = dom_info
    except Exception as exc:
        summary["dom_error"] = str(exc)

    summary_path = out_dir / "dom_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if logger is not None:
        logger.warning("已保存页面诊断快照：%s", out_dir)
    return out_dir
