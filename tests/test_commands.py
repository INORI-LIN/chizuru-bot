import unittest

from astrbot_plugin_chizuru.commands import (
    COMMAND_TEXTS,
    CommandIntent,
    CommandKind,
    Permission,
    is_command,
    normalize,
    parse,
)

# 需求文档 §4.4 的指令表：每一条都必须可解析，且权限与表中一致。
EXPECTED = {
    "帮助": (CommandKind.HELP, Permission.MEMBER),
    "上下文 退出": (CommandKind.CONTEXT_LEAVE, Permission.SELF),
    "上下文 加入": (CommandKind.CONTEXT_JOIN, Permission.SELF),
    "记忆 开启": (CommandKind.MEMORY_ENABLE, Permission.SELF),
    "记忆 状态": (CommandKind.MEMORY_STATUS, Permission.SELF),
    "记忆 查看": (CommandKind.MEMORY_LIST, Permission.SELF),
    "记忆 查看 确认": (CommandKind.MEMORY_LIST_CONFIRM, Permission.SELF),
    "记忆 关闭": (CommandKind.MEMORY_DISABLE, Permission.SELF),
    "记忆 删除全部": (CommandKind.MEMORY_DISABLE, Permission.SELF),
    "千鹤 暂停": (CommandKind.PAUSE, Permission.MAINTAINER),
    "千鹤 恢复": (CommandKind.RESUME, Permission.MAINTAINER),
    "群上下文 开启": (CommandKind.GROUP_NOTICE_OPEN, Permission.MAINTAINER),
    "群上下文 确认开启": (CommandKind.GROUP_NOTICE_CONFIRM, Permission.MAINTAINER),
    "群上下文 关闭": (CommandKind.GROUP_CONTEXT_CLOSE, Permission.MAINTAINER),
    "上下文 清空": (CommandKind.CONTEXT_CLEAR, Permission.MAINTAINER),
    "千鹤 状态": (CommandKind.STATUS, Permission.MAINTAINER),
    "记忆 确认开启": (CommandKind.MEMORY_CONFIRM, Permission.SELF),
}


class RequirementCoverageTests(unittest.TestCase):
    def test_every_requirement_command_parses(self):
        for text, (kind, permission) in EXPECTED.items():
            with self.subTest(text=text):
                intent = parse(text)
                self.assertEqual(intent.kind, kind)
                self.assertEqual(intent.permission, permission)

    def test_command_texts_advertises_every_requirement_command(self):
        for text in EXPECTED:
            self.assertIn(text, COMMAND_TEXTS)

    def test_no_extra_commands_beyond_requirement_table(self):
        # 解析器不认领需求表以外的指令，防止实现阶段悄悄加指令。
        parsed = {text for text in COMMAND_TEXTS if parse(text) is not None}
        self.assertEqual(parsed, set(EXPECTED))


class PermissionTests(unittest.TestCase):
    def test_memory_commands_are_self_scoped(self):
        for text in ("记忆 开启", "记忆 状态", "记忆 查看", "记忆 查看 确认", "记忆 关闭", "记忆 删除全部"):
            self.assertEqual(parse(text).permission, Permission.SELF)

    def test_maintainer_commands_are_not_member_level(self):
        for text in ("千鹤 暂停", "千鹤 恢复", "千鹤 状态", "上下文 清空"):
            self.assertEqual(parse(text).permission, Permission.MAINTAINER)

    def test_group_notice_flow_both_steps_need_maintainer(self):
        self.assertEqual(parse("群上下文 开启").permission, Permission.MAINTAINER)
        self.assertEqual(parse("群上下文 确认开启").permission, Permission.MAINTAINER)
        self.assertNotEqual(
            parse("群上下文 开启").kind,
            parse("群上下文 确认开启").kind,
        )

    def test_memory_view_flow_both_steps_are_distinct(self):
        # 需求 §4.4：查看是两步——先提示群内可见，确认后才列出。
        first, second = parse("记忆 查看"), parse("记忆 查看 确认")
        self.assertEqual(first.kind, CommandKind.MEMORY_LIST)
        self.assertEqual(second.kind, CommandKind.MEMORY_LIST_CONFIRM)
        self.assertNotEqual(first.kind, second.kind)
        for intent in (first, second):
            self.assertEqual(intent.permission, Permission.SELF)


