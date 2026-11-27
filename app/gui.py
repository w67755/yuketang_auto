"""雨课堂自动学习助手 CustomTkinter 桌面可视化界面。

设计特点：
- 界面逻辑与业务调度彻底解耦：GUI 仅负责收集配置、启停后台工作线程、展示队列日志；
- 提供「打开浏览器」按钮，方便用户先完成微信扫码或手动进入课程，再点击「开始刷课」一键接管；
- 提供「页面结构诊断」按钮，随时导出当前页面截图与 DOM 摘要，便于校准选择器。
"""

from __future__ import annotations

import queue
import threading
from typing import Optional

import customtkinter as ctk

from app.config import (
    ALLOWED_BROWSERS,
    ALLOWED_VIDEO_SPEEDS,
    DEEPSEEK_MODEL_PRESETS,
    SERVER_PRESETS,
    AppConfig,
    load_config,
    save_config,
)
from app.logger import LogMessage, setup_logger
from core.auth import ensure_logged_in
from core.browser import BrowserManager
from core.diagnostics import capture_diagnostics
from core.scheduler import CourseScheduler


class YuketangApp(ctk.CTk):
    """雨课堂自动学习助手主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title("雨课堂自动学习助手 · DeepSeek 版")
        self.geometry("1120x720")
        self.minsize(980, 640)

        # 线程安全日志队列与工作线程控制状态
        self.log_queue: "queue.Queue[LogMessage]" = queue.Queue()
        self.logger = setup_logger(log_queue=self.log_queue)
        self.config_data: AppConfig = load_config()

        self.browser: Optional[BrowserManager] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        self._build_layout()
        self._load_config_into_form(self.config_data)
        self.after(150, self._poll_log_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # 界面布局构建
    # ------------------------------------------------------------------
    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=430)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 左侧：可滚动配置区
        left_panel = ctk.CTkScrollableFrame(self, width=420, corner_radius=10)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(14, 8), pady=14)
        left_panel.grid_columnconfigure(1, weight=1)

        row = 0
        row = self._add_section_title(left_panel, row, "1. 平台与浏览器设置")

        self.server_preset_var = ctk.StringVar(value="雨课堂（标准版）")
        ctk.CTkLabel(left_panel, text="服务器版本", anchor="w").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkOptionMenu(
            left_panel,
            variable=self.server_preset_var,
            values=list(SERVER_PRESETS.keys()),
            command=self._on_server_preset_change,
        ).grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        row += 1

        self.base_url_var = ctk.StringVar()
        row = self._add_labeled_entry(left_panel, row, "雨课堂网址", self.base_url_var, placeholder="https://www.yuketang.cn/web")

        self.browser_var = ctk.StringVar(value="edge")
        ctk.CTkLabel(left_panel, text="浏览器类型", anchor="w").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkOptionMenu(left_panel, variable=self.browser_var, values=list(ALLOWED_BROWSERS)).grid(
            row=row, column=1, sticky="ew", padx=8, pady=6
        )
        row += 1

        self.courses_var = ctk.StringVar()
        row = self._add_labeled_entry(
            left_panel,
            row,
            "课程名（可选）",
            self.courses_var,
            placeholder="多个课程用中文或英文逗号隔开；留空可手动进课接管",
        )

        self.takeover_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            left_panel,
            text="若浏览器已在课程/任务页，直接接管当前页面",
            variable=self.takeover_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 10))
        row += 1

        # 第二区：功能模块开关
        row = self._add_section_title(left_panel, row, "2. 任务模式开关（按顺序自动调度）")
        switch_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        switch_frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=4)

        self.enable_video_var = ctk.BooleanVar(value=True)
        self.enable_ppt_var = ctk.BooleanVar(value=True)
        self.enable_quiz_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(switch_frame, text="自动看视频", variable=self.enable_video_var).pack(side="left", padx=(0, 14))
        ctk.CTkCheckBox(switch_frame, text="自动看课件", variable=self.enable_ppt_var).pack(side="left", padx=(0, 14))
        ctk.CTkCheckBox(switch_frame, text="AI 自动做题", variable=self.enable_quiz_var).pack(side="left")
        row += 1

        # 第三区：视频与课件策略
        row = self._add_section_title(left_panel, row, "3. 视频与课件停留参数")

        self.video_speed_var = ctk.StringVar(value="2.0")
        ctk.CTkLabel(left_panel, text="原生播放倍速", anchor="w").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkOptionMenu(left_panel, variable=self.video_speed_var, values=list(ALLOWED_VIDEO_SPEEDS)).grid(
            row=row, column=1, sticky="ew", padx=8, pady=6
        )
        row += 1

        self.video_mute_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(left_panel, text="视频静音播放", variable=self.video_mute_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4
        )
        row += 1

        # 课件每页停留随机区间
        dwell_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        dwell_frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(dwell_frame, text="课件单页停留(秒)").pack(side="left", padx=(0, 8))
        self.ppt_min_var = ctk.StringVar(value="5.0")
        self.ppt_max_var = ctk.StringVar(value="8.0")
        ctk.CTkEntry(dwell_frame, textvariable=self.ppt_min_var, width=64).pack(side="left")
        ctk.CTkLabel(dwell_frame, text=" ~ ").pack(side="left")
        ctk.CTkEntry(dwell_frame, textvariable=self.ppt_max_var, width=64).pack(side="left")
        row += 1

        self.ppt_retry_var = ctk.StringVar(value="2")
        row = self._add_labeled_entry(left_panel, row, "未看页补看轮数", self.ppt_retry_var, placeholder="默认 2 轮")

        # 第四区：DeepSeek API 设置
        row = self._add_section_title(left_panel, row, "4. DeepSeek API 做题设置")

        self.api_key_var = ctk.StringVar()
        ctk.CTkLabel(left_panel, text="DeepSeek Key", anchor="w").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        key_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        key_frame.grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        key_frame.grid_columnconfigure(0, weight=1)
        self.api_key_entry = ctk.CTkEntry(key_frame, textvariable=self.api_key_var, show="*", placeholder_text="sk-...")
        self.api_key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            key_frame,
            text="显示",
            width=48,
            variable=self.show_key_var,
            command=self._toggle_api_key_visibility,
        ).grid(row=0, column=1, padx=(6, 0))
        row += 1

        self.model_var = ctk.StringVar(value="deepseek-v4.1-flash")
        ctk.CTkLabel(left_panel, text="DeepSeek 模型", anchor="w").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkComboBox(
            left_panel,
            variable=self.model_var,
            values=list(DEEPSEEK_MODEL_PRESETS),
        ).grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        row += 1

        self.auto_submit_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(left_panel, text="全部题型答完直接自动提交", variable=self.auto_submit_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4
        )
        row += 1

        self.diag_on_err_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(left_panel, text="任务异常时自动保存截图与页面 DOM 诊断", variable=self.diag_on_err_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 10)
        )

        # 右侧：状态控制条 + 实时日志控制台
        right_panel = ctk.CTkFrame(self, corner_radius=10)
        right_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 14), pady=14)
        right_panel.grid_rowconfigure(2, weight=1)
        right_panel.grid_columnconfigure(0, weight=1)

        # 顶部状态栏
        header = ctk.CTkFrame(right_panel, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        ctk.CTkLabel(header, text="运行控制台", font=ctk.CTkFont(size=18, weight="bold")).pack(side="left")
        self.status_label = ctk.CTkLabel(header, text="状态：待机中", text_color="#3B8ED0")
        self.status_label.pack(side="right")

        # 操作按钮区（紧凑排布，确保在默认窗口宽度下全部按钮完整显示）
        btn_bar = ctk.CTkFrame(right_panel, fg_color="transparent")
        btn_bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 8))

        self.open_browser_btn = ctk.CTkButton(btn_bar, text="打开浏览器扫码", width=116, command=self._on_open_browser)
        self.open_browser_btn.pack(side="left", padx=(0, 6))

        self.start_btn = ctk.CTkButton(
            btn_bar, text="开始刷课", width=90, fg_color="#2FA572", hover_color="#228B5B", command=self._on_start
        )
        self.start_btn.pack(side="left", padx=(0, 6))

        self.stop_btn = ctk.CTkButton(
            btn_bar,
            text="停止任务",
            width=86,
            fg_color="#D9534F",
            hover_color="#B52B27",
            state="disabled",
            command=self._on_stop,
        )
        self.stop_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(btn_bar, text="保存配置", width=80, command=self._on_save_config).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_bar, text="页面诊断", width=84, command=self._on_manual_diagnostics).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btn_bar, text="清空日志", width=76, fg_color="gray45", command=self._clear_logs).pack(side="right")

        # 实时日志文本框：采用深色控制台底色 + 高对比度字体，避免浅色模式下看不清
        self.log_box = ctk.CTkTextbox(
            right_panel,
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
            fg_color="#181C24",
            text_color="#E6EDF3",
            border_width=1,
            border_color="#30363D",
            wrap="word",
        )
        self.log_box.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.log_box.configure(state="disabled")
        # 配置不同日志等级的高对比度颜色标签（使用底层 tk.Text 的 tag_config）
        self.log_box._textbox.tag_config("INFO", foreground="#F0F6FC")
        self.log_box._textbox.tag_config("WARNING", foreground="#FFD166")
        self.log_box._textbox.tag_config("ERROR", foreground="#FF7B72")

        self.logger.info("欢迎使用雨课堂自动学习助手！建议首次使用先点击「打开浏览器扫码」，登录并进入课程后再点击「开始刷课」。")

    def _add_section_title(self, parent: ctk.CTkScrollableFrame, row: int, title: str) -> int:
        label = ctk.CTkLabel(parent, text=title, font=ctk.CTkFont(size=14, weight="bold"), anchor="w")
        label.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=(12, 4))
        return row + 1

    def _add_labeled_entry(
        self,
        parent: ctk.CTkScrollableFrame,
        row: int,
        label_text: str,
        var: ctk.StringVar,
        placeholder: str = "",
    ) -> int:
        ctk.CTkLabel(parent, text=label_text, anchor="w").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ctk.CTkEntry(parent, textvariable=var, placeholder_text=placeholder).grid(
            row=row, column=1, sticky="ew", padx=8, pady=6
        )
        return row + 1

    def _toggle_api_key_visibility(self) -> None:
        self.api_key_entry.configure(show="" if self.show_key_var.get() else "*")

    def _on_server_preset_change(self, selected: str) -> None:
        """切换服务器版本下拉框时自动填入对应官方网页版入口。"""
        preset_url = SERVER_PRESETS.get(selected, "")
        if preset_url:
            self.base_url_var.set(preset_url)

    # ------------------------------------------------------------------
    # 配置双向同步
    # ------------------------------------------------------------------
    def _load_config_into_form(self, cfg: AppConfig) -> None:
        matched_preset = "自定义网址"
        for name, url in SERVER_PRESETS.items():
            if url and cfg.base_url.rstrip("/") == url.rstrip("/"):
                matched_preset = name
                break
        self.server_preset_var.set(matched_preset)
        self.base_url_var.set(cfg.base_url)
        self.browser_var.set(cfg.browser)
        self.courses_var.set("，".join(cfg.course_names))
        self.takeover_var.set(cfg.takeover_current_page)
        self.enable_video_var.set(cfg.enable_video)
        self.enable_ppt_var.set(cfg.enable_ppt)
        self.enable_quiz_var.set(cfg.enable_quiz)
        self.video_speed_var.set(cfg.video_speed)
        self.video_mute_var.set(cfg.video_mute)
        self.ppt_min_var.set(str(cfg.ppt_dwell_min))
        self.ppt_max_var.set(str(cfg.ppt_dwell_max))
        self.ppt_retry_var.set(str(cfg.ppt_retry_rounds))
        self.api_key_var.set(cfg.deepseek_api_key)
        self.model_var.set(cfg.deepseek_model)
        self.auto_submit_var.set(cfg.auto_submit_quiz)
        self.diag_on_err_var.set(cfg.save_diagnostics_on_error)

    def _collect_config_from_form(self) -> AppConfig:
        raw_courses = self.courses_var.get().replace("，", ",").replace("\n", ",")
        course_list = [item.strip() for item in raw_courses.split(",") if item.strip()]

        def _parse_float(raw: str, default: float) -> float:
            try:
                return float(raw.strip())
            except ValueError:
                return default

        def _parse_int(raw: str, default: int) -> int:
            try:
                return int(raw.strip())
            except ValueError:
                return default

        cfg = AppConfig(
            base_url=self.base_url_var.get(),
            browser=self.browser_var.get(),
            driver_path=self.config_data.driver_path,
            profile_dir=self.config_data.profile_dir,
            course_names=course_list,
            takeover_current_page=self.takeover_var.get(),
            enable_video=self.enable_video_var.get(),
            enable_ppt=self.enable_ppt_var.get(),
            enable_quiz=self.enable_quiz_var.get(),
            video_speed=self.video_speed_var.get(),
            video_mute=self.video_mute_var.get(),
            ppt_dwell_min=_parse_float(self.ppt_min_var.get(), 5.0),
            ppt_dwell_max=_parse_float(self.ppt_max_var.get(), 8.0),
            ppt_retry_rounds=_parse_int(self.ppt_retry_var.get(), 2),
            ppt_retry_extra_seconds=self.config_data.ppt_retry_extra_seconds,
            deepseek_api_key=self.api_key_var.get(),
            deepseek_model=self.model_var.get(),
            deepseek_base_url=self.config_data.deepseek_base_url,
            auto_submit_quiz=self.auto_submit_var.get(),
            save_diagnostics_on_error=self.diag_on_err_var.get(),
        ).normalized()
        self.config_data = cfg
        return cfg

    # ------------------------------------------------------------------
    # 按钮事件处理
    # ------------------------------------------------------------------
    def _on_save_config(self) -> None:
        cfg = self._collect_config_from_form()
        save_config(cfg)
        self._load_config_into_form(cfg)
        self.logger.info("配置已保存至 config.json。")

    def _on_open_browser(self) -> None:
        cfg = self._collect_config_from_form()
        save_config(cfg)

        def _launch() -> None:
            try:
                if self.browser is None:
                    self.browser = BrowserManager(cfg, self.logger)
                else:
                    self.browser.config = cfg
                ensure_logged_in(self.browser, cfg, threading.Event(), self.logger, wait_timeout=300)
            except Exception as exc:
                self.logger.error("打开浏览器失败：%s", exc)

        threading.Thread(target=_launch, daemon=True).start()

    def _on_start(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.logger.warning("任务已在运行中，请勿重复启动。")
            return

        cfg = self._collect_config_from_form()
        save_config(cfg)
        if not (cfg.enable_video or cfg.enable_ppt or cfg.enable_quiz):
            self.logger.warning("请至少勾选一项任务开关（看视频 / 看课件 / AI 做题）。")
            return

        self.stop_event.clear()
        self._set_running_ui(True)

        def _worker() -> None:
            try:
                if self.browser is None:
                    self.browser = BrowserManager(cfg, self.logger)
                else:
                    self.browser.config = cfg

                scheduler = CourseScheduler(cfg, self.browser, self.logger, self.stop_event)
                summary = scheduler.run()
                self.logger.info(
                    "本轮任务结束！汇总：完成 %d 项 | 跳过 %d 项 | 失败 %d 项。",
                    summary.completed,
                    summary.skipped,
                    summary.failed,
                )
            except Exception as exc:
                self.logger.exception("运行过程中出现未捕获异常：%s", exc)
            finally:
                self.after(0, lambda: self._set_running_ui(False))

        self.worker_thread = threading.Thread(target=_worker, daemon=True)
        self.worker_thread.start()

    def _on_stop(self) -> None:
        self.stop_event.set()
        self.status_label.configure(text="状态：正在停止...", text_color="#F0AD4E")
        self.logger.warning("已发送停止信号，等待当前步骤安全退出...")

    def _on_manual_diagnostics(self) -> None:
        if self.browser is None or not self.browser.is_alive():
            self.logger.warning("当前没有正在运行的受控浏览器，请先点击「打开浏览器扫码」。")
            return

        def _dump() -> None:
            out_dir = capture_diagnostics(self.browser.driver, "manual_check", "用户手动触发诊断", self.logger)
            if out_dir:
                self.logger.info("诊断文件已导出，可用于校准 core/selectors.py：%s", out_dir)

        threading.Thread(target=_dump, daemon=True).start()

    def _clear_logs(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _set_running_ui(self, running: bool) -> None:
        if running:
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.status_label.configure(text="状态：运行中", text_color="#2FA572")
        else:
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.status_label.configure(text="状态：待机中", text_color="#3B8ED0")

    def _poll_log_queue(self) -> None:
        """定时在主线程拉取后台日志并渲染到滚动文本框。"""
        try:
            while True:
                level, msg = self.log_queue.get_nowait()
                tag = level if level in ("INFO", "WARNING", "ERROR") else "INFO"
                self.log_box.configure(state="normal")
                self.log_box.insert("end", msg + "\n", tag)
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        finally:
            self.after(150, self._poll_log_queue)

    def _on_close(self) -> None:
        self.stop_event.set()
        self.destroy()


def run_gui() -> None:
    """启动桌面应用主循环。"""
    app = YuketangApp()
    app.mainloop()
