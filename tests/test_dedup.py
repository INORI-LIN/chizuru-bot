import dataclasses
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.dedup import (
    ActionKind,
    Claim,
    DedupKey,
    DedupStore,
    Outcome,
)
from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot_plugin_chizuru"
INSTANCE = BotInstanceKey("qq-local", "10001")
WINDOW = 10.0


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_key(
    message_id: str = "m1",
    *,
    action: ActionKind = ActionKind.CHAT_REPLY,
    group_id: str = "20001",
    instance: BotInstanceKey = INSTANCE,
) -> DedupKey:
    return DedupKey(GroupKey(instance, group_id), message_id, action)


def make_store(clock: FakeClock, *, window: float = WINDOW, capacity: int = 100) -> DedupStore:
    return DedupStore(window_seconds=window, capacity=capacity, clock=clock)


class ClaimLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = make_store(self.clock)

    def test_first_claim_then_in_flight_then_done(self):
        key = make_key()
        self.assertEqual(self.store.begin(key), Claim.FIRST)
        # 同一事件再次到达（重连回放）：不再执行。
        self.assertEqual(self.store.begin(key), Claim.IN_FLIGHT)
        self.assertTrue(self.store.finish(key))
        self.assertEqual(self.store.begin(key), Claim.DONE)

    def test_uncertain_outcome_is_not_resent(self):
        key = make_key()
        self.store.begin(key)
        self.store.finish(key, Outcome.SEND_UNCERTAIN)
        self.assertEqual(self.store.begin(key), Claim.UNCERTAIN)

    def test_release_allows_a_retry(self):
        key = make_key()
        self.store.begin(key)
        self.assertTrue(self.store.release(key))
        self.assertEqual(self.store.begin(key), Claim.FIRST)
        self.assertFalse(self.store.release(make_key("missing")))

    def test_finish_on_unknown_key_changes_nothing(self):
        self.assertFalse(self.store.finish(make_key("never-seen")))
        self.assertEqual(self.store.stats().size, 0)


class IsolationTests(unittest.TestCase):
    """键的每一段都必须参与隔离，否则会串用或漏判。"""

    def setUp(self):
        self.clock = FakeClock()
        self.store = make_store(self.clock)
        self.base = make_key()

    def test_each_key_segment_isolates(self):
        self.store.begin(self.base)
        others = {
            "message_id": make_key("m2"),
            "action": make_key(action=ActionKind.MEMORY_EXTRACT),
            "group": make_key(group_id="20002"),
            "instance": make_key(instance=BotInstanceKey("qq-local", "10002")),
        }
        for label, key in others.items():
            with self.subTest(segment=label):
                self.assertEqual(self.store.begin(key), Claim.FIRST)


class BoundedRetentionTests(unittest.TestCase):
    def test_expired_entry_can_be_reprocessed(self):
        clock = FakeClock()
        store = make_store(clock)
        key = make_key()
        store.begin(key)
        clock.advance(WINDOW + 0.001)
        self.assertEqual(store.begin(key), Claim.FIRST)

    def test_window_does_not_slide_on_repeated_events(self):
        """窗口自首次出现起算：重放不续期，否则去重表会随重放无限延长。"""
        clock = FakeClock()
        store = make_store(clock)
        key = make_key()
        store.begin(key)
        clock.advance(WINDOW / 2)
        self.assertEqual(store.begin(key), Claim.IN_FLIGHT)
        clock.advance(WINDOW / 2 + 0.001)
        self.assertEqual(store.begin(key), Claim.FIRST)

    def test_capacity_evicts_the_oldest_entry(self):
        clock = FakeClock()
        store = make_store(clock, capacity=2)
        first, second, third = make_key("m1"), make_key("m2"), make_key("m3")
        store.begin(first)
        store.begin(second)
        store.begin(third)
        self.assertEqual(store.stats().size, 2)
        self.assertEqual(store.stats().evicted, 1)
        # 被淘汰意味着失去保护：同一个键可以重新处理，且随后重新生效。
        self.assertEqual(store.begin(first), Claim.FIRST)
        self.assertEqual(store.stats().size, 2)
        self.assertEqual(store.stats().evicted, 2)
        self.assertEqual(store.begin(first), Claim.IN_FLIGHT)
        self.assertEqual(store.begin(second), Claim.FIRST)

    def test_purge_reports_removed_entries(self):
        clock = FakeClock()
        store = make_store(clock)
        store.begin(make_key("m1"))
        store.begin(make_key("m2"))
        clock.advance(WINDOW + 0.001)
        self.assertEqual(store.purge(), 2)
        self.assertEqual(store.purge(), 0)

    def test_stats_reports_states_and_bounds(self):
        clock = FakeClock()
        store = make_store(clock, capacity=5)
        store.begin(make_key("m1"))
        store.begin(make_key("m2"))
        store.finish(make_key("m2"))
        store.begin(make_key("m3"))
        store.finish(make_key("m3"), Outcome.SEND_UNCERTAIN)
        stats = store.stats()
        self.assertEqual((stats.size, stats.capacity, stats.window_seconds), (3, 5, WINDOW))
        self.assertEqual((stats.in_flight, stats.done, stats.uncertain), (1, 1, 1))


class ValidationTests(unittest.TestCase):
    def test_window_and_capacity_are_required_and_sane(self):
        clock = FakeClock()
        for window in (0, -1, 0.0001, True, "10"):
            with self.subTest(window=window):
                with self.assertRaises(ValueError):
                    DedupStore(window_seconds=window, capacity=1, clock=clock)
        for capacity in (0, -1, True, 1.5, "10"):
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError):
                    DedupStore(window_seconds=WINDOW, capacity=capacity, clock=clock)

    def test_key_requires_trusted_metadata_only(self):
        for message_id in ("", "   ", " m1", "m1 "):
            with self.subTest(message_id=message_id):
                with self.assertRaises(ValueError):
                    make_key(message_id)
        for action in ("chat_reply", None):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    DedupKey(group("20001"), "m1", action)


def group(group_id: str) -> GroupKey:
    return GroupKey(INSTANCE, group_id)


class StructuralTests(unittest.TestCase):
    def test_key_carries_no_content_fields(self):
        names = {f.name for f in dataclasses.fields(DedupKey)}
        for forbidden in ("text", "content", "message", "body", "nickname", "card"):
            self.assertNotIn(forbidden, names)

    def test_module_does_not_import_the_framework(self):
        text = (PLUGIN_ROOT / "dedup.py").read_text()
        self.assertNotIn("import astrbot", text)
        self.assertNotIn("from astrbot", text)


if __name__ == "__main__":
    unittest.main()
