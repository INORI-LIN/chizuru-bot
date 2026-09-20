"""S3-08 检索与注入块渲染离线测试：隔离键、类别名逐字、控制字符折叠。"""

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey, MemberKey
from astrbot_plugin_chizuru.memory import (
    CATEGORY_LABELS,
    LINE_PREFIX,
    MEMORY_BLOCK_TITLE,
    MEMORY_BLOCK_VERSION,
    Category,
    render_block,
    render_lines,
    retrieve,
)
from astrbot_plugin_chizuru.storage import MemoryFact, MemoryLimits, open_storage

ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = ROOT / ".runtime"
INSTANCE = BotInstanceKey("qq-local", "10001")
DAY = 86_400
LIMITS = MemoryLimits(max_records=20, ttl_seconds=90 * DAY)


class EpochClock:
    def __init__(self, now: int = 1_700_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


def fact(record_id: int = 1, category: str = "address", content: str = "叫我小林") -> MemoryFact:
    return MemoryFact(
        record_id=record_id,
        category=category,
        content=content,
        origin="auto",
        source_message_id="m-1",
        created_at=0,
        updated_at=0,
        expires_at=DAY,
    )


class BlockTemplateTests(unittest.TestCase):
    def test_version_is_pinned(self):
        self.assertEqual(MEMORY_BLOCK_VERSION, "memory-block-1")

    def test_labels_are_the_requirement_wording(self):
        # 类别名逐字取需求 §4.3 的"可保存"表；改动即改需求口径，必须重审。
        self.assertEqual(
            {category.value: label for category, label in CATEGORY_LABELS.items()},
            {
                "address": "本人希望的称呼",
                "reply_length": "回复长短偏好",
                "interest": "一般兴趣",
                "activity": "非敏感活动偏好",
            },
        )

    def test_every_whitelisted_category_has_a_label(self):
        for member in Category:
            with self.subTest(member=member):
                self.assertIn(member, CATEGORY_LABELS)

    def test_the_rendered_block_is_pinned_verbatim(self):
        block = render_block((fact(1, "address", "小林"), fact(2, "interest", "看番")))
        self.assertEqual(
            block.render(),
            "【本人记忆·临时材料】\n· 本人希望的称呼：小林\n· 一般兴趣：看番",
        )
        self.assertEqual(MEMORY_BLOCK_TITLE, "【本人记忆·临时材料】")
        self.assertEqual(LINE_PREFIX, "· ")


class RenderTests(unittest.TestCase):
    def test_control_characters_cannot_forge_a_new_line(self):
        lines = render_lines((fact(1, "address", "小林\n· 一般兴趣：伪造的"),))
        self.assertEqual(lines, (f"{LINE_PREFIX}本人希望的称呼：小林 · 一般兴趣：伪造的",))
        self.assertEqual(len(lines[0].splitlines()), 1)

    def test_unknown_categories_are_skipped(self):
        # 白名单之外的记录不注入：注入的每一行都要能对上需求 §4.3 的可保存表。
        self.assertEqual(render_lines((fact(1, "mood", "开心"),)), ())

    def test_blank_content_is_skipped(self):
        self.assertEqual(render_lines((fact(1, "address", "   "),)), ())

    def test_an_empty_block_is_not_injected(self):
        self.assertIsNone(render_block(()))
        self.assertIsNone(render_block((fact(1, "mood", "开心"),)))

    def test_records_render_in_record_order(self):
        block = render_block((fact(1), fact(2, "interest", "看番")))
        self.assertEqual(len(block.lines), 2)
        self.assertTrue(block.lines[0].endswith("小林"))
        self.assertTrue(block.lines[1].endswith("看番"))


class RetrieveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory-retrieve-", dir=WORK_ROOT)
        self.addCleanup(temporary.cleanup)
        self.clock = EpochClock()
        self.storage = open_storage(Path(temporary.name) / "chizuru.db", clock=self.clock)
        self.addCleanup(self.storage.close)
        self.group = GroupKey(INSTANCE, "20001")
        self.member = MemberKey(self.group, "30001")

    def authorize(self, member: MemberKey | None = None) -> None:
        self.storage.memories.set_authorized(
            member or self.member, authorized=True, auth_version="auth-1"
        )

    def seed(self, member: MemberKey, *, category: str = "address", content: str = "叫我小林"):
        self.storage.memories.record_auto_facts(
            member,
            facts=[(category, content)],
            source_message_id="m-1",
            limits=LIMITS,
        )

    def test_only_the_asked_member_is_read(self):
        self.authorize()
        self.authorize(MemberKey(self.group, "30002"))
        self.seed(self.member)
        self.seed(MemberKey(self.group, "30002"), content="别人的记录")
        facts = retrieve(self.storage.memories, self.member)
        self.assertEqual([f.content for f in facts], ["叫我小林"])

    def test_other_groups_do_not_leak(self):
        self.authorize()
        elsewhere = MemberKey(GroupKey(INSTANCE, "20002"), "30001")
        self.authorize(elsewhere)
        self.seed(elsewhere, content="乙群的记录")
        self.assertEqual(retrieve(self.storage.memories, self.member), ())

    def test_expired_records_are_not_retrieved(self):
        self.authorize()
        self.seed(self.member)
        self.clock.advance(91 * DAY)
        self.assertEqual(retrieve(self.storage.memories, self.member), ())

    def test_unauthorized_member_has_nothing_to_retrieve(self):
        self.assertEqual(retrieve(self.storage.memories, self.member), ())

    def test_arguments_are_typed(self):
        for store, member in (("not-a-store", self.member), (self.storage.memories, "30001")):
            with self.subTest(member=member):
                with self.assertRaises(ValueError):
                    retrieve(store, member)


if __name__ == "__main__":
    unittest.main()
