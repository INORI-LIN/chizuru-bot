"""S2-02 群告知文案与两步确认窗口的离线测试：文本指纹、窗口取严、失败语义。"""

import ast
import hashlib
import unittest
from pathlib import Path

from astrbot_plugin_chizuru import notice
from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey
from astrbot_plugin_chizuru.notice import (
    CONFIRM_WINDOW_SECONDS,
    NOTICE_TEXT,
    NOTICE_VERSION,
    ConfirmOutcome,
    NoticeGate,
    describe_policy,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "astrbot_plugin_chizuru"
INSTANCE = BotInstanceKey("qq-local", "10001")

# **批准来源的逐字副本**（2026-09-18 用户定稿）：docs/03 附录 C.1 的 ```text 块。
# 改动任一侧都应让本测试失败；改文本必须同时递增 NOTICE_VERSION 与指纹。
NOTICE_BODY = """【关于本群启用的聊天机器人】

本群将启用一个参考《租借女友》水原千鹤设计的聊天机器人。
它不是真人，也不是官方账号，是仅在本群使用的非官方角色机器人。

它出现的方式很有限：
· 只有被 @ 时才会回复
· 你主动发送控制指令时，它会按权限执行

为了让对话能接上群里的话题，它会在内存中短暂保留本群近期的普通聊天
文本（每群最多 30 条、不超过 10 分钟）。这些内容不写入数据库，服务重
启即丢失。只有有人 @ 它时，它才会把必要的部分发送给 DeepSeek 处理，
不会逐条分析，也不会给群成员做画像。

你可以随时 @ 它并发送「上下文 退出」，停止采集你自己的发言，并清除
相关缓冲。退出后你仍然可以正常 @ 它聊天。

长期记忆默认关闭。除非你本人主动开启并确认，它不会记住任何关于你的
事情。

请不要向它发送密码、证件、电话、住址、财务或健康信息。群消息会经由
DeepSeek 处理，而且已经发出的群消息其他成员都能看到，无法撤回。

有问题可以 @ 它发送「帮助」，或联系本群维护者。"""


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_group(group_id: str = "20001") -> GroupKey:
    return GroupKey(INSTANCE, group_id)


class NoticeTextTests(unittest.TestCase):
    def test_text_is_verbatim_from_the_approved_copy(self):
        self.assertEqual(NOTICE_TEXT, NOTICE_BODY)

    def test_version_and_fingerprint_are_pinned(self):
        self.assertEqual(NOTICE_VERSION, "notice-1")
        self.assertEqual(
            hashlib.sha256(NOTICE_TEXT.encode("utf-8")).hexdigest(),
            "3e69dbd0d3d1d7a707aad55bd28be403a524be41bcc7dd819568dc27d3192ab1",
        )

    def test_required_statements_are_present(self):
        for statement in (
            "它不是真人，也不是官方账号",
            "只有被 @ 时才会回复",
            "每群最多 30 条、不超过 10 分钟",
            "上下文 退出",
            "长期记忆默认关闭",
            "DeepSeek",
            "无法撤回",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, NOTICE_TEXT)

    def test_text_has_no_trailing_whitespace_noise(self):
        self.assertEqual(NOTICE_TEXT, NOTICE_TEXT.strip())
        self.assertNotIn("\n\n\n", NOTICE_TEXT)


class DescribePolicyTests(unittest.TestCase):
    def test_absent_policy_is_unnotified(self):
        self.assertEqual(
            describe_policy(notice_version="", context_enabled=False, paused=False),
            "群上下文：未告知",
        )

    def test_open_and_closed_variants(self):
        self.assertEqual(
            describe_policy(notice_version="notice-1", context_enabled=True, paused=False),
            "群上下文：已开启（notice-1）",
        )
        self.assertEqual(
            describe_policy(notice_version="notice-1", context_enabled=False, paused=False),
            "群上下文：已关闭（notice-1）",
        )

    def test_pause_wins_over_enabled(self):
        self.assertEqual(
            describe_policy(notice_version="notice-1", context_enabled=True, paused=True),
            "群上下文：已暂停（notice-1）",
        )

    def test_empty_version_wins_priority(self):
        # bump_revision 建行后就是这一形态：没有告知版本，显示"未告知"而不是"已暂停（）"。
        self.assertEqual(
            describe_policy(notice_version="  ", context_enabled=True, paused=True),
            "群上下文：未告知",
        )


class NoticeGateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = NoticeGate(clock=self.clock)

    def begin(self, *, group=None, actor="30001", version=NOTICE_VERSION, advance=0.0):
        if advance:
            self.clock.advance(advance)
        self.gate.begin(group or make_group(), actor_id=actor, version=version)

    def confirm(self, *, group=None, actor="30001", version=NOTICE_VERSION, advance=0.0):
        if advance:
            self.clock.advance(advance)
        return self.gate.confirm(group or make_group(), actor_id=actor, version=version)

    def test_confirmation_within_window_succeeds(self):
        self.begin()
        self.clock.advance(CONFIRM_WINDOW_SECONDS - 1)
        self.assertIs(self.confirm(), ConfirmOutcome.CONFIRMED)

    def test_window_boundary_is_strict(self):
        self.begin()
        self.clock.advance(CONFIRM_WINDOW_SECONDS)
        self.assertIs(self.confirm(), ConfirmOutcome.EXPIRED)

    def test_confirmation_without_pending_is_refused(self):
        self.assertIs(self.confirm(), ConfirmOutcome.NO_PENDING)

    def test_other_actor_must_restart(self):
        self.begin(actor="30001")
        self.assertIs(self.confirm(actor="30002"), ConfirmOutcome.ACTOR_MISMATCH)

    def test_version_mismatch_is_refused(self):
        self.begin(version="notice-1")
        self.assertIs(self.confirm(version="notice-2"), ConfirmOutcome.VERSION_MISMATCH)

    def test_restart_refreshes_actor_and_time(self):
        self.begin(actor="30001")
        self.begin(actor="30002", advance=200)
        # 换人后第一个维护者不再有效，第二个人在窗口内可以确认。
        self.assertIs(self.confirm(actor="30001"), ConfirmOutcome.ACTOR_MISMATCH)
        self.begin(actor="30002", advance=0.01)
        self.clock.advance(CONFIRM_WINDOW_SECONDS - 1)
        self.assertIs(self.confirm(actor="30002"), ConfirmOutcome.CONFIRMED)

    def test_window_is_consumed_on_success(self):
        self.begin()
        self.assertIs(self.confirm(), ConfirmOutcome.CONFIRMED)
        self.assertIs(self.confirm(), ConfirmOutcome.NO_PENDING)

    def test_window_is_consumed_on_failure(self):
        self.begin()
        self.assertIs(self.confirm(actor="30002"), ConfirmOutcome.ACTOR_MISMATCH)
        self.assertIs(self.gate.pending(make_group()), None)

    def test_expired_entries_are_evicted_lazily(self):
        self.begin()
        self.clock.advance(CONFIRM_WINDOW_SECONDS + 1)
        self.assertIsNone(self.gate.pending(make_group()))
        self.assertEqual(self.gate.stats()[0], 0)

    def test_groups_are_isolated(self):
        self.begin(group=make_group("20001"))
        self.assertIs(self.confirm(group=make_group("20002")), ConfirmOutcome.NO_PENDING)
        self.assertIs(self.confirm(group=make_group("20001")), ConfirmOutcome.CONFIRMED)

    def test_capacity_is_bounded(self):
        gate = NoticeGate(clock=self.clock, capacity=3)
        for index in range(5):
            gate.begin(make_group(f"2000{index}"), actor_id="30001", version=NOTICE_VERSION)
        self.assertEqual(gate.stats(), (3, 2))
        # 最旧的先被淘汰：第一个群已经找不到待确认记录。
        self.assertIsNone(gate.pending(make_group("20000")))
        self.assertIsNotNone(gate.pending(make_group("20004")))

    def test_invalid_parameters_are_rejected(self):
        for kwargs in ({"window_seconds": 0}, {"window_seconds": -1}, {"window_seconds": True}, {"capacity": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    NoticeGate(clock=self.clock, **kwargs)
        with self.assertRaises(ValueError):
            self.gate.begin(make_group(), actor_id=" ", version=NOTICE_VERSION)
        with self.assertRaises(ValueError):
            self.gate.begin(make_group(), actor_id="30001", version="")
        with self.assertRaises(ValueError):
            self.gate.begin("20001", actor_id="30001", version=NOTICE_VERSION)


class ModuleBoundaryTests(unittest.TestCase):
    def test_module_is_pure_logic(self):
        tree = ast.parse((PLUGIN_ROOT / "notice.py").read_text(encoding="utf-8"))
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
        for forbidden in ("asyncio", "astrbot", "sqlite3", "logging"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imports)
        awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
        self.assertEqual(awaits, [])


if __name__ == "__main__":
    unittest.main()
