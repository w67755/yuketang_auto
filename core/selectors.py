"""长江雨课堂前端页面选择器与语义特征集中配置。

设计原则（避免硬编码散落各处）：
- 长江雨课堂包含多个子前端（如 /v2/web/ 班级学习日志与 /pro/lms/ 章节 MOOC 页面），
  DOM 类名偶尔会随版本迭代微调。
- 所有 CSS/XPath 选择器与文本关键词统一收口在本文件，按“最精确 -> 通用语义回退”排序。
- 若实际课程页面有差异，点击 GUI 中的「页面结构诊断」导出 dom_summary.json 后，
  仅需调整本文件即可完成适配，无需改动业务流程代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class SelectorRegistry:
    """长江雨课堂页面元素选择器与状态关键词注册表。"""

    # ---------- URL 路径特征（用于自动识别当前所处页面类型） ----------
    login_url_keywords: Tuple[str, ...] = (
        "/web/login",
        "/v2/web/login",
        "login",
        "oauth",
    )
    course_page_url_keywords: Tuple[str, ...] = (
        "/studentlog/",
        "/studentcourse/",
        "/studentcards/",
        "/pro/lms/",
        "/lms#",
        "/studycontent",
        "/courselist",
    )
    video_url_keywords: Tuple[str, ...] = (
        "/video/",
        "type=video",
        "leaf_type=0",
        "mooc-video",
    )
    ppt_url_keywords: Tuple[str, ...] = (
        "/ppt/",
        "/presentation/",
        "/slide/",
        "/cards/",
        "/studentcards/",
        "/cloud/student/download/",
        "/courseware/",
        "type=ppt",
        "leaf_type=1",
        "leaf_type=3",
    )
    quiz_url_keywords: Tuple[str, ...] = (
        "/homework/",
        "/exercise/",
        "/quiz/",
        "/exam/",
        "/problem/",
        "leaf_type=6",
    )

    # ---------- 登录状态与课程列表选择器 ----------
    login_qr_selectors: Tuple[str, ...] = (
        "img[src*='qrcode']",
        "canvas[class*='qr']",
        ".login-box",
        ".wechat-login",
        "[class*='qr-code']",
    )
    my_courses_tab_texts: Tuple[str, ...] = (
        "我听的课",
        "我的课程",
        "课程",
    )
    course_card_selectors: Tuple[str, ...] = (
        ".lesson-card",
        ".course-card",
        ".card-item",
        "[class*='course-item']",
        "[class*='classroom-item']",
        ".el-card",
    )

    # ---------- 课程目录 / 学习日志任务项选择器 ----------
    # 点击展开折叠章节的触发元素
    chapter_expand_selectors: Tuple[str, ...] = (
        ".el-collapse-item__header:not(.is-active)",
        "[class*='chapter-header']",
        "[class*='section-header']",
        ".chapter-title",
    )
    # 单个学习任务条目（兼容 v2/web 学习日志卡片 与 pro/lms 章节叶子节点）
    task_item_selectors: Tuple[str, ...] = (
        ".study-unit",
        ".leaf-detail",
        ".log-item",
        ".activity-item",
        ".chapter-list .leaf",
        "[class*='leaf-item']",
        "[class*='unit-item']",
        "[class*='content-item']",
        "li[class*='item']",
    )
    # 表明任务已完成的文本关键词
    completed_keywords: Tuple[str, ...] = (
        "已完成",
        "已读",
        "已看完",
        "100%",
        "已提交",
        "已作答",
    )
    # 用于基于任务标题/角标判断任务类型的关键词
    video_type_keywords: Tuple[str, ...] = (
        "视频",
        "录播",
        "回放",
        "微课",
        "video",
    )
    ppt_type_keywords: Tuple[str, ...] = (
        "课件",
        "PPT",
        "ppt",
        "幻灯片",
        "预习",
        "图文",
        "讲义",
        "文档",
    )
    quiz_type_keywords: Tuple[str, ...] = (
        "习题",
        "作业",
        "测验",
        "测试",
        "随堂",
        "考试",
        "练习",
        "试卷",
    )

    # ---------- 视频播放器选择器 ----------
    video_element_selectors: Tuple[str, ...] = (
        "video",
        ".xt_video_player video",
        "#video-box video",
    )
    video_play_button_selectors: Tuple[str, ...] = (
        "xt-playbutton",
        ".xt_video_player_play_btn",
        ".xt_video_player_big_play_layer",
        "[class*='play-btn']",
        "button[aria-label*='播放']",
    )
    video_mute_button_selectors: Tuple[str, ...] = (
        "xt-volumebutton",
        ".xt_video_player_volume_btn",
        "[class*='volume']",
    )
    video_speed_button_selectors: Tuple[str, ...] = (
        "xt-speedbutton",
        ".xt_video_player_speed",
        "[class*='speed-btn']",
        "[class*='playback-rate']",
    )
    video_speed_option_selectors: Tuple[str, ...] = (
        "xt-speedlist li",
        ".xt_video_player_common_list li",
        "[class*='speed'] li",
        "[class*='rate'] li",
    )
    video_progress_text_selectors: Tuple[str, ...] = (
        ".title-fl .text",
        ".progress-wrap",
        "[class*='complete']",
        "[class*='progress']",
    )

    # ---------- 课件 / PPT 观看选择器 ----------
    # 左侧或底部的每一页缩略图/页码节点
    ppt_slide_item_selectors: Tuple[str, ...] = (
        ".ppt-thumb-item",
        ".slide-item",
        ".thumb-item",
        ".preview-item",
        ".page-item",
        "[class*='slide-list'] > *",
        "[class*='thumb-list'] > *",
        "[class*='ppt-list'] > *",
        ".swiper-slide",
    )
    ppt_next_button_selectors: Tuple[str, ...] = (
        "[class*='next-page']",
        "[class*='next-btn']",
        ".btn-next",
        ".swiper-button-next",
        "button[aria-label*='下一']",
        "[title*='下一页']",
    )
    ppt_prev_button_selectors: Tuple[str, ...] = (
        "[class*='prev-page']",
        "[class*='prev-btn']",
        ".btn-prev",
        ".swiper-button-prev",
        "button[aria-label*='上一']",
        "[title*='上一页']",
    )
    # 类似 “70 / 70” 或 “12/68” 的页码计数器
    ppt_counter_selectors: Tuple[str, ...] = (
        "[class*='page-num']",
        "[class*='page-count']",
        "[class*='slide-num']",
        "[class*='indicator']",
        ".pagination",
    )
    # 结束页“您还有以下页面未看”后的未看页码按钮及“继续观看”按钮
    ppt_unviewed_badge_selectors: Tuple[str, ...] = (
        "[class*='unviewed'] button",
        "[class*='unviewed'] span",
        "[class*='unread'] button",
        "[class*='unread'] span",
        "[class*='miss'] span",
        "[class*='not-read'] span",
        "[class*='page-tag']",
    )
    ppt_continue_button_texts: Tuple[str, ...] = (
        "继续观看",
        "去观看",
        "重新观看",
        "继续学习",
    )

    # ---------- 习题 / 测试页面选择器 ----------
    quiz_question_container_selectors: Tuple[str, ...] = (
        ".subject-item",
        ".question-item",
        ".problem-item",
        "[class*='question-box']",
        "[class*='problem-box']",
        "[class*='exam-item']",
    )
    quiz_stem_selectors: Tuple[str, ...] = (
        ".item-body",
        ".question-title",
        ".problem-body",
        "[class*='stem']",
        "[class*='title']",
        "h4",
        "h3",
    )
    quiz_option_selectors: Tuple[str, ...] = (
        ".option-item",
        ".el-radio",
        ".el-checkbox",
        "label[class*='option']",
        "li[class*='option']",
        "[class*='choice-item']",
    )
    quiz_input_selectors: Tuple[str, ...] = (
        "input[type='text']",
        "textarea",
        "[contenteditable='true']",
    )
    quiz_submit_button_texts: Tuple[str, ...] = (
        "提交",
        "提交答案",
        "提交作业",
        "交卷",
        "确认提交",
    )
    quiz_confirm_button_texts: Tuple[str, ...] = (
        "确定",
        "确认",
        "确认提交",
        "继续提交",
    )


# 默认全局选择器实例，可在启动前按需覆盖
SELECTORS = SelectorRegistry()