class ParameterizedTests(unittest.TestCase):
    def test_correct_carries_record_id_and_content(self):
        intent = parse("记忆 纠正 3 其实我喜欢安静一点的地方")
        self.assertEqual(intent.kind, CommandKind.MEMORY_CORRECT)
        self.assertEqual(intent.permission, Permission.SELF)
        self.assertEqual(intent.record_id, "3")
        self.assertEqual(intent.argument, "其实我喜欢安静一点的地方")

    def test_delete_carries_record_id(self):
        intent = parse("记忆 删除 12")
        self.assertEqual(intent.kind, CommandKind.MEMORY_DELETE)
        self.assertEqual(intent.record_id, "12")
        self.assertEqual(intent.argument, "")

    def test_correct_requires_both_parts(self):
        for text in ("记忆 纠正 3", "记忆 纠正 3 ", "记忆 纠正 内容"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_delete_requires_numeric_id(self):
        for text in ("记忆 删除", "记忆 删除 abc", "记忆 删除 3 4", "记忆 删除 -1"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_record_id_rejects_zero_and_leading_zero(self):
        # 编号从 1 开始；0 与前导零不是合法形式（与 QQ 号校验同口径）。
        for text in ("记忆 删除 0", "记忆 删除 007", "记忆 纠正 0 内容", "记忆 纠正 007 内容"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_content_may_contain_spaces(self):
        intent = parse("记忆 纠正 7 喜欢 安静 一点")
        self.assertEqual(intent.record_id, "7")
        self.assertEqual(intent.argument, "喜欢 安静 一点")


class WhitespaceTests(unittest.TestCase):
    def test_internal_whitespace_is_normalized(self):
        for text in ("记忆   删除 3", "记忆\t删除 3", "  记忆 删除 3  ", "记忆\n删除 3"):
            with self.subTest(text=text):
                intent = parse(text)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.kind, CommandKind.MEMORY_DELETE)

    def test_normalize_only_touches_whitespace(self):
        self.assertEqual(normalize("  a \t b \n c "), "a b c")
        self.assertEqual(normalize("记忆"), "记忆")


class UnknownTextTests(unittest.TestCase):
    """未知文本不得被误判为命令——否则会把普通聊天当成控制操作执行。"""

    def test_unknown_and_near_miss_texts_are_not_commands(self):
        for text in (
            "",
            "   ",
            "你好",
            "记忆",
            "记忆 删除 全部",  # 空格错位，不是已知指令
            "记忆 删除全部 3",
            "记忆开启",
            "记忆 开启 吧",
            "记忆 查看确认",  # 缺分隔空格，不是已知指令
            "记忆 查看 确认 一下",
            "帮助我",
            "帮助 我",
            "/help",
            "@千鹤 记忆 删除 3",  # 昵称/文本不构成指令前缀
            "千鹤",
            "上下文",
            "群上下文",
            "上下文 退出 现在",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))
                self.assertFalse(is_command(text))

    def test_prefix_lookalikes_do_not_raise(self):
        for text in ("记忆 纠正", "记忆 纠正 ", "记忆 删除 ", "记忆 纠正 0 内容"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))


class IntentShapeTests(unittest.TestCase):
    def test_intent_is_immutable(self):
        from dataclasses import FrozenInstanceError

        intent = parse("记忆 删除 1")
        with self.assertRaises(FrozenInstanceError):
            intent.record_id = "2"

    def test_default_intent_has_no_arguments(self):
        self.assertEqual(CommandIntent(CommandKind.HELP, Permission.MEMBER).record_id, "")
        self.assertEqual(CommandIntent(CommandKind.HELP, Permission.MEMBER).argument, "")


if __name__ == "__main__":
    unittest.main()
