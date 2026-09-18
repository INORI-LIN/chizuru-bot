import ast
import dataclasses
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from astrbot_plugin_chizuru.budget import TokenUsage
from astrbot_plugin_chizuru.config import LimitsSettings
from astrbot_plugin_chizuru.llm import (
    TRANSIENT_CODES,
    Answer,
    AnswerKind,
    FollowUp,
    LLMRequestPlan,
    ResponseFacts,
    RetryDecision,
    RetryPolicy,
    RetryReason,
    classify_error_text,
    classify_exception,
    classify_status,
    follow_up,
    interpret,
    map_usage,
)
from astrbot_plugin_chizuru.redact import ErrorCode
from astrbot_plugin_chizuru.scheduler import Deadline

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot_plugin_chizuru"

DEADLINE = Deadline(accepted_at=100.0, expires_at=200.0)
NOW = 150.0


class TripwireResponse:
    """真实响应对象的替身：一旦有人读推理字段就炸。"""

    role = "assistant"
    completion_text = "今天也要好好吃饭"

    def __init__(self, usage: object = None) -> None:
        self.usage = usage

    @property
    def reasoning_content(self) -> str:
        raise AssertionError("不允许读取推理字段")


class FrameworkUsage:
    """上游 TokenUsage 的字段形态（input_other/input_cached/output）。"""

    def __init__(self, other: int = 0, cached: int = 0, output: int = 0) -> None:
        self.input_other = other
        self.input_cached = cached
        self.output = output


def failed(code: ErrorCode) -> Answer:
    return Answer(kind=AnswerKind.FAILED, code=code)


class ResponseFactsTests(unittest.TestCase):
    def test_reads_only_the_three_allowed_attributes(self):
        facts = ResponseFacts.from_response(TripwireResponse())
        self.assertEqual(facts.role, "assistant")
        self.assertEqual(facts.text, "今天也要好好吃饭")
        self.assertIsNone(facts.usage)

    def test_maps_framework_usage(self):
        response = TripwireResponse(FrameworkUsage(other=10, cached=5, output=7))
        self.assertEqual(
            ResponseFacts.from_response(response).usage,
            TokenUsage(input_tokens=15, output_tokens=7),
        )

    def test_rejects_responses_without_usable_role(self):
        class Bare:
            completion_text = "x"

        for bad in (Bare(), object(), None, type("Empty", (), {"role": "  "})()):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    ResponseFacts.from_response(bad)

    def test_rejects_non_string_completion_text(self):
        class Odd:
            role = "assistant"
            completion_text = 42

        with self.assertRaises(ValueError):
            ResponseFacts.from_response(Odd())

    def test_carries_no_reasoning_or_raw_response_field(self):
        names = {f.name for f in dataclasses.fields(ResponseFacts)}
        self.assertEqual(names, {"role", "text", "usage"})


class InterpretTests(unittest.TestCase):
    def test_assistant_text_becomes_text_answer(self):
        for raw in ("你好呀", "  你好呀  ", "第一行\n第二行"):
            with self.subTest(raw=raw):
                answer = interpret(ResponseFacts(role="assistant", text=raw))
                self.assertIs(answer.kind, AnswerKind.TEXT)
                self.assertEqual(answer.text, raw.strip())
                self.assertIsNone(answer.code)

    def test_empty_completion_is_empty_answer(self):
        for raw in ("", "   ", "\n"):
            with self.subTest(raw=raw):
                answer = interpret(ResponseFacts(role="assistant", text=raw))
                self.assertIs(answer.kind, AnswerKind.EMPTY)
                self.assertEqual(answer.text, "")
                self.assertIs(answer.code, ErrorCode.EMPTY_REPLY)

    def test_error_role_becomes_failed_without_text(self):
        long_error = (
            'All chat models failed: Error code: 401 - {"error": '
            '{"message": "Authentication Fails, Your api key is invalid: sk-secret"}}'
        )
        answer = interpret(ResponseFacts(role="err", text=long_error))
        self.assertIs(answer.kind, AnswerKind.FAILED)
        self.assertEqual(answer.text, "")
        self.assertIs(answer.code, ErrorCode.AUTH_FAILED)
        self.assertNotIn("sk-secret", repr(answer))

    def test_unknown_error_text_is_unclassified(self):
        answer = interpret(ResponseFacts(role="err", text="something nobody has seen"))
        self.assertIs(answer.code, ErrorCode.UNCLASSIFIED)

    def test_usage_is_carried_through_every_branch(self):
        usage = TokenUsage(input_tokens=3, output_tokens=4)
        for role, text in (("assistant", "hi"), ("assistant", ""), ("err", "boom")):
            with self.subTest(role=role, text=text):
                self.assertEqual(interpret(ResponseFacts(role, text, usage)).usage, usage)

    def test_facts_argument_is_validated(self):
        with self.assertRaises(ValueError):
            interpret("assistant")


