"""隔离键与修订号快照：纯数据，无框架依赖。

所有键只由**可信事件元数据**构造——平台连接实例、机器人 self_id、群 ID、成员 ID。
昵称、群名与消息正文一律不参与构造：重命名不得改变身份，不同连接不得因群号相同
而串用（R-DATA、R-CTX、A11）。

键必须完整。缺任一段时构造即失败，而不是产生一个"看起来能用"的短键。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class BotInstanceKey:
    """机器人实例：平台连接实例 + self_id。

    两者缺一不可。只用群号做隔离会在"同一群号出现在不同平台连接"时串用
    （架构 §6.1）。
    """

    platform_id: str
    self_id: str

    def __post_init__(self) -> None:
        if not self.platform_id or not self.self_id:
            raise ValueError("BotInstanceKey 需要非空的 platform_id 与 self_id")


@dataclass(frozen=True, order=True)
class GroupKey:
    """群：机器人实例 + 群 ID。同群共享上下文、不同群隔离（R-CTX）。"""

    instance: BotInstanceKey
    group_id: str

    def __post_init__(self) -> None:
        if not self.group_id:
            raise ValueError("GroupKey 需要非空 group_id")


@dataclass(frozen=True, order=True)
class MemberKey:
    """成员：群 + 成员 ID。

    权限与记忆的隔离范围是"当前机器人 + 当前群 + 本人"，不自动扩展到其他群
    （需求 §4.3）。
    """

    group: GroupKey
    member_id: str

    def __post_init__(self) -> None:
        if not self.member_id:
            raise ValueError("MemberKey 需要非空 member_id")

    @property
    def instance(self) -> BotInstanceKey:
        return self.group.instance


@dataclass(frozen=True)
class RevisionSnapshot:
    """发起工作时的数据修订号快照。

    在读取、抽取、写回与发送前都要重新比对：不一致意味着发生了退出、授权变更、
    纠正或删除，旧结果必须丢弃。队列取消只是优化，**修订校验才是阻止旧任务
    写回的最终规则**（架构 §6.2）。
    """

    group_revision: int
    member_revision: int

    def matches(self, group_revision: int, member_revision: int) -> bool:
        """与当前实际修订号比对；返回 False 即表示快照已失效。"""
        return (self.group_revision, self.member_revision) == (
            group_revision,
            member_revision,
        )
