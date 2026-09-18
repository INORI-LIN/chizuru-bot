"""成员在普通群上下文中的退出与加入状态。

**与长期记忆授权无关**：本表没有授权列，退出与加入都不触碰 S3 的记忆数据。
"不改变本人的长期记忆授权"是表结构的事实，不是实现纪律。

无行 = 未退出；采集是否开启由群策略决定，本表只回答"这个人是否要求停止采集
本人的普通群聊"。退出会清理本人缓冲与相关群历史（由 main 执行）并增加群修订号；
加入要求本群已告知且未关闭、未暂停（需求 §4.4），且不恢复旧材料。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from ..keys import MemberKey
from .db import Database
from .groups import PolicyRefused, bump_group_revision, read_policy


@dataclass(frozen=True)
class MemberState:
    """成员在普通群上下文中的状态；``absent()`` 表示从未操作过（即未退出）。"""

    opted_out: bool
    revision: int

    @classmethod
    def absent(cls) -> "MemberState":
        return cls(opted_out=False, revision=0)


@dataclass(frozen=True)
class Transition:
    """一次退出/加入的结果：新状态、群修订号，以及状态是否真的发生变化。"""

    state: MemberState
    group_revision: int
    changed: bool


def key_params(member: MemberKey) -> tuple[str, str, str, str]:
    """把四重键展平成列值：机器人实例 + 群 + 成员（架构 §6.1）。"""
    group = member.group
    return (
        group.instance.platform_id,
        group.instance.self_id,
        group.group_id,
        member.member_id,
    )


def read_member_state(connection: sqlite3.Connection, member: MemberKey) -> MemberState:
    """在既有连接/事务内读取成员状态。"""
    row = _read_row(connection, member)
    if row is None:
        return MemberState.absent()
    return MemberState(
        opted_out=bool(row["context_opt_out"]),
        revision=int(row["revision"]),
    )


class MemberStore:
    """成员上下文状态的读写入口。全部方法同步、短事务、fail-closed。"""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], int],
    ) -> None:
        self._database = database
        self._clock = clock

    def state(self, member: MemberKey) -> MemberState:
        with self._database.read() as connection:
            if connection is None:
                return MemberState.absent()
            return read_member_state(connection, member)

    def revision(self, member: MemberKey) -> int:
        return self.state(member).revision

    def opt_out(self, member: MemberKey) -> Transition:
        """退出：持久化退出状态，并在同一事务内把群修订号 +1（架构 §6.2）。"""
        now = self._clock()
        with self._database.transaction() as connection:
            current = read_member_state(connection, member)
            if current.opted_out:
                return Transition(
                    state=current,
                    group_revision=read_policy(connection, member.group).revision,
                    changed=False,
                )
            revision = current.revision + 1
            if current.revision == 0:
                connection.execute(
                    "INSERT INTO member_state (platform_id, self_id, group_id, member_id, "
                    "context_opt_out, revision, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (*key_params(member), revision, now),
                )
            else:
                connection.execute(
                    "UPDATE member_state SET context_opt_out = 1, revision = ?, updated_at = ? "
                    "WHERE platform_id = ? AND self_id = ? AND group_id = ? AND member_id = ?",
                    (revision, now, *key_params(member)),
                )
            group_revision = bump_group_revision(connection, member.group, at=now)
            return Transition(
                state=MemberState(opted_out=True, revision=revision),
                group_revision=group_revision,
                changed=True,
            )

    def opt_in(
        self,
        member: MemberKey,
        *,
        required_notice_version: str,
    ) -> Transition:
        """加入：仅在本群已告知并开启、且未暂停时生效（需求 §4.4）；等值重复调用幂等。

        加入不恢复任何旧材料——退出时缓冲与相关历史已被清理。
        """
        if not isinstance(required_notice_version, str) or not required_notice_version.strip():
            raise ValueError("required_notice_version 必须是非空文本")
        with self._database.transaction() as connection:
            policy = read_policy(connection, member.group)
            if not policy.is_collection_open(
                required_notice_version=required_notice_version
            ):
                raise PolicyRefused("本群未告知、未开启采集或已暂停，不能加入")
            row = _read_row(connection, member)
            if row is None or not bool(row["context_opt_out"]):
                return Transition(
                    state=MemberState(
                        opted_out=False,
                        revision=int(row["revision"]) if row is not None else 0,
                    ),
                    group_revision=policy.revision,
                    changed=False,
                )
            revision = int(row["revision"]) + 1
            connection.execute(
                "UPDATE member_state SET context_opt_out = 0, revision = ?, updated_at = ? "
                "WHERE platform_id = ? AND self_id = ? AND group_id = ? AND member_id = ?",
                (revision, self._clock(), *key_params(member)),
            )
            return Transition(
                state=MemberState(opted_out=False, revision=revision),
                group_revision=policy.revision,
                changed=True,
            )


def _read_row(connection: sqlite3.Connection, member: MemberKey) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT context_opt_out, revision FROM member_state "
        "WHERE platform_id = ? AND self_id = ? AND group_id = ? AND member_id = ?",
        key_params(member),
    ).fetchone()
