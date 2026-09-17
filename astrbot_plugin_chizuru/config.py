"""插件配置：fail-closed 边界。

任何字段缺失或畸形都让整个 Settings 退回哨兵值（空 platform_id / self_id /
允许群），从而拒绝全部处理。部分有效的配置不是"更宽松"，而是不可接受的：
一条无意放行的群开关或成员授权就足以违反 R-DATA。

配置只承载**部署参数与显式授权映射**。每群的开关、告知确认、成员退出与授权
状态属于运行时状态，存在插件 SQLite（架构 §6.1），不在这里。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


def is_qq_id(value: object) -> bool:
    """QQ 号：ASCII 十进制、不以 0 开头、非空。排除 bool 与全角数字。"""
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and not value.startswith("0")
    )


def _is_plain_str(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _is_bounded_int(value: object, low: int, high: int) -> bool:
    # bool 是 int 的子类，必须显式排除，否则 True/False 会被当作 1/0。
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _is_flag(value: object) -> bool:
    return isinstance(value, bool)


def _is_non_negative_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )


@dataclass(frozen=True)
class MaintainerMap:
    """群 ID → 已授权维护者 ID 的集合。

    这是**显式配置授权**：QQ 群管理员身份不自动等于维护者（需求 §4.2）。
    """

    mapping: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def maintainers_of(self, group_id: str) -> frozenset[str]:
        return self.mapping.get(group_id, frozenset())

    def is_maintainer(self, group_id: str, member_id: str) -> bool:
        return member_id in self.mapping.get(group_id, frozenset())


@dataclass(frozen=True)
class ContextSettings:
    """普通群聊缓冲与互动历史窗口（需求 §6 建议初值）。"""

    buffer_max_messages: int = 30
    buffer_ttl_seconds: int = 600
    history_max_turns: int = 20
    history_ttl_hours: int = 24


@dataclass(frozen=True)
class MemorySettings:
    """授权长期记忆。默认关闭，且预算未配置时不生效（见 BudgetSettings）。"""

    max_records_per_member: int = 20
    ttl_days: int = 90
    auth_confirm_ttl_seconds: int = 300
    extraction_enabled: bool = False


@dataclass(frozen=True)
class BudgetSettings:
    """用量与预算。

    金额为 0 表示**未配置**：此时自动记忆抽取保持关闭（需求 §6），而不是
    "不限额度"。
    """

    input_token_budget: int = 8192
    output_token_budget: int = 512
    daily_amount: float = 0.0
    monthly_amount: float = 0.0
    require_budget_for_extraction: bool = True

    @property
    def budget_configured(self) -> bool:
        return self.daily_amount > 0 or self.monthly_amount > 0

    def extraction_allowed(self, requested: bool) -> bool:
        """抽取是否真正可运行：需要成员开关与（若要求）预算同时满足。"""
        if not requested:
            return False
        if self.require_budget_for_extraction and not self.budget_configured:
            return False
        return True


@dataclass(frozen=True)
class LimitsSettings:
    """并发、队列与期限（需求 §6 建议初值）。"""

    provider_concurrency_global: int = 2
    provider_concurrency_per_group: int = 1
    chat_queue_per_group: int = 3
    extraction_queue_global: int = 20
    schedule_wait_seconds: int = 30
    task_deadline_seconds: int = 45
    connect_timeout_seconds: int = 5
    read_timeout_seconds: int = 30
    max_retries: int = 1


@dataclass(frozen=True)
class LogSettings:
    retention_days: int = 7


@dataclass(frozen=True)
class Settings:
    """部署级配置。空 platform_id / self_id / allowed_group_ids 即拒绝全部。"""

    platform_id: str = ""
    self_id: str = ""
    allowed_group_ids: frozenset[str] = frozenset()
    maintainers: MaintainerMap = field(default_factory=MaintainerMap)
    notice_version: str = "notice-1"
    context: ContextSettings = field(default_factory=ContextSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    limits: LimitsSettings = field(default_factory=LimitsSettings)
    log: LogSettings = field(default_factory=LogSettings)

    @property
    def identity_configured(self) -> bool:
        """身份与允许群是否齐备；任缺即整插件拒绝处理。"""
        return bool(self.platform_id and self.self_id and self.allowed_group_ids)

    def allows_group(self, platform_id: str, self_id: str, group_id: str) -> bool:
        """三重校验：平台连接实例、机器人实例、群都要对上（架构 §6.1）。"""
        return (
            self.identity_configured
            and platform_id == self.platform_id
            and self_id == self.self_id
            and group_id in self.allowed_group_ids
        )

    # ---- 构造 ----

    @classmethod
    def from_mapping(cls, config: Mapping[str, object]) -> "Settings":
        """从插件配置构造；任一字段不合格即返回哨兵 Settings()（拒绝全部）。"""
        try:
            return cls._parse(config)
        except (KeyError, TypeError, ValueError):
            return cls()

    @classmethod
    def _parse(cls, config: Mapping[str, object]) -> "Settings":
        # 未提供的键回落到默认值：只有"提供了但不合格"才拒绝。
        # AstrBot 会补齐 schema 默认，但调用方可能只传关心的一部分。
        merged = default_fields()
        for key in merged:
            if key in config:
                merged[key] = config[key]
        config = merged

        platform_id = config["platform_id"]
        self_id = config["self_id"]
        group_ids = config["allowed_group_ids"]
        if not _is_plain_str(platform_id) or not is_qq_id(self_id):
            raise ValueError("platform_id / self_id 不合格")
        if not isinstance(group_ids, list) or not all(is_qq_id(g) for g in group_ids):
            raise ValueError("allowed_group_ids 不合格")

        notice_version = config["notice_version"]
        if not _is_plain_str(notice_version):
            raise ValueError("notice_version 不合格")

        return cls(
            platform_id=platform_id,
            self_id=self_id,
            allowed_group_ids=frozenset(group_ids),
            maintainers=_parse_maintainers(config["group_maintainers"]),
            notice_version=notice_version,
            context=_parse_context(config),
            memory=_parse_memory(config),
            budget=_parse_budget(config),
            limits=_parse_limits(config),
            log=_parse_log(config),
        )


def default_fields() -> dict[str, object]:
    """把哨兵 Settings 展平成扁平映射，作为 from_mapping 的兜底默认值。"""
    sentinel = Settings()
    return {
        "platform_id": sentinel.platform_id,
        "self_id": sentinel.self_id,
        "allowed_group_ids": [],
        "group_maintainers": {},
        "notice_version": sentinel.notice_version,
        "context_buffer_max_messages": sentinel.context.buffer_max_messages,
        "context_buffer_ttl_seconds": sentinel.context.buffer_ttl_seconds,
        "interaction_history_max_turns": sentinel.context.history_max_turns,
        "interaction_history_ttl_hours": sentinel.context.history_ttl_hours,
        "memory_max_records_per_member": sentinel.memory.max_records_per_member,
        "memory_ttl_days": sentinel.memory.ttl_days,
        "auth_confirm_ttl_seconds": sentinel.memory.auth_confirm_ttl_seconds,
        "memory_extraction_enabled": sentinel.memory.extraction_enabled,
        "input_token_budget": sentinel.budget.input_token_budget,
        "output_token_budget": sentinel.budget.output_token_budget,
        "daily_budget_amount": sentinel.budget.daily_amount,
        "monthly_budget_amount": sentinel.budget.monthly_amount,
        "require_budget_for_extraction": sentinel.budget.require_budget_for_extraction,
        "provider_concurrency_global": sentinel.limits.provider_concurrency_global,
        "provider_concurrency_per_group": sentinel.limits.provider_concurrency_per_group,
        "chat_queue_per_group": sentinel.limits.chat_queue_per_group,
        "extraction_queue_global": sentinel.limits.extraction_queue_global,
        "schedule_wait_seconds": sentinel.limits.schedule_wait_seconds,
        "task_deadline_seconds": sentinel.limits.task_deadline_seconds,
        "connect_timeout_seconds": sentinel.limits.connect_timeout_seconds,
        "read_timeout_seconds": sentinel.limits.read_timeout_seconds,
        "max_retries": sentinel.limits.max_retries,
        "log_retention_days": sentinel.log.retention_days,
    }


def _require_int(config: Mapping[str, object], key: str, low: int, high: int) -> int:
    value = config.get(key)
    if not _is_bounded_int(value, low, high):
        raise ValueError(f"{key} 不合格：{value!r}")
    return value


def _require_flag(config: Mapping[str, object], key: str) -> bool:
    value = config.get(key)
    if not _is_flag(value):
        raise ValueError(f"{key} 不合格：{value!r}")
    return value


def _require_amount(config: Mapping[str, object], key: str) -> float:
    value = config.get(key)
    if not _is_non_negative_number(value):
        raise ValueError(f"{key} 不合格：{value!r}")
    return float(value)


def _parse_maintainers(raw: object) -> MaintainerMap:
    if not isinstance(raw, dict):
        raise ValueError("group_maintainers 必须是群 ID 到维护者列表的映射")
    mapping: dict[str, frozenset[str]] = {}
    for group_id, member_ids in raw.items():
        if not is_qq_id(group_id):
            raise ValueError(f"group_maintainers 的群 ID 不合格：{group_id!r}")
        if not isinstance(member_ids, list) or not all(is_qq_id(m) for m in member_ids):
            raise ValueError(f"group_maintainers[{group_id}] 的维护者列表不合格")
        mapping[group_id] = frozenset(member_ids)
    return MaintainerMap(mapping)


def _parse_context(config: Mapping[str, object]) -> ContextSettings:
    return ContextSettings(
        buffer_max_messages=_require_int(config, "context_buffer_max_messages", 1, 10_000),
        buffer_ttl_seconds=_require_int(config, "context_buffer_ttl_seconds", 1, 86_400),
        history_max_turns=_require_int(config, "interaction_history_max_turns", 1, 1_000),
        history_ttl_hours=_require_int(config, "interaction_history_ttl_hours", 1, 24 * 30),
    )


def _parse_memory(config: Mapping[str, object]) -> MemorySettings:
    return MemorySettings(
        max_records_per_member=_require_int(config, "memory_max_records_per_member", 1, 1_000),
        ttl_days=_require_int(config, "memory_ttl_days", 1, 3_650),
        auth_confirm_ttl_seconds=_require_int(config, "auth_confirm_ttl_seconds", 30, 3_600),
        extraction_enabled=_require_flag(config, "memory_extraction_enabled"),
    )


def _parse_budget(config: Mapping[str, object]) -> BudgetSettings:
    return BudgetSettings(
        input_token_budget=_require_int(config, "input_token_budget", 1, 10_000_000),
        output_token_budget=_require_int(config, "output_token_budget", 1, 1_000_000),
        daily_amount=_require_amount(config, "daily_budget_amount"),
        monthly_amount=_require_amount(config, "monthly_budget_amount"),
        require_budget_for_extraction=_require_flag(config, "require_budget_for_extraction"),
    )


def _parse_limits(config: Mapping[str, object]) -> LimitsSettings:
    global_concurrency = _require_int(config, "provider_concurrency_global", 1, 64)
    group_concurrency = _require_int(config, "provider_concurrency_per_group", 1, 64)
    if group_concurrency > global_concurrency:
        raise ValueError("同群并发不得大于全局并发")
    connect_timeout = _require_int(config, "connect_timeout_seconds", 1, 600)
    read_timeout = _require_int(config, "read_timeout_seconds", 1, 600)
    deadline = _require_int(config, "task_deadline_seconds", 1, 3_600)
    if connect_timeout + read_timeout > deadline:
        raise ValueError("连接与读取超时之和不得大于单次业务期限")
    return LimitsSettings(
        provider_concurrency_global=global_concurrency,
        provider_concurrency_per_group=group_concurrency,
        chat_queue_per_group=_require_int(config, "chat_queue_per_group", 1, 1_000),
        extraction_queue_global=_require_int(config, "extraction_queue_global", 1, 1_000),
        schedule_wait_seconds=_require_int(config, "schedule_wait_seconds", 1, 600),
        task_deadline_seconds=deadline,
        connect_timeout_seconds=connect_timeout,
        read_timeout_seconds=read_timeout,
        max_retries=_require_int(config, "max_retries", 0, 5),
    )


def _parse_log(config: Mapping[str, object]) -> LogSettings:
    return LogSettings(retention_days=_require_int(config, "log_retention_days", 1, 365))
