import ast
import dataclasses
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from astrbot_plugin_chizuru.config import Settings
from astrbot_plugin_chizuru.dedup import Outcome
from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey, RevisionSnapshot
from astrbot_plugin_chizuru.policy import MessageFacts
from astrbot_plugin_chizuru.send_gate import (
    DropReason,
    GateFacts,
    GroupState,
    SendDecision,
    SendKind,
    SendRequest,
    evaluate,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot_plugin_chizuru"

CONFIG = {
    "platform_id": "qq-local",
    "self_id": "10001",
    "allowed_group_ids": ["20001"],
    "group_maintainers": {"20001": ["30001"]},
}

SOURCE_GROUP = GroupKey(BotInstanceKey("qq-local", "10001"), "20001")


def facts(**overrides) -> MessageFacts:
    base = MessageFacts(
        platform_id="qq-local",
        self_id="10001",
        group_id="20001",
        sender_id="30002",
        is_group=True,
        mention_targets=("10001",),
        direct_text="今天也要好好吃饭",
    )
    return replace(base, **overrides) if overrides else base


def request(kind: SendKind = SendKind.CHAT, **overrides) -> SendRequest:
    base = SendRequest(
        kind=kind,
        source=facts(),
        target=SOURCE_GROUP,
        revision=None,
        llm_invoked=kind is SendKind.CHAT,
    )
    return replace(base, **overrides) if overrides else base


def gate_facts(**overrides) -> GateFacts:
    base = GateFacts(group_state=GroupState.OPEN)
    return replace(base, **overrides) if overrides else base


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.from_mapping(CONFIG)

    def test_chat_reply_is_allowed(self):
        decision = evaluate(request(), self.settings, gate_facts())
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.drop)

    def test_fixed_notice_is_allowed_for_text_and_attachment(self):
        cases = {
            "text": facts(),
            "attachment": facts(direct_text="", has_attachment=True),
        }
        for label, source in cases.items():
            with self.subTest(case=label):
                decision = evaluate(
                    request(SendKind.FIXED_NOTICE, source=source),
                    self.settings,
                    gate_facts(),
                )
                self.assertTrue(decision.allowed)

    def test_revision_match_passes_and_both_none_passes(self):
        same = RevisionSnapshot(group_revision=4, member_revision=7)
        self.assertTrue(
            evaluate(
                request(revision=same),
                self.settings,
                gate_facts(current=RevisionSnapshot(group_revision=4, member_revision=7)),
            ).allowed
        )
        self.assertTrue(evaluate(request(revision=None), self.settings, gate_facts()).allowed)


