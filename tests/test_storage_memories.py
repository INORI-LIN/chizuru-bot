"""S3-01/S3-02 低敏事实仓储离线测试：授权、上限与保留期、人工纠正优先、来源去重。"""

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey, MemberKey
from astrbot_plugin_chizuru.storage import (
    ORIGIN_AUTO,
    ORIGIN_MANUAL,
    MemoryLimits,
    PolicyRefused,
    open_storage,
)

ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = ROOT / ".runtime"
INSTANCE = BotInstanceKey("qq-local", "10001")
AUTH_VERSION = "auth-1"
DAY = 86_400
LIMITS = MemoryLimits(max_records=3, ttl_seconds=90 * DAY)


class EpochClock:
    def __init__(self, now: int = 1_700_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


class MemoryStoreTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="storage-memories-", dir=WORK_ROOT)
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.db_path = self.dir / "chizuru.db"
        self.clock = EpochClock()
        self.storage = open_storage(self.db_path, clock=self.clock)
        self.addCleanup(self.storage.close)

    def group(self, group_id: str = "20001", self_id: str = "10001") -> GroupKey:
        return GroupKey(BotInstanceKey(INSTANCE.platform_id, self_id), group_id)

    def member(self, member_id: str = "30001", group_id: str = "20001") -> MemberKey:
        return MemberKey(self.group(group_id), member_id)

    def authorize(self, member: MemberKey | None = None, version: str = AUTH_VERSION) -> None:
        self.storage.memories.set_authorized(
            member or self.member(), authorized=True, auth_version=version
        )

    def write(
        self,
        facts: list[tuple[str, str]],
        *,
        member: MemberKey | None = None,
        source: str = "m-1",
        limits: MemoryLimits = LIMITS,
    ):
        return self.storage.memories.record_auto_facts(
            member or self.member(),
            facts=facts,
            source_message_id=source,
            limits=limits,
        )


class BaselineTests(MemoryStoreTestCase):
    def test_absent_state_is_unauthorized(self):
        state = self.storage.memories.state(self.member())
        self.assertFalse(state.authorized)
        self.assertEqual(state.auth_version, "")
        self.assertEqual(state.next_record_id, 1)

    def test_reads_do_not_create_the_database(self):
        self.storage.memories.state(self.member())
        self.storage.memories.facts(self.member())
        self.assertFalse(self.db_path.exists())

    def test_unauthorized_write_is_refused_and_leaves_nothing_behind(self):
        with self.assertRaises(PolicyRefused):
            self.write([("称呼", "小林")])
        self.assertEqual(self.storage.memories.facts(self.member()), ())
        self.assertEqual(self.storage.members.revision(self.member()), 0)


