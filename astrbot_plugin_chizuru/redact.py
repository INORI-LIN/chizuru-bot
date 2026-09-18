"""日志与脱敏：把可落地的日志字段收敛成**结构上写不进正文**的记录（S1-13）。

只记类别、耗时、错误码、token 与脱敏关联标识；不记正文、请求体、密钥、推理文本。
这不是靠"记得别写"的约定，而是靠类型：`AuditRecord` 没有能放自由文本的字段，
错误码必须是 `ErrorCode` 的取值——传一段异常文本会直接报错。

关联标识（架构 §11.1「用脱敏关联标识定位请求」）用**每进程随机盐**做摘要：
无盐摘要对 5—11 位 QQ 号可在秒级穷举，等于变相记录一份可还原的映射表；随机盐
只存在于内存，不落盘、不进日志。代价是跨重启无法关联同一条目——这是刻意取舍。

保留与大小（架构 §11.1）：默认 7 天（复用配置里的 ``log_retention_days``）与总大小
上限 `DEFAULT_LOG_TOTAL_BYTES`。**本模块只定义策略，不执行日志 IO、不轮转文件**；
强制与核验归 S1-14 的装配与 S4-04 的审计。上游日志不做任何脱敏
（``astrbot/core/log.py`` 原样转发格式化文本，WebUI 另存 500 条缓存），
所以只能靠"只经由本模块记录"来保证；审计清单见 `AUDIT_CHECKLIST`。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

from .budget import TokenUsage
from .config import LogSettings

DEFAULT_LOG_TOTAL_BYTES = 20 * 1024 * 1024
"""日志总大小上限（20 MiB），**建议参数待评审**。

