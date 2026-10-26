#!/usr/bin/env python3
"""glean :: scan_sessions 自测（标准库 unittest，无第三方依赖）。

  python test_scan_sessions.py -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scan_sessions as s


def write_temp(name, content):
    handle = tempfile.NamedTemporaryFile("w", suffix=name, delete=False, encoding="utf-8")
    handle.write(content)
    handle.close()
    return handle.name


class TestInjectedStripping(unittest.TestCase):
    def test_user_query_preferred(self):
        raw = "<system-reminder>注入</system-reminder><user_query>帮我改 a.py</user_query>"
        self.assertEqual(s.strip_injected(raw), "帮我改 a.py")

    def test_reminder_removed_without_query_tag(self):
        raw = '<system-reminder data-role="x">大量注入</system-reminder>审查这个报告'
        self.assertEqual(s.strip_injected(raw), "审查这个报告")

    def test_ide_opened_file_removed(self):
        raw = "<ide_opened_file>文件在 IDE 中打开</ide_opened_file>看看这个报错"
        self.assertEqual(s.strip_injected(raw), "看看这个报错")

    def test_plain_text_untouched(self):
        raw = "用 <div> 包一层就行"
        self.assertEqual(s.strip_injected(raw), raw)


class TestGenuineUser(unittest.TestCase):
    def test_meta_notification_rejected(self):
        self.assertFalse(s.is_workbuddy_user({"providerData": {"isMeta": True}}, "<task-notification>"))

    def test_background_command_rejected_by_text(self):
        self.assertFalse(s.is_workbuddy_user({}, "<task-notification>\n<status>completed</status>"))

    def test_compaction_summary_rejected(self):
        self.assertFalse(s.is_workbuddy_user({}, "Summary of the conversation between an AI agent and a user."))

    def test_real_request_accepted(self):
        self.assertTrue(s.is_workbuddy_user({"providerData": {"startsNewUserRequest": True}}, "完成这个作业"))


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

    def test_mixed_turn_detection(self):
        self.assertTrue(s.looks_mixed("问题\n# 标题\n**a**\n**b**\n" + "填充" * 450))
        self.assertFalse(s.looks_mixed("帮我把 a.py 改成异步的，顺便加日志"))


class TestFormatting(unittest.TestCase):
    def test_millisecond_epoch_to_iso(self):
        self.assertTrue(s.to_iso(1792939466315).startswith("2026-"))

    def test_iso_string_passthrough(self):
        self.assertTrue(s.to_iso("2026-06-18T03:58:27.000Z").startswith("2026-06-18T03:58:27"))

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


class TestAdapters(unittest.TestCase):
    def test_detect_workbuddy(self):
        path = write_temp(".jsonl", json.dumps({"type": "message", "providerData": {}, "role": "user"}) + "\n")
        self.addCleanup(os.unlink, path)
        self.assertEqual(s.detect_source(path), "workbuddy")

    def test_detect_claude_code(self):
        path = write_temp(".jsonl", json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        self.addCleanup(os.unlink, path)
        self.assertEqual(s.detect_source(path), "claude-code")

    def test_detect_text_by_extension(self):
        path = write_temp(".md", "## User\n帮我改脚本")
        self.addCleanup(os.unlink, path)
        self.assertEqual(s.detect_source(path), "text")

    def test_parse_claude_code_filters_tool_and_injection(self):
        rows = [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "<ide_opened_file>IDE 注入</ide_opened_file>"},
                {"type": "text", "text": "帮我把脚本改成异步"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}, "id": "t1"},
                {"type": "text", "text": "已修改"}]}},
            {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "子代理消息"}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": False}]}},
        ]
        path = write_temp(".jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
        self.addCleanup(os.unlink, path)
        session = s.parse_claude_code(path)
        self.assertEqual([r for r, _ in session.messages], ["user", "assistant"])
        self.assertEqual(session.messages[0][1], "帮我把脚本改成异步")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], "Edit")
        self.assertEqual(session.filtered, 2)

    def test_parse_text_handles_markdown_markers(self):
        path = write_temp(".md", "## User\n帮我把这个脚本改一下\n\n## Assistant\n文件在哪\n\n**User:** a.py\n")
        self.addCleanup(os.unlink, path)
        session = s.parse_text(path)
        self.assertEqual(session.messages[0], ("user", "帮我把这个脚本改一下"))
        self.assertEqual(session.messages[1], ("assistant", "文件在哪"))
        self.assertEqual(session.messages[2][0], "user")
        self.assertTrue(session.messages[2][1].startswith("a.py"))


if __name__ == "__main__":
    unittest.main()
