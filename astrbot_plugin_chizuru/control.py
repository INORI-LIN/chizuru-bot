"""确定性权限判定。

权限只来自两处：**可信事件身份**（平台连接实例 + self_id + 群 + 成员）与
**显式配置映射**（``Settings.maintainers``）。

不参与判定的东西：昵称、群名片、消息正文、以及 **QQ 群管理员身份**。QQ 群管理
员不自动等于 AstrBot 系统管理员，也不自动等于本项目的授权维护者（需求 §2、§4.4）。

本模块不读事件对象、不导入 AstrBot、不调用模型，只接受已经提取好的事实——"权限
不由 LLM 决定"因此是结构性的，而不是靠约定。

**范围说明**：S1-06 只交付授权决策。文档中"执行状态变更；变更前先增修订号"属
持久化状态的职责（架构 §6.1 的群策略/成员状态），由 S2-01 的 ``storage/`` 交付；
S1 尚无可增的修订号，因此那半条判据不在本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .commands import CommandIntent, Permission
from .config import Settings
from .policy import MessageFacts, is_trusted_scope


class Denial(StrEnum):
    IDENTITY_NOT_TRUSTED = "identity_not_trusted"
    """身份或范围不可信：私聊、非允许群、自身消息、身份字段畸形。"""

    NOT_A_MAINTAINER = "not_a_maintainer"
    """需要维护者权限，但该成员不在本群的显式配置名单中。"""


@dataclass(frozen=True)
class Authorization:
    """判定结果。允许与拒绝理由必须恰好互斥，构造不出"既允许又有理由"的对象。"""

    allowed: bool
    denial: Denial | None = None

    def __post_init__(self) -> None:
        if self.allowed is (self.denial is not None):
            raise ValueError("allowed 与 denial 必须恰好互斥")

    @classmethod
    def ok(cls) -> "Authorization":
        return cls(allowed=True)

    @classmethod
    def denied(cls, reason: Denial) -> "Authorization":
        return cls(allowed=False, denial=reason)


def authorize(
    intent: CommandIntent,
    facts: MessageFacts,
    settings: Settings,
) -> Authorization:
    """判定这条指令能否由这个身份执行。

    不做任何状态变更，也不产生回复文本——回复与状态变更由上层按判定结果处理。
    """
    # 身份与范围先行：安全判定不假设调用方已经校验过。
    if not is_trusted_scope(facts, settings):
        return Authorization.denied(Denial.IDENTITY_NOT_TRUSTED)

    if intent.permission is Permission.MAINTAINER and not settings.maintainers.is_maintainer(
        facts.group_id,
        facts.sender_id,
    ):
        return Authorization.denied(Denial.NOT_A_MAINTAINER)

    # MEMBER 与 SELF 都由可信范围内的成员满足。
    # SELF 的"只能作用于本人"由 CommandIntent 结构保证：它不携带任何目标成员，
    # 因此不存在"代他人开启记忆"的入口，无需在此另设判据。
    return Authorization.ok()