class AuthorizationTests(MemoryStoreTestCase):
    def test_authorizing_bumps_the_member_revision(self):
        transition = self.storage.memories.set_authorized(
            self.member(), authorized=True, auth_version=AUTH_VERSION
        )
        self.assertTrue(transition.changed)
        self.assertEqual(transition.revision, 1)
        self.assertTrue(transition.state.authorized)
        self.assertEqual(self.storage.members.revision(self.member()), 1)

    def test_repeating_the_same_authorization_is_idempotent(self):
        self.authorize()
        again = self.storage.memories.set_authorized(
            self.member(), authorized=True, auth_version=AUTH_VERSION
        )
        self.assertFalse(again.changed)
        self.assertEqual(again.revision, 1)

    def test_a_new_authorization_version_is_recorded(self):
        self.authorize()
        changed = self.storage.memories.set_authorized(
            self.member(), authorized=True, auth_version="auth-2"
        )
        self.assertTrue(changed.changed)
        self.assertEqual(changed.revision, 2)
        self.assertEqual(changed.state.auth_version, "auth-2")

    def test_revoking_clears_the_version_and_bumps_again(self):
        self.authorize()
        revoked = self.storage.memories.set_authorized(
            self.member(), authorized=False, auth_version=AUTH_VERSION
        )
        self.assertTrue(revoked.changed)
        self.assertFalse(revoked.state.authorized)
        self.assertEqual(revoked.state.auth_version, "")
        self.assertEqual(revoked.revision, 2)
        # 撤回是幂等的。
        again = self.storage.memories.set_authorized(
            self.member(), authorized=False, auth_version=AUTH_VERSION
        )
        self.assertFalse(again.changed)
        self.assertEqual(again.revision, 2)

    def test_revoking_does_not_delete_facts_by_itself(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.storage.memories.set_authorized(
            self.member(), authorized=False, auth_version=AUTH_VERSION
        )
        # "关闭是否连动删除"属命令语义（S3-04），仓储层只改授权位。
        self.assertEqual(len(self.storage.memories.facts(self.member())), 1)

    def test_authorization_version_must_be_plain_text(self):
        for value in ("", "  ", "auth-1 "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.storage.memories.set_authorized(
                        self.member(), authorized=True, auth_version=value
                    )

    def test_context_opt_out_does_not_touch_memory_authorization(self):
        self.authorize()
        self.storage.members.opt_out(self.member())
        state = self.storage.memories.state(self.member())
        self.assertTrue(state.authorized)
        self.assertEqual(state.auth_version, AUTH_VERSION)


class WriteTests(MemoryStoreTestCase):
    def test_writes_are_numbered_from_one_and_counted(self):
        self.authorize()
        written = self.write([("称呼", "小林"), ("兴趣", "看番")])
        self.assertEqual([fact.record_id for fact in written], [1, 2])
        self.assertEqual([fact.origin for fact in written], [ORIGIN_AUTO, ORIGIN_AUTO])
        self.assertEqual(len(self.storage.memories.facts(self.member())), 2)

    def test_a_write_bumps_the_member_revision_once(self):
        self.authorize()
        self.write([("称呼", "小林"), ("兴趣", "看番")])
        self.assertEqual(self.storage.members.revision(self.member()), 2)

    def test_writing_nothing_does_not_bump_the_revision(self):
        self.authorize()
        self.write([("称呼", "小林")])
        before = self.storage.members.revision(self.member())
        self.write([("称呼", "小林")])  # 同类别同内容，全部跳过
        self.assertEqual(self.storage.members.revision(self.member()), before)

    def test_cap_refuses_extra_candidates_without_evicting_records(self):
        self.authorize()
        written = self.write(
            [("称呼", "小林"), ("兴趣", "看番"), ("活动", "看电影"), ("口味", "香菜")]
        )
        self.assertEqual([fact.record_id for fact in written], [1, 2, 3])
        kept = [(fact.record_id, fact.category) for fact in self.storage.memories.facts(self.member())]
        self.assertEqual(kept, [(1, "称呼"), (2, "兴趣"), (3, "活动")])

    def test_identical_candidate_is_not_written_twice(self):
        self.authorize()
        written = self.write([("称呼", "小林"), ("称呼", "小林")])
        self.assertEqual(len(written), 1)

    def test_manual_category_blocks_later_automatic_writes(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.storage.memories.correct(self.member(), 1, content="林同学")
        self.assertEqual(self.write([("称呼", "小千")]), ())
        facts = self.storage.memories.facts(self.member())
        self.assertEqual(
            [(fact.record_id, fact.content, fact.origin) for fact in facts],
            [(1, "林同学", ORIGIN_MANUAL)],
        )

    def test_record_ids_are_not_reused_after_clear(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.storage.memories.clear(self.member())
        written = self.write([("称呼", "小林")])
        self.assertEqual([fact.record_id for fact in written], [2])

    def test_candidates_must_be_plain_text(self):
        self.authorize()
        with self.assertRaises(ValueError):
            self.write([("称呼", " ")])
        with self.assertRaises(ValueError):
            self.write([("", "小林")])


class ExpiryTests(MemoryStoreTestCase):
    def test_expired_facts_are_not_returned(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.clock.advance(89 * DAY)
        self.assertEqual(len(self.storage.memories.facts(self.member())), 1)
        self.clock.advance(2 * DAY)
        self.assertEqual(self.storage.memories.facts(self.member()), ())

    def test_reading_does_not_extend_the_expiry(self):
        self.authorize()
        (fact,) = self.write([("称呼", "小林")])
        self.clock.advance(89 * DAY)
        (seen,) = self.storage.memories.facts(self.member())
        self.assertEqual(seen.expires_at, fact.expires_at)

    def test_expired_rows_are_purged_on_the_next_write(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.clock.advance(91 * DAY)
        self.write([("兴趣", "看番")], source="m-2")
        with self.storage.database.read() as connection:
            categories = [
                row["category"]
                for row in connection.execute("SELECT category FROM memory_fact ORDER BY record_id")
            ]
        self.assertEqual(categories, ["兴趣"])

    def test_authorization_survives_expiry(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.clock.advance(91 * DAY)
        self.assertTrue(self.storage.memories.state(self.member()).authorized)

    def test_correct_and_delete_treat_expired_records_as_absent(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.clock.advance(91 * DAY)
        self.assertIsNone(self.storage.memories.correct(self.member(), 1, content="林同学"))
        self.assertFalse(self.storage.memories.delete(self.member(), 1))


class CorrectionTests(MemoryStoreTestCase):
    def test_correct_marks_manual_and_keeps_the_expiry(self):
        self.authorize()
        (fact,) = self.write([("称呼", "小林")])
        corrected = self.storage.memories.correct(self.member(), 1, content="林同学")
        self.assertEqual(corrected.content, "林同学")
        self.assertEqual(corrected.origin, ORIGIN_MANUAL)
        self.assertEqual(corrected.expires_at, fact.expires_at)
        self.assertEqual(corrected.record_id, 1)

    def test_correct_bumps_the_member_revision(self):
        self.authorize()
        self.write([("称呼", "小林")])
        before = self.storage.members.revision(self.member())
        self.storage.memories.correct(self.member(), 1, content="林同学")
        self.assertEqual(self.storage.members.revision(self.member()), before + 1)

    def test_correct_of_an_unknown_record_returns_none(self):
        self.authorize()
        self.assertIsNone(self.storage.memories.correct(self.member(), 7, content="林同学"))

    def test_delete_bumps_the_revision_only_on_success(self):
        self.authorize()
        self.write([("称呼", "小林")])
        before = self.storage.members.revision(self.member())
        self.assertTrue(self.storage.memories.delete(self.member(), 1))
        self.assertEqual(self.storage.members.revision(self.member()), before + 1)
        self.assertFalse(self.storage.memories.delete(self.member(), 1))
        self.assertEqual(self.storage.members.revision(self.member()), before + 1)

    def test_clear_removes_everything_and_is_idempotent(self):
        self.authorize()
        self.write([("称呼", "小林"), ("兴趣", "看番")])
        self.assertEqual(self.storage.memories.clear(self.member()), 2)
        self.assertEqual(self.storage.memories.clear(self.member()), 0)
        self.assertEqual(self.storage.memories.facts(self.member()), ())

    def test_reinitializing_after_clear_does_not_resurrect_rows(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.storage.memories.clear(self.member())
        for _ in range(3):
            with self.storage.database.transaction() as connection:
                connection.execute("SELECT 1")
        self.assertEqual(self.storage.memories.facts(self.member()), ())
        # 清空记录不等于撤回授权：授权位由 set_authorized 单独管。
        self.assertTrue(self.storage.memories.state(self.member()).authorized)

    def test_record_id_must_be_a_positive_integer(self):
        self.authorize()
        for value in (0, -1, True, "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.storage.memories.correct(self.member(), value, content="林同学")


class SourceTests(MemoryStoreTestCase):
    def remember(self, *, member: MemberKey | None = None, source: str = "m-1") -> bool:
        return self.storage.memories.remember_source(
            member or self.member(),
            source_message_id=source,
            action="extract",
            retention_seconds=600,
        )

    def test_a_source_is_registered_once(self):
        self.assertTrue(self.remember())
        self.assertFalse(self.remember())
        self.assertTrue(self.remember(source="m-2"))

    def test_deleting_facts_does_not_free_the_source(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.assertTrue(self.remember())
        self.storage.memories.clear(self.member())
        # 登记活的比事实久：同一条消息再次投递不能把刚删掉的记录抽回来。
        self.assertFalse(self.remember())

    def test_source_registration_is_per_member(self):
        self.assertTrue(self.remember())
        self.assertTrue(self.remember(member=self.member("30002")))

    def test_source_registration_is_per_group(self):
        self.assertTrue(self.remember())
        self.assertTrue(self.remember(member=self.member("30001", "20002")))

    def test_old_registrations_expire_with_the_injected_window(self):
        self.assertTrue(self.remember())
        self.clock.advance(601)
        self.assertTrue(self.remember())

    def test_source_and_action_must_be_plain_text(self):
        for value in ("", " m-1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.storage.memories.remember_source(
                        self.member(),
                        source_message_id=value,
                        action="extract",
                        retention_seconds=600,
                    )


class IsolationTests(MemoryStoreTestCase):
    def test_groups_do_not_leak_into_each_other(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.assertEqual(self.storage.memories.facts(self.member("30001", "20002")), ())
        self.assertFalse(self.storage.memories.state(self.member("30001", "20002")).authorized)

    def test_bot_instances_do_not_leak_into_each_other(self):
        self.authorize()
        self.write([("称呼", "小林")])
        other = MemberKey(self.group("20001", self_id="10002"), "30001")
        self.assertEqual(self.storage.memories.facts(other), ())
        self.assertFalse(self.storage.memories.state(other).authorized)

    def test_other_members_facts_are_invisible(self):
        self.authorize()
        self.write([("称呼", "小林")])
        self.assertEqual(self.storage.memories.facts(self.member("30002")), ())


class LimitsTests(unittest.TestCase):
    def test_limits_require_positive_bounded_values(self):
        for max_records, ttl_seconds in ((0, 10), (10, 0), (True, 10), (10, "10")):
            with self.subTest(max_records=max_records, ttl_seconds=ttl_seconds):
                with self.assertRaises(ValueError):
                    MemoryLimits(max_records=max_records, ttl_seconds=ttl_seconds)


class StructuralTests(unittest.TestCase):
    def columns(self, table: str) -> set[str]:
        database = None
        try:
            with tempfile.TemporaryDirectory(prefix="storage-structure-", dir=WORK_ROOT) as tmp:
                path = Path(tmp) / "chizuru.db"
                database = open_storage(path, clock=lambda: 0)
                with database.database.transaction() as connection:
                    connection.execute("SELECT 1")
                with database.database.read() as connection:
                    return {
                        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                    }
        finally:
            if database is not None:
                database.close()

    def test_state_and_source_tables_carry_no_chat_body(self):
        for table in ("memory_state", "memory_source"):
            with self.subTest(table=table):
                self.assertNotIn("content", self.columns(table))

    def test_state_table_has_no_second_revision_counter(self):
        # 成员修订号复用 member_state.revision，本表不得再放一个计数器。
        self.assertNotIn("revision", self.columns("memory_state"))

    def test_fact_table_keeps_the_documented_columns(self):
        columns = self.columns("memory_fact")
        for name in ("record_id", "category", "content", "origin", "source_message_id", "expires_at"):
            with self.subTest(column=name):
                self.assertIn(name, columns)


if __name__ == "__main__":
    unittest.main()
