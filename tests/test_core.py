"""核心纯逻辑单元测试（无需联网与真实浏览器，验证关键解析与配置行为）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import AppConfig, load_config, save_config
from core.scheduler import (
    classify_task_text,
    detect_page_task_kind,
    is_collapsible_bundle_text,
    is_progress_fraction_completed,
)
from tasks.auto_quiz import detect_question_type, match_option_indices
from tasks.base import TaskKind
from tasks.deepseek_client import build_question_prompt, parse_answer_list
from tasks.watch_ppt import parse_page_counter, parse_study_progress, parse_unviewed_pages


class TestCoreLogic(unittest.TestCase):
    """覆盖配置校验、课件未看页码解析、DeepSeek 答案解析与题目匹配。"""

    def test_config_normalization_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg = AppConfig(
                base_url="www.yuketang.cn/web/",
                browser="invalid",
                video_speed="16.0",  # 非法高倍速应回退到安全的 2.0
                ppt_dwell_min=7.0,
                ppt_dwell_max=3.0,   # max < min 时应自动修正
                course_names=[" 思政课 ", ""],
            )
            save_config(cfg, cfg_path)
            loaded = load_config(cfg_path)

            self.assertEqual(loaded.base_url, "https://www.yuketang.cn/web")
            self.assertEqual(loaded.browser, "edge")
            self.assertEqual(loaded.video_speed, "2.0")
            self.assertEqual(loaded.ppt_dwell_min, 7.0)
            self.assertEqual(loaded.ppt_dwell_max, 7.0)
            self.assertEqual(loaded.course_names, ["思政课"])

    def test_parse_unviewed_pages_matches_rain_classroom_prompt(self) -> None:
        # 对应实际雨课堂结束页截图中的文案格式
        sample_text = "同学你没有好好观看喔\n您还有以下页面未看\n18 52 54 57 64 65\n继续观看"
        pages = parse_unviewed_pages(sample_text, badge_texts=["52", "18"])
        self.assertEqual(pages, [18, 52, 54, 57, 64, 65])

    def test_parse_page_counter(self) -> None:
        self.assertEqual(parse_page_counter("70 / 70"), (70, 70))
        self.assertEqual(parse_page_counter("第 12/65 页"), (12, 65))
        self.assertIsNone(parse_page_counter("已完成"))
        self.assertEqual(parse_study_progress("第六讲大国外交 | 学习进度：10/25"), (10, 25))
        self.assertEqual(parse_study_progress("学习进度: 75/75"), (75, 75))

    def test_deepseek_answer_parsing(self) -> None:
        self.assertEqual(parse_answer_list('["A", "C"]'), ["A", "C"])
        self.assertEqual(parse_answer_list("```json\n[\"文化自信\"]\n```"), ["文化自信"])
        self.assertEqual(parse_answer_list("答案如下：['B']"), ["B"])

        prompt = build_question_prompt("填空题", "我国全称是____。")
        self.assertIn("填空题", prompt)
        self.assertIn("几个空", prompt)

    def test_quiz_type_and_option_matching(self) -> None:
        self.assertEqual(detect_question_type("【多选题】以下属于...", 4, 0), "多选题")
        self.assertEqual(detect_question_type("判断对错", 2, 0), "判断题")

        options = ["A. 文化自信", "B. 道路自信", "C. 理论自信", "D. 制度自信"]
        self.assertEqual(match_option_indices(["A", "C"], options), [0, 2])
        self.assertEqual(match_option_indices(["BD"], options), [1, 3])
        self.assertEqual(match_option_indices(["文化自信"], options), [0])

        judge_opts = ["正确", "错误"]
        self.assertEqual(match_option_indices(["对"], judge_opts), [0])

    def test_task_kind_detection(self) -> None:
        self.assertEqual(classify_task_text("第四讲文化自信 课件 PPT 未完成"), TaskKind.PPT)
        self.assertEqual(classify_task_text("第五讲 双碳行动中的中国与世界 0/75 学习进度"), TaskKind.PPT)
        self.assertEqual(classify_task_text("1.2 课程视频 12:30"), TaskKind.VIDEO)
        self.assertEqual(classify_task_text("第一章随堂测验"), TaskKind.QUIZ)
        self.assertTrue(is_progress_fraction_completed("第四讲文化自信与文化强国 69/69 学习进度"))
        self.assertFalse(is_progress_fraction_completed("第五讲 双碳行动中的中国与世界 0/75 学习进度"))
        self.assertTrue(is_collapsible_bundle_text("视频学习 等 包含3章，0小节，共计9个学习单元 展开 进行中"))
        self.assertEqual(
            detect_page_task_kind("https://changjiang.yuketang.cn/v2/web/studentLog/123", "您还有以下页面未看 18"),
            TaskKind.PPT,
        )


if __name__ == "__main__":
    unittest.main()
