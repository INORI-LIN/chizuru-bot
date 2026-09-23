"""S4-01 固定提示文案的离线测试：逐字副本、指纹、错误码映射与模块纯度。"""

import ast
import hashlib
import unittest
from pathlib import Path

from astrbot_plugin_chizuru import fixed_notice, llm
from astrbot_plugin_chizuru.fixed_notice import (
    ATTACHMENT_NOTICE_TEXT,
    BUSY_NOTICE_TEXT,
    EMPTY_REPLY_NOTICE_TEXT,
    FAILURE_NOTICE_TEXT,
    FIXED_NOTICE_VERSION,
    OVER_BUDGET_NOTICE_TEXT,
    UNAVAILABLE_NOTICE_TEXT,
    text_for,
)
from astrbot_plugin_chizuru.llm import Answer, AnswerKind, FollowUp, follow_up
from astrbot_plugin_chizuru.redact import ErrorCode

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "astrbot_plugin_chizuru"

# **批准来源的逐字副本**（2026-09-23 随批次 4a 定稿）：docs/03 附录 C.5。
# 改动任一侧都应让本测试失败；改文本必须同时递增 FIXED_NOTICE_VERSION 与指纹。
ATTACHMENT_BODY = "我目前只能读文字消息，还看不了图片、语音和文件。请把想说的内容用文字发给我。"
OVER_BUDGET_BODY = "这条消息太长了，我一次处理不了，请缩短一些再发一次。"
FAILURE_BODY = "这次没有回复成功，请稍后再试一次。"
EMPTY_REPLY_BODY = "这次没能生成回复，请再发一次。"
BUSY_BODY = "现在消息有点多，请稍后再发一次。"
UNAVAILABLE_BODY = "现在我暂时不可用，请稍后再试，或联系本群维护者。"

# 规范串指纹：按常量名升序，`名称=文本` 以换行连接。
CANONICAL = "\n".join(
    f"{name}={text}"
    for name, text in (
        ("ATTACHMENT_NOTICE_TEXT", ATTACHMENT_BODY),
        ("BUSY_NOTICE_TEXT", BUSY_BODY),
        ("EMPTY_REPLY_NOTICE_TEXT", EMPTY_REPLY_BODY),
        ("FAILURE_NOTICE_TEXT", FAILURE_BODY),
        ("OVER_BUDGET_NOTICE_TEXT", OVER_BUDGET_BODY),
        ("UNAVAILABLE_NOTICE_TEXT", UNAVAILABLE_BODY),
    )
)


def answer_for(code: ErrorCode) -> Answer:
    """按 `llm.Answer` 的形状约束构造一个回答（空回复只能用 `EMPTY` 分支）。"""
    if code is ErrorCode.EMPTY_REPLY:
        return Answer(kind=AnswerKind.EMPTY, code=code)
    return Answer(kind=AnswerKind.FAILED, code=code)


class TextTests(unittest.TestCase):
    def test_texts_are_verbatim_from_the_approved_copy(self):
        self.assertEqual(ATTACHMENT_NOTICE_TEXT, ATTACHMENT_BODY)
        self.assertEqual(OVER_BUDGET_NOTICE_TEXT, OVER_BUDGET_BODY)
        self.assertEqual(FAILURE_NOTICE_TEXT, FAILURE_BODY)
        self.assertEqual(EMPTY_REPLY_NOTICE_TEXT, EMPTY_REPLY_BODY)
        self.assertEqual(BUSY_NOTICE_TEXT, BUSY_BODY)
        self.assertEqual(UNAVAILABLE_NOTICE_TEXT, UNAVAILABLE_BODY)

    def test_version_and_fingerprint_are_pinned(self):
        self.assertEqual(FIXED_NOTICE_VERSION, "fixed-notice-1")
        self.assertEqual(
            hashlib.sha256(CANONICAL.encode("utf-8")).hexdigest(),
            "ca30978801c72731ef479b615f1ab2a85788a4337a08e3c63aa87756034af8e4",
        )

    def test_attachment_text_makes_no_parsing_promise(self):
        """能力提示不得暗示会读取附件内容（需求 §4.1：不解析附件）。"""
        self.assertIn("只能读文字消息", ATTACHMENT_NOTICE_TEXT)
        for forbidden in ("识别", "解析", "稍后再发图片"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, ATTACHMENT_NOTICE_TEXT)

    def test_notices_are_short_single_line_and_leak_nothing(self):
        texts = (
            ATTACHMENT_NOTICE_TEXT,
            OVER_BUDGET_NOTICE_TEXT,
            FAILURE_NOTICE_TEXT,
            EMPTY_REPLY_NOTICE_TEXT,
            BUSY_NOTICE_TEXT,
            UNAVAILABLE_NOTICE_TEXT,
        )
        forbidden = [code.value for code in ErrorCode]
        forbidden += ["http", "sk-", "token", "API", "traceback"]
        for text in texts:
            with self.subTest(text=text):
                self.assertLessEqual(len(text), 60)
                self.assertNotIn("\n", text)
                self.assertTrue(text.endswith("。"))
                for word in forbidden:
                    self.assertNotIn(word, text)


class MappingTests(unittest.TestCase):
    def test_every_error_code_matches_follow_up(self):
        """`text_for` 与 `llm.follow_up` 必须逐码一致（防两处判定漂移）。"""
        for code in ErrorCode:
            with self.subTest(code=code):
                wants_notice = follow_up(answer_for(code)) is FollowUp.FIXED_NOTICE
                self.assertEqual(
                    wants_notice,
                    text_for(code) is not None,
                    f"{code} 的两处判定不一致",
                )

    def test_notice_codes_map_to_the_expected_text(self):
        expected = {
            ErrorCode.REQUEST_INVALID: FAILURE_NOTICE_TEXT,
            ErrorCode.AUTH_FAILED: FAILURE_NOTICE_TEXT,
            ErrorCode.PROVIDER_UNAVAILABLE: FAILURE_NOTICE_TEXT,
            ErrorCode.RATE_LIMITED: FAILURE_NOTICE_TEXT,
            ErrorCode.EMPTY_REPLY: EMPTY_REPLY_NOTICE_TEXT,
            ErrorCode.QUEUE_FULL: BUSY_NOTICE_TEXT,
            ErrorCode.BALANCE_INSUFFICIENT: UNAVAILABLE_NOTICE_TEXT,
            ErrorCode.BUDGET_EXHAUSTED: UNAVAILABLE_NOTICE_TEXT,
        }
        self.assertEqual(set(fixed_notice.FIXED_NOTICE_TEXTS), set(expected))
        for code, text in expected.items():
            with self.subTest(code=code):
                self.assertEqual(text_for(code), text)

    def test_silent_codes_have_no_text(self):
        for code in (
            ErrorCode.REVISION_CHANGED,
            ErrorCode.DELETE_FAILED,
            ErrorCode.MEMORY_STORE_FAILED,
            ErrorCode.UNCLASSIFIED,
        ):
            with self.subTest(code=code):
                self.assertIsNone(text_for(code))

    def test_unknown_input_is_rejected(self):
        for bad in ("request_invalid", None, 400, ErrorCode.REQUEST_INVALID.value):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    text_for(bad)


class ModuleBoundaryTests(unittest.TestCase):
    def test_module_is_pure_logic(self):
        tree = ast.parse((PLUGIN_ROOT / "fixed_notice.py").read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports |= {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        for forbidden in ("asyncio", "astrbot", "sqlite3", "logging", "llm"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imports)
        awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
        self.assertEqual(awaits, [])


if __name__ == "__main__":
    unittest.main()
