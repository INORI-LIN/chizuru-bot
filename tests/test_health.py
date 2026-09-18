import ast
import dataclasses
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

from astrbot_plugin_chizuru.budget import BudgetSnapshot, TokenUsage
from astrbot_plugin_chizuru.health import (
    ACCOUNT_STATE_NOTE,
    Degradation,
    HealthMonitor,
    HealthSnapshot,
    PlatformHealth,
    PlatformState,
    format_report,
    platform_state,
)
from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey
from astrbot_plugin_chizuru.scheduler import SchedulerStats

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot_plugin_chizuru"

GROUP = GroupKey(BotInstanceKey("qq-local", "10001"), "20001")


def health(instance_present: bool = True, state: PlatformState = PlatformState.RUNNING,
           error_count: int = 0) -> PlatformHealth:
    return PlatformHealth(
        instance_present=instance_present,
        state=state,
        error_count=error_count,
    )


def budget(configured: bool = False, paused: bool = False) -> BudgetSnapshot:
    return BudgetSnapshot(
        configured=configured,
        day_tokens=TokenUsage(),
        month_tokens=TokenUsage(),
        day_amount=Decimal("0") if not configured else Decimal("1"),
        month_amount=Decimal("0") if not configured else Decimal("10"),
        day_limit=None,
        month_limit=None,
        day_estimated=False,
        month_estimated=False,
        outstanding=0,
        paused=paused,
        extraction_allowed=False,
    )


def scheduler_stats(waiting: int = 0, closed: bool = False) -> SchedulerStats:
    return SchedulerStats(
        running_global=1,
        running_per_group={GROUP: 1},
        waiting_chat={GROUP: waiting},
        waiting_extraction=2,
        closed=closed,
    )


