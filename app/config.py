"""应用配置模型与本地 JSON 持久化管理。

设计说明：
- 使用 dataclass 集中定义所有可配置字段，避免散落的字典键名硬编码。
- 实际运行配置保存在本地 `config.json`（已被 .gitignore 忽略，防止 API Key 泄露）；
  仓库中仅提交不含密钥的 `config.example.json`。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List

# 项目根目录与默认配置文件路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

# 雨课堂官方四大服务器预设（与手机端「选择服务器」一一对应）
SERVER_PRESETS = {
    "雨课堂（标准版）": "https://www.yuketang.cn/web",
    "荷塘·雨课堂": "https://pro.yuketang.cn/web",
    "长江·雨课堂": "https://changjiang.yuketang.cn/web",
    "黄河·雨课堂": "https://huanghe.yuketang.cn/web",
    "自定义网址": "",
}

# DeepSeek 常用模型预设（支持逗号分隔多模型自动切换与回退）
DEEPSEEK_MODEL_PRESETS = (
    "deepseek-v4-pro,deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-chat",
    "deepseek-reasoner",
)

# 雨课堂原生支持的合法视频倍速集合（避免超高倍速导致进度不计分）
ALLOWED_VIDEO_SPEEDS = ("1.0", "1.5", "2.0")
ALLOWED_BROWSERS = ("edge", "chrome")


@dataclass
class AppConfig:
    """雨课堂自动刷课助手的全局配置。"""

    # ---------- 平台与浏览器配置 ----------
    base_url: str = "https://www.yuketang.cn/web"
    browser: str = "edge"                  # 默认使用 Windows 内置 Edge，可选 "chrome"
    driver_path: str = ""                  # 留空时由 Selenium 4 Manager 自动匹配下载驱动
    profile_dir: str = "browser_profile"   # 持久化用户目录，保留微信扫码后的登录态

    # ---------- 选课与入口模式 ----------
    course_names: List[str] = field(default_factory=list)  # 按课程名称关键词自动进入
    takeover_current_page: bool = True                     # 若用户已手动点进课程页，直接接管当前页

    # ---------- 功能开关（顺序调度执行） ----------
    enable_video: bool = True
    enable_ppt: bool = True
    enable_quiz: bool = True

    # ---------- 看视频配置 ----------
    video_speed: str = "2.0"               # 仅允许雨课堂原生倍速：1.0 / 1.5 / 2.0
    video_mute: bool = True

    # ---------- 看课件 / PPT 配置 ----------
    ppt_dwell_min: float = 5.0             # 单页最小停留秒数
    ppt_dwell_max: float = 8.0             # 单页最大停留秒数
    ppt_retry_rounds: int = 2              # 检测到未看页时的最大补看轮数
    ppt_retry_extra_seconds: float = 3.0   # 补看轮次每轮额外增加的停留秒数

    # ---------- DeepSeek API 做题配置 ----------
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-pro,deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    auto_submit_quiz: bool = True          # 全部题型答完后直接自动提交

    # ---------- 诊断与容错 ----------
    save_diagnostics_on_error: bool = True

    def normalized(self) -> "AppConfig":
        """清洗并校验配置边界，确保非法输入不会导致运行期崩溃。"""
        self.base_url = (self.base_url or "https://www.yuketang.cn/web").strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"https://{self.base_url}"

        self.browser = self.browser.strip().lower()
        if self.browser not in ALLOWED_BROWSERS:
            self.browser = "edge"

        self.video_speed = str(self.video_speed).strip()
        if self.video_speed not in ALLOWED_VIDEO_SPEEDS:
            self.video_speed = "2.0"

        # 保障课件随机停留区间合理（至少 1 秒，且 min <= max）
        self.ppt_dwell_min = max(1.0, float(self.ppt_dwell_min))
        self.ppt_dwell_max = max(self.ppt_dwell_min, float(self.ppt_dwell_max))
        self.ppt_retry_rounds = max(0, min(5, int(self.ppt_retry_rounds)))
        self.ppt_retry_extra_seconds = max(0.0, float(self.ppt_retry_extra_seconds))

        # 清理课程名列表中的空项
        self.course_names = [name.strip() for name in self.course_names if name and name.strip()]

        self.deepseek_api_key = (self.deepseek_api_key or "").strip()
        self.deepseek_model = (self.deepseek_model or "deepseek-v4-pro,deepseek-v4-flash").strip()
        self.deepseek_base_url = (self.deepseek_base_url or "https://api.deepseek.com").strip().rstrip("/")
        return self

    def resolved_profile_dir(self) -> Path:
        """返回浏览器用户数据目录的绝对路径。"""
        path = Path(self.profile_dir)
        return path if path.is_absolute() else (PROJECT_ROOT / path)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """从 JSON 文件加载配置；文件不存在或部分字段缺失时自动补全默认值。"""
    if not path.exists():
        cfg = AppConfig().normalized()
        save_config(cfg, path)
        return cfg

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig().normalized()

    valid_fields = set(AppConfig.__dataclass_fields__.keys())
    filtered = {k: v for k, v in raw.items() if k in valid_fields}
    return AppConfig(**filtered).normalized()


def save_config(config: AppConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """将当前配置持久化写入 JSON 文件。"""
    config.normalized()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
