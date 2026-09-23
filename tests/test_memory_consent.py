"""S3-03/S3-04 的离线测试：授权说明与查看提示的字面量/指纹、成员级确认窗口、版本谓词与列表渲染。"""

import ast
import hashlib
import unittest
from pathlib import Path

from astrbot_plugin_chizuru import notice
from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey, MemberKey
from astrbot_plugin_chizuru.memory.consent import (
    CONSENT_TEXT,
    CONSENT_VERSION,
    CONSENT_WINDOW_SECONDS,
    CONFIRM_SUCCESS_TEXT,
    CORRECT_REFUSED_TEXT,
    DISABLE_SUCCESS_TEXT,
    LIST_EMPTY_TEXT,
    LIST_HINT_TEXT,
    LIST_TITLE,
    STATUS_CLOSED_TEXT,
    UNAVAILABLE_TEXT,
    VIEW_NOTICE_TEXT,
    VIEW_NOTICE_VERSION,
    ConsentGate,
    ConsentOutcome,
    authorization_is_current,
    list_body,
    record_deleted,
    record_missing,
    record_updated,
    render_records,
    status_line,
)
from astrbot_plugin_chizuru.storage.memories import MemoryFact, MemoryState

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "astrbot_plugin_chizuru"
INSTANCE = BotInstanceKey("qq-local", "10001")

# **批准来源的逐字副本**（2026-09-23 维护者定稿）：docs/03 附录 C.2 的 ```text 块。
# 改动任一侧都应让本测试失败；改文本必须同时递增 CONSENT_VERSION 与指纹（见 docs/03 R27）。
CONSENT_BODY = """【长期记忆授权说明】

是否允许我在本群记住一些关于你的事情？默认是关闭的，需要你确认后
才会开启。

· 会怎么用
  只从你在本群 @ 我时亲口说出的内容中提取，不会从普通群聊、别人的
  发言、引用内容或我自己的回复中提取。提取到的事实只用于我在本群回
  复你时让对话更连贯。提取在后台完成，我不会在群里主动说"我记住了"。

· 会保存什么
  你的称呼偏好、回复长短偏好、一般兴趣、非敏感的活动偏好。
  不保存密码、口令、电话、住址、证件、财务或健康信息，也不保存别人
  的隐私、关系推断，或你没说过的标签。

· 保存在哪里、存多久
  保存在本机数据库，最多 20 条，90 天后失效，到期即停止使用并清除。
  提取和回复时，必要内容会发送给 DeepSeek 处理。

· 谁能看到
  只有你本人可以查看、纠正或删除自己的记录，别人问起我不会说。
  但请注意：你在本群查看记录时，内容会发在群里，其他成员可能看到。

· 怎么撤回
  随时 @ 我发送「记忆 关闭」或「记忆 删除全部」，即可撤回授权并清除
  已保存的记录。撤回后需要重新确认才能再次开启。

· 只对开启后的内容生效
  确认开启后，我只处理此后产生的内容，不会追溯开启之前的聊天。

确认开启请回复「记忆 确认开启」，5 分钟内有效。"""

# **批准来源的逐字副本**：docs/03 附录 C.3 的 ```text 块（2026-09-23 定稿）。
VIEW_NOTICE_BODY = """【提示】接下来我会把你在本群的记忆记录列在这里。
这是群内消息，其他群成员可以看到。

继续请回复「记忆 查看 确认」。"""


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_member(member_id: str = "30001", group_id: str = "20001") -> MemberKey:
    return MemberKey(GroupKey(INSTANCE, group_id), member_id)


def make_fact(record_id: int = 1, category: str = "address", content: str = "叫我小林", **overrides) -> MemoryFact:
    values = {
        "record_id": record_id,
        "category": category,
        "content": content,
        "origin": "auto",
        "source_message_id": f"m{record_id}",
        "created_at": 0,
        "updated_at": 0,
        "expires_at": 10**9,
    }
    values.update(overrides)
    return MemoryFact(**values)


