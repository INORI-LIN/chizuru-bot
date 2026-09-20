"""S3-05 候选层离线测试：类别白名单、源文本与候选校验（长度、敏感、形状）。"""

import ast
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.context_buffer import MAX_TEXT_LENGTH
from astrbot_plugin_chizuru.memory import (
    MAX_SOURCE_LENGTH,
    Category,
    build_candidate,
    prepare_source,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "astrbot_plugin_chizuru"


class CategoryTests(unittest.TestCase):
    def test_the_whitelist_is_exactly_the_four_confirmed_categories(self):
        # 白名单扩项必须同时改需求 §4.3 与本模块文档：这条用例让扩项成为显式动作。
        self.assertEqual(
            {member.value for member in Category},
            {"address", "reply_length", "interest", "activity"},
        )

    def test_values_are_ascii_identifiers(self):
        # 面向群成员的展示名不在本批（属 S3-04 的待审文案），因此值只有 ASCII 标识。
        for member in Category:
            with self.subTest(member=member):
                self.assertTrue(member.value.isascii())


class PrepareSourceTests(unittest.TestCase):
    def test_plain_text_is_stripped_and_returned(self):
        self.assertEqual(prepare_source("  叫我小林就好  "), "叫我小林就好")

    def test_rejects_non_text_and_blank(self):
        for value in (None, 42, True, ["叫我小林"], "", "   ", "\n\t"):
            with self.subTest(value=value):
                self.assertIsNone(prepare_source(value))

    def test_length_boundary(self):
        longest = "x" * MAX_SOURCE_LENGTH
        self.assertEqual(prepare_source(longest), longest)
        self.assertIsNone(prepare_source("x" * (MAX_SOURCE_LENGTH + 1)))

    def test_rejects_sensitive_text_entirely(self):
        for value in ("我的手机号 13800138000", "密码是 abc", "写信到 a@b.com"):
            with self.subTest(value=value):
                self.assertIsNone(prepare_source(value))

    def test_the_source_bound_reuses_the_buffer_bound(self):
        # 不另立数字：两者面向同一类"成员在当前群说的一句话"。
        self.assertEqual(MAX_SOURCE_LENGTH, MAX_TEXT_LENGTH)


class BuildCandidateTests(unittest.TestCase):
    def test_accepts_the_four_categories(self):
        for member in Category:
            with self.subTest(member=member):
                candidate = build_candidate(member.value, "看番")
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.category, member)

    def test_category_is_matched_verbatim(self):
        # 不做大小写或近义词容错：容错会让白名单退化成模糊匹配。
        for value in ("Address", "ADDRESS", "称呼", "mood", "remark", ""):
            with self.subTest(value=value):
                self.assertIsNone(build_candidate(value, "小林"))

    def test_surrounding_whitespace_in_the_field_is_tolerated(self):
        self.assertIsNotNone(build_candidate(" address ", " 小林 "))

    def test_non_text_category_is_rejected(self):
        for value in (None, 1, True, ["address"], {"category": "address"}):
            with self.subTest(value=value):
                self.assertIsNone(build_candidate(value, "小林"))

    def test_rejects_unusable_content(self):
        for value in (None, 42, "", "   ", "x" * (MAX_TEXT_LENGTH + 1), "我的手机号 13800138000"):
            with self.subTest(value=value):
                self.assertIsNone(build_candidate("address", value))

    def test_content_is_stripped(self):
        self.assertEqual(build_candidate("address", "  小林  ").content, "小林")


class CandidateShapeTests(unittest.TestCase):
    def test_to_pair_flattens_for_storage(self):
        candidate = build_candidate("interest", "看番")
        self.assertEqual(candidate.to_pair(), ("interest", "看番"))

    def test_content_stays_out_of_repr(self):
        # 正文只应在受控路径里被使用，不随对象一起被打印（与 BufferEntry.text 同例）。
        self.assertNotIn("小林", repr(build_candidate("address", "小林")))


class StructuralTests(unittest.TestCase):
    def test_memory_modules_are_synchronous_and_framework_free(self):
        """整个 ``memory/`` 包纯同步、只 import 标准库与插件内模块。"""
        for path in sorted((PLUGIN_ROOT / "memory").glob("*.py")):
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imports = {
                    alias.name.split(".")[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                }
                imports |= {
                    (node.module or "").split(".")[0].lstrip(".")
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                }
                self.assertNotIn("asyncio", imports)
                self.assertNotIn("astrbot", imports)
                awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
                self.assertEqual(awaits, [])


if __name__ == "__main__":
    unittest.main()
