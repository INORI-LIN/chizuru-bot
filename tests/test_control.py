import dataclasses
import unittest
from dataclasses import replace
from pathlib import Path

from astrbot_plugin_chizuru.commands import CommandIntent, CommandKind, Permission, parse
from astrbot_plugin_chizuru.config import Settings
from astrbot_plugin_chizuru.control import Authorization, Denial, authorize
from astrbot_plugin_chizuru.policy import MessageFacts

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot_plugin_chizuru"

MAINTAINER_ID = "30001"
ORDINARY_ID = "30002"

CONFIG = {
    "platform_id": "qq-local",
    "self_id": "10001",
    "allowed_group_ids": ["20001"],
    # 维护者必须显式配置：QQ 群管理员身份不会自动生效。
    "group_maintainers": {"20001": [MAINTAINER_ID]},
}


def facts(sender_id: str = ORDINARY_ID, **overrides) -> MessageFacts:
    base = MessageFacts(
        platform_id="qq-local",
        self_id="10001",
        group_id="20001",
        sender_id=sender_id,
        is_group=True,
    )
    return replace(base, **overrides) if overrides else base


class MemberCommandTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.from_mapping(CONFIG)

    def test_help_is_available_to_any_member(self):
        intent = parse("帮助")
        self.assertTrue(authorize(intent, facts(), self.settings).allowed)

    def test_self_scoped_commands_are_available_to_any_member(self):
        for text in ("上下文 退出", "记忆 开启", "记忆 状态", "记忆 查看", "记忆 关闭"):
            with self.subTest(text=text):
                result = authorize(parse(text), facts(), self.settings)
                self.assertTrue(result.allowed)
                self.assertIsNone(result.denial)


class MaintainerCommandTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.from_mapping(CONFIG)

    def test_configured_maintainer_is_allowed(self):
        for text in ("千鹤 暂停", "千鹤 恢复", "千鹤 状态", "上下文 清空", "群上下文 开启"):
            with self.subTest(text=text):
                self.assertTrue(authorize(parse(text), facts(MAINTAINER_ID), self.settings).allowed)

    def test_ordinary_member_is_denied(self):
        for text in ("千鹤 暂停", "千鹤 恢复", "千鹤 状态", "上下文 清空", "群上下文 确认开启"):
            with self.subTest(text=text):
                result = authorize(parse(text), facts(ORDINARY_ID), self.settings)
                self.assertFalse(result.allowed)
                self.assertEqual(result.denial, Denial.NOT_A_MAINTAINER)

    def test_maintainer_of_another_group_is_denied_here(self):
        settings = Settings.from_mapping(
            {**CONFIG, "allowed_group_ids": ["20001", "20002"], "group_maintainers": {"20002": [ORDINARY_ID]}},
        )
        result = authorize(parse("千鹤 暂停"), facts(ORDINARY_ID), settings)
        self.assertEqual(result.denial, Denial.NOT_A_MAINTAINER)

    def test_group_admin_is_not_automatically_a_maintainer(self):
        """唯一判据是显式配置映射：把同一身份从映射里去掉，权限随之消失。"""
        with_map = Settings.from_mapping(CONFIG)
        without_map = Settings.from_mapping({**CONFIG, "group_maintainers": {}})
        intent = parse("千鹤 暂停")

        self.assertTrue(authorize(intent, facts(MAINTAINER_ID), with_map).allowed)
        self.assertEqual(
            authorize(intent, facts(MAINTAINER_ID), without_map).denial,
            Denial.NOT_A_MAINTAINER,
        )

    def test_message_facts_carries_no_role_or_admin_field(self):
        # 身份事实里根本没有"群管理员/系统管理员"这类字段可供误用。
        names = {f.name for f in dataclasses.fields(MessageFacts)}
        for forbidden in ("role", "is_admin", "admin", "is_owner", "nickname", "card"):
            self.assertNotIn(forbidden, names)


class UntrustedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.from_mapping(CONFIG)
        self.intent = parse("帮助")

    def test_untrusted_scope_is_denied_before_permission(self):
        cases = {
            "private": {"is_group": False},
            "other_platform": {"platform_id": "other"},
            "other_bot": {"self_id": "10002"},
            "unlisted_group": {"group_id": "29999"},
            "self_message": {"sender_id": "10001"},
            "malformed_sender": {"sender_id": "030001"},
            "empty_sender": {"sender_id": ""},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                result = authorize(self.intent, facts(**overrides), self.settings)
                self.assertFalse(result.allowed)
                self.assertEqual(result.denial, Denial.IDENTITY_NOT_TRUSTED)

    def test_untrusted_identity_beats_maintainer_mapping(self):
        # 即使身份在维护者名单里，范围不可信也一律拒绝——顺序不能反过来。
        result = authorize(
            parse("千鹤 暂停"),
            facts(MAINTAINER_ID, group_id="29999"),
            self.settings,
        )
        self.assertEqual(result.denial, Denial.IDENTITY_NOT_TRUSTED)

    def test_sentinel_settings_denies_everything(self):
        for text in ("帮助", "记忆 开启", "千鹤 暂停"):
            with self.subTest(text=text):
                result = authorize(parse(text), facts(MAINTAINER_ID), Settings())
                self.assertEqual(result.denial, Denial.IDENTITY_NOT_TRUSTED)


class AuthorizationShapeTests(unittest.TestCase):
    def test_allowed_and_denial_are_mutually_exclusive(self):
        for allowed, denial in ((True, None), (False, Denial.NOT_A_MAINTAINER)):
            Authorization(allowed=allowed, denial=denial)  # 合法组合
        for allowed, denial in ((True, Denial.NOT_A_MAINTAINER), (False, None)):
            with self.subTest(allowed=allowed, denial=denial):
                with self.assertRaises(ValueError):
                    Authorization(allowed=allowed, denial=denial)

    def test_helpers_build_consistent_objects(self):
        self.assertTrue(Authorization.ok().allowed)
        self.assertFalse(Authorization.denied(Denial.NOT_A_MAINTAINER).allowed)

    def test_authorization_is_immutable(self):
        from dataclasses import FrozenInstanceError

        with self.assertRaises(FrozenInstanceError):
            Authorization.ok().allowed = False


class StructuralGuaranteeTests(unittest.TestCase):
    """权限路径必须与模型、框架、外部文本彻底分离——这些是结构性的，不靠约定。"""

    def test_pure_modules_do_not_import_the_framework(self):
        for name in ("policy.py", "commands.py", "control.py", "config.py", "keys.py"):
            with self.subTest(module=name):
                text = (PLUGIN_ROOT / name).read_text()
                self.assertNotIn("import astrbot", text)
                self.assertNotIn("from astrbot", text)

    def test_command_intent_carries_no_target_member(self):
        # "不能代他人开启记忆"是结构保证：指令里没有可以填别人 ID 的位置。
        names = {f.name for f in dataclasses.fields(CommandIntent)}
        for forbidden in ("member_id", "target", "target_id", "user_id", "who"):
            self.assertNotIn(forbidden, names)

    def test_every_intent_kind_declares_a_permission_tier(self):
        for text in ("帮助", "记忆 开启", "记忆 删除 3", "记忆 纠正 3 内容", "千鹤 暂停"):
            with self.subTest(text=text):
                intent = parse(text)
                self.assertIsInstance(intent.permission, Permission)

    def test_denial_reasons_are_distinct(self):
        self.assertEqual(len({Denial.IDENTITY_NOT_TRUSTED, Denial.NOT_A_MAINTAINER}), 2)

    def test_maintainer_tier_is_never_satisfied_by_member_tier(self):
        # 防止将来把 Permission 的取值判断写成"大于等于"之类的顺序比较。
        self.assertNotEqual(Permission.MEMBER, Permission.MAINTAINER)
        self.assertNotEqual(Permission.SELF, Permission.MAINTAINER)


if __name__ == "__main__":
    unittest.main()