class PlatformStateMappingTests(unittest.TestCase):
    def test_known_upstream_values_are_mapped(self):
        cases = {
            "pending": PlatformState.PENDING,
            "running": PlatformState.RUNNING,
            "error": PlatformState.ERROR,
            "stopped": PlatformState.STOPPED,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertIs(platform_state(raw), expected)

    def test_framework_enum_object_is_accepted(self):
        class UpstreamStatus:
            value = "running"

        self.assertIs(platform_state(UpstreamStatus()), PlatformState.RUNNING)

    def test_unknown_values_never_map_to_running(self):
        # 框架换了取值、传了异常对象或传了 None，都必须落到 UNKNOWN。
        for value in (None, "", "RUNNING", " running", "running ", 42, True, ValueError("boom")):
            with self.subTest(value=repr(value)):
                self.assertIs(platform_state(value), PlatformState.UNKNOWN)

    def test_instance_missing_is_not_produced_by_mapping(self):
        # INSTANCE_MISSING 只能来自"找不到实例"这一事实，不能来自状态字符串。
        self.assertNotIn(PlatformState.INSTANCE_MISSING.value, {"pending", "running", "error", "stopped"})
        self.assertIs(platform_state(PlatformState.INSTANCE_MISSING.value), PlatformState.UNKNOWN)


class PlatformHealthTests(unittest.TestCase):
    def test_instance_presence_and_state_must_agree(self):
        with self.assertRaises(ValueError):
            PlatformHealth(instance_present=False, state=PlatformState.RUNNING)
        with self.assertRaises(ValueError):
            PlatformHealth(instance_present=True, state=PlatformState.INSTANCE_MISSING)

    def test_error_count_is_validated(self):
        for bad in (True, -1, "1", 1.5, None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    health(error_count=bad)

    def test_reachable_only_when_running(self):
        self.assertTrue(health(state=PlatformState.RUNNING).reachable)
        for state in (
            PlatformState.PENDING,
            PlatformState.ERROR,
            PlatformState.STOPPED,
            PlatformState.UNKNOWN,
            PlatformState.INSTANCE_MISSING,
        ):
            with self.subTest(state=state):
                present = state is not PlatformState.INSTANCE_MISSING
                self.assertFalse(health(instance_present=present, state=state).reachable)

    def test_carries_no_error_text_field(self):
        names = {f.name for f in dataclasses.fields(PlatformHealth)}
        for forbidden in ("message", "error", "traceback", "last_error", "detail", "config"):
            self.assertNotIn(forbidden, names)

    def test_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            health().error_count = 1


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.monitor = HealthMonitor()

    def test_missing_instance_is_recorded_and_count_reset(self):
        result = self.monitor.observe_platform(instance_present=False, error_count=7)
        self.assertIs(result.state, PlatformState.INSTANCE_MISSING)
        self.assertEqual(result.error_count, 0)

    def test_error_count_is_kept_without_any_error_text(self):
        result = self.monitor.observe_platform(
            instance_present=True,
            status="error",
            error_count=3,
        )
        self.assertIs(result.state, PlatformState.ERROR)
        self.assertEqual(result.error_count, 3)
        self.assertTrue(result.has_errors)

    def test_exception_object_is_not_stored_as_state(self):
        # 传进异常对象时只落成 UNKNOWN：异常文本没有可存之处。
        result = self.monitor.observe_platform(instance_present=True, status=ValueError("sk-secret"))
        self.assertIs(result.state, PlatformState.UNKNOWN)
        self.assertEqual(result, self.monitor.platform())

    def test_arguments_are_validated(self):
        with self.assertRaises(ValueError):
            self.monitor.observe_platform(instance_present=1)
        with self.assertRaises(ValueError):
            self.monitor.observe_platform(instance_present=True, error_count=True)


class DegradationTests(unittest.TestCase):
    def setUp(self):
        self.monitor = HealthMonitor()

    def test_only_persistent_states_are_registerable(self):
        # 瞬态错误归 redact.ErrorCode，不该在这里变成永远清不掉的标志。
        self.assertEqual(len(Degradation), 5)
        for forbidden in ("rate_limited", "unclassified", "empty_reply", "provider_unavailable"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, {member.value for member in Degradation})

    def test_set_and_clear_are_reflected_in_snapshot(self):
        self.monitor.set_degraded(Degradation.PROVIDER_DISABLED)
        self.monitor.set_degraded(Degradation.EXTRACTION_SUSPENDED)
        self.assertEqual(
            self.monitor.degradations(),
            frozenset({Degradation.PROVIDER_DISABLED, Degradation.EXTRACTION_SUSPENDED}),
        )
        self.assertTrue(self.monitor.clear_degraded(Degradation.PROVIDER_DISABLED))
        self.assertFalse(self.monitor.clear_degraded(Degradation.PROVIDER_DISABLED))
        self.assertEqual(
            self.monitor.snapshot().degradations,
            frozenset({Degradation.EXTRACTION_SUSPENDED}),
        )

    def test_rejects_non_degradation(self):
        for bad in ("provider_disabled", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    self.monitor.set_degraded(bad)
                with self.assertRaises(ValueError):
                    self.monitor.clear_degraded(bad)


class ExtractionCounterTests(unittest.TestCase):
    def test_counters_accumulate_without_deciding_anything(self):
        monitor = HealthMonitor()
        self.assertEqual(monitor.record_extraction_failure(), 1)
        self.assertEqual(monitor.record_extraction_failure(), 2)
        self.assertEqual(monitor.record_extraction_success(), 1)
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.extraction_failures, 2)
        self.assertEqual(snapshot.extraction_successes, 1)
        # 计数本身不置降级：阈值判断属于调用方。
        self.assertEqual(snapshot.degradations, frozenset())


class SnapshotTests(unittest.TestCase):
    def test_budget_and_scheduler_are_passed_through_unmodified(self):
        snapshot = HealthSnapshot(
            platform=health(),
            budget=budget(configured=True),
            scheduler=scheduler_stats(waiting=3),
        )
        self.assertTrue(snapshot.budget.configured)
        self.assertEqual(snapshot.scheduler.waiting_chat[GROUP], 3)

    def test_degraded_covers_platform_state_and_flags(self):
        self.assertFalse(HealthSnapshot(platform=health()).degraded)
        self.assertTrue(
            HealthSnapshot(platform=health(state=PlatformState.STOPPED)).degraded,
        )
        self.assertTrue(
            HealthSnapshot(
                platform=health(),
                degradations=frozenset({Degradation.MEMORY_STORE_FAILED}),
            ).degraded,
        )
        # 未装配（找不到实例）时也算不可用，不能默认当作正常。
        self.assertTrue(
            HealthSnapshot(
                platform=health(instance_present=False, state=PlatformState.INSTANCE_MISSING),
            ).degraded,
        )

    def test_types_are_validated(self):
        with self.assertRaises(ValueError):
            HealthSnapshot(platform=object())
        with self.assertRaises(ValueError):
            HealthSnapshot(platform=health(), degradations=frozenset({"provider_disabled"}))
        with self.assertRaises(ValueError):
            HealthSnapshot(platform=health(), budget=object())
        with self.assertRaises(ValueError):
            HealthSnapshot(platform=health(), scheduler=object())
        with self.assertRaises(ValueError):
            HealthSnapshot(platform=health(), extraction_failures=-1)


class ReportTests(unittest.TestCase):
    def test_report_lines_are_enum_and_number_only(self):
        monitor = HealthMonitor()
        monitor.observe_platform(instance_present=True, status="running", error_count=2)
        lines = format_report(monitor.snapshot(), configured=True)
        self.assertEqual(lines[0], "部署配置：已配置")
        self.assertIn("运行中", lines[1])
        self.assertIn("2", lines[1])
        self.assertIn(ACCOUNT_STATE_NOTE, lines)
        self.assertEqual(lines[-1], "抽取：成功 0 次，失败 0 次")

    def test_sentinel_configuration_is_reported_as_closed(self):
        snapshot = HealthSnapshot(
            platform=health(instance_present=False, state=PlatformState.INSTANCE_MISSING),
        )
        lines = format_report(snapshot, configured=False)
        self.assertEqual(lines[0], "部署配置：未配置（全部处理已拒绝）")
        self.assertIn("未找到平台实例（未配置或未加载）", lines[1])

    def test_report_order_is_stable_regardless_of_insertion_order(self):
        first = HealthMonitor()
        first.set_degraded(Degradation.EXTRACTION_SUSPENDED)
        first.set_degraded(Degradation.PROVIDER_DISABLED)
        second = HealthMonitor()
        second.set_degraded(Degradation.PROVIDER_DISABLED)
        second.set_degraded(Degradation.EXTRACTION_SUSPENDED)
        self.assertEqual(
            format_report(first.snapshot(), configured=True),
            format_report(second.snapshot(), configured=True),
        )

    def test_optional_sections_appear_only_when_present(self):
        bare = format_report(HealthSnapshot(platform=health()), configured=True)
        self.assertFalse(any(line.startswith("预算：") for line in bare))
        self.assertFalse(any(line.startswith("队列：") for line in bare))

        rich = format_report(
            HealthSnapshot(
                platform=health(),
                budget=budget(paused=True),
                scheduler=scheduler_stats(waiting=3),
            ),
            configured=True,
        )
        budget_line = next(line for line in rich if line.startswith("预算："))
        self.assertIn("未配置（自动抽取保持关闭）", budget_line)
        self.assertIn("暂停 是", budget_line)
        queue_line = next(line for line in rich if line.startswith("队列："))
        self.assertIn("聊天等待 3", queue_line)

    def test_arguments_are_validated(self):
        snapshot = HealthSnapshot(platform=health())
        with self.assertRaises(ValueError):
            format_report(snapshot, configured=1)
        with self.assertRaises(ValueError):
            format_report("not a snapshot", configured=True)


class StructuralGuaranteeTests(unittest.TestCase):
    """健康状态路径必须与框架、凭据和正文彻底分离——这些是结构性的，不靠约定。"""

    def setUp(self):
        self.tree = ast.parse((PLUGIN_ROOT / "health.py").read_text())
        self.imports = {
            node.module or ""
            for node in ast.walk(self.tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.attributes = {
            node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)
        }

    def test_pure_module_does_not_import_the_framework(self):
        self.assertFalse({name for name in self.imports if name.startswith("astrbot")})

    def test_never_touches_credential_or_error_text_sources(self):
        # 契约里可以点名"绝不读"，但代码里不得存在这些属性的取值语句。
        for forbidden in ("config", "get_stats", "traceback", "last_error", "message", "text"):
            with self.subTest(attribute=forbidden):
                self.assertNotIn(forbidden, self.attributes)

    def test_snapshot_carries_no_free_text_field(self):
        names = {f.name for f in dataclasses.fields(HealthSnapshot)}
        for forbidden in ("message", "text", "reason", "traceback", "detail"):
            self.assertNotIn(forbidden, names)

    def test_no_sending_or_logging_capability(self):
        self.assertNotIn("logging", self.imports)
        for forbidden in ("send", "send_message", "send_streaming"):
            with self.subTest(attribute=forbidden):
                self.assertNotIn(forbidden, self.attributes)


if __name__ == "__main__":
    unittest.main()
