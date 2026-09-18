"""S2-03 普通群聊环形缓冲离线测试：容量/TTL 取严、排除规则、清理与结构边界。"""

import ast
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.context_buffer import (
    MAX_TEXT_LENGTH,
    BufferEntry,
    BufferShape,
    ContextBuffer,
    IngestOutcome,
    is_sensitive,
)
from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey, MemberKey

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "astrbot_plugin_chizuru"
INSTANCE = BotInstanceKey("qq-local", "10001")


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_group(group_id: str = "20001") -> GroupKey:
    return GroupKey(INSTANCE, group_id)


def make_member(member_id: str = "30001", group_id: str = "20001") -> MemberKey:
    return MemberKey(make_group(group_id), member_id)


class BufferTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.buffer = ContextBuffer(
            max_messages=30,
            ttl_seconds=600.0,
            clock=self.clock,
        )

    def ingest(
        self,
        text: str = "今天天气不错",
        *,
        message_id: str = "m1",
        member_id: str = "30001",
        group_id: str = "20001",
        shape: BufferShape = BufferShape.TEXT_ONLY,
    ) -> IngestOutcome:
        return self.buffer.ingest(
            group=make_group(group_id),
            member=make_member(member_id, group_id),
            message_id=message_id,
            text=text,
            shape=shape,
        )


