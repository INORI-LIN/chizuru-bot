"""发送门控：唯一出站路径前的最后一道复核（S1-12）。

任何一次发送在真正落到适配器之前都要先过这里。门控只做一件事：把**生成结果时的
快照**与**当前事实**比对，不一致即丢弃。不做部分放行、不做补救、不重试。

复核顺序固定为：源范围 → 源触发形态 → 目标群一致 → 群开关 → 修订号 → 发送不确定。
顺序只决定报告哪个原因，**任一项不成立都足以丢弃**。

边界与限度（都是已核验的事实，不是保守估计）：

- **本模块不发送任何东西**，也不调用模型。它只返回决定；真正的发送与唯一装配点
  在 `main.py`（S1-14）。
- **固定提示不得调用模型**：`SendKind.FIXED_NOTICE` 的请求在构造时就禁止携带
  ``llm_invoked=True``，所以"模型故障提示"不会反向产生新的模型调用（架构 §8.3）。
- **不承诺 exactly-once。** ``event.send()`` 不返回 message_id，``Context.send_message()``
  只返回"是否找到平台"，``after_message_sent`` 也不是已读回执。因此本模块不提供任何
  回执或成功状态 API；调用方只能记 ``Outcome.COMPLETED``（调用返回）或
  ``Outcome.SEND_UNCERTAIN``（超时/异常），且不确定状态不重发（架构 §8.2）。
- ``evaluate()`` 与实际发送之间存在 await 间隙，修订校验是**收窄**而不是原子保证
  （与 R11 同族：原生流程甚至在门控前就写历史）。框架也没有全局出站门控，
  本门控只覆盖本插件自己的出站（R2）。
- 群开关与修订号在 S1 没有存储层，由调用方注入；**模块内不设默认值**。未提供时
  按 ``GroupState.UNKNOWN``（拒绝）与"两侧皆无修订"（无可失配）处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .config import Settings
from .dedup import Outcome
from .keys import BotInstanceKey, GroupKey, RevisionSnapshot
from .policy import Classification, MessageFacts, classify, is_trusted_scope


class SendKind(StrEnum):
    """这次出站是什么性质。性质决定源事件必须是什么形态。"""

    CHAT = "chat"
    """模型生成的聊天回复：源事件必须是 ``TEXT_CANDIDATE``。"""

    FIXED_NOTICE = "fixed_notice"
    """固定文本（能力提示、失败提示、控制命令回复）：**不得调用模型**。"""

    @property
    def uses_model(self) -> bool:
        return self is SendKind.CHAT


class GroupState(StrEnum):
    """本群聊天开关（含维护者暂停）。S1 无存储，由调用方注入。"""

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    """读不到有效策略：按"无有效策略默认关闭"处理（架构 §6.1）。"""


class DropReason(StrEnum):
    """丢弃原因闭集。每一条都对应一次"快照与当前事实不一致"。"""

    SOURCE_NOT_TRUSTED = "source_not_trusted"
    """源事件已不在可信范围（未允许群、机器人自身消息、配置退回哨兵等）。"""

    SOURCE_NOT_TRIGGERED = "source_not_triggered"
    """源事件的触发形态不支持这类出站（例如空 @）。"""

    TARGET_MISMATCH = "target_mismatch"
    """目标群与源事件推算出的群键不一致：平台实例、机器人或群号任一不同。"""

    GROUP_DISABLED = "group_disabled"
    """群开关非 OPEN（含读不到状态的 UNKNOWN）。"""

    REVISION_CHANGED = "revision_changed"
    """群或成员的修订号已变化：期间发生了退出、授权变更、纠正或删除。"""

    REVISION_UNVERIFIABLE = "revision_unverifiable"
    """快照与当前值只有一侧存在，无法比对；无法确认即丢弃。"""

    SEND_UNCERTAIN = "send_uncertain"
    """上一次发送结果不确定：不盲目重发（架构 §8.2）。"""


# 每类出站允许的源事件形态；表驱动，新增出站类型时必须显式登记。
_ALLOWED_TRIGGERS = {
    SendKind.CHAT: frozenset({Classification.TEXT_CANDIDATE}),
    # "@ 后只有图片/语音/文件"可回固定能力提示（需求 §4.1）；空 @ 仍被拒绝。
    SendKind.FIXED_NOTICE: frozenset(
        {Classification.TEXT_CANDIDATE, Classification.UNSUPPORTED_ATTACHMENT}
    ),
}


@dataclass(frozen=True)
class SendRequest:
    """一次待发送的请求。**不含正文、不含目标成员**——发送内容是调用方的事。"""

    kind: SendKind
    source: MessageFacts
    target: GroupKey
    revision: RevisionSnapshot | None = None
    """生成结果时的修订快照；S1 没有持久化状态，允许两侧都为 None。"""

    llm_invoked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SendKind):
            raise ValueError("kind 必须是 SendKind")
        if not isinstance(self.source, MessageFacts):
            raise ValueError("source 必须是 MessageFacts")
        if not isinstance(self.target, GroupKey):
            raise ValueError("target 必须是 GroupKey")
        if self.revision is not None and not isinstance(self.revision, RevisionSnapshot):
            raise ValueError("revision 必须是 RevisionSnapshot 或 None")
        if not isinstance(self.llm_invoked, bool):
            raise ValueError("llm_invoked 必须是布尔值")
        if self.kind is SendKind.FIXED_NOTICE and self.llm_invoked:
            raise ValueError("固定提示不得调用模型")


@dataclass(frozen=True)
class GateFacts:
    """门控需要的外部事实。全部由调用方注入，本模块不读存储、不读事件对象。"""

    group_state: GroupState = GroupState.UNKNOWN
    current: RevisionSnapshot | None = None
    previous: Outcome | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.group_state, GroupState):
            raise ValueError("group_state 必须是 GroupState")
        if self.current is not None and not isinstance(self.current, RevisionSnapshot):
            raise ValueError("current 必须是 RevisionSnapshot 或 None")
        if self.previous is not None and not isinstance(self.previous, Outcome):
            raise ValueError("previous 必须是 Outcome 或 None")


@dataclass(frozen=True)
class SendDecision:
    """放行或丢弃，二选一。**没有"已送达"这类状态**（见模块文档）。"""

    allowed: bool
    drop: DropReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed 必须是布尔值")
        if self.allowed is (self.drop is not None):
            raise ValueError("allowed 与 drop 互斥：放行不得带丢弃原因，丢弃必须带原因")
        if self.drop is not None and not isinstance(self.drop, DropReason):
            raise ValueError("drop 必须是 DropReason")

    @classmethod
    def allow(cls) -> "SendDecision":
        return cls(allowed=True)

    @classmethod
    def dropped(cls, reason: DropReason) -> "SendDecision":
        return cls(allowed=False, drop=reason)


def evaluate(request: SendRequest, settings: Settings, facts: GateFacts) -> SendDecision:
    """按固定顺序复核；任一项不成立即丢弃。

    调用方在放行后自行发送，并按下发结果记 ``Outcome.COMPLETED`` 或
    ``Outcome.SEND_UNCERTAIN``（``dedup.finish``）；本模块不代劳。
    """
    if not isinstance(request, SendRequest):
        raise ValueError("request 必须是 SendRequest")
    if not isinstance(settings, Settings):
        raise ValueError("settings 必须是 Settings")
    if not isinstance(facts, GateFacts):
        raise ValueError("facts 必须是 GateFacts")

    source = request.source
    if not is_trusted_scope(source, settings):
        return SendDecision.dropped(DropReason.SOURCE_NOT_TRUSTED)
    if classify(source, settings) not in _ALLOWED_TRIGGERS[request.kind]:
        return SendDecision.dropped(DropReason.SOURCE_NOT_TRIGGERED)
    if request.target != _source_group(source):
        return SendDecision.dropped(DropReason.TARGET_MISMATCH)
    if facts.group_state is not GroupState.OPEN:
        return SendDecision.dropped(DropReason.GROUP_DISABLED)

    revision = _revision_decision(request.revision, facts.current)
    if revision is not None:
        return revision
    if facts.previous is Outcome.SEND_UNCERTAIN:
        return SendDecision.dropped(DropReason.SEND_UNCERTAIN)
    return SendDecision.allow()


def _source_group(source: MessageFacts) -> GroupKey:
    """源事件推算出的群键：平台连接实例 + 机器人 + 群，三段缺一不可。"""
    return GroupKey(
        BotInstanceKey(platform_id=source.platform_id, self_id=source.self_id),
        group_id=source.group_id,
    )


def _revision_decision(
    expected: RevisionSnapshot | None,
    current: RevisionSnapshot | None,
) -> SendDecision | None:
    """两侧皆无 → 无可失配（S1）；只有一侧 → 无法确认即丢弃。"""
    if expected is None and current is None:
        return None
    if expected is None or current is None:
        return SendDecision.dropped(DropReason.REVISION_UNVERIFIABLE)
    if not expected.matches(current.group_revision, current.member_revision):
        return SendDecision.dropped(DropReason.REVISION_CHANGED)
    return None
