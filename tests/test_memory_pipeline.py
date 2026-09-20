"""S3-07/S3-10 管线离线测试：准入判定、写回重核与来源去重。"""

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.keys import (
    BotInstanceKey,
    GroupKey,
    MemberKey,
    RevisionSnapshot,
)
from astrbot_plugin_chizuru.memory import (
    Admission,
    Candidate,
    Category,
    ExtractionResult,
    ExtractionStatus,
    Refusal,
    WriteOutcome,
    WritePlan,
    WriteResult,
    admit,
    counts_as_failure,
    plan_candidates,
    write_back,
)
from astrbot_plugin_chizuru.redact import ErrorCode
from astrbot_plugin_chizuru.storage import MemoryLimits, open_storage

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


def allowed_admission(**overrides) -> Admission:
    values = {
        "source_text": "叫我小林就好",
        "authorized": True,
        "paused": False,
        "extraction_enabled": True,
        "budget_allows": True,
    }
    values.update(overrides)
    return admit(**values)


class AdmissionTests(unittest.TestCase):
    def test_permission_is_checked_before_the_text(self):
        # 判定顺序：未授权时不触碰正文——拒绝结果里也不带正文。
        admission = allowed_admission(authorized=False, source_text="我的手机号 13800138000")
        self.assertFalse(admission.allowed)
        self.assertIs(admission.reason, Refusal.NOT_AUTHORIZED)
        self.assertEqual(admission.source_text, "")

    def test_paused_group_stops_extraction(self):
        admission = allowed_admission(paused=True)
        self.assertIs(admission.reason, Refusal.GROUP_PAUSED)

    def test_unusable_source_is_refused(self):
        for value in ("", "   ", "我的手机号 13800138000", "x" * 400):
            with self.subTest(value=value[:8]):
                admission = allowed_admission(source_text=value)
                self.assertIs(admission.reason, Refusal.SOURCE_UNUSABLE)

    def test_switch_and_budget_are_separate_gates(self):
        self.assertIs(allowed_admission(extraction_enabled=False).reason, Refusal.EXTRACTION_DISABLED)
        self.assertIs(allowed_admission(budget_allows=False).reason, Refusal.BUDGET_BLOCKED)

    def test_allowed_carries_the_cleaned_source(self):
        admission = allowed_admission(source_text="  叫我小林就好  ")
        self.assertTrue(admission.allowed)
        self.assertIsNone(admission.reason)
        self.assertEqual(admission.source_text, "叫我小林就好")

    def test_source_text_stays_out_of_repr(self):
        self.assertNotIn("小林", repr(allowed_admission()))

    def test_non_boolean_inputs_are_refused(self):
        for name in ("authorized", "paused", "extraction_enabled", "budget_allows"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    allowed_admission(**{name: "yes"})

    def test_invalid_admissions_are_refused(self):
        bad = (
            {"allowed": True},
            {"allowed": True, "reason": Refusal.NOT_AUTHORIZED, "source_text": "x"},
            {"allowed": False},
            {"allowed": False, "reason": Refusal.NOT_AUTHORIZED, "source_text": "x"},
            {"allowed": False, "reason": "not_authorized"},
        )
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Admission(**kwargs)


class PlanCandidateTests(unittest.TestCase):
    def candidate(self) -> Candidate:
        return Candidate(category=Category.ADDRESS, content="叫我小林")

    def test_only_ok_status_is_written_back(self):
        ok = ExtractionResult(status=ExtractionStatus.OK, candidates=(self.candidate(),))
        self.assertEqual(plan_candidates(ok), (("address", "叫我小林"),))
        for result in (
            ExtractionResult(status=ExtractionStatus.OK),
            ExtractionResult(status=ExtractionStatus.EMPTY, code=ErrorCode.EMPTY_REPLY),
            ExtractionResult(status=ExtractionStatus.MALFORMED),
            ExtractionResult(status=ExtractionStatus.FAILED, code=ErrorCode.RATE_LIMITED),
        ):
            with self.subTest(status=result.status):
                self.assertEqual(plan_candidates(result), ())

    def test_failure_counting_covers_unusable_output(self):
        self.assertFalse(counts_as_failure(ExtractionResult(status=ExtractionStatus.OK)))
        for result in (
            ExtractionResult(status=ExtractionStatus.EMPTY, code=ErrorCode.EMPTY_REPLY),
            ExtractionResult(status=ExtractionStatus.MALFORMED),
            ExtractionResult(status=ExtractionStatus.FAILED, code=ErrorCode.RATE_LIMITED),
        ):
            with self.subTest(status=result.status):
                self.assertTrue(counts_as_failure(result))


class WriteBackTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory-pipeline-", dir=WORK_ROOT)
        self.addCleanup(temporary.cleanup)
        self.clock = EpochClock()
        self.storage = open_storage(Path(temporary.name) / "chizuru.db", clock=self.clock)
        self.addCleanup(self.storage.close)
        self.member = MemberKey(GroupKey(INSTANCE, "20001"), "30001")

    def authorize(self, member=None):
        self.storage.memories.set_authorized(
            member or self.member, authorized=True, auth_version="auth-1"
        )

    def snapshot(self) -> RevisionSnapshot:
        return RevisionSnapshot(
            group_revision=self.storage.groups.policy(self.member.group).revision,
            member_revision=self.storage.members.state(self.member).revision,
        )

    def plan(self, **overrides) -> WritePlan:
        values = {
            "member": self.member,
            "source_message_id": "m-1",
            "source_action": "memory_extract",
            "source_retention_seconds": 600,
            "limits": LIMITS,
            "candidates": (("address", "叫我小林"),),
        }
        values.update(overrides)
        return WritePlan(**values)

    def write(self, plan: WritePlan):
        with self.storage.database.transaction() as connection:
            return write_back(connection, plan, at=self.clock())


class WriteBackTests(WriteBackTestCase):
    def test_written_persists_the_facts(self):
        self.authorize()
        result = self.write(self.plan(expected_revision=self.snapshot()))
        self.assertIs(result.outcome, WriteOutcome.WRITTEN)
        self.assertEqual(result.written, 1)
        facts = self.storage.memories.facts(self.member)
        self.assertEqual([(fact.category, fact.content) for fact in facts], [("address", "叫我小林")])

    def test_a_matching_snapshot_does_not_block_the_write(self):
        self.authorize()
        self.write(self.plan(expected_revision=self.snapshot()))
        self.assertEqual(len(self.storage.memories.facts(self.member)), 1)

    def test_revision_change_drops_everything(self):
        self.authorize()
        stale = self.snapshot()
        self.storage.members.opt_out(self.member)  # 在途期间状态改变
        result = self.write(self.plan(expected_revision=stale))
        self.assertIs(result.outcome, WriteOutcome.REVISION_CHANGED)
        self.assertEqual(self.storage.memories.facts(self.member), ())

    def test_authorization_is_rechecked_inside_the_transaction(self):
        result = self.write(self.plan())
        self.assertIs(result.outcome, WriteOutcome.NOT_AUTHORIZED)

    def test_nothing_to_write_still_remembers_the_source(self):
        self.authorize()
        result = self.write(self.plan(candidates=()))
        self.assertIs(result.outcome, WriteOutcome.NOTHING_TO_WRITE)
        # 同一条消息再写一次：来源已登记，直接丢弃（不会问模型的机会也没有了）。
        again = self.write(self.plan(candidates=(("address", "叫我小林"),)))
        self.assertIs(again.outcome, WriteOutcome.DUPLICATE)
        self.assertEqual(self.storage.memories.facts(self.member), ())

    def test_a_second_write_of_the_same_source_is_a_duplicate(self):
        self.authorize()
        self.write(self.plan())
        again = self.write(self.plan(candidates=(("interest", "看番"),)))
        self.assertIs(again.outcome, WriteOutcome.DUPLICATE)
        self.assertEqual(len(self.storage.memories.facts(self.member)), 1)

    def test_a_different_message_is_not_a_duplicate(self):
        self.authorize()
        self.write(self.plan())
        second = self.write(self.plan(source_message_id="m-2", candidates=(("interest", "看番"),)))
        self.assertIs(second.outcome, WriteOutcome.WRITTEN)
        self.assertEqual(len(self.storage.memories.facts(self.member)), 2)

    def test_the_source_window_is_injected(self):
        self.authorize()
        self.write(self.plan())
        self.clock.advance(601)
        fresh = self.write(self.plan(candidates=(("interest", "看番"),)))
        self.assertIs(fresh.outcome, WriteOutcome.WRITTEN)

    def test_plan_requires_a_member_and_limits(self):
        for overrides in ({"member": "30001"}, {"limits": None}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.plan(**overrides)

    def test_invalid_results_are_refused(self):
        for kwargs in (
            {"outcome": WriteOutcome.WRITTEN},
            {"outcome": WriteOutcome.DUPLICATE, "written": 1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    WriteResult(**kwargs)


if __name__ == "__main__":
    unittest.main()