class IngestTests(BufferTestCase):
    def test_stored_entry_keeps_attribution_and_time(self):
        self.clock.advance(12)
        self.assertEqual(self.ingest("今天天气不错"), IngestOutcome.STORED)
        entries = self.buffer.entries(make_group())
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.member_id, "30001")
        self.assertEqual(entry.message_id, "m1")
        self.assertEqual(entry.text, "今天天气不错")
        self.assertEqual(entry.at, self.clock.now)

    def test_shapes_other_than_text_only_are_rejected(self):
        for shape in (
            BufferShape.HAS_MENTION,
            BufferShape.HAS_ATTACHMENT,
            BufferShape.HAS_QUOTE,
            BufferShape.HAS_FORWARD,
            BufferShape.OTHER,
        ):
            with self.subTest(shape=shape):
                self.assertEqual(self.ingest(shape=shape), IngestOutcome.REJECTED_SHAPE)
        self.assertEqual(self.buffer.stats().entries, 0)

    def test_bot_self_message_is_rejected(self):
        self.assertEqual(
            self.ingest(member_id=INSTANCE.self_id),
            IngestOutcome.REJECTED_SELF,
        )

    def test_empty_and_whitespace_text_are_rejected(self):
        for text in ("", "   ", "\n\t "):
            with self.subTest(text=text):
                self.assertEqual(self.ingest(text), IngestOutcome.REJECTED_EMPTY)

    def test_text_length_limit_is_inclusive(self):
        self.assertEqual(self.ingest("好" * MAX_TEXT_LENGTH), IngestOutcome.STORED)
        self.assertEqual(
            self.ingest("好" * (MAX_TEXT_LENGTH + 1), message_id="m2"),
            IngestOutcome.REJECTED_TOO_LONG,
        )
        self.assertEqual(self.buffer.stats().entries, 1)

    def test_commands_are_rejected(self):
        for text in ("千鹤 状态", "上下文 退出", "记忆 删除 3"):
            with self.subTest(text=text):
                self.assertEqual(self.ingest(text), IngestOutcome.REJECTED_COMMAND)

    def test_sensitive_texts_are_rejected_entirely(self):
        cases = (
            "我的手机号是13800138000",
            "打款到 6222021234567890 这个卡",
            "邮箱是 someone@example.com",
            "token 是 sk-abcdefgh1234",
            "验证码是 1234",
            "这是我的密码",
            "身份证号发你",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(is_sensitive(text))
                self.assertEqual(self.ingest(text), IngestOutcome.REJECTED_SENSITIVE)

    def test_near_miss_texts_are_stored(self):
        for text in ("订单号1234567890", "千鹤好可爱", "考试得了100分"):
            with self.subTest(text=text):
                self.assertFalse(is_sensitive(text))
        self.assertEqual(self.ingest("订单号1234567890"), IngestOutcome.STORED)
        self.assertEqual(self.ingest("千鹤好可爱", message_id="m2"), IngestOutcome.STORED)

    def test_duplicate_message_id_is_rejected_within_group(self):
        self.assertEqual(self.ingest(message_id="same"), IngestOutcome.STORED)
        self.assertEqual(
            self.ingest("另一条", message_id="same"),
            IngestOutcome.REJECTED_DUPLICATE,
        )
        self.assertEqual(self.buffer.stats().entries, 1)

    def test_same_message_id_in_other_group_is_accepted(self):
        self.assertEqual(self.ingest(message_id="same"), IngestOutcome.STORED)
        self.assertEqual(
            self.ingest(message_id="same", group_id="20002"),
            IngestOutcome.STORED,
        )

    def test_member_must_belong_to_the_group(self):
        with self.assertRaises(ValueError):
            self.buffer.ingest(
                group=make_group("20001"),
                member=make_member("30001", "20002"),
                message_id="m1",
                text="你好",
                shape=BufferShape.TEXT_ONLY,
            )

    def test_message_id_must_be_a_non_empty_string(self):
        for value in ("", None, 7):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.buffer.ingest(
                        group=make_group(),
                        member=make_member(),
                        message_id=value,
                        text="你好",
                        shape=BufferShape.TEXT_ONLY,
                    )


class BoundsTests(BufferTestCase):
    def test_capacity_is_strict(self):
        buffer = ContextBuffer(max_messages=3, ttl_seconds=600.0, clock=self.clock)
        for index in range(4):
            self.assertEqual(
                buffer.ingest(
                    group=make_group(),
                    member=make_member(),
                    message_id=f"m{index}",
                    text="消息",
                    shape=BufferShape.TEXT_ONLY,
                ),
                IngestOutcome.STORED,
            )
        entries = buffer.entries(make_group())
        self.assertEqual([entry.message_id for entry in entries], ["m1", "m2", "m3"])
        self.assertEqual(buffer.stats().evicted_capacity, 1)

    def test_ttl_is_strict(self):
        self.ingest()
        self.clock.advance(599)
        self.assertEqual(len(self.buffer.entries(make_group())), 1)
        self.clock.advance(1)
        self.assertEqual(self.buffer.entries(make_group()), ())
        self.assertEqual(self.buffer.stats().evicted_expired, 1)
        self.assertEqual(self.buffer.stats().groups, 0)

    def test_capacity_eviction_also_expires_stale_entries(self):
        buffer = ContextBuffer(max_messages=2, ttl_seconds=100.0, clock=self.clock)
        buffer.ingest(
            group=make_group(),
            member=make_member(),
            message_id="old",
            text="旧消息",
            shape=BufferShape.TEXT_ONLY,
        )
        self.clock.advance(101)
        buffer.ingest(
            group=make_group(),
            member=make_member(),
            message_id="new",
            text="新消息",
            shape=BufferShape.TEXT_ONLY,
        )
        entries = buffer.entries(make_group())
        self.assertEqual([entry.message_id for entry in entries], ["new"])

    def test_group_isolation(self):
        self.ingest("甲群消息", message_id="a")
        self.ingest("乙群消息", message_id="b", group_id="20002")
        self.assertEqual(
            [entry.text for entry in self.buffer.entries(make_group("20001"))],
            ["甲群消息"],
        )
        self.assertEqual(
            [entry.text for entry in self.buffer.entries(make_group("20002"))],
            ["乙群消息"],
        )


class ClearTests(BufferTestCase):
    def test_clear_group_only_clears_that_group(self):
        self.ingest(message_id="a")
        self.ingest(message_id="b", group_id="20002")
        self.assertEqual(self.buffer.clear_group(make_group("20001")), 1)
        self.assertEqual(self.buffer.entries(make_group("20001")), ())
        self.assertEqual(len(self.buffer.entries(make_group("20002"))), 1)

    def test_clear_member_removes_only_their_entries(self):
        self.ingest("甲说", message_id="a", member_id="30001")
        self.ingest("乙说", message_id="b", member_id="30002")
        self.ingest("甲又说", message_id="c", member_id="30001")
        self.assertEqual(self.buffer.clear_member(make_member("30001")), 2)
        entries = self.buffer.entries(make_group())
        self.assertEqual([entry.text for entry in entries], ["乙说"])

    def test_clear_member_keeps_other_groups_untouched(self):
        self.ingest("甲群", message_id="a", member_id="30001")
        self.ingest("乙群", message_id="b", member_id="30001", group_id="20002")
        self.assertEqual(self.buffer.clear_member(make_member("30001", "20001")), 1)
        self.assertEqual(len(self.buffer.entries(make_group("20002"))), 1)

    def test_clear_missing_member_returns_zero(self):
        self.assertEqual(self.buffer.clear_member(make_member("39999")), 0)

    def test_clear_all_drops_every_group(self):
        self.ingest(message_id="a")
        self.ingest(message_id="b", group_id="20002")
        self.buffer.clear_all()
        self.assertEqual(self.buffer.stats().groups, 0)
        self.assertEqual(self.buffer.entries(make_group()), ())


class StatsTests(BufferTestCase):
    def test_rejected_entries_do_not_create_group_state(self):
        self.ingest("", message_id="empty")
        self.ingest(shape=BufferShape.HAS_MENTION, message_id="mention")
        stats = self.buffer.stats()
        self.assertEqual(stats.groups, 0)
        self.assertEqual(stats.entries, 0)

    def test_stats_report_evictions(self):
        buffer = ContextBuffer(max_messages=1, ttl_seconds=10.0, clock=self.clock)
        for index in range(3):
            buffer.ingest(
                group=make_group(),
                member=make_member(),
                message_id=f"m{index}",
                text="消息",
                shape=BufferShape.TEXT_ONLY,
            )
        self.clock.advance(10)
        stats = buffer.stats()
        self.assertEqual(stats.entries, 0)
        self.assertEqual(stats.evicted_capacity, 2)
        self.assertEqual(stats.evicted_expired, 1)


class ConstructorTests(unittest.TestCase):
    def test_rejects_invalid_limits(self):
        clock = FakeClock()
        for kwargs in (
            {"max_messages": 0, "ttl_seconds": 600.0},
            {"max_messages": -1, "ttl_seconds": 600.0},
            {"max_messages": True, "ttl_seconds": 600.0},
            {"max_messages": 30, "ttl_seconds": 0},
            {"max_messages": 30, "ttl_seconds": -1.0},
            {"max_messages": 30, "ttl_seconds": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ContextBuffer(clock=clock, **kwargs)


class StructuralTests(unittest.TestCase):
    def test_buffer_module_has_no_framework_or_asyncio_dependency(self):
        tree = ast.parse((PLUGIN_ROOT / "context_buffer.py").read_text(encoding="utf-8"))
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
        self.assertNotIn("asyncio", imports)
        self.assertNotIn("astrbot", imports)
        awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
        self.assertEqual(awaits, [])

    def test_entry_text_stays_out_of_repr(self):
        entry = BufferEntry(member_id="30001", message_id="m1", text="不该出现的正文", at=1.0)
        self.assertNotIn("不该出现的正文", repr(entry))


if __name__ == "__main__":
    unittest.main()