class AnswerTests(unittest.TestCase):
    def test_invariants(self):
        Answer(kind=AnswerKind.TEXT, text="你好")
        Answer(kind=AnswerKind.EMPTY, code=ErrorCode.EMPTY_REPLY)
        Answer(kind=AnswerKind.FAILED, code=ErrorCode.RATE_LIMITED)
        bad_cases = (
            {"kind": AnswerKind.TEXT},
            {"kind": AnswerKind.TEXT, "text": "  "},
            {"kind": AnswerKind.TEXT, "text": "hi", "code": ErrorCode.RATE_LIMITED},
            {"kind": AnswerKind.EMPTY, "code": ErrorCode.RATE_LIMITED},
            {"kind": AnswerKind.EMPTY, "text": "hi", "code": ErrorCode.EMPTY_REPLY},
            {"kind": AnswerKind.FAILED},
            {"kind": AnswerKind.FAILED, "code": ErrorCode.EMPTY_REPLY},
            {"kind": AnswerKind.FAILED, "code": ErrorCode.RATE_LIMITED, "text": "raw error"},
        )
        for case in bad_cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    Answer(**case)

    def test_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            Answer(kind=AnswerKind.TEXT, text="hi").text = "bye"

    def test_only_text_field_can_carry_model_output(self):
        names = {f.name for f in dataclasses.fields(Answer)}
        self.assertEqual(names, {"kind", "text", "code", "usage"})