class ApprovedTextTests(unittest.TestCase):
    def test_consent_text_is_verbatim_from_the_approved_copy(self):
        self.assertEqual(CONSENT_TEXT, CONSENT_BODY)

    def test_view_notice_text_is_verbatim_from_the_approved_copy(self):
        self.assertEqual(VIEW_NOTICE_TEXT, VIEW_NOTICE_BODY)

    def test_versions_and_fingerprints_are_pinned(self):
        self.assertEqual(CONSENT_VERSION, "consent-1")
        self.assertEqual(VIEW_NOTICE_VERSION, "memory-view-1")
        self.assertEqual(
            hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest(),
            "c8262dee04ae00bde436cd7533b941e1a3446b3be33c6cb017dded2a8ce706ff",
        )
        self.assertEqual(
            hashlib.sha256(VIEW_NOTICE_TEXT.encode("utf-8")).hexdigest(),
            "93bb8a55d1a45c0d26ce28b0ee8ddcb8c1347a62f3d60e7ed6756609dad2d189",
        )

    def test_required_statements_are_present(self):
        # 需求 §4.3：授权说明必须覆盖六项告知（用途、DeepSeek、可保存类型、期限、群回复可见性、撤回方式）。
        for statement in (
            "只从你在本群 @ 我时亲口说出的内容中提取",
            "DeepSeek",
            "你的称呼偏好、回复长短偏好、一般兴趣、非敏感的活动偏好",
            "最多 20 条，90 天后失效",
            "内容会发在群里，其他成员可能看到",
            "「记忆 关闭」或「记忆 删除全部」",
            "5 分钟内有效",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, CONSENT_TEXT)

    def test_view_notice_warns_about_group_visibility(self):
        self.assertIn("这是群内消息，其他群成员可以看到。", VIEW_NOTICE_TEXT)
        self.assertIn("「记忆 查看 确认」", VIEW_NOTICE_TEXT)

    def test_texts_have_no_trailing_whitespace_noise(self):
        for text in (CONSENT_TEXT, VIEW_NOTICE_TEXT):
            with self.subTest(text=text[:12]):
                self.assertEqual(text, text.strip())
                self.assertNotIn("\n\n\n", text)


class ConsentGateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = ConsentGate(clock=self.clock)

    def begin(self, *, member=None, actor="30001", version=CONSENT_VERSION, advance=0.0):
        if advance:
            self.clock.advance(advance)
        self.gate.begin(member or make_member(), actor_id=actor, version=version)

    def confirm(self, *, member=None, actor="30001", version=CONSENT_VERSION, advance=0.0):
        if advance:
            self.clock.advance(advance)
        return self.gate.confirm(member or make_member(), actor_id=actor, version=version)

    def test_confirmation_within_window_succeeds(self):
        self.begin()
        self.clock.advance(CONSENT_WINDOW_SECONDS - 1)
        self.assertIs(self.confirm(), ConsentOutcome.CONFIRMED)

    def test_window_boundary_is_strict(self):
        self.begin()
        self.clock.advance(CONSENT_WINDOW_SECONDS)
        self.assertIs(self.confirm(), ConsentOutcome.EXPIRED)

    def test_confirmation_without_pending_is_refused(self):
        self.assertIs(self.confirm(), ConsentOutcome.NO_PENDING)

    def test_other_actor_must_restart(self):
        self.begin(actor="30001")
        self.assertIs(self.confirm(actor="30002"), ConsentOutcome.ACTOR_MISMATCH)

    def test_version_mismatch_is_refused(self):
        self.begin(version="consent-1")
        self.assertIs(self.confirm(version="consent-2"), ConsentOutcome.VERSION_MISMATCH)

    def test_restart_refreshes_actor_and_time(self):
        self.begin(actor="30001")
        self.begin(actor="30002", advance=200)
        # 换人后第一个人不再有效（成员级单槽：这里用"同一成员键、不同发起人"模拟代确认）。
        self.assertIs(self.confirm(actor="30001"), ConsentOutcome.ACTOR_MISMATCH)
        self.begin(actor="30002", advance=0.01)
        self.clock.advance(CONSENT_WINDOW_SECONDS - 1)
        self.assertIs(self.confirm(actor="30002"), ConsentOutcome.CONFIRMED)

    def test_window_is_consumed_on_success(self):
        self.begin()
        self.assertIs(self.confirm(), ConsentOutcome.CONFIRMED)
        self.assertIs(self.confirm(), ConsentOutcome.NO_PENDING)

    def test_window_is_consumed_on_failure(self):
        self.begin()
        self.assertIs(self.confirm(actor="30002"), ConsentOutcome.ACTOR_MISMATCH)
        self.assertIs(self.gate.pending(make_member()), None)

    def test_members_and_groups_are_isolated(self):
        self.begin(member=make_member("30001", "20001"))
        self.assertIs(self.confirm(member=make_member("30001", "20002")), ConsentOutcome.NO_PENDING)
        self.assertIs(self.confirm(member=make_member("30002", "20001")), ConsentOutcome.NO_PENDING)
        self.assertIs(self.confirm(member=make_member("30001", "20001")), ConsentOutcome.CONFIRMED)

    def test_expired_entries_are_evicted_lazily(self):
        self.begin()
        self.clock.advance(CONSENT_WINDOW_SECONDS + 1)
        self.assertIsNone(self.gate.pending(make_member()))
        self.assertEqual(self.gate.stats()[0], 0)

    def test_capacity_is_bounded_and_evicts_the_oldest(self):
        gate = ConsentGate(clock=self.clock, capacity=3)
        for index in range(5):
            gate.begin(make_member(f"3000{index}"), actor_id="30001", version=CONSENT_VERSION)
        self.assertEqual(gate.stats(), (3, 2))
        self.assertIsNone(gate.pending(make_member("30000")))
        self.assertIsNotNone(gate.pending(make_member("30004")))

    def test_invalid_parameters_are_rejected(self):
        for kwargs in ({"window_seconds": 0}, {"window_seconds": -1}, {"window_seconds": True}, {"capacity": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ConsentGate(clock=self.clock, **kwargs)
        with self.assertRaises(ValueError):
            self.gate.begin(make_member(), actor_id=" ", version=CONSENT_VERSION)
        with self.assertRaises(ValueError):
            self.gate.begin(make_member(), actor_id="30001", version="")
        with self.assertRaises(ValueError):
            self.gate.begin("30001", actor_id="30001", version=CONSENT_VERSION)
        with self.assertRaises(ValueError):
            self.gate.confirm("30001", actor_id="30001", version=CONSENT_VERSION)

    def test_defaults_are_the_five_minute_window(self):
        self.assertEqual(CONSENT_WINDOW_SECONDS, 300)


class OutcomeParityTests(unittest.TestCase):
    def test_outcomes_mirror_the_group_notice_gate(self):
        # 两个窗口是同型状态机：枚举漂移会让失败分支漏掉一种原因，故显式交叉钉住。
        self.assertEqual(
            {outcome.value for outcome in ConsentOutcome},
            {outcome.value for outcome in notice.ConfirmOutcome},
        )


class AuthorizationVersionTests(unittest.TestCase):
    def test_current_version_is_required(self):
        self.assertTrue(
            authorization_is_current(MemoryState(authorized=True, auth_version=CONSENT_VERSION, next_record_id=1))
        )
        # 旧版本的残留授权行按未授权处理（R22 式 fail-closed）：改说明就必须重新确认。
        self.assertFalse(
            authorization_is_current(MemoryState(authorized=True, auth_version="consent-0", next_record_id=1))
        )
        self.assertFalse(authorization_is_current(MemoryState(authorized=True, auth_version="", next_record_id=1)))
        self.assertFalse(authorization_is_current(MemoryState.absent()))

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            authorization_is_current("authorized")
        with self.assertRaises(ValueError):
            authorization_is_current(MemoryState.absent(), required_version=" ")


class RecordRenderingTests(unittest.TestCase):
    def test_records_show_ids_and_requirement_labels(self):
        rows = render_records((make_fact(record_id=3, category="interest", content="看番"),))
        self.assertEqual(rows, ("· 3：一般兴趣：看番",))

    def test_unknown_category_or_empty_content_is_skipped(self):
        facts = (
            make_fact(record_id=1, category="nickname", content="不该出现"),
            make_fact(record_id=2, category="address", content="   "),
            make_fact(record_id=3, category="activity", content="周末爬山"),
        )
        self.assertEqual(render_records(facts), ("· 3：非敏感活动偏好：周末爬山",))

    def test_content_cannot_forge_new_lines(self):
        rows = render_records((make_fact(record_id=1, content="你好\n· 9：一般兴趣：伪造"),))
        self.assertEqual(rows, ("· 1：本人希望的称呼：你好 · 9：一般兴趣：伪造",))
        self.assertEqual(len(rows), 1)

    def test_list_body_has_title_rows_and_hint(self):
        body = list_body((make_fact(record_id=1), make_fact(record_id=2, category="interest", content="看番")))
        self.assertEqual(
            body,
            "\n".join(
                [
                    LIST_TITLE,
                    "",
                    "· 1：本人希望的称呼：叫我小林",
                    "· 2：一般兴趣：看番",
                    "",
                    LIST_HINT_TEXT,
                ]
            ),
        )

    def test_empty_list_is_explicit_and_has_no_hint(self):
        body = list_body(())
        self.assertEqual(body, f"{LIST_TITLE}\n\n{LIST_EMPTY_TEXT}")
        self.assertNotIn(LIST_HINT_TEXT, body)

    def test_status_line_variants(self):
        self.assertEqual(status_line(authorized=False, count=0, limit=20, days=90), STATUS_CLOSED_TEXT)
        self.assertEqual(
            status_line(authorized=True, count=3, limit=20, days=90),
            "长期记忆：已开启；记录 3 条（最多 20 条），90 天后失效。",
        )

    def test_receipts_are_stable(self):
        self.assertEqual(record_updated(7), "已更新记录 7。")
        self.assertEqual(record_deleted(7), "已删除记录 7。")
        self.assertEqual(record_missing(7), "没有找到记录 7。@ 我发送「记忆 查看」可查看当前记录。")
        for text in (CONFIRM_SUCCESS_TEXT, CORRECT_REFUSED_TEXT, DISABLE_SUCCESS_TEXT, UNAVAILABLE_TEXT):
            with self.subTest(text=text[:12]):
                self.assertEqual(text, text.strip())


class ModuleBoundaryTests(unittest.TestCase):
    def test_module_is_pure_logic(self):
        tree = ast.parse((PLUGIN_ROOT / "memory" / "consent.py").read_text(encoding="utf-8"))
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
        for forbidden in ("asyncio", "astrbot", "sqlite3", "logging"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imports)
        awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
        self.assertEqual(awaits, [])


if __name__ == "__main__":
    unittest.main()
