#!/usr/bin/env python3
"""glean :: scan_sessions 自测（标准库 unittest，无第三方依赖）。

  python test_scan_sessions.py -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scan_sessions as s


class TestInjectedStripping(unittest.TestCase):
    def test_user_query_preferred(self):
        raw = "<system-reminder><user_info>OS: win32</user_info></system-reminder><user_query>帮我改 a.py</user_query>"
        self.assertEqual(s.strip_injected(raw), "帮我改 a.py")

    def test_reminder_removed_without_query_tag(self):
        raw = "<system-reminder data-role=\"x\">大量注入内容</system-reminder>审查一下这个报告"
        self.assertEqual(s.strip_injected(raw), "审查一下这个报告")


class TestGenuineUser(unittest.TestCase):
    def test_meta_notification_rejected(self):
        rec = {"providerData": {"isMeta": True}}
        self.assertFalse(s.is_genuine_user(rec, "<task-notification>...</task-notification>"))

    def test_background_command_rejected_by_text(self):
        self.assertFalse(s.is_genuine_user({}, "<task-notification>\n<status>completed</status>"))

    def test_compaction_summary_rejected(self):
        self.assertFalse(s.is_genuine_user({}, "Summary of the conversation between an AI agent and a user."))

    def test_real_request_accepted(self):
        rec = {"providerData": {"startsNewUserRequest": True}}
        self.assertTrue(s.is_genuine_user(rec, "完成这个作业"))


class TestSignals(unittest.TestCase):
    def test_restatement_detected(self):
        a = "按照要求完成实验4，实验报告模板：模板.docx，实验网址：http://x/"
        b = "按照要求完成实验4，实验报告模板：模板.docx"
        self.assertGreaterEqual(s.similarity(a, b), 0.6)

    def test_different_intent_below_threshold(self):
        a = "帮我审查一下这份实验报告是否按要求完成"
        b = "今天天气怎么样，适合出门吗"
        self.assertLess(s.similarity(a, b), 0.6)

    def test_question_tail(self):
        self.assertTrue(s.QUESTION_TAIL.search("你想让我继续吗？"))
        self.assertTrue(s.QUESTION_TAIL.search("要继续吗"))
        self.assertFalse(s.QUESTION_TAIL.search("已完成全部修改。"))

    def test_correction_exclusion(self):
        self.assertIsNotNone(s.CORRECTION_EXCLUDE.search("重新运行一下"))
        self.assertIsNotNone(s.CORRECTION_EXCLUDE.search("是不是这样"))
        self.assertIsNone(s.CORRECTION_EXCLUDE.search("步骤太长了，重新搞"))

    def test_strong_correction_vocabulary(self):
        text = "步骤太长了，不像是正常解答，重新搞"
        self.assertTrue(any(w in text for w in s.STRONG_CORRECTIONS))


class TestFormatting(unittest.TestCase):
    def test_millisecond_epoch_to_iso(self):
        self.assertTrue(s.to_iso(1792939466315).startswith("2026-"))

    def test_none_timestamp(self):
        self.assertIsNone(s.to_iso(None))

    def test_home_collapse_case_insensitive(self):
        home = os.path.expanduser("~")
        swapped = home[0].swapcase() + home[1:]
        self.assertEqual(s.home_collapse(swapped + "\\Desktop"), "~\\Desktop")

    def test_secret_masking(self):
        self.assertNotIn("sk-abcdefgh12345", s.mask_secrets("key is sk-abcdefgh12345 ok"))

    def test_display_path_keeps_case(self):
        self.assertIn("Desktop", s.display_path("C:/Users/MECHREVO/Desktop/作业"))


if __name__ == "__main__":
    unittest.main()
