import unittest
from dataclasses import replace

from astrbot_plugin_chizuru.config import (
    BudgetSettings,
    MaintainerMap,
    Settings,
    default_fields,
    is_qq_id,
)

VALID = {
    "platform_id": "qq-local",
    "self_id": "10001",
    "allowed_group_ids": ["20001", "20002"],
    "group_maintainers": {"20001": ["30001"]},
    "notice_version": "notice-1",
}


class FailClosedTests(unittest.TestCase):
    """任何不合格输入都必须整体退回哨兵，而不是"部分放行"。"""

    def test_empty_mapping_is_sentinel(self):
        self.assertEqual(Settings.from_mapping({}), Settings())
        self.assertFalse(Settings.from_mapping({}).identity_configured)

    def test_missing_identity_fields(self):
        # 缺任一段身份都不是"可用配置"：必须拒绝全部处理。
        for key in ("platform_id", "self_id", "allowed_group_ids"):
            with self.subTest(missing=key):
                config = {k: v for k, v in VALID.items() if k != key}
                settings = Settings.from_mapping(config)
                self.assertFalse(settings.identity_configured)
                self.assertFalse(settings.allows_group("qq-local", "10001", "20001"))

    def test_empty_allowed_groups_denies_all(self):
        # 空允许群不是哨兵，但必须等价于拒绝全部——这是需求 §3 的"空名单即全关"。
        settings = Settings.from_mapping({**VALID, "allowed_group_ids": []})
        self.assertFalse(settings.identity_configured)
        self.assertFalse(settings.allows_group("qq-local", "10001", "20001"))
        self.assertEqual(settings.allowed_group_ids, frozenset())

    def test_malformed_identity(self):
        for key, value in (
            ("platform_id", None),
            ("platform_id", " qq-local"),
            ("platform_id", ""),
            ("self_id", 10001),
            ("self_id", "０１０"),  # 全角
            ("self_id", "0"),
            ("allowed_group_ids", "20001"),
            ("allowed_group_ids", [20001]),
            ("allowed_group_ids", [True]),
            ("allowed_group_ids", ["20001", ""]),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(Settings.from_mapping({**VALID, key: value}), Settings())

    def test_malformed_parameter_collapses_whole_config(self):
        # 参数不合格与身份不合格同等严重：都不能保留一份"看起来可用"的配置。
        for key, value in (
            ("notice_version", ""),
            ("notice_version", None),
            ("context_buffer_max_messages", 0),
            ("context_buffer_max_messages", True),
            ("context_buffer_ttl_seconds", -1),
            ("interaction_history_max_turns", 0),
            ("memory_ttl_days", 0),
            ("auth_confirm_ttl_seconds", 1),
            ("memory_extraction_enabled", "true"),
            ("daily_budget_amount", -1),
            ("daily_budget_amount", "10"),
            ("max_retries", 9),
            ("log_retention_days", 0),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(Settings.from_mapping({**VALID, key: value}), Settings())


class MaintainerMapTests(unittest.TestCase):
    def test_explicit_mapping_is_required(self):
        # QQ 群管理员身份不自动等于维护者：只有显式配置才生效。
        settings = Settings.from_mapping(VALID)
        self.assertTrue(settings.maintainers.is_maintainer("20001", "30001"))
        self.assertFalse(settings.maintainers.is_maintainer("20001", "30002"))
        self.assertFalse(settings.maintainers.is_maintainer("20002", "30001"))
        self.assertEqual(settings.maintainers.maintainers_of("20002"), frozenset())

    def test_malformed_mapping_rejects_all(self):
        for value in (
            [],
            "20001:30001",
            {"20001": "30001"},
            {"20001": [30001]},
            {"020001": ["30001"]},
            {"20001": ["030001"]},
            {"20001": [True]},
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    Settings.from_mapping({**VALID, "group_maintainers": value}),
                    Settings(),
                )

    def test_default_is_empty(self):
        self.assertEqual(MaintainerMap().mapping, {})
        self.assertFalse(MaintainerMap().is_maintainer("20001", "30001"))


class BudgetTests(unittest.TestCase):
    def test_unset_amount_means_not_configured(self):
        budget = BudgetSettings()
        self.assertFalse(budget.budget_configured)

    def test_extraction_stays_closed_without_budget(self):
        budget = BudgetSettings()
        self.assertFalse(budget.extraction_allowed(requested=True))

    def test_extraction_closed_when_member_has_not_enabled(self):
        budget = BudgetSettings(daily_amount=1.0)
        self.assertFalse(budget.extraction_allowed(requested=False))

    def test_extraction_allowed_only_with_both(self):
        budget = BudgetSettings(daily_amount=1.0)
        self.assertTrue(budget.extraction_allowed(requested=True))

    def test_monthly_alone_is_enough(self):
        self.assertTrue(BudgetSettings(monthly_amount=5.0).budget_configured)

    def test_requirement_can_be_waived_explicitly(self):
        budget = BudgetSettings(require_budget_for_extraction=False)
        self.assertTrue(budget.extraction_allowed(requested=True))


class LimitsTests(unittest.TestCase):
    def test_group_concurrency_cannot_exceed_global(self):
        config = {
            **VALID,
            "provider_concurrency_global": 1,
            "provider_concurrency_per_group": 2,
        }
        self.assertEqual(Settings.from_mapping(config), Settings())

    def test_timeouts_must_fit_inside_deadline(self):
        config = {
            **VALID,
            "connect_timeout_seconds": 20,
            "read_timeout_seconds": 30,
            "task_deadline_seconds": 45,
        }
        self.assertEqual(Settings.from_mapping(config), Settings())

    def test_defaults_fit_inside_deadline(self):
        limits = Settings().limits
        self.assertLessEqual(
            limits.connect_timeout_seconds + limits.read_timeout_seconds,
            limits.task_deadline_seconds,
        )


class AllowsGroupTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.from_mapping(VALID)

    def test_all_three_scopes_must_match(self):
        self.assertTrue(self.settings.allows_group("qq-local", "10001", "20001"))
        self.assertFalse(self.settings.allows_group("other", "10001", "20001"))
        self.assertFalse(self.settings.allows_group("qq-local", "10002", "20001"))
        self.assertFalse(self.settings.allows_group("qq-local", "10001", "20003"))

    def test_sentinel_allows_nothing(self):
        self.assertFalse(Settings().allows_group("qq-local", "10001", "20001"))


class DefaultsTests(unittest.TestCase):
    def test_defaults_follow_requirement_baseline(self):
        settings = Settings()
        self.assertEqual(settings.context.buffer_max_messages, 30)
        self.assertEqual(settings.context.buffer_ttl_seconds, 600)
        self.assertEqual(settings.context.history_max_turns, 20)
        self.assertEqual(settings.context.history_ttl_hours, 24)
        self.assertEqual(settings.memory.max_records_per_member, 20)
        self.assertEqual(settings.memory.ttl_days, 90)
        self.assertEqual(settings.memory.auth_confirm_ttl_seconds, 300)
        self.assertIs(settings.memory.extraction_enabled, False)
        self.assertEqual(settings.budget.input_token_budget, 8192)
        self.assertEqual(settings.budget.output_token_budget, 512)
        self.assertEqual(settings.limits.provider_concurrency_global, 2)
        self.assertEqual(settings.limits.provider_concurrency_per_group, 1)
        self.assertEqual(settings.limits.chat_queue_per_group, 3)
        self.assertEqual(settings.limits.extraction_queue_global, 20)
        self.assertEqual(settings.limits.schedule_wait_seconds, 30)
        self.assertEqual(settings.limits.task_deadline_seconds, 45)
        self.assertEqual(settings.limits.max_retries, 1)
        self.assertEqual(settings.log.retention_days, 7)

    def test_default_fields_covers_every_key(self):
        fields = default_fields()
        self.assertEqual(set(fields), set(default_fields()))  # 稳定
        self.assertEqual(Settings.from_mapping(fields), Settings())

    def test_valid_config_round_trips(self):
        settings = Settings.from_mapping(VALID)
        self.assertTrue(settings.identity_configured)
        self.assertEqual(settings.allowed_group_ids, frozenset({"20001", "20002"}))
        self.assertEqual(settings.notice_version, "notice-1")

    def test_settings_are_immutable(self):
        from dataclasses import FrozenInstanceError

        with self.assertRaises(FrozenInstanceError):
            Settings().platform_id = "x"
        self.assertNotEqual(replace(Settings(), platform_id="x"), Settings())


class IsQqIdTests(unittest.TestCase):
    def test_accepts_plain_ascii_digits(self):
        for value in ("1", "10001", "999999999"):
            self.assertTrue(is_qq_id(value))

    def test_rejects_everything_else(self):
        for value in ("", "0", "010", "１０００１", "abc", "100 01", 10001, True, None):
            with self.subTest(value=value):
                self.assertFalse(is_qq_id(value))


if __name__ == "__main__":
    unittest.main()
