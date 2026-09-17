import unittest
from dataclasses import FrozenInstanceError

from astrbot_plugin_chizuru.keys import (
    BotInstanceKey,
    GroupKey,
    MemberKey,
    RevisionSnapshot,
)

INSTANCE = BotInstanceKey("qq-local", "10001")
GROUP = GroupKey(INSTANCE, "20001")
MEMBER = MemberKey(GROUP, "30001")


class BotInstanceKeyTests(unittest.TestCase):
    def test_requires_both_parts(self):
        for platform_id, self_id in (("", "10001"), ("qq-local", ""), ("", "")):
            with self.subTest(platform_id=platform_id, self_id=self_id):
                with self.assertRaises(ValueError):
                    BotInstanceKey(platform_id, self_id)

    def test_each_part_changes_identity(self):
        self.assertNotEqual(BotInstanceKey("qq-local", "10001"), BotInstanceKey("other", "10001"))
        self.assertNotEqual(BotInstanceKey("qq-local", "10001"), BotInstanceKey("qq-local", "10002"))


class GroupKeyTests(unittest.TestCase):
    def test_requires_group_id(self):
        with self.assertRaises(ValueError):
            GroupKey(INSTANCE, "")

    def test_same_group_id_on_different_instances_is_different(self):
        other = GroupKey(BotInstanceKey("qq-local", "10002"), "20001")
        self.assertNotEqual(GROUP, other)
        self.assertNotEqual(hash(GROUP), hash(other))

    def test_repr_does_not_carry_message_text(self):
        self.assertNotIn("你好", repr(MEMBER))


class MemberKeyTests(unittest.TestCase):
    def test_requires_member_id(self):
        with self.assertRaises(ValueError):
            MemberKey(GROUP, "")

    def test_every_scope_participates(self):
        key = MEMBER
        variants = (
            MemberKey(GroupKey(BotInstanceKey("other", "10001"), "20001"), "30001"),
            MemberKey(GroupKey(BotInstanceKey("qq-local", "10002"), "20001"), "30001"),
            MemberKey(GroupKey(INSTANCE, "20002"), "30001"),
            MemberKey(GROUP, "30002"),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(variant, key)

    def test_instance_is_reachable(self):
        self.assertEqual(MEMBER.instance, INSTANCE)


class RevisionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = RevisionSnapshot(group_revision=3, member_revision=7)

    def test_matches_when_unchanged(self):
        self.assertTrue(self.snapshot.matches(3, 7))

    def test_group_revision_change_invalidates(self):
        self.assertFalse(self.snapshot.matches(4, 7))

    def test_member_revision_change_invalidates(self):
        self.assertFalse(self.snapshot.matches(3, 8))

    def test_both_changed_invalidates(self):
        self.assertFalse(self.snapshot.matches(0, 0))

    def test_snapshot_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.snapshot.group_revision = 4


if __name__ == "__main__":
    unittest.main()
