"""S2-04 成员上下文状态离线测试：退出持久化、加入前置、修订号与结构边界。"""

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey, MemberKey
from astrbot_plugin_chizuru.storage import PolicyRefused, StorageFailure, open_storage

ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = ROOT / ".runtime"
INSTANCE = BotInstanceKey("qq-local", "10001")
NOTICE = "notice-1"


class EpochClock:
    def __init__(self, now: int = 1_700_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


class MemberStoreTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="storage-members-", dir=WORK_ROOT)
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.db_path = self.dir / "chizuru.db"
        self.clock = EpochClock()
        self.storage = open_storage(self.db_path, clock=self.clock)
        self.addCleanup(self.storage.close)

    def group(self, group_id: str = "20001") -> GroupKey:
        return GroupKey(INSTANCE, group_id)

    def member(self, member_id: str = "30001", group_id: str = "20001") -> MemberKey:
        return MemberKey(self.group(group_id), member_id)

    def open_group(self, group_id: str = "20001") -> None:
        self.storage.groups.record_notice_confirmed(
            self.group(group_id), version=NOTICE, actor_id="30001"
        )


class BaselineTests(MemberStoreTestCase):
    def test_absent_state_is_not_opted_out(self):
        state = self.storage.members.state(self.member())
        self.assertFalse(state.opted_out)
        self.assertEqual(state.revision, 0)

    def test_reads_do_not_create_the_database(self):
        self.storage.members.state(self.member())
        self.storage.members.revision(self.member())
        self.assertFalse(self.db_path.exists())


class OptOutTests(MemberStoreTestCase):
    def test_opt_out_persists_across_reopen(self):
        transition = self.storage.members.opt_out(self.member())
        self.assertTrue(transition.changed)
        self.assertTrue(transition.state.opted_out)
        # 重启不复活：关闭后重新打开同一路径，退出状态仍在。
        self.storage.close()
        reopened = open_storage(self.db_path, clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.members.state(self.member()).opted_out)

    def test_opt_out_bumps_member_and_group_revision(self):
        transition = self.storage.members.opt_out(self.member())
        self.assertEqual(transition.state.revision, 1)
        self.assertEqual(transition.group_revision, 1)
        self.assertEqual(self.storage.members.revision(self.member()), 1)
        self.assertEqual(self.storage.groups.revision(self.group()), 1)

    def test_repeated_opt_out_is_idempotent(self):
        first = self.storage.members.opt_out(self.member())
        second = self.storage.members.opt_out(self.member())
        self.assertFalse(second.changed)
        self.assertEqual(second.state.revision, first.state.revision)
        self.assertEqual(second.group_revision, first.group_revision)

    def test_opt_out_is_per_member_and_per_group(self):
        self.storage.members.opt_out(self.member("30001"))
        self.assertFalse(self.storage.members.state(self.member("30002")).opted_out)
        self.assertFalse(self.storage.members.state(self.member("30001", "20099")).opted_out)

    def test_opt_out_requires_no_group_policy_but_records_one(self):
        self.storage.members.opt_out(self.member())
        policy = self.storage.groups.policy(self.group())
        self.assertTrue(policy.exists)
        self.assertFalse(policy.context_enabled)
        self.assertFalse(policy.is_collection_open(required_notice_version=NOTICE))


class OptInTests(MemberStoreTestCase):
    def test_opt_in_without_open_group_is_refused(self):
        self.storage.members.opt_out(self.member())
        with self.assertRaises(PolicyRefused):
            self.storage.members.opt_in(self.member(), required_notice_version=NOTICE)
        self.assertTrue(self.storage.members.state(self.member()).opted_out)

    def test_opt_in_with_mismatched_notice_version_is_refused(self):
        self.open_group()
        self.storage.members.opt_out(self.member())
        with self.assertRaises(PolicyRefused):
            self.storage.members.opt_in(self.member(), required_notice_version="notice-2")

    def test_opt_in_restores_and_keeps_group_revision(self):
        self.open_group()
        self.storage.members.opt_out(self.member())
        group_revision_before = self.storage.groups.revision(self.group())
        transition = self.storage.members.opt_in(self.member(), required_notice_version=NOTICE)
        self.assertTrue(transition.changed)
        self.assertFalse(transition.state.opted_out)
        self.assertEqual(transition.state.revision, 2)
        self.assertEqual(transition.group_revision, group_revision_before)
        self.assertEqual(self.storage.groups.revision(self.group()), group_revision_before)

    def test_opt_in_is_idempotent(self):
        self.open_group()
        first = self.storage.members.opt_in(self.member(), required_notice_version=NOTICE)
        self.assertFalse(first.changed)
        self.assertEqual(first.state.revision, 0)

    def test_paused_group_cannot_be_joined(self):
        self.open_group()
        self.storage.members.opt_out(self.member())
        self.storage.groups.set_paused(self.group(), paused=True)
        with self.assertRaises(PolicyRefused):
            self.storage.members.opt_in(self.member(), required_notice_version=NOTICE)

    def test_join_applies_to_one_group_only(self):
        self.open_group()
        self.open_group("20002")
        self.storage.members.opt_out(self.member("30001"))
        self.storage.members.opt_out(self.member("30001", "20002"))
        self.storage.members.opt_in(self.member("30001"), required_notice_version=NOTICE)
        self.assertFalse(self.storage.members.state(self.member("30001")).opted_out)
        self.assertTrue(self.storage.members.state(self.member("30001", "20002")).opted_out)


class StructuralTests(MemberStoreTestCase):
    def test_schema_has_no_memory_authorization_columns(self):
        # 先触发一次写入建库，再检查结构。
        self.storage.members.opt_out(self.member())
        with self.storage.database.read() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(member_state)")
            }
            group_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(group_policy)")
            }
        self.assertEqual(tables, {"group_policy", "member_state"})
        forbidden = {"memory", "authorized", "grant", "consent", "allowed"}
        self.assertFalse(forbidden & columns)
        self.assertFalse(forbidden & group_columns)

    def test_opt_out_does_not_change_group_collection_flag(self):
        self.open_group()
        self.storage.members.opt_out(self.member())
        policy = self.storage.groups.policy(self.group())
        self.assertTrue(policy.context_enabled)
        self.assertTrue(policy.is_collection_open(required_notice_version=NOTICE))


class FailureTests(MemberStoreTestCase):
    def test_closed_storage_refuses_opt_out(self):
        self.storage.close()
        with self.assertRaises(StorageFailure):
            self.storage.members.opt_out(self.member())
        # 未产生任何部分写入：重新打开后该成员仍是未退出。
        reopened = open_storage(self.db_path, clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.members.state(self.member()).opted_out)


if __name__ == "__main__":
    unittest.main()
