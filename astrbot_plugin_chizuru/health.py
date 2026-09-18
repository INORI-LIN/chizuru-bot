"""连接与健康状态：把框架侧的可观测事实收敛成一份可安全展示的快照（S1-04）。

**本模块是纯逻辑**：不导入框架、不读事件对象、不发消息、不写日志、不探测网络。
框架事实由 `main.py` 提取后传入——与 `control.py` 的分工相同。

提取契约（S1-14 装配时必须照此实现，否则会引入凭据泄漏）：

- 只读：``context.get_platform_inst(settings.platform_id)``（找不到时返回 ``None``，
  不抛异常）、``instance.status``、``len(instance.errors)``。
- **绝不读**：``instance.config``（aiocqhttp 的 ``ws_reverse_token`` 就在其中）、
  ``instance.get_stats()`` 的 ``last_error.traceback`` 或 ``last_error.message``
  （自由文本，可能夹带 URL、密钥或消息片段），以及任何异常对象的文本。
- **QQ 登录态不在 ``Platform.status`` 里**：该字段只描述适配器任务的生命周期。
  账号是否在线必须由 NapCat 侧核实（风险 R17），因此报告固定输出"未知"，
  不得渲染成"在线"。

本模块只描述状态，不做决定：是否放行、是否降级由调用方按架构 §8.3 的故障矩阵
决定；**本模块不主动向群里广播任何故障**（R-OPS、R-ADM）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .budget import BudgetSnapshot
from .scheduler import SchedulerStats

ACCOUNT_STATE_NOTE = "账号在线性：未知（平台状态不含 QQ 登录态，须由 NapCat 侧核实）"


def _require_count(value: object, name: str) -> int:
    # bool 是 int 的子类，必须显式排除，否则 True 会被当成 1。
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} 必须是非负整数：{value!r}")
    return value


class PlatformState(StrEnum):
    """平台适配器状态。取值只来自 `Platform.status` 的既有取值，不做推断。"""

    RUNNING = "running"
    PENDING = "pending"
    ERROR = "error"
    STOPPED = "stopped"

    INSTANCE_MISSING = "instance_missing"
    """按配置的 platform_id 找不到平台实例：未配置、未加载或已被移除。"""

    UNKNOWN = "unknown"
    """框架返回了本模块不认识的取值。**不回落到 running。**"""


# 只认这四个上游取值；多一个字符也不认，避免把新版本的未知状态读成"运行中"。
_OBSERVED_STATES = {
    PlatformState.PENDING.value: PlatformState.PENDING,
    PlatformState.RUNNING.value: PlatformState.RUNNING,
    PlatformState.ERROR.value: PlatformState.ERROR,
    PlatformState.STOPPED.value: PlatformState.STOPPED,
}


def platform_state(value: object) -> PlatformState:
    """把 ``Platform.status``（或其 ``.value``）归一化；不认识的值一律 `UNKNOWN`。"""
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        return PlatformState.UNKNOWN
    return _OBSERVED_STATES.get(raw, PlatformState.UNKNOWN)


class Degradation(StrEnum):
    """**会改变行为的持久降级**（架构 §8.3）。

    只登记"停用 / 暂停 / 保持关闭"这类状态。429、5xx、空回复、超时属于瞬态错误，
    归 `redact.ErrorCode` 由日志承载——把它们做成标志会让"降级"永远无法清除。
    """

    PROVIDER_DISABLED = "provider_disabled"
    """401：停用异常提供商的调用，等维护侧处理。"""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """402 或本地预算耗尽：暂停付费请求，抽取优先停用。"""

    MEMORY_STORE_FAILED = "memory_store_failed"
    """记忆库读取失败：无法确认授权/退出状态时，停止受影响的采集、调用与发送。"""

    DELETION_FAILED = "deletion_failed"
    """删除请求执行失败：保持相关读写与采集关闭，不虚报已删除。"""

    EXTRACTION_SUSPENDED = "extraction_suspended"
    """仅抽取失败：放弃该次抽取并保留正常聊天。由调用方判定，本模块不设阈值。"""


@dataclass(frozen=True)
class PlatformHealth:
    """平台侧可安全展示的状态：**不含凭据、错误文本或堆栈**。"""

    instance_present: bool
    state: PlatformState
    error_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.instance_present, bool):
            raise ValueError("instance_present 必须是布尔值")
        if not isinstance(self.state, PlatformState):
            raise ValueError("state 必须是 PlatformState")
        _require_count(self.error_count, "error_count")
        # 两个客观事实必须一致：没有实例就不可能有实例状态，反之亦然。
        if self.instance_present is (self.state is PlatformState.INSTANCE_MISSING):
            raise ValueError("instance_present 与 INSTANCE_MISSING 必须一致")

    @property
    def reachable(self) -> bool:
        """只有 ``RUNNING`` 算可用；其余（含 ``UNKNOWN``）都不是。"""
        return self.state is PlatformState.RUNNING

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0


@dataclass(frozen=True)
class HealthSnapshot:
    """供维护者状态查询的一次性快照（S2-05「千鹤 状态」消费）。

    预算与队列是既有对象的**透传**，本模块不复制它们的内部结构；两者都可以缺省，
    缺省表示"尚未装配"而不是"正常"。
    """

    platform: PlatformHealth
    degradations: frozenset[Degradation] = frozenset()
    extraction_failures: int = 0
    extraction_successes: int = 0
    budget: BudgetSnapshot | None = None
    scheduler: SchedulerStats | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.platform, PlatformHealth):
            raise ValueError("platform 必须是 PlatformHealth")
        if not isinstance(self.degradations, frozenset) or not all(
            isinstance(item, Degradation) for item in self.degradations
        ):
            raise ValueError("degradations 必须是 Degradation 的 frozenset")
        _require_count(self.extraction_failures, "extraction_failures")
        _require_count(self.extraction_successes, "extraction_successes")
        if self.budget is not None and not isinstance(self.budget, BudgetSnapshot):
            raise ValueError("budget 必须是 BudgetSnapshot 或 None")
        if self.scheduler is not None and not isinstance(self.scheduler, SchedulerStats):
            raise ValueError("scheduler 必须是 SchedulerStats 或 None")

    @property
    def degraded(self) -> bool:
        """平台不可用或任一降级标志置位。"""
        return not self.platform.reachable or bool(self.degradations)


class HealthMonitor:
    """状态聚合点：只记录调用方认定的事实，不探测、不推断、不发送。"""

    def __init__(self) -> None:
        self._platform = PlatformHealth(
            instance_present=False,
            state=PlatformState.INSTANCE_MISSING,
        )
        self._degradations: set[Degradation] = set()
        self._extraction_failures = 0
        self._extraction_successes = 0

    # ---- 观测 ----

    def observe_platform(
        self,
        *,
        instance_present: bool,
        status: object = None,
        error_count: int = 0,
    ) -> PlatformHealth:
        """记录一次平台观测，返回归一化后的结果。

        实例不存在时状态恒为 ``INSTANCE_MISSING``，错误计数归零——实例已被移除时
        上一轮的计数不再代表任何东西。
        """
        if not isinstance(instance_present, bool):
            raise ValueError("instance_present 必须是布尔值")
        _require_count(error_count, "error_count")
        if not instance_present:
            health = PlatformHealth(
                instance_present=False,
                state=PlatformState.INSTANCE_MISSING,
            )
        else:
            health = PlatformHealth(
                instance_present=True,
                state=platform_state(status),
                error_count=error_count,
            )
        self._platform = health
        return health

    def platform(self) -> PlatformHealth:
        return self._platform

    # ---- 降级 ----

    def set_degraded(self, reason: Degradation) -> None:
        """置位降级标志。阈值判断属于调用方（架构 §8.3），不在本模块。"""
        if not isinstance(reason, Degradation):
            raise ValueError("reason 必须是 Degradation")
        self._degradations.add(reason)

    def clear_degraded(self, reason: Degradation) -> bool:
        """清除降级标志；返回是否确实清除了一个。"""
        if not isinstance(reason, Degradation):
            raise ValueError("reason 必须是 Degradation")
        if reason not in self._degradations:
            return False
        self._degradations.discard(reason)
        return True

    def degradations(self) -> frozenset[Degradation]:
        return frozenset(self._degradations)

    # ---- 计数 ----

    def record_extraction_failure(self) -> int:
        """记一次抽取失败，返回累计值；是否据此暂停抽取由调用方决定。"""
        self._extraction_failures += 1
        return self._extraction_failures

    def record_extraction_success(self) -> int:
        self._extraction_successes += 1
        return self._extraction_successes

    # ---- 快照 ----

    def snapshot(
        self,
        *,
        budget: BudgetSnapshot | None = None,
        scheduler: SchedulerStats | None = None,
    ) -> HealthSnapshot:
        return HealthSnapshot(
            platform=self._platform,
            degradations=frozenset(self._degradations),
            extraction_failures=self._extraction_failures,
            extraction_successes=self._extraction_successes,
            budget=budget,
            scheduler=scheduler,
        )


_STATE_TEXT = {
    PlatformState.RUNNING: "运行中",
    PlatformState.PENDING: "启动中",
    PlatformState.ERROR: "错误",
    PlatformState.STOPPED: "已停止",
    PlatformState.INSTANCE_MISSING: "未找到平台实例（未配置或未加载）",
    PlatformState.UNKNOWN: "未知（框架返回了不认识的取值）",
}

_DEGRADATION_TEXT = {
    Degradation.PROVIDER_DISABLED: "提供商已停用（鉴权失败）",
    Degradation.BUDGET_EXHAUSTED: "付费请求已暂停（预算耗尽）",
    Degradation.MEMORY_STORE_FAILED: "记忆库不可用（相关采集与发送已停止）",
    Degradation.DELETION_FAILED: "删除失败未恢复（相关读写保持关闭）",
    Degradation.EXTRACTION_SUSPENDED: "抽取已暂停（聊天不受影响）",
}


def format_report(snapshot: HealthSnapshot, *, configured: bool) -> tuple[str, ...]:
    """生成维护者可读的状态文本。

    只用枚举、计数与布尔值拼装，**结构上不可能带出凭据、成员档案或聊天正文**；
    降级项按固定声明顺序输出，保证同一状态每次得到同样的行。
    """
    if not isinstance(snapshot, HealthSnapshot):
        raise ValueError("snapshot 必须是 HealthSnapshot")
    if not isinstance(configured, bool):
        raise ValueError("configured 必须是布尔值")

    lines = [
        f"部署配置：{'已配置' if configured else '未配置（全部处理已拒绝）'}",
        f"平台连接：{_STATE_TEXT[snapshot.platform.state]}；累计错误 {snapshot.platform.error_count} 次",
        ACCOUNT_STATE_NOTE,
    ]

    ordered = [reason for reason in Degradation if reason in snapshot.degradations]
    if ordered:
        lines.append("降级：" + "；".join(_DEGRADATION_TEXT[reason] for reason in ordered))
    else:
        lines.append("降级：无")

    lines.append(
        f"抽取：成功 {snapshot.extraction_successes} 次，失败 {snapshot.extraction_failures} 次"
    )

    if snapshot.budget is not None:
        budget = snapshot.budget
        lines.append(
            "预算："
            + ("已配置" if budget.configured else "未配置（自动抽取保持关闭）")
            + f"；暂停 {'是' if budget.paused else '否'}"
            + f"；抽取 {'允许' if budget.extraction_allowed else '关闭'}"
        )

    if snapshot.scheduler is not None:
        stats = snapshot.scheduler
        waiting_chat = sum(stats.waiting_chat.values())
        lines.append(
            f"队列：运行 {stats.running_global} 个；"
            f"聊天等待 {waiting_chat}；抽取等待 {stats.waiting_extraction}；"
            f"调度器 {'已关闭' if stats.closed else '运行中'}"
        )

    return tuple(lines)