class DropReasonTests(unittest.TestCase):
    """每一条丢弃原因都必须能被单独触发，且互不替代。"""

    def setUp(self):
        self.settings = Settings.from_mapping(CONFIG)

    def test_each_reason_is_triggered_by_its_own_condition(self):
        cases = {
            DropReason.SOURCE_NOT_TRUSTED: (
                request(source=facts(platform_id="other-instance")),
                gate_facts(),
            ),
            DropReason.SOURCE_NOT_TRIGGERED: (
                request(source=facts(direct_text="")),
                gate_facts(),
            ),
            DropReason.TARGET_MISMATCH: (
                request(target=GroupKey(BotInstanceKey("qq-local", "10001"), "20002")),
                gate_facts(),
            ),
            DropReason.GROUP_DISABLED: (
                request(),
                gate_facts(group_state=GroupState.CLOSED),
            ),
            DropReason.REVISION_CHANGED: (
                request(revision=RevisionSnapshot(group_revision=1, member_revision=1)),
                gate_facts(current=RevisionSnapshot(group_revision=1, member_revision=2)),
            ),
            DropReason.REVISION_UNVERIFIABLE: (
                request(revision=RevisionSnapshot(group_revision=1, member_revision=1)),
                gate_facts(current=None),
            ),
            DropReason.SEND_UNCERTAIN: (
                request(),
                gate_facts(previous=Outcome.SEND_UNCERTAIN),
            ),
        }
        for reason, (req, gate) in cases.items():
            with self.subTest(reason=reason):
                decision = evaluate(req, self.settings, gate)
                self.assertFalse(decision.allowed)
                self.assertIs(decision.drop, reason)

    def test_missing_side_of_revision_is_never_silently_accepted(self):
        # 只有一侧存在修订号，说明调用方装配不完整：无法确认即丢弃。
        self.assertIs(
            evaluate(
                request(revision=None),
                self.settings,
                gate_facts(current=RevisionSnapshot(group_revision=0, member_revision=0)),
            ).drop,
            DropReason.REVISION_UNVERIFIABLE,
        )

    def test_group_state_unknown_counts_as_disabled(self):
        for state in (GroupState.CLOSED, GroupState.UNKNOWN):
            with self.subTest(state=state):
                self.assertIs(
                    evaluate(request(), self.settings, gate_facts(group_state=state)).drop,
                    DropReason.GROUP_DISABLED,
                )

    def test_target_must_match_connection_and_bot_not_just_group_id(self):
        mismatches = {
            "other_connection": GroupKey(BotInstanceKey("qq-remote", "10001"), "20001"),
            "other_bot": GroupKey(BotInstanceKey("qq-local", "10002"), "20001"),
        }
        for label, target in mismatches.items():
            with self.subTest(case=label):
                self.assertIs(
                    evaluate(request(target=target), self.settings, gate_facts()).drop,
                    DropReason.TARGET_MISMATCH,
                )

    def test_ignored_trigger_forms_are_dropped(self):
        cases = {
            "at_all": {"mention_targets": ("all", "10001")},
            "no_real_at": {"mention_targets": ()},
            "blank_text": {"direct_text": "   "},
            "private": {"is_group": False},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                decision = evaluate(request(source=facts(**overrides)), self.settings, gate_facts())
                self.assertFalse(decision.allowed)

    def test_empty_mention_cannot_get_a_fixed_notice(self):
        decision = evaluate(
            request(SendKind.FIXED_NOTICE, source=facts(direct_text="")),
            self.settings,
            gate_facts(),
        )
        self.assertIs(decision.drop, DropReason.SOURCE_NOT_TRIGGERED)

    def test_sentinel_settings_drop_everything(self):
        for kind in (SendKind.CHAT, SendKind.FIXED_NOTICE):
            with self.subTest(kind=kind):
                decision = evaluate(request(kind), Settings.from_mapping({}), gate_facts())
                self.assertIs(decision.drop, DropReason.SOURCE_NOT_TRUSTED)


class PrecedenceTests(unittest.TestCase):
    """顺序只决定报告哪个原因；任一项不成立都足以丢弃。"""

    def setUp(self):
        self.settings = Settings.from_mapping(CONFIG)

    def test_source_scope_beats_every_later_check(self):
        decision = evaluate(
            request(
                source=facts(platform_id="other-instance"),
                revision=RevisionSnapshot(group_revision=0, member_revision=0),
            ),
            self.settings,
            gate_facts(
                group_state=GroupState.CLOSED,
                current=RevisionSnapshot(group_revision=9, member_revision=9),
                previous=Outcome.SEND_UNCERTAIN,
            ),
        )
        self.assertIs(decision.drop, DropReason.SOURCE_NOT_TRUSTED)

    def test_trigger_form_beats_target_and_group_checks(self):
        decision = evaluate(
            request(
                source=facts(direct_text=""),
                target=GroupKey(BotInstanceKey("qq-local", "10001"), "20002"),
            ),
            self.settings,
            gate_facts(group_state=GroupState.CLOSED),
        )
        self.assertIs(decision.drop, DropReason.SOURCE_NOT_TRIGGERED)

    def test_group_switch_beats_revision_check(self):
        decision = evaluate(
            request(revision=RevisionSnapshot(group_revision=0, member_revision=0)),
            self.settings,
            gate_facts(
                group_state=GroupState.CLOSED,
                current=RevisionSnapshot(group_revision=5, member_revision=5),
            ),
        )
        self.assertIs(decision.drop, DropReason.GROUP_DISABLED)

    def test_uncertain_send_is_reported_after_revision_checks(self):
        decision = evaluate(
            request(revision=RevisionSnapshot(group_revision=1, member_revision=1)),
            self.settings,
            gate_facts(
                current=RevisionSnapshot(group_revision=2, member_revision=1),
                previous=Outcome.SEND_UNCERTAIN,
            ),
        )
        self.assertIs(decision.drop, DropReason.REVISION_CHANGED)


class FixedNoticeModelTests(unittest.TestCase):
    def test_fixed_notice_cannot_carry_a_model_call(self):
        with self.assertRaises(ValueError):
            request(SendKind.FIXED_NOTICE, llm_invoked=True)

    def test_send_kind_declares_whether_it_uses_the_model(self):
        self.assertTrue(SendKind.CHAT.uses_model)
        self.assertFalse(SendKind.FIXED_NOTICE.uses_model)

    def test_chat_request_does_not_require_a_model_flag(self):
        # 这里的保证是"固定提示不得调用模型"，不是"聊天必然调用模型"：
        # 是否真的调用由 llm.py / S1-14 负责，门控不越界断言。
        self.assertTrue(request(SendKind.CHAT, llm_invoked=False).kind is SendKind.CHAT)


class RequestAndFactShapeTests(unittest.TestCase):
    def test_request_fields_are_validated(self):
        with self.assertRaises(ValueError):
            request(kind="chat")
        with self.assertRaises(ValueError):
            request(source="not facts")
        with self.assertRaises(ValueError):
            request(target="20001")
        with self.assertRaises(ValueError):
            request(revision="revision")
        with self.assertRaises(ValueError):
            request(llm_invoked="yes")

    def test_gate_facts_fields_are_validated(self):
        with self.assertRaises(ValueError):
            GateFacts(group_state="open")
        with self.assertRaises(ValueError):
            GateFacts(current="revision")
        with self.assertRaises(ValueError):
            GateFacts(previous="completed")

    def test_evaluate_validates_arguments(self):
        settings = Settings.from_mapping(CONFIG)
        with self.assertRaises(ValueError):
            evaluate("request", settings, gate_facts())
        with self.assertRaises(ValueError):
            evaluate(request(), "settings", gate_facts())
        with self.assertRaises(ValueError):
            evaluate(request(), settings, "facts")


class DecisionTests(unittest.TestCase):
    def test_allowed_and_drop_are_mutually_exclusive(self):
        for allowed, drop in ((True, None), (False, DropReason.GROUP_DISABLED)):
            SendDecision(allowed=allowed, drop=drop)  # 合法组合
        for allowed, drop in ((True, DropReason.GROUP_DISABLED), (False, None)):
            with self.subTest(allowed=allowed, drop=drop):
                with self.assertRaises(ValueError):
                    SendDecision(allowed=allowed, drop=drop)

    def test_helpers_build_consistent_objects(self):
        self.assertTrue(SendDecision.allow().allowed)
        self.assertIs(SendDecision.dropped(DropReason.SOURCE_NOT_TRUSTED).drop, DropReason.SOURCE_NOT_TRUSTED)

    def test_decision_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            SendDecision.allow().allowed = False


class StructuralGuaranteeTests(unittest.TestCase):
    """门控不发送、不调模型、不承诺送达——这些必须是结构性的。"""

    def setUp(self):
        self.tree = ast.parse((PLUGIN_ROOT / "send_gate.py").read_text())
        self.imports = {
            node.module or ""
            for node in ast.walk(self.tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.attributes = {
            node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)
        }
        self.names = {
            node.name
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_pure_module_does_not_import_the_framework(self):
        self.assertFalse({name for name in self.imports if name.startswith("astrbot")})

    def test_never_sends_and_never_calls_a_model(self):
        for forbidden in ("send", "send_message", "send_streaming", "llm", "provider", "chat"):
            with self.subTest(attribute=forbidden):
                self.assertNotIn(forbidden, self.attributes)

    def test_no_exactly_once_or_receipt_api_exists(self):
        # 上游不返回 message_id、没有已读回执：本模块不得凭空提供这类语义。
        for forbidden in ("receipt", "delivered", "delivery", "exactly_once", "message_id", "idempotent"):
            with self.subTest(name=forbidden):
                self.assertFalse({name for name in self.names if forbidden in name.lower()})

    def test_request_and_decision_carry_no_content_field(self):
        for cls in (SendRequest, SendDecision):
            names = {f.name for f in dataclasses.fields(cls)}
            for forbidden in ("text", "content", "message", "reply", "notice", "body"):
                with self.subTest(cls=cls.__name__, field=forbidden):
                    self.assertNotIn(forbidden, names)

    def test_decision_has_no_success_state(self):
        names = {f.name for f in dataclasses.fields(SendDecision)}
        for forbidden in ("sent", "delivered", "receipt", "message_id", "attempt"):
            self.assertNotIn(forbidden, names)


if __name__ == "__main__":
    unittest.main()
