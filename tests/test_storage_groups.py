"""S2-01 群策略仓储离线测试：无有效策略默认关闭、告知版本、暂停与修订号。"""

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey
from astrbot_plugin_chizuru.storage import PolicyRefused, open_storage

ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = ROOT / ".runtime"
INSTANCE = BotInstanceKey("qq-local", "10001")
NOTICE = "notice-1"


class EpochClock:
    """Unix 秒时钟；存储层的时间戳只用于可追溯，不参与判定。"""

    def __init__(self, now: int = 1_700_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


class PolicyStoreTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="storage-groups-", dir=WORK_ROOT)
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.clock = EpochClock()
        self.storage = open_storage(self.dir / "chizuru.db", clock=self.clock)
        self.addCleanup(self.storage.close)

    def group(
        self,
        *,
        platform_id: str = "qq-local",
        self_id: str = "10001",
        group_id: str = "20001",
    ) -> GroupKey:
        return GroupKey(BotInstanceKey(platform_id, self_id), group_id)


class AbsentPolicyTests(PolicyStoreTestCase):
    def test_no_row_means_closed(self):
        policy = self.storage.groups.policy(self.group())
        self.assertFalse(policy.exists)
        self.assertFalse(policy.context_enabled)
        self.assertFalse(policy.is_collection_open(required_notice_version=NOTICE))
        self.assertEqual(policy.revision, 0)

    def test_reading_does_not_create_rows(self):
        db_path = self.dir / "chizuru.db"
        for _ in range(3):
            self.storage.groups.policy(self.group())
            self.storage.groups.revision(self.group())
        self.assertFalse(db_path.exists())


class NoticeTests(PolicyStoreTestCase):
    def test_confirmation_opens_and_is_traceable(self):
        group = self.group()
        self.clock.advance(60)
        policy = self.storage.groups.record_notice_confirmed(
            group, version=NOTICE, actor_id="30001"
        )
        self.assertTrue(policy.exists)
        self.assertTrue(policy.context_enabled)
        self.assertFalse(policy.paused)
        self.assertEqual(policy.notice_version, NOTICE)
        self.assertEqual(policy.revision, 1)
        self.assertTrue(policy.is_collection_open(required_notice_version=NOTICE))

        with self.storage.database.read() as connection:
            row = connection.execute(
                "SELECT notice_at, notice_by FROM group_policy WHERE group_id = '20001'"
            ).fetchone()
        self.assertEqual(row["notice_at"], self.clock.now)
        self.assertEqual(row["notice_by"], "30001")

    def test_version_mismatch_is_closed(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        policy = self.storage.groups.policy(group)
        self.assertFalse(policy.is_collection_open(required_notice_version="notice-2"))

    def test_reconfirmation_is_idempotent(self):
        group = self.group()
        first = self.storage.groups.record_notice_confirmed(
            group, version=NOTICE, actor_id="30001"
        )
        self.clock.advance(120)
        second = self.storage.groups.record_notice_confirmed(
            group, version=NOTICE, actor_id="30001"
        )
        self.assertEqual(second.revision, first.revision)

    def test_new_version_bumps_revision(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        updated = self.storage.groups.record_notice_confirmed(
            group, version="notice-2", actor_id="30001"
        )
        self.assertEqual(updated.notice_version, "notice-2")
        self.assertEqual(updated.revision, 2)
        self.assertTrue(updated.is_collection_open(required_notice_version="notice-2"))
        self.assertFalse(updated.is_collection_open(required_notice_version=NOTICE))

    def test_key_triple_isolation(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        for other in (
            self.group(platform_id="qq-other"),
            self.group(self_id="10002"),
            self.group(group_id="20002"),
        ):
            with self.subTest(other=other):
                self.assertFalse(self.storage.groups.policy(other).exists)


class PauseTests(PolicyStoreTestCase):
    def test_pause_closes_collection_until_resumed(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        paused = self.storage.groups.set_paused(group, paused=True)
        self.assertTrue(paused.paused)
        self.assertFalse(paused.is_collection_open(required_notice_version=NOTICE))
        self.assertEqual(paused.revision, 2)
        resumed = self.storage.groups.set_paused(group, paused=False)
        self.assertTrue(resumed.is_collection_open(required_notice_version=NOTICE))
        self.assertEqual(resumed.revision, 3)

    def test_pause_is_idempotent(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        first = self.storage.groups.set_paused(group, paused=True)
        second = self.storage.groups.set_paused(group, paused=True)
        self.assertEqual(second.revision, first.revision)

    def test_pause_without_policy_is_refused(self):
        with self.assertRaises(PolicyRefused):
            self.storage.groups.set_paused(self.group(), paused=True)


class EnableDisableTests(PolicyStoreTestCase):
    def test_disable_then_enable_with_matching_notice(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        disabled = self.storage.groups.set_context_enabled(
            group, enabled=False, required_notice_version=NOTICE
        )
        self.assertFalse(disabled.context_enabled)
        self.assertFalse(disabled.is_collection_open(required_notice_version=NOTICE))
        enabled = self.storage.groups.set_context_enabled(
            group, enabled=True, required_notice_version=NOTICE
        )
        self.assertTrue(enabled.context_enabled)
        self.assertTrue(enabled.is_collection_open(required_notice_version=NOTICE))

    def test_enable_without_matching_notice_is_refused_without_partial_write(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        disabled = self.storage.groups.set_context_enabled(
            group, enabled=False, required_notice_version=NOTICE
        )
        with self.assertRaises(PolicyRefused):
            self.storage.groups.set_context_enabled(
                group, enabled=True, required_notice_version="notice-9"
            )
        policy = self.storage.groups.policy(group)
        self.assertFalse(policy.context_enabled)
        self.assertEqual(policy.revision, disabled.revision)

    def test_enable_without_policy_row_is_refused(self):
        with self.assertRaises(PolicyRefused):
            self.storage.groups.set_context_enabled(
                self.group(), enabled=True, required_notice_version=NOTICE
            )

    def test_disable_without_policy_row_is_refused(self):
        with self.assertRaises(PolicyRefused):
            self.storage.groups.set_context_enabled(
                self.group(), enabled=False, required_notice_version=NOTICE
            )


class ValidationTests(PolicyStoreTestCase):
    def test_notice_version_must_be_plain_text(self):
        for value in ("", "  ", "notice-1 ", None, 7):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.storage.groups.record_notice_confirmed(
                        self.group(), version=value, actor_id="30001"
                    )

    def test_actor_must_be_qq_id(self):
        for value in ("", "0", "abc", "0030001", 30001):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.storage.groups.record_notice_confirmed(
                        self.group(), version=NOTICE, actor_id=value
                    )

    def test_flags_must_be_booleans(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        for value in (1, 0, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.storage.groups.set_paused(group, paused=value)
                with self.assertRaises(ValueError):
                    self.storage.groups.set_context_enabled(
                        group, enabled=value, required_notice_version=NOTICE
                    )

    def test_required_notice_version_must_be_plain_text(self):
        group = self.group()
        self.storage.groups.record_notice_confirmed(group, version=NOTICE, actor_id="30001")
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.storage.groups.set_context_enabled(
                        group, enabled=False, required_notice_version=value
                    )


if __name__ == "__main__":
    unittest.main()
