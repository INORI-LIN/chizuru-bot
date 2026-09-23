"""用量与预算核算：调度前预留、返回后按 usage 结算。

**本模块只做核算与门槛**：不发起请求、不写日志、不落库、不硬编码价目。价格必须
由调用方注入（``PriceTable``）；金额结果只求可核对的保守近似，不声称等于提供商
账单（架构 §7.2）。

规则（对应 docs/03 §5.2 的 S1-09 判据）：

- 预留按**最大 token**（``input_token_budget`` / ``output_token_budget``）估算并
  计入在途；``settle`` 按实际 usage 与价格结算，``cancel`` 释放且不计费。每个预留
  必须二选一收尾，否则在途额度不会释放。
- **缺失 usage 不计为零**：以预留估算入账并标记 ``estimated``（架构 §7.2）。
- 金额未配置（0）时：只统计 token，聊天不因金额被拒；自动抽取保持关闭并在快照
  中给出原因——这是需求 §6 的硬要求，不是"不限额度"。
- 已配置金额但价格未知 ⇒ 无法核算 ⇒ 保守拒绝新任务（fail-closed）：不拿"估算
  不出"当"不花钱"。
- 抽取优先停用：``EXTRACTION_STOP_RATIO`` 为**建议参数**，抽取在上限的该比例即
  停用，聊天到上限才停（架构 §7.2"临近上限保守暂停，抽取优先停用"）。
- 判定用"已用 + 在途 vs 阈值"，不含新任务自身的估算：最后一次放行可能让结算后
  的总量略微超过上限（不超过一次预留估算），此后立即停用。属性与 ``reserve``
  的结论因此始终一致。

与调度的关系：``scheduler.py`` 不认识预算。"先预留、再调度、后结算"的顺序由装配
层保证（docs/03 §5.2 实施说明）——调度器同时服务聊天与抽取，强绑凭据只增加耦合，
并不带来真实强制。成员本人的记忆开关（需求 §4.3）也不在这里判定。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from .config import BudgetSettings

_MILLION = Decimal(1_000_000)
_AMOUNT_QUANTUM = Decimal("0.000001")

EXTRACTION_STOP_RATIO = Decimal("0.9")
"""抽取停用阈值（相对日/月金额上限的比例）。