取值与上游 ``log_file_max_mb`` 的默认值一致：插件与框架共用同一个日志文件，
总量的合理期望就是单文件上限；超过它说明还存在未被排除的副本（trace 文件、
WebUI 缓存、NapCat 日志），需要在 S4-04 审计里处理。与 ``budget.EXTRACTION_STOP_RATIO``
同例：需求只给了"限制总大小"，系数由本模块提出并等待评审。
"""

_CORRELATION_PATTERN = re.compile(r"[0-9a-f]{32}")
_MAX_PART_LENGTH = 128
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _require_optional_count(value: object, name: str) -> None:
    if value is None:
        return
    # bool 是 int 的子类，必须显式排除。
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} 必须是非负整数或 None：{value!r}")


class EventCategory(StrEnum):
    """日志类别。与架构 §11.1 的"记录"清单逐项对应。"""

    PLATFORM_STATE = "platform_state"
    VALID_AT = "valid_at"
    """有效 @ 计数。"""

    IGNORED = "ignored"
    """被忽略的事件类别。"""

    QUEUE_DEPTH = "queue_depth"
    MODEL_CALL = "model_call"
    """模型耗时与错误码。"""

    TOKEN_USAGE = "token_usage"
    SEND_RESULT = "send_result"
    GATE_DROP = "gate_drop"
    MEMORY_OP = "memory_op"
    """记忆操作成功/失败计数。"""

    CONTEXT_OP = "context_op"
    """普通群上下文操作（退出/加入、采集状态）成功/失败计数。"""

    CLEANUP_FAILURE = "cleanup_failure"
    DEGRADATION = "degradation"


class ErrorCode(StrEnum):
    """错误码闭集。与架构 §8.3 的故障矩阵逐行对应。"""

    REQUEST_INVALID = "request_invalid"
    """400/422：请求参数错误，不自动重试。"""

    AUTH_FAILED = "auth_failed"
    """401：鉴权失败，停用异常提供商调用。"""

    BALANCE_INSUFFICIENT = "balance_insufficient"
    """402：余额不足，暂停付费请求。"""

    RATE_LIMITED = "rate_limited"
    """429：限流。"""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """5xx、连接中断或超时：最多重试 1 次，总期限不重置。"""

    EMPTY_REPLY = "empty_reply"
    """模型空结果：聊天给固定提示，抽取直接放弃。"""

    QUEUE_FULL = "queue_full"
    """排队已满：对有效 @ 返回一次固定忙碌提示。"""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """本地预算耗尽：停止新模型任务，抽取优先停用。"""

    REVISION_CHANGED = "revision_changed"
    """授权或数据修订号变化：丢弃在途结果。"""

    DELETE_FAILED = "delete_failed"
    """删除请求执行失败：保持相关读写关闭，不虚报已删除。"""

    MEMORY_STORE_FAILED = "memory_store_failed"
    """记忆库读取失败：停止受影响的采集、调用与发送。"""

    UNCLASSIFIED = "unclassified"
    """本模块不认识的错误码。**不猜成已知分类。**"""

    @classmethod
    def from_status(cls, status: int) -> "ErrorCode":
        """HTTP 状态码 → 闭集取值；未知码一律 ``UNCLASSIFIED``。"""
        if not isinstance(status, int) or isinstance(status, bool):
            raise ValueError("status 必须是整数状态码")
        if status in (400, 422):
            return cls.REQUEST_INVALID
        if status == 401:
            return cls.AUTH_FAILED
        if status == 402:
            return cls.BALANCE_INSUFFICIENT
        if status == 429:
            return cls.RATE_LIMITED
        if 500 <= status <= 599:
            return cls.PROVIDER_UNAVAILABLE
        return cls.UNCLASSIFIED


@dataclass(frozen=True)
class CorrelationId:
    """脱敏关联标识：32 位小写十六进制摘要，不可还原为账号 ID。"""

    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or not _CORRELATION_PATTERN.fullmatch(self.digest):
            raise ValueError("CorrelationId 需要 32 位小写十六进制摘要")


class Redactor:
    """关联标识签发器。盐只存在于本进程内存中。"""

    def __init__(self, *, salt: bytes | None = None) -> None:
        if salt is None:
            salt = secrets.token_bytes(32)
        if not isinstance(salt, bytes) or len(salt) < 16:
            raise ValueError("salt 必须是至少 16 字节的 bytes")
        self._salt = salt

    def correlation(self, *parts: str) -> CorrelationId:
        """为标识段（平台实例、群、成员、message_id、动作）签发关联标识。

        只接受**标识**：每一段都必须是非空、无首尾空白、无控制字符、不超过
        ``_MAX_PART_LENGTH`` 的字符串。正文不应传进来——传了也会被长度与字符检查挡下。
        """
        if not parts:
            raise ValueError("关联标识至少需要一段")
        for part in parts:
            if not isinstance(part, str) or not part or part != part.strip():
                raise ValueError("关联标识的每一段都必须是非空、无首尾空白的字符串")
            if len(part) > _MAX_PART_LENGTH:
                raise ValueError(f"关联标识的每一段不得超过 {_MAX_PART_LENGTH} 字符")
            if _CONTROL_CHARS.search(part):
                raise ValueError("关联标识的每一段不得包含控制字符")
        material = b"\x1f".join(part.encode("utf-8") for part in parts)
        return CorrelationId(hashlib.blake2s(self._salt + material, digest_size=16).hexdigest())


@dataclass(frozen=True)
class AuditRecord:
    """一条可落日志的记录。

    **没有任何能放自由文本的字段**：类别、错误码、耗时、token、计数、关联标识。
    正文、请求体、密钥与推理文本在类型上无处可放（R-DATA、R-OPS）。
    """

    category: EventCategory
    code: ErrorCode | None = None
    duration_ms: int | None = None
    tokens: TokenUsage | None = None
    count: int | None = None
    correlation: CorrelationId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, EventCategory):
            raise ValueError("category 必须是 EventCategory")
        # 字符串型错误码一律拒绝：那正是"把异常文本塞进日志"的入口。
        if self.code is not None and not isinstance(self.code, ErrorCode):
            raise ValueError("code 必须是 ErrorCode 或 None")
        _require_optional_count(self.duration_ms, "duration_ms")
        _require_optional_count(self.count, "count")
        if self.tokens is not None and not isinstance(self.tokens, TokenUsage):
            raise ValueError("tokens 必须是 TokenUsage 或 None")
        if self.correlation is not None and not isinstance(self.correlation, CorrelationId):
            raise ValueError("correlation 必须是 CorrelationId 或 None")


def format_record(record: AuditRecord) -> str:
    """确定性单行文本。键序固定，字段都是枚举/整数/摘要，无法注入换行或正文。"""
    if not isinstance(record, AuditRecord):
        raise ValueError("record 必须是 AuditRecord")
    parts = [f"category={record.category.value}"]
    if record.code is not None:
        parts.append(f"code={record.code.value}")
    if record.duration_ms is not None:
        parts.append(f"duration_ms={record.duration_ms}")
    if record.tokens is not None:
        parts.append(
            f"tokens_in={record.tokens.input_tokens} tokens_out={record.tokens.output_tokens}"
        )
    if record.count is not None:
        parts.append(f"count={record.count}")
    if record.correlation is not None:
        parts.append(f"correlation={record.correlation.digest}")
    return " ".join(parts)


@dataclass(frozen=True)
class RetentionPolicy:
    """保留期与总大小上限。策略对象，不是执行器（执行归 S1-14/S4-04）。"""

    retention_days: int
    max_total_bytes: int = DEFAULT_LOG_TOTAL_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.retention_days, int) or isinstance(self.retention_days, bool):
            raise ValueError("retention_days 必须是整数")
        if self.retention_days < 1:
            raise ValueError("retention_days 必须为正")
        if (
            not isinstance(self.max_total_bytes, int)
            or isinstance(self.max_total_bytes, bool)
            or self.max_total_bytes < 1
        ):
            raise ValueError("max_total_bytes 必须是正整数")

    @classmethod
    def from_settings(cls, log: LogSettings) -> "RetentionPolicy":
        if not isinstance(log, LogSettings):
            raise ValueError("log 必须是 LogSettings")
        return cls(retention_days=log.retention_days)


@dataclass(frozen=True)
class AuditItem:
    """审计清单的一项：查哪里、查什么、合格线是什么。"""

    topic: str
    checkpoint: str
    expectation: str

    def __post_init__(self) -> None:
        for name, value in (
            ("topic", self.topic),
            ("checkpoint", self.checkpoint),
            ("expectation", self.expectation),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")


AUDIT_CHECKLIST: tuple[AuditItem, ...] = (
    AuditItem(
        topic="插件自身日志",
        checkpoint="所有记录都经 redact.format_record 后写入 astrbot.plugin.<插件名> 通道",
        expectation="任何一行都不含聊天正文、请求体、密钥、推理文本或异常堆栈",
    ),
    AuditItem(
        topic="AstrBot 日志文件",
        checkpoint="log_file_path（默认 logs/astrbot.log）与 log_file_max_mb",
        expectation="上游不做脱敏，文件中不得出现群消息原文或 ws_reverse_token",
    ),
    AuditItem(
        topic="WebUI 日志缓存",
        checkpoint="LogBroker.log_cache（deque(maxlen=500)）与 WebUI 的访问范围",
        expectation="缓存与文件日志同源，同样不得含敏感内容；管理面不对公网暴露",
    ),
    AuditItem(
        topic="轮转与保留",
        checkpoint="上游 backup_count 硬编码为 3、无保留天数配置；本插件 RetentionPolicy",
        expectation="按 retention_days 定期清理；总大小超过 max_total_bytes 即说明存在额外副本",
    ),
    AuditItem(
        topic="OneBot 鉴权 Token",
        checkpoint="ws_reverse_token 非空，且只在框架运行时配置中",
        expectation="不出现在日志、千鹤 状态输出与仓库任何文件中",
    ),
    AuditItem(
        topic="平台错误详情",
        checkpoint="Platform.get_stats() 的 last_error.traceback / last_error.message",
        expectation="不得进入日志、状态输出或群消息；本插件只记 error_count（health.py）",
    ),
    AuditItem(
        topic="NapCat 自身日志",
        checkpoint="独立进程的日志级别与目录",
        expectation="默认级别下不落消息正文；保留期与大小同样按本策略复核",
    ),
    AuditItem(
        topic="发送语义与运行数据副本",
        checkpoint="event.send() 无 message_id、after_message_sent 不是已读回执；宿主机快照与同步盘",
        expectation="不据此宣称送达；运行数据排除出备份与同步盘（架构 §11.3）",
    ),
)
