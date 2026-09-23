"""抽取的准入与写回（S3-07/S3-10）。

**只做两件事**，都在这里收口，装配层不再判断：

- :func:`admit`：一次抽取是否该发生——授权 → 群状态 → 源文本 → 开关与预算。判定顺序
  体现"先看权限、再看状态、最后看内容"：未授权时**不触碰正文**。**所有拒绝都静默**
  （后台不产生任何群内提示，架构 §8.3）。
- :func:`write_back`：在**调用方的单个短事务内**重核修订号与授权、按"源消息 + 动作"去重，
  然后写入候选。修订不一致即丢弃，且**不自动重做**（S3-09 的机制面在这里落地）。

**必须不做**：不调模型、不发送、不预留也不结算额度、不做调度、不导入框架、不把正文写进
``repr`` 或异常文本。三条门槛的分工写在 :func:`admit` 的文档里。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum

from ..keys import MemberKey, RevisionSnapshot
from ..storage.groups import read_policy
from ..storage.members import read_member_state
from ..storage.memories import (
    MemoryLimits,
    add_auto_facts,
    mark_source,
    read_memory_state,
)
from .extract import ExtractionResult, ExtractionStatus
from .types import prepare_source


class Refusal(StrEnum):
    """抽取未发生的原因。**每一种都是静默的**——后台故障不发群通知。"""

    NOT_AUTHORIZED = "not_authorized"
    """成员未授权长期记忆（默认状态）。"""

    GROUP_PAUSED = "group_paused"
    """本群被 `千鹤 暂停`：连采集与抽取一起停（S2-05 判据），成员删除通道仍保留。"""

    SOURCE_UNUSABLE = "source_unusable"
    """本人原话为空、超长或命中敏感规则：不调模型、不写任何东西。"""

    EXTRACTION_DISABLED = "extraction_disabled"
    """`memory_extraction_enabled` 关闭（默认值即关闭）。"""

    BUDGET_BLOCKED = "budget_blocked"
    """预算未配置或临近上限：自动抽取保守关闭（S3-10、需求 §6）。"""


@dataclass(frozen=True)
class Admission:
    """一次准入判定的结论。源文本**不进 repr**（与 ``MessageFacts.direct_text`` 同例）。"""

    allowed: bool
    reason: Refusal | None = None
    source_text: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed 必须是布尔值")
        if self.reason is not None and not isinstance(self.reason, Refusal):
            raise ValueError("reason 必须是 Refusal 或 None")
        if self.allowed and (self.reason is not None or not self.source_text):
            raise ValueError("放行必须带非空源文本且没有拒绝原因")
        if not self.allowed and (self.reason is None or self.source_text):
            raise ValueError("拒绝必须带原因且不得携带源文本")


class WriteOutcome(StrEnum):
    """一次写回的结论。前三种是正常路径，后两种是"状态变了"的确定性丢弃。"""

    WRITTEN = "written"
    NOTHING_TO_WRITE = "nothing_to_write"
    """本次没有任何候选（模型给了 `[]`），但来源已登记，重复投递不再处理。"""

    DUPLICATE = "duplicate"
    """同一条源消息的同类动作已经处理过。"""

    NOT_AUTHORIZED = "not_authorized"
    """写入前重核时授权已不是开启状态。"""

    REVISION_CHANGED = "revision_changed"
    """群或成员修订号已变（退出/清空/暂停/纠正）：丢弃且**不自动重做**。"""


@dataclass(frozen=True)
class WritePlan:
    """一次写回所需的全部输入，由装配层从配置、结果与受理时的快照拼好。"""

    member: MemberKey
    source_message_id: str
    source_action: str
    source_retention_seconds: int
    limits: MemoryLimits
    candidates: tuple[tuple[str, str], ...] = ()
    expected_revision: RevisionSnapshot | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.member, MemberKey):
            raise ValueError("member 必须是 MemberKey")
        if not isinstance(self.limits, MemoryLimits):
            raise ValueError("limits 必须是 MemoryLimits")
        if not isinstance(self.candidates, tuple):
            raise ValueError("candidates 必须是 tuple")
        if self.expected_revision is not None and not isinstance(
            self.expected_revision, RevisionSnapshot
        ):
            raise ValueError("expected_revision 必须是 RevisionSnapshot 或 None")


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    written: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WriteOutcome):
            raise ValueError("outcome 必须是 WriteOutcome")
        if not isinstance(self.written, int) or isinstance(self.written, bool) or self.written < 0:
            raise ValueError("written 必须是非负整数")
        if (self.written > 0) is not (self.outcome is WriteOutcome.WRITTEN):
            raise ValueError("只有 written 分支可以带写入条数")


def admit(
    *,
    source_text: str,
    authorized: bool,
    paused: bool,
    extraction_enabled: bool,
    budget_allows: bool,
) -> Admission:
    """判定一次抽取是否该发生。

    三道门槛分开表达，因为它们**来源不同、将来也会分开变化**：

    - ``authorized``：成员在 `memory_state` 里的授权位**是否当前有效**；装配层用
      `memory.consent.authorization_is_current` 把"说明版本比对"折叠进这个布尔值
      （S3-03），因此旧版本的残留授权行到这里已经是 ``False``。
    - ``paused``：本群是否被暂停。调用方在**读不到策略时传 ``True``**："无法确认未暂停"
      就按已暂停处理（架构 §8.3 的记忆库失败行：不以"不用记忆"绕过未知权限）。
    - ``extraction_enabled`` 与 ``budget_allows``：前者是 `memory_extraction_enabled`
      总开关，后者是 `BudgetLedger.extraction_allowed`（预算未配置或临近上限）。
      两者必须分开传：账本不认识 `MemorySettings`，只看得到预算。
    """
    for name, value in (
        ("authorized", authorized),
        ("paused", paused),
        ("extraction_enabled", extraction_enabled),
        ("budget_allows", budget_allows),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{name} 必须是布尔值")

    if not authorized:
        return Admission(False, Refusal.NOT_AUTHORIZED)
    if paused:
        return Admission(False, Refusal.GROUP_PAUSED)
    cleaned = prepare_source(source_text)
    if cleaned is None:
        return Admission(False, Refusal.SOURCE_UNUSABLE)
    if not extraction_enabled:
        return Admission(False, Refusal.EXTRACTION_DISABLED)
    if not budget_allows:
        return Admission(False, Refusal.BUDGET_BLOCKED)
    return Admission(True, None, cleaned)


def plan_candidates(result: ExtractionResult) -> tuple[tuple[str, str], ...]:
    """只有 ``ok`` 状态的候选会被写回：失败不写、空不写、模型输出不可用也不写。"""
    if not isinstance(result, ExtractionResult):
        raise ValueError("result 必须是 ExtractionResult")
    if result.status is not ExtractionStatus.OK:
        return ()
    return tuple(candidate.to_pair() for candidate in result.candidates)


def counts_as_failure(result: ExtractionResult) -> bool:
    """这次结论是否记一次"抽取失败"（供调用方计数，**不据此暂停任何东西**）。

    失败＝调用失败、模型空结果、模型输出不可用；``ok``（含"没有候选"）不算失败。
    没有跑起来的路径（队列满、修订变化、重复来源、未授权）**不计入**——它们不是抽取
    的失败，而是"没跑或不该跑"。阈值判断属调用方（``health`` 只计数）。
    """
    if not isinstance(result, ExtractionResult):
        raise ValueError("result 必须是 ExtractionResult")
    return result.status is not ExtractionStatus.OK


def write_back(connection: sqlite3.Connection, plan: WritePlan, *, at: int) -> WriteResult:
    """在调用方的事务内完成重核与写入。

    顺序即优先级：**先比修订号**（状态变了就什么都别做），**再重核授权**，**再登记来源**，
    最后写候选。来源登记先于写入，因此"模型给了 `[]`"的消息也不会被重复处理；登记失败
    （重复）时直接返回，不做第二次尝试。
    """
    if not isinstance(plan, WritePlan):
        raise ValueError("plan 必须是 WritePlan")
    if not isinstance(at, int) or isinstance(at, bool) or at < 0:
        raise ValueError("at 必须是 Unix 秒整数")

    if plan.expected_revision is not None:
        current = RevisionSnapshot(
            group_revision=read_policy(connection, plan.member.group).revision,
            member_revision=read_member_state(connection, plan.member).revision,
        )
        if current != plan.expected_revision:
            return WriteResult(WriteOutcome.REVISION_CHANGED)

    if not read_memory_state(connection, plan.member).authorized:
        return WriteResult(WriteOutcome.NOT_AUTHORIZED)

    fresh = mark_source(
        connection,
        plan.member,
        source_message_id=plan.source_message_id,
        action=plan.source_action,
        retention_seconds=plan.source_retention_seconds,
        at=at,
    )
    if not fresh:
        return WriteResult(WriteOutcome.DUPLICATE)
    if not plan.candidates:
        return WriteResult(WriteOutcome.NOTHING_TO_WRITE)

    written = add_auto_facts(
        connection,
        plan.member,
        facts=plan.candidates,
        source_message_id=plan.source_message_id,
        limits=plan.limits,
        at=at,
    )
    return WriteResult(WriteOutcome.WRITTEN, written=len(written))