需求 §6 与架构 §7.2 要求"临近上限保守暂停，抽取优先停用"，但没有给具体系数，
因此这里是**建议参数**：抽取在达到上限的 90% 即停止，聊天到 100% 才停。
金额未配置时本系数不生效；评审后若不需要提前停用，把它改为 ``Decimal(1)``。
"""


class UsageKind(StrEnum):
    CHAT = "chat"
    EXTRACTION = "extraction"


class BudgetRefusal(StrEnum):
    LIMIT_EXHAUSTED = "limit_exhausted"
    EXTRACTION_DISABLED = "extraction_disabled"
    PRICE_UNKNOWN = "price_unknown"


class BudgetRefused(Exception):
    """未获准产生这次模型调用。调用方据此选择固定提示或静默放弃。"""

    def __init__(self, reason: BudgetRefusal) -> None:
        super().__init__(f"预算未放行：{reason.value}")
        self.reason = reason


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class TokenPrice:
    """每百万 token 的金额。单位与金额上限保持一致，不做币种假设。"""

    input_per_million: Decimal
    output_per_million: Decimal

    def __post_init__(self) -> None:
        for name in ("input_per_million", "output_per_million"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or value < 0:
                raise ValueError(f"{name} 必须是非负 Decimal")


class PriceTable:
    """模型 ID → 价格。缺项即"价格未知"，不拿默认价目代替。"""

    def __init__(self, prices: Mapping[str, TokenPrice] | None = None) -> None:
        self._prices = dict(prices or {})

    @property
    def configured(self) -> bool:
        return bool(self._prices)

    def cost(self, model: str, usage: TokenUsage) -> Decimal | None:
        price = self._prices.get(model)
        if price is None:
            return None
        amount = (
            price.input_per_million * usage.input_tokens
            + price.output_per_million * usage.output_tokens
        ) / _MILLION
        return amount.quantize(_AMOUNT_QUANTUM)


@dataclass(frozen=True)
class Reservation:
    """一次模型调用的预留凭据。``sequence`` 由账本分配，用于配对结算。"""

    sequence: int
    kind: UsageKind
    model: str
    estimated_usage: TokenUsage
    estimated_cost: Decimal | None
    reserved_at: datetime


@dataclass(frozen=True)
class BudgetSnapshot:
    """供"千鹤 状态"（S2-05）与降级可见性（S3-11）读取的只读快照。"""

    configured: bool
    day_tokens: TokenUsage
    month_tokens: TokenUsage
    day_amount: Decimal | None
    month_amount: Decimal | None
    day_limit: Decimal | None
    month_limit: Decimal | None
    day_estimated: bool
    month_estimated: bool
    outstanding: int
    paused: bool
    extraction_allowed: bool
    prices_configured: bool = False
    """价目表是否已配置（S4-01）。**只在金额已配置时才参与判定**（R20）。"""


class BudgetLedger:
    """日/月用量与金额账本。所有方法都是同步的，只在事件循环内使用。"""

    def __init__(
        self,
        settings: BudgetSettings,
        *,
        prices: PriceTable,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._prices = prices
        self._clock = clock or datetime.now
        self._day_key: date | None = None
        self._month_key: tuple[int, int] | None = None
        self._day_tokens = TokenUsage()
        self._month_tokens = TokenUsage()
        self._day_amount = Decimal(0)
        self._month_amount = Decimal(0)
        self._day_estimated = False
        self._month_estimated = False
        self._outstanding: dict[int, Reservation] = {}
        self._next_sequence = 0

    # ---- 门槛 ----

    @property
    def extraction_allowed(self) -> bool:
        """自动抽取当前是否可运行：配置门槛 + 临近上限的保守停用。

        与 ``reserve`` 使用同一个判定，因此本属性为 False 时 ``reserve`` 必然拒绝。
        """
        if not self._settings.extraction_allowed(True):
            return False
        return not self._limit_reached(UsageKind.EXTRACTION)

    def reserve(self, kind: UsageKind, model: str) -> Reservation:
        """调度前预留。被拒绝时调用方不得发起调用。"""
        if not isinstance(kind, UsageKind):
            raise ValueError("kind 必须是 UsageKind")
        if not isinstance(model, str) or not model or model != model.strip():
            raise ValueError("model 必须是非空且无首尾空白的字符串")

        self._rollover(self._clock())
        if kind is UsageKind.EXTRACTION and not self.extraction_allowed:
            raise BudgetRefused(BudgetRefusal.EXTRACTION_DISABLED)

        estimated_usage = TokenUsage(
            self._settings.input_token_budget,
            self._settings.output_token_budget,
        )
        estimated_cost = self._prices.cost(model, estimated_usage)
        if self._settings.budget_configured:
            if estimated_cost is None:
                # 无法估算金额时不能假装免费：保守拒绝（fail-closed）。
                raise BudgetRefused(BudgetRefusal.PRICE_UNKNOWN)
            if self._limit_reached(kind):
                raise BudgetRefused(BudgetRefusal.LIMIT_EXHAUSTED)

        reservation = Reservation(
            sequence=self._next_sequence,
            kind=kind,
            model=model,
            estimated_usage=estimated_usage,
            estimated_cost=estimated_cost,
            reserved_at=self._clock(),
        )
        self._next_sequence += 1
        self._outstanding[reservation.sequence] = reservation
        return reservation

    # ---- 收尾 ----

    def settle(
        self,
        reservation: Reservation,
        usage: TokenUsage | None = None,
        *,
        cost: Decimal | None = None,
    ) -> None:
        """返回后结算。``usage`` 缺失时按预留估算入账并标记估算，不计为零。"""
        self._require_outstanding(reservation)
        if usage is not None and not isinstance(usage, TokenUsage):
            raise ValueError("usage 必须是 TokenUsage 或 None")
        if cost is not None and (not isinstance(cost, Decimal) or cost < 0):
            raise ValueError("cost 必须是非负 Decimal 或 None")

        self._rollover(self._clock())
        del self._outstanding[reservation.sequence]

        settled_usage = reservation.estimated_usage if usage is None else usage
        estimated = usage is None
        self._day_tokens = self._day_tokens + settled_usage
        self._month_tokens = self._month_tokens + settled_usage

        if self._settings.budget_configured:
            amount = cost if cost is not None else self._prices.cost(reservation.model, settled_usage)
            if amount is None:
                # 预留时价格必然可算（reserve 已保证）；走到这里说明价目表在使用中变化，
                # 按预留估算入账并标记，绝不记零。
                amount = reservation.estimated_cost or Decimal(0)
                estimated = True
            self._day_amount += amount
            self._month_amount += amount
            self._day_estimated = self._day_estimated or estimated
            self._month_estimated = self._month_estimated or estimated

    def cancel(self, reservation: Reservation) -> None:
        """任务未产生调用时释放预留；不计费、不产生用量。"""
        self._require_outstanding(reservation)
        del self._outstanding[reservation.sequence]

    # ---- 观测 ----

    def snapshot(self) -> BudgetSnapshot:
        self._rollover(self._clock())
        configured = self._settings.budget_configured
        return BudgetSnapshot(
            configured=configured,
            day_tokens=self._day_tokens,
            month_tokens=self._month_tokens,
            day_amount=self._day_amount if configured else None,
            month_amount=self._month_amount if configured else None,
            day_limit=self._limit(self._settings.daily_amount),
            month_limit=self._limit(self._settings.monthly_amount),
            day_estimated=self._day_estimated,
            month_estimated=self._month_estimated,
            outstanding=len(self._outstanding),
            paused=self._limit_reached(UsageKind.CHAT),
            extraction_allowed=self.extraction_allowed,
            prices_configured=self._prices.configured,
        )

    # ---- 内部 ----

    def _require_outstanding(self, reservation: Reservation) -> None:
        if (
            not isinstance(reservation, Reservation)
            or reservation.sequence not in self._outstanding
        ):
            raise ValueError("未知或已结算的预留")

    def _limit(self, amount: float) -> Decimal | None:
        """0 表示未配置（不是不限额度）；未配置的维度不参与判定。"""
        if amount <= 0:
            return None
        return Decimal(str(amount))

    def _reserved_amount(self) -> Decimal:
        total = Decimal(0)
        for reservation in self._outstanding.values():
            total += reservation.estimated_cost or Decimal(0)
        return total

    def _limit_reached(self, kind: UsageKind) -> bool:
        """已用 + 在途是否已达阈值。``paused`` 与 ``extraction_allowed`` 都由此得出。

        判定**不含本次新任务的估算成本**：额度尚有余量就放行，最后一次放行可能让
        结算后的总量略微超过上限（超出量不超过一次预留估算），此后立即停用。
        这样"属性说可跑、reserve 却拒绝"的不一致不会出现。
        """
        if not self._settings.budget_configured:
            return False
        ratio = EXTRACTION_STOP_RATIO if kind is UsageKind.EXTRACTION else Decimal(1)
        reserved = self._reserved_amount()
        for spent, limit in (
            (self._day_amount, self._limit(self._settings.daily_amount)),
            (self._month_amount, self._limit(self._settings.monthly_amount)),
        ):
            if limit is None:
                continue
            if spent + reserved >= limit * ratio:
                return True
        return False

    def _rollover(self, now: datetime) -> None:
        day_key = now.date()
        month_key = (now.year, now.month)
        if self._day_key != day_key:
            self._day_key = day_key
            self._day_tokens = TokenUsage()
            self._day_amount = Decimal(0)
            self._day_estimated = False
        if self._month_key != month_key:
            self._month_key = month_key
            self._month_tokens = TokenUsage()
            self._month_amount = Decimal(0)
            self._month_estimated = False