class ClassificationTests(unittest.TestCase):
    def test_status_classification_delegates_to_the_shared_closed_set(self):
        with mock.patch.object(
            ErrorCode,
            "from_status",
            return_value=ErrorCode.RATE_LIMITED,
        ) as patched:
            self.assertIs(classify_status(418), ErrorCode.RATE_LIMITED)
            patched.assert_called_once_with(418)

    def test_exception_with_status_code_uses_status_table(self):
        class ProviderError(Exception):
            status_code = 402

        self.assertIs(classify_exception(ProviderError("boom")), ErrorCode.BALANCE_INSUFFICIENT)

    def test_network_exceptions_are_provider_unavailable(self):
        for error in (TimeoutError("slow"), ConnectionError("reset"), OSError("broken pipe")):
            with self.subTest(error=type(error).__name__):
                self.assertIs(classify_exception(error), ErrorCode.PROVIDER_UNAVAILABLE)

    def test_text_markers_are_the_fallback(self):
        cases = {
            "HTTP 429 Too Many Requests": ErrorCode.RATE_LIMITED,
            "Rate limit reached": ErrorCode.RATE_LIMITED,
            "Insufficient Balance": ErrorCode.BALANCE_INSUFFICIENT,
            "invalid api key provided": ErrorCode.AUTH_FAILED,
            "This model's maximum context length is 65536 tokens": ErrorCode.REQUEST_INVALID,
            "request timed out": ErrorCode.PROVIDER_UNAVAILABLE,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIs(classify_error_text(text), expected)

    def test_unknown_text_is_unclassified(self):
        for text in ("", "something else entirely", "千鹤今天心情很好"):
            with self.subTest(text=text):
                self.assertIs(classify_error_text(text), ErrorCode.UNCLASSIFIED)

    def test_classification_never_raises_and_never_returns_none(self):
        class Weird:
            def __str__(self) -> str:
                raise RuntimeError("no string for you")

        for error in (None, object(), Weird(), RuntimeError("boom"), 42):
            with self.subTest(error=type(error).__name__):
                code = classify_exception(error)
                self.assertIsInstance(code, ErrorCode)

    def test_marker_table_stays_small_and_literal(self):
        from astrbot_plugin_chizuru import llm

        self.assertLessEqual(len(llm._ERROR_MARKERS), 12)
        for marker, code in llm._ERROR_MARKERS:
            with self.subTest(marker=marker):
                self.assertTrue(marker)
                self.assertEqual(marker, marker.lower())
                self.assertIsInstance(code, ErrorCode)
        # HTTP 状态码只走 StatusError 与 redact 闭集，不靠文本猜。
        self.assertNotIn("422", {marker for marker, _ in llm._ERROR_MARKERS})


class RequestPlanTests(unittest.TestCase):
    def test_call_kwargs_key_set_is_exact(self):
        plan = LLMRequestPlan(prompt="你好", system_prompt="你是千鹤", max_retries=1)
        self.assertEqual(
            set(plan.call_kwargs()),
            {
                "prompt",
                "system_prompt",
                "contexts",
                "extra_user_content_parts",
                "model",
                "request_max_retries",
            },
        )

    def test_plan_carries_no_tool_surface(self):
        names = {f.name for f in dataclasses.fields(LLMRequestPlan)}
        self.assertEqual(
            names,
            {"prompt", "system_prompt", "max_retries", "contexts", "extra_user_content_parts", "model"},
        )
        for name in names:
            self.assertNotIn("tool", name)
        kwargs = LLMRequestPlan("你好", "你是千鹤", 1).call_kwargs()
        for key in kwargs:
            self.assertNotIn("tool", key)

    def test_kwargs_pass_through_the_injected_values(self):
        plan = LLMRequestPlan(
            prompt="你好",
            system_prompt="你是千鹤",
            max_retries=2,
            contexts=("history-1",),
            extra_user_content_parts=("temp-1",),
            model="deepseek-flash",
        )
        kwargs = plan.call_kwargs()
        self.assertEqual(kwargs["contexts"], ["history-1"])
        self.assertEqual(kwargs["extra_user_content_parts"], ["temp-1"])
        self.assertEqual(kwargs["model"], "deepseek-flash")
        self.assertEqual(kwargs["request_max_retries"], 2)

    def test_max_retries_must_be_injected(self):
        with self.assertRaises(TypeError):
            LLMRequestPlan(prompt="你好", system_prompt="你是千鹤")  # type: ignore[call-arg]

    def test_invariants(self):
        bad_cases = (
            {"prompt": "   ", "system_prompt": "你是千鹤", "max_retries": 1},
            {"prompt": "你好", "system_prompt": " ", "max_retries": 1},
            {"prompt": "你好", "system_prompt": "你是千鹤", "max_retries": True},
            {"prompt": "你好", "system_prompt": "你是千鹤", "max_retries": -1},
            {"prompt": "你好", "system_prompt": "你是千鹤", "max_retries": 1.0},
            {"prompt": "你好", "system_prompt": "你是千鹤", "max_retries": 1, "contexts": []},
            {
                "prompt": "你好",
                "system_prompt": "你是千鹤",
                "max_retries": 1,
                "extra_user_content_parts": [],
            },
            {"prompt": "你好", "system_prompt": "你是千鹤", "max_retries": 1, "model": ""},
            {"prompt": "你好", "system_prompt": "你是千鹤", "max_retries": 1, "model": 5},
        )
        for case in bad_cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    LLMRequestPlan(**case)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.policy = RetryPolicy(max_retries=1)

    def test_transient_set_is_exact(self):
        self.assertEqual(
            TRANSIENT_CODES,
            frozenset({ErrorCode.RATE_LIMITED, ErrorCode.PROVIDER_UNAVAILABLE}),
        )

    def test_decision_matrix(self):
        cases = {
            "first_transient": (failed(ErrorCode.PROVIDER_UNAVAILABLE), 1, True, RetryReason.TRANSIENT),
            "first_rate_limited": (failed(ErrorCode.RATE_LIMITED), 1, True, RetryReason.TRANSIENT),
            "second_transient": (failed(ErrorCode.PROVIDER_UNAVAILABLE), 2, False, RetryReason.EXHAUSTED),
            "permanent": (failed(ErrorCode.AUTH_FAILED), 1, False, RetryReason.PERMANENT),
            "unknown": (failed(ErrorCode.UNCLASSIFIED), 1, False, RetryReason.PERMANENT),
        }
        for label, (answer, attempt, expected_retry, expected_reason) in cases.items():
            with self.subTest(case=label):
                decision = self.policy.decide(
                    answer,
                    attempt=attempt,
                    deadline=DEADLINE,
                    now=NOW,
                )
                self.assertIs(decision.retry, expected_retry)
                self.assertIs(decision.reason, expected_reason)

    def test_expired_deadline_blocks_even_a_transient_failure(self):
        decision = self.policy.decide(
            failed(ErrorCode.PROVIDER_UNAVAILABLE),
            attempt=1,
            deadline=DEADLINE,
            now=DEADLINE.expires_at,
        )
        self.assertFalse(decision.retry)
        self.assertIs(decision.reason, RetryReason.DEADLINE_EXHAUSTED)

    def test_successful_and_empty_answers_are_never_retried(self):
        for answer in (
            Answer(kind=AnswerKind.TEXT, text="你好"),
            Answer(kind=AnswerKind.EMPTY, code=ErrorCode.EMPTY_REPLY),
        ):
            with self.subTest(kind=answer.kind):
                decision = self.policy.decide(answer, attempt=1, deadline=DEADLINE, now=NOW)
                self.assertFalse(decision.retry)
                self.assertIs(decision.reason, RetryReason.PERMANENT)

    def test_policy_takes_the_injected_limit(self):
        self.assertEqual(RetryPolicy.from_limits(LimitsSettings()).max_retries, 1)
        self.assertEqual(RetryPolicy.from_limits(LimitsSettings(max_retries=3)).max_retries, 3)
        self.assertEqual(RetryPolicy(max_retries=0).max_retries, 0)
        for bad in (True, -1, "1"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    RetryPolicy(max_retries=bad)
        with self.assertRaises(ValueError):
            RetryPolicy.from_limits({"max_retries": 1})

    def test_zero_policy_still_allows_the_first_answer_but_no_retry(self):
        decision = RetryPolicy(max_retries=0).decide(
            failed(ErrorCode.RATE_LIMITED),
            attempt=1,
            deadline=DEADLINE,
            now=NOW,
        )
        self.assertFalse(decision.retry)

    def test_decide_validates_arguments(self):
        with self.assertRaises(ValueError):
            self.policy.decide("answer", attempt=1, deadline=DEADLINE, now=NOW)
        with self.assertRaises(ValueError):
            self.policy.decide(failed(ErrorCode.RATE_LIMITED), attempt=0, deadline=DEADLINE, now=NOW)
        with self.assertRaises(ValueError):
            self.policy.decide(
                failed(ErrorCode.RATE_LIMITED),
                attempt=1,
                deadline=object(),
                now=NOW,
            )

    def test_decision_cannot_carry_a_new_deadline_or_backoff(self):
        names = {f.name for f in dataclasses.fields(RetryDecision)}
        self.assertEqual(names, {"retry", "reason"})
        self.assertEqual({f.name for f in dataclasses.fields(RetryPolicy)}, {"max_retries"})


class UsageMappingTests(unittest.TestCase):
    def test_sums_framework_fields(self):
        self.assertEqual(
            map_usage(FrameworkUsage(other=10, cached=5, output=7)),
            TokenUsage(input_tokens=15, output_tokens=7),
        )

    def test_missing_or_malformed_usage_is_unknown_never_zero(self):
        class Partial:
            input_other = 10

        class Negative:
            input_other = -1
            input_cached = 0
            output = 5

        class Boolean:
            input_other = True
            input_cached = 0
            output = 5

        for raw in (
            None,
            object(),
            Partial(),
            Negative(),
            Boolean(),
            FrameworkUsage(),
            "usage",
            42,
        ):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(map_usage(raw))

    def test_budget_token_usage_passes_through_but_zero_means_unknown(self):
        self.assertEqual(map_usage(TokenUsage(3, 4)), TokenUsage(input_tokens=3, output_tokens=4))
        self.assertIsNone(map_usage(TokenUsage()))


class FollowUpTests(unittest.TestCase):
    def test_matrix(self):
        cases = {
            "text": (Answer(kind=AnswerKind.TEXT, text="你好"), FollowUp.SEND_TEXT),
            "empty": (Answer(kind=AnswerKind.EMPTY, code=ErrorCode.EMPTY_REPLY), FollowUp.FIXED_NOTICE),
            "balance": (failed(ErrorCode.BALANCE_INSUFFICIENT), FollowUp.FIXED_NOTICE),
            "auth": (failed(ErrorCode.AUTH_FAILED), FollowUp.FIXED_NOTICE),
            "request_invalid": (failed(ErrorCode.REQUEST_INVALID), FollowUp.FIXED_NOTICE),
            "rate_limited": (failed(ErrorCode.RATE_LIMITED), FollowUp.FIXED_NOTICE),
            "provider_unavailable": (failed(ErrorCode.PROVIDER_UNAVAILABLE), FollowUp.FIXED_NOTICE),
            "unknown_failure": (failed(ErrorCode.UNCLASSIFIED), FollowUp.NOTHING),
            "revision_changed": (failed(ErrorCode.REVISION_CHANGED), FollowUp.NOTHING),
        }
        for label, (answer, expected) in cases.items():
            with self.subTest(case=label):
                self.assertIs(follow_up(answer), expected)

    def test_background_work_never_speaks_in_the_group(self):
        for answer in (
            Answer(kind=AnswerKind.TEXT, text="你好"),
            Answer(kind=AnswerKind.EMPTY, code=ErrorCode.EMPTY_REPLY),
            failed(ErrorCode.RATE_LIMITED),
            failed(ErrorCode.UNCLASSIFIED),
        ):
            with self.subTest(kind=answer.kind, code=answer.code):
                self.assertIs(follow_up(answer, background=True), FollowUp.NOTHING)

    def test_arguments_are_validated(self):
        with self.assertRaises(ValueError):
            follow_up("answer")
        with self.assertRaises(ValueError):
            follow_up(Answer(kind=AnswerKind.TEXT, text="你好"), background=1)


class StructuralGuaranteeTests(unittest.TestCase):
    """工具、推理与文案都不进这个模块——这些必须是结构性的。"""

    def setUp(self):
        self.source = (PLUGIN_ROOT / "llm.py").read_text()
        self.tree = ast.parse(self.source)
        self.imports = {
            node.module or "" for node in ast.walk(self.tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.attributes = {
            node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)
        }
        self.calls = {
            node.func.id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.names = {
            node.name
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_pure_module_does_not_import_the_framework(self):
        self.assertFalse({name for name in self.imports if name.startswith("astrbot")})

    def test_never_sends_logs_or_touches_the_ledger(self):
        for forbidden in ("send", "send_message", "logging", "getLogger"):
            with self.subTest(name=forbidden):
                self.assertNotIn(forbidden, self.attributes)
        self.assertNotIn("logging", self.imports)
        for forbidden in ("reserve", "settle", "cancel"):
            with self.subTest(call=forbidden):
                self.assertFalse({name for name in self.names if forbidden in name})

    def test_reasoning_is_never_mentioned(self):
        self.assertNotIn("reasoning", self.source.lower())

    def test_no_user_facing_copy(self):
        # 文档字符串与枚举成员的说明性字符串不算文案；其余字符串不得像一句话。
        documentation = {
            node.value.value
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in documentation:
                continue
            with self.subTest(text=node.value):
                for punctuation in ("。", "！", "？"):
                    self.assertNotIn(punctuation, node.value)

    def test_tool_parameters_do_not_appear_even_as_strings(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                with self.subTest(text=node.value):
                    self.assertNotIn("func_tool", node.value)
                    self.assertNotIn("tool_choice", node.value)


if __name__ == "__main__":
    unittest.main()
