"""提供商调用封装与错误分类（S1-10，离线部分）。

**调用路径是直调提供商**（附录 D.2）：handler 侧取到提供商实例后按
`LLMRequestPlan.call_kwargs()` 调用 `Provider.text_chat(...)`，本模块负责把
"要问什么、拿回来的算什么、出错算哪一类、还能不能再试一次" 全部收敛成确定逻辑。
`provider_settings.enable=false` 是这条路径的前提：框架侧的模型路径整体关闭
（K14），插件自己的调用不受影响。

**本模块是纯逻辑**：不导入框架、不建网关与客户端、不发送、不读写历史、不 reserve
也不结算金额。它只做四件事：

- **请求成形**：`LLMRequestPlan` 的字段与 `call_kwargs()` 的键集固定，**没有任何
  工具参数**——"不追加工具权限"于是成为结构事实，而不是约定。
- **回复解释**：`ResponseFacts` 只读 `role` 与 `completion_text`，**从不读取推理
  字段**；`Answer` 的文本分支是唯一能携带模型输出形态，且 `failed` 分支禁止携带
  文本——`role="err"` 的原始报错文本（可能含 URL、密钥片段）永远到不了发送路径。
- **错误分类**：状态码 → `redact.ErrorCode`（同一份闭集，不另立表）；异常 →
  错误码；文本标记表只作兜底，不认识的输入落 `UNCLASSIFIED`，不猜。
- **重试与去向**：`RetryPolicy` 只回答"能不能再试一次"（≤ 配置次数、只对暂时故障、
  受同一个 `Deadline` 约束，**从不创建新期限**）；`FollowUp` 只回答"这次该在群里
  产生什么类别"（不含任何文案）。

在线部分仍未验证：真实 401/402/429/5xx 的可分类性、usage 的实际可读性，以及框架与
SDK 叠加后的真实请求次数（S0-07、R19、S4-02/G06）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .budget import TokenUsage
from .config import LimitsSettings
from .redact import ErrorCode
from .scheduler import Deadline

_ERROR_ROLE = "err"

# 兜底标记表：只在拿不到状态码与异常类型时使用。全部小写字面量，无正则。
_ERROR_MARKERS: tuple[tuple[str, ErrorCode], ...] = (
    ("401", ErrorCode.AUTH_FAILED),
    ("unauthorized", ErrorCode.AUTH_FAILED),
    ("invalid api key", ErrorCode.AUTH_FAILED),
    ("402", ErrorCode.BALANCE_INSUFFICIENT),
    ("insufficient balance", ErrorCode.BALANCE_INSUFFICIENT),
    ("429", ErrorCode.RATE_LIMITED),
    ("rate limit", ErrorCode.RATE_LIMITED),
    ("503", ErrorCode.PROVIDER_UNAVAILABLE),
    ("timeout", ErrorCode.PROVIDER_UNAVAILABLE),
    ("timed out", ErrorCode.PROVIDER_UNAVAILABLE),
    ("connection", ErrorCode.PROVIDER_UNAVAILABLE),
    ("context length", ErrorCode.REQUEST_INVALID),
)

TRANSIENT_CODES = frozenset({ErrorCode.RATE_LIMITED, ErrorCode.PROVIDER_UNAVAILABLE})
"""可重试的错误码（架构 §8.3）：限流与 5xx/断连。其余一律不重试。"""

_NOTICE_CODES = frozenset(
    {
        ErrorCode.REQUEST_INVALID,
        ErrorCode.AUTH_FAILED,
        ErrorCode.BALANCE_INSUFFICIENT,
        ErrorCode.RATE_LIMITED,
        ErrorCode.PROVIDER_UNAVAILABLE,
        ErrorCode.EMPTY_REPLY,
        ErrorCode.QUEUE_FULL,
        ErrorCode.BUDGET_EXHAUSTED,
    }
)
"""§8.3 中"仅对已有有效 @ 给简短提示"的失败类别。未知类别不在此列。"""


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串：{value!r}")
    return value


# ---- 请求 ----


@dataclass(frozen=True)
class LLMRequestPlan:
    """一次模型调用的请求形态。

    字段就是全部可调项：**没有工具、没有重试以外的参数**。`max_retries` 没有默认值，
    必须由调用方从 `LimitsSettings.max_retries` 注入——本模块不替部署做决定。
    """

    prompt: str
    system_prompt: str
    max_retries: int
    contexts: tuple[object, ...] = ()
    extra_user_content_parts: tuple[object, ...] = ()
    model: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.prompt, "prompt")
        # 固定规则缺失就不调用模型：宁可失败，也不能在没有边界的提示下说话。
        _require_text(self.system_prompt, "system_prompt")
        if (
            not isinstance(self.max_retries, int)
            or isinstance(self.max_retries, bool)
            or self.max_retries < 0
        ):
            raise ValueError(f"max_retries 必须是非负整数：{self.max_retries!r}")
        for name, value in (
            ("contexts", self.contexts),
            ("extra_user_content_parts", self.extra_user_content_parts),
        ):
            if not isinstance(value, tuple):
                raise ValueError(f"{name} 必须是 tuple")
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError("model 必须是字符串或 None")
        if self.model == "":
            raise ValueError("model 不得为空字符串：用 None 表示提供商的当前模型")

    def call_kwargs(self) -> dict[str, object]:
        """映射到 `Provider.text_chat` 的参数。键集固定，**不含任何工具参数**。"""
        return {
            "prompt": self.prompt,
            "system_prompt": self.system_prompt,
            "contexts": list(self.contexts),
            "extra_user_content_parts": list(self.extra_user_content_parts),
            "model": self.model,
            "request_max_retries": self.max_retries,
        }


# ---- 回复 ----


class AnswerKind(StrEnum):
    TEXT = "text"
    """有可发送的正式回复内容。"""

    EMPTY = "empty"
    """模型返回空结果：聊天给固定提示，抽取直接放弃。"""

    FAILED = "failed"
    """调用失败：只有错误码，没有任何模型文本。"""


@dataclass(frozen=True)
class Answer:
    """一次调用的结论。`text` 是**唯一**能携带模型输出的字段。"""

    kind: AnswerKind
    text: str = ""
    code: ErrorCode | None = None
    usage: TokenUsage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AnswerKind):
            raise ValueError("kind 必须是 AnswerKind")
        if not isinstance(self.text, str):
            raise ValueError("text 必须是字符串")
        if self.code is not None and not isinstance(self.code, ErrorCode):
            raise ValueError("code 必须是 ErrorCode 或 None")
        if self.usage is not None and not isinstance(self.usage, TokenUsage):
            raise ValueError("usage 必须是 TokenUsage 或 None")
        if self.kind is AnswerKind.TEXT:
            if not self.text.strip() or self.code is not None:
                raise ValueError("text 分支必须带非空文本且没有错误码")
        elif self.kind is AnswerKind.EMPTY:
            if self.text or self.code is not ErrorCode.EMPTY_REPLY:
                raise ValueError("empty 分支必须为空文本且错误码为 empty_reply")
        else:
            if self.text:
                raise ValueError("failed 分支不得携带任何模型文本")
            if self.code is None or self.code is ErrorCode.EMPTY_REPLY:
                raise ValueError("failed 分支必须有 empty_reply 以外的错误码")


@dataclass(frozen=True)
class ResponseFacts:
    """从提供商返回对象里提取的**全部**可用事实：`role`、正式文本、usage。

    没有推理字段，也没有原始响应对象——想读也无处可读。
    """

    role: str
    text: str = ""
    usage: TokenUsage | None = None

    def __post_init__(self) -> None:
        _require_text(self.role, "role")
        if not isinstance(self.text, str):
            raise ValueError("text 必须是字符串")
        if self.usage is not None and not isinstance(self.usage, TokenUsage):
            raise ValueError("usage 必须是 TokenUsage 或 None")

    @classmethod
    def from_response(cls, response: object) -> "ResponseFacts":
        """只鸭子读 ``role`` / ``completion_text`` / ``usage`` 三个属性。"""
        role = getattr(response, "role", None)
        if not isinstance(role, str) or not role.strip():
            raise ValueError("响应对象缺少可用的 role")
        text = getattr(response, "completion_text", "")
        if not isinstance(text, str):
            raise ValueError("completion_text 必须是字符串")
        return cls(role=role, text=text, usage=map_usage(getattr(response, "usage", None)))


def interpret(facts: ResponseFacts) -> Answer:
    """把响应事实解释成结论；失败时**不外传** `role="err"` 的原始文本。"""
    if not isinstance(facts, ResponseFacts):
        raise ValueError("facts 必须是 ResponseFacts")
    if facts.role == _ERROR_ROLE:
        return Answer(
            kind=AnswerKind.FAILED,
            code=classify_error_text(facts.text),
            usage=facts.usage,
        )
    text = facts.text.strip()
    if not text:
        return Answer(kind=AnswerKind.EMPTY, code=ErrorCode.EMPTY_REPLY, usage=facts.usage)
    return Answer(kind=AnswerKind.TEXT, text=text, usage=facts.usage)


# ---- 错误分类 ----


def classify_status(status: int) -> ErrorCode:
    """HTTP 状态码 → 错误码。委托 `redact.ErrorCode.from_status`，不另立表。"""
    return ErrorCode.from_status(status)


def classify_error_text(text: str) -> ErrorCode:
    """按小写标记表兜底分类；不认识的一律 `UNCLASSIFIED`（不猜）。"""
    if not isinstance(text, str):
        return ErrorCode.UNCLASSIFIED
    lowered = text.lower()
    for marker, code in _ERROR_MARKERS:
        if marker in lowered:
            return code
    return ErrorCode.UNCLASSIFIED


def classify_exception(error: object) -> ErrorCode:
    """异常 → 错误码。**不抛异常、不返回 None**（分类失败也要有结论）。"""
    status = _status_of(error)
    if status is not None:
        return classify_status(status)
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return ErrorCode.PROVIDER_UNAVAILABLE
    if error is None:
        return ErrorCode.UNCLASSIFIED
    try:
        text = str(error)
    except Exception:
        # 连 __str__ 都坏掉的异常不该让失败路径再抛一次。
        return ErrorCode.UNCLASSIFIED
    return classify_error_text(text)


def _status_of(error: object) -> int | None:
    for name in ("status_code", "status"):
        value = getattr(error, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


# ---- 重试 ----


class RetryReason(StrEnum):
    TRANSIENT = "transient"
    """暂时故障，且还有次数与剩余时间。"""

    PERMANENT = "permanent"
    """不是暂时故障（含结果非失败）：重试只会重复消耗。"""

    EXHAUSTED = "exhausted"
    """次数已用完（不含本次）。"""

    DEADLINE_EXHAUSTED = "deadline_exhausted"
    """业务期限已到：重试不得重置期限。"""


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    reason: RetryReason

    def __post_init__(self) -> None:
        if not isinstance(self.retry, bool):
            raise ValueError("retry 必须是布尔值")
        if not isinstance(self.reason, RetryReason):
            raise ValueError("reason 必须是 RetryReason")
        retryable = self.reason is RetryReason.TRANSIENT
        if self.retry is not retryable:
            raise ValueError("retry 与 reason 必须一致")


@dataclass(frozen=True)
class RetryPolicy:
    """重试判定：≤ 配置次数、只对暂时故障、**共用同一个 `Deadline`**。

    退避时长不在这里：架构 §8.3 只要求"期限内有限退避"，具体秒数要等真实 429 行为
    测量后确定（S4-02），先写一个数字就是把未核验的假设写成参数。
    """

    max_retries: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_retries, int)
            or isinstance(self.max_retries, bool)
            or self.max_retries < 0
        ):
            raise ValueError(f"max_retries 必须是非负整数：{self.max_retries!r}")

    @classmethod
    def from_limits(cls, limits: LimitsSettings) -> "RetryPolicy":
        if not isinstance(limits, LimitsSettings):
            raise ValueError("limits 必须是 LimitsSettings")
        return cls(max_retries=limits.max_retries)

    def decide(
        self,
        answer: Answer,
        *,
        attempt: int,
        deadline: Deadline,
        now: float,
    ) -> RetryDecision:
        """`attempt` 是**已经完成**的尝试次数（首次调用后为 1）。"""
        if not isinstance(answer, Answer):
            raise ValueError("answer 必须是 Answer")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError(f"attempt 必须是正整数：{attempt!r}")
        if not isinstance(deadline, Deadline):
            raise ValueError("deadline 必须是 Deadline")
        if answer.kind is not AnswerKind.FAILED:
            return RetryDecision(False, RetryReason.PERMANENT)
        if answer.code not in TRANSIENT_CODES:
            return RetryDecision(False, RetryReason.PERMANENT)
        if attempt > self.max_retries:
            return RetryDecision(False, RetryReason.EXHAUSTED)
        if deadline.remaining(now) <= 0:
            return RetryDecision(False, RetryReason.DEADLINE_EXHAUSTED)
        return RetryDecision(True, RetryReason.TRANSIENT)


# ---- 用量 ----


def map_usage(raw: object) -> TokenUsage | None:
    """把提供商的 usage 映射为预算用的 `TokenUsage`。

    缺失、畸形或**全零**一律返回 `None`（未知）：框架适配器在 API 未返回 usage 时
    会塞一个全零对象，与"真的用了 0 token"无法区分，因此按未知处理，由
    `budget.settle` 按预留估算——**绝不记成 0**（架构 §7.2、K10）。
    """
    if raw is None:
        return None
    if isinstance(raw, TokenUsage):
        return raw if (raw.input_tokens or raw.output_tokens) else None
    values: list[int] = []
    for name in ("input_other", "input_cached", "output"):
        value = getattr(raw, name, None)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        values.append(value)
    input_tokens = values[0] + values[1]
    output_tokens = values[2]
    if not input_tokens and not output_tokens:
        return None
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)


# ---- 去向 ----


class FollowUp(StrEnum):
    """这次调用的结果该在群里产生什么类别。**不含文案**（文案在 `fixed_notice`）。"""

    SEND_TEXT = "send_text"
    FIXED_NOTICE = "fixed_notice"
    NOTHING = "nothing"


def follow_up(answer: Answer, *, background: bool = False) -> FollowUp:
    """判定群内去向。

    后台（自动抽取）恒为 `NOTHING`：架构 §8.3 要求后台故障不发群通知。未知错误码
    同样不发话——宁可沉默，也不把未分类的故障说成用户能理解的原因。
    """
    if not isinstance(answer, Answer):
        raise ValueError("answer 必须是 Answer")
    if not isinstance(background, bool):
        raise ValueError("background 必须是布尔值")
    if background:
        return FollowUp.NOTHING
    if answer.kind is AnswerKind.TEXT:
        return FollowUp.SEND_TEXT
    if answer.code in _NOTICE_CODES:
        return FollowUp.FIXED_NOTICE
    return FollowUp.NOTHING
