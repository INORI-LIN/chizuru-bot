import ast
import dataclasses
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from astrbot_plugin_chizuru.budget import TokenUsage
from astrbot_plugin_chizuru.config import LogSettings
from astrbot_plugin_chizuru.redact import (
    AUDIT_CHECKLIST,
    DEFAULT_LOG_TOTAL_BYTES,
    AuditItem,
    AuditRecord,
    CorrelationId,
    ErrorCode,
    EventCategory,
    Redactor,
    RetentionPolicy,
    format_record,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot_plugin_chizuru"

SALT = b"0123456789abcdef0123456789abcdef"


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor(salt=SALT)

    def test_same_parts_give_the_same_id(self):
        first = self.redactor.correlation("qq-local", "20001", "30002")
        second = self.redactor.correlation("qq-local", "20001", "30002")
        self.assertEqual(first, second)

    def test_different_parts_or_order_give_different_ids(self):
        base = self.redactor.correlation("qq-local", "20001", "30002")
        other_group = self.redactor.correlation("qq-local", "20002", "30002")
        other_member = self.redactor.correlation("qq-local", "20001", "30003")
        swapped = self.redactor.correlation("20001", "qq-local", "30002")
        self.assertEqual(len({base, other_group, other_member, swapped}), 4)

    def test_id_is_a_fixed_shape_digest_not_the_raw_identifier(self):
        digest = self.redactor.correlation("20001").digest
        self.assertRegex(digest, r"^[0-9a-f]{32}$")
        self.assertNotIn("20001", digest)

    def test_different_salts_give_different_ids(self):
        other = Redactor(salt=b"fedcba9876543210fedcba9876543210")
        self.assertNotEqual(
            self.redactor.correlation("20001"),
            other.correlation("20001"),
        )

    def test_default_salt_is_per_process_random(self):
        # 跨重启不可关联是刻意取舍：无盐摘要在秒级就能把 QQ 号试出来。
        first = Redactor().correlation("20001")
        second = Redactor().correlation("20001")
        self.assertNotEqual(first, second)

    def test_salt_is_never_exposed(self):
        self.assertFalse(hasattr(self.redactor, "salt"))

    def test_parts_must_be_identifiers(self):
        cases = {
            "no_parts": (),
            "empty": ("",),
            "blank": (" 20001 ",),
            "too_long": ("x" * 129,),
            "control_chars": ("20001\nsecret",),
            "not_a_string": (20001,),
        }
        for label, parts in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    self.redactor.correlation(*parts)

    def test_salt_must_be_long_enough(self):
        for bad in (b"short", "0" * 32, b""):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    Redactor(salt=bad)

    def test_correlation_id_shape_is_validated(self):
        CorrelationId("0" * 32)
        for bad in ("0" * 31, "0" * 33, "A" * 32, "", "z" * 32, 12345, None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    CorrelationId(bad)

    def test_correlation_id_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            CorrelationId("0" * 32).digest = "1" * 32


class ErrorCodeTests(unittest.TestCase):
    def test_http_status_mapping(self):
        cases = {
            400: ErrorCode.REQUEST_INVALID,
            422: ErrorCode.REQUEST_INVALID,
            401: ErrorCode.AUTH_FAILED,
            402: ErrorCode.BALANCE_INSUFFICIENT,
            429: ErrorCode.RATE_LIMITED,
            500: ErrorCode.PROVIDER_UNAVAILABLE,
            503: ErrorCode.PROVIDER_UNAVAILABLE,
            599: ErrorCode.PROVIDER_UNAVAILABLE,
            200: ErrorCode.UNCLASSIFIED,
            403: ErrorCode.UNCLASSIFIED,
            418: ErrorCode.UNCLASSIFIED,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertIs(ErrorCode.from_status(status), expected)

    def test_unknown_status_is_never_guessed_into_a_known_class(self):
        self.assertIs(ErrorCode.from_status(302), ErrorCode.UNCLASSIFIED)

    def test_status_must_be_an_integer(self):
        for bad in (True, "401", 401.0, None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    ErrorCode.from_status(bad)

    def test_covers_every_failure_row_of_the_architecture(self):
        # 架构 §8.3 的每一行都要有对应错误码，否则那行故障无法被记录。
        required = {
            "request_invalid",
            "auth_failed",
            "balance_insufficient",
            "rate_limited",
            "provider_unavailable",
            "empty_reply",
            "queue_full",
            "budget_exhausted",
            "revision_changed",
            "delete_failed",
            "memory_store_failed",
        }
        self.assertTrue(required <= {code.value for code in ErrorCode})


class AuditRecordTests(unittest.TestCase):
    def test_free_text_cannot_be_smuggled_in_as_an_error_code(self):
        for bad in ("401 unauthorized sk-abc123", 401, "boom"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    AuditRecord(category=EventCategory.MODEL_CALL, code=bad)

    def test_category_must_be_from_the_closed_set(self):
        with self.assertRaises(ValueError):
            AuditRecord(category="model_call")

    def test_numeric_fields_are_bounded(self):
        for field in ("duration_ms", "count"):
            for bad in (True, -1, "1", 1.5):
                with self.subTest(field=field, bad=repr(bad)):
                    with self.assertRaises(ValueError):
                        AuditRecord(category=EventCategory.MODEL_CALL, **{field: bad})

    def test_typed_fields_are_validated(self):
        with self.assertRaises(ValueError):
            AuditRecord(category=EventCategory.TOKEN_USAGE, tokens=(1, 2))
        with self.assertRaises(ValueError):
            AuditRecord(category=EventCategory.MODEL_CALL, correlation="deadbeef")

    def test_carries_no_free_text_field(self):
        names = {f.name for f in dataclasses.fields(AuditRecord)}
        for forbidden in ("message", "text", "body", "detail", "exception", "traceback", "reason"):
            self.assertNotIn(forbidden, names)

    def test_is_immutable(self):
        record = AuditRecord(category=EventCategory.MODEL_CALL)
        with self.assertRaises(FrozenInstanceError):
            record.category = EventCategory.SEND_RESULT


class FormatRecordTests(unittest.TestCase):
    def test_key_order_is_fixed(self):
        record = AuditRecord(
            category=EventCategory.MODEL_CALL,
            code=ErrorCode.RATE_LIMITED,
            duration_ms=1250,
            tokens=TokenUsage(input_tokens=100, output_tokens=20),
            count=3,
            correlation=CorrelationId("0" * 32),
        )
        self.assertEqual(
            format_record(record),
            "category=model_call code=rate_limited duration_ms=1250 "
            "tokens_in=100 tokens_out=20 count=3 correlation=" + "0" * 32,
        )

    def test_optional_parts_are_omitted(self):
        self.assertEqual(
            format_record(AuditRecord(category=EventCategory.IGNORED)),
            "category=ignored",
        )

    def test_output_is_a_single_line(self):
        record = AuditRecord(
            category=EventCategory.GATE_DROP,
            code=ErrorCode.REVISION_CHANGED,
            duration_ms=5,
        )
        self.assertNotIn("\n", format_record(record))

    def test_argument_is_validated(self):
        with self.assertRaises(ValueError):
            format_record("category=model_call")


class RetentionPolicyTests(unittest.TestCase):
    def test_defaults_come_from_config_and_module_constant(self):
        policy = RetentionPolicy.from_settings(LogSettings())
        self.assertEqual(policy.retention_days, 7)
        self.assertEqual(policy.max_total_bytes, DEFAULT_LOG_TOTAL_BYTES)
        self.assertEqual(DEFAULT_LOG_TOTAL_BYTES, 20 * 1024 * 1024)

    def test_configured_retention_is_respected(self):
        self.assertEqual(RetentionPolicy.from_settings(LogSettings(retention_days=30)).retention_days, 30)

    def test_values_are_validated(self):
        for bad in (0, -1, True, "7"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    RetentionPolicy(retention_days=bad)
        for bad in (0, -1, True, "1024"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    RetentionPolicy(retention_days=7, max_total_bytes=bad)

    def test_settings_object_is_required(self):
        with self.assertRaises(ValueError):
            RetentionPolicy.from_settings({"retention_days": 7})

    def test_policy_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            RetentionPolicy(retention_days=7).retention_days = 1


class AuditChecklistTests(unittest.TestCase):
    def test_is_a_small_tuple_of_complete_items(self):
        self.assertIsInstance(AUDIT_CHECKLIST, tuple)
        self.assertLessEqual(len(AUDIT_CHECKLIST), 8)
        self.assertEqual(len({item.topic for item in AUDIT_CHECKLIST}), len(AUDIT_CHECKLIST))
        for item in AUDIT_CHECKLIST:
            with self.subTest(topic=item.topic):
                self.assertIsInstance(item, AuditItem)
                self.assertTrue(item.topic.strip())
                self.assertTrue(item.checkpoint.strip())
                self.assertTrue(item.expectation.strip())

    def test_covers_every_component_that_can_leak(self):
        text = " ".join(
            f"{item.topic} {item.checkpoint} {item.expectation}" for item in AUDIT_CHECKLIST
        )
        for marker in ("astrbot.plugin", "astrbot.log", "WebUI", "NapCat", "ws_reverse_token", "traceback"):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_item_fields_are_validated(self):
        AuditItem(topic="t", checkpoint="c", expectation="e")
        for field in ("topic", "checkpoint", "expectation"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    AuditItem(**{"topic": "t", "checkpoint": "c", "expectation": "e", field: "  "})


class EventCategoryTests(unittest.TestCase):
    def test_covers_the_observability_list(self):
        # 架构 §11.1 的"记录"清单必须逐项有类别。
        required = {
            "platform_state",
            "valid_at",
            "ignored",
            "queue_depth",
            "model_call",
            "token_usage",
            "memory_op",
            "cleanup_failure",
            "degradation",
        }
        self.assertTrue(required <= {category.value for category in EventCategory})


class StructuralGuaranteeTests(unittest.TestCase):
    """日志路径只能记录类别化事实——这必须是结构性的，而不是靠纪律。"""

    def setUp(self):
        self.tree = ast.parse((PLUGIN_ROOT / "redact.py").read_text())
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
        self.calls = {
            node.func.id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    def test_does_not_import_the_framework_or_a_logger(self):
        self.assertFalse({name for name in self.imports if name.startswith("astrbot")})
        self.assertNotIn("logging", self.imports)

    def test_does_not_perform_io(self):
        for forbidden in ("open", "print", "input", "compile", "eval", "exec"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, self.calls)


if __name__ == "__main__":
    unittest.main()
