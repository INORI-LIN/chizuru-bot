"""群策略仓储：告知版本、采集开关、暂停状态与群修订号。

**无有效策略默认关闭**：没有行就是没有告知、没有开启。本模块不插入任何"默认策略"
行，行只在真实动作（告知确认、暂停、成员退出）时创建；读取一律按关闭处理。

写接口的调用方：``record_notice_confirmed`` 属 S2-02 的两步告知流程，
``set_context_enabled`` / ``set_paused`` 属 S2-05 的维护者命令。两张卡都因目标群与
维护者清单未提供而阻塞——本批先把状态模型与 fail-closed 读取落地，命令接线等它们。

读取采集资格时**每次都要带上当前文案版本**（``Settings.notice_version``）：告知
文案一改，版本就不匹配，采集自动回到关闭（需求 §4.2"文案一旦修改必须重新告知"）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from ..config import is_qq_id
from ..keys import GroupKey
from .db import Database

_SELECT = (
    "SELECT context_enabled, paused, notice_version, revision FROM group_policy "
    "WHERE platform_id = ? AND self_id = ? AND group_id = ?"
)

_SELECT_REVISION = (
    "SELECT revision FROM group_policy WHERE platform_id = ? AND self_id = ? AND group_id = ?"
)


@dataclass(frozen=True)
class GroupPolicy:
    """一个群的持久化策略；``absent()`` 表示"没有策略"，即关闭。"""

    exists: bool
    context_enabled: bool
    paused: bool
    notice_version: str
    revision: int

    @classmethod
    def absent(cls) -> "GroupPolicy":
        return cls(
            exists=False,
            context_enabled=False,
            paused=False,
            notice_version="",
            revision=0,
        )

    def is_collection_open(self, *, required_notice_version: str) -> bool:
        """采集是否开启：有策略行、已开启、未暂停，且告知版本与当前文案一致。"""
        return bool(
            self.exists
            and self.context_enabled
            and not self.paused
            and required_notice_version
            and self.notice_version == required_notice_version
        )


class PolicyRefused(RuntimeError):
    """状态前置不满足（没有策略行、告知不匹配等）：调用方按"未发生"处理。

    抛出发生在事务内，``Database.transaction`` 会回滚——拒绝不会留下部分写入。
    """


def key_params(group: GroupKey) -> tuple[str, str, str]:
    """把三重键展平成列值：平台连接实例 + 机器人 self_id + 群（架构 §6.1）。"""
    return (group.instance.platform_id, group.instance.self_id, group.group_id)


def read_policy(connection: sqlite3.Connection, group: GroupKey) -> GroupPolicy:
    """在既有连接/事务内读取策略；供 ``members.py`` 复用。"""
    row = connection.execute(_SELECT, key_params(group)).fetchone()
    if row is None:
        return GroupPolicy.absent()
    return GroupPolicy(
        exists=True,
        context_enabled=bool(row["context_enabled"]),
        paused=bool(row["paused"]),
        notice_version=str(row["notice_version"]),
        revision=int(row["revision"]),
    )


def bump_group_revision(connection: sqlite3.Connection, group: GroupKey, *, at: int) -> int:
    """群修订号 +1（无行则以关闭状态建行，revision 从 1 起）；在调用方事务内使用。

    普通退出等会影响整个群上下文的操作必须先增修订号，让在途结果失效（架构 §6.2）。
    """
    row = connection.execute(_SELECT_REVISION, key_params(group)).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO group_policy (platform_id, self_id, group_id, revision, updated_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (*key_params(group), at),
        )
        return 1
    revision = int(row["revision"]) + 1
    connection.execute(
        "UPDATE group_policy SET revision = ?, updated_at = ? "
        "WHERE platform_id = ? AND self_id = ? AND group_id = ?",
        (revision, at, *key_params(group)),
    )
    return revision


class GroupPolicyStore:
    """群策略读写入口。全部方法同步、短事务、fail-closed。"""

    def __init__(self, database: Database, *, clock: Callable[[], int]) -> None:
        self._database = database
        self._clock = clock

    def policy(self, group: GroupKey) -> GroupPolicy:
        with self._database.read() as connection:
            if connection is None:
                return GroupPolicy.absent()
            return read_policy(connection, group)

    def revision(self, group: GroupKey) -> int:
        return self.policy(group).revision

    def record_notice_confirmed(
        self,
        group: GroupKey,
        *,
        version: str,
        actor_id: str,
    ) -> GroupPolicy:
        """记录一次完成的告知并开启采集（S2-02 的写入面）。

        等值重复确认是幂等的：不重复写、不增修订号。
        """
        if not _is_plain_text(version):
            raise ValueError("告知版本必须是非空且无首尾空白的文本")
        if not is_qq_id(actor_id):
            raise ValueError("告知人必须是 QQ 号形式")
        now = self._clock()
        with self._database.transaction() as connection:
            current = read_policy(connection, group)
            if (
                current.exists
                and current.context_enabled
                and current.notice_version == version
            ):
                return current
            revision = current.revision + 1
            if current.exists:
                connection.execute(
                    "UPDATE group_policy SET context_enabled = 1, notice_version = ?, "
                    "notice_at = ?, notice_by = ?, revision = ?, updated_at = ? "
                    "WHERE platform_id = ? AND self_id = ? AND group_id = ?",
                    (version, now, actor_id, revision, now, *key_params(group)),
                )
            else:
                connection.execute(
                    "INSERT INTO group_policy (platform_id, self_id, group_id, "
                    "context_enabled, notice_version, notice_at, notice_by, revision, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                    (*key_params(group), version, now, actor_id, revision, now),
                )
            return GroupPolicy(
                exists=True,
                context_enabled=True,
                paused=current.paused,
                notice_version=version,
                revision=revision,
            )

    def set_context_enabled(
        self,
        group: GroupKey,
        *,
        enabled: bool,
        required_notice_version: str,
    ) -> GroupPolicy:
        """开启 / 关闭采集（S2-05 的写入面）。

        开启要求已有策略行且告知版本与当前文案一致；否则抛 ``PolicyRefused``，
        等值重复调用幂等。
        """
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        if not _is_plain_text(required_notice_version):
            raise ValueError("required_notice_version 必须是非空且无首尾空白的文本")
        now = self._clock()
        with self._database.transaction() as connection:
            current = read_policy(connection, group)
            if not current.exists:
                raise PolicyRefused("该群没有群策略行")
            if enabled and current.notice_version != required_notice_version:
                raise PolicyRefused("告知版本不匹配，不能开启采集")
            if current.context_enabled is enabled:
                return current
            revision = current.revision + 1
            connection.execute(
                "UPDATE group_policy SET context_enabled = ?, revision = ?, updated_at = ? "
                "WHERE platform_id = ? AND self_id = ? AND group_id = ?",
                (int(enabled), revision, now, *key_params(group)),
            )
            return GroupPolicy(
                exists=True,
                context_enabled=enabled,
                paused=current.paused,
                notice_version=current.notice_version,
                revision=revision,
            )

    def set_paused(self, group: GroupKey, *, paused: bool) -> GroupPolicy:
        """暂停 / 恢复本群（S2-05 的写入面）；等值重复调用幂等。"""
        if not isinstance(paused, bool):
            raise ValueError("paused 必须是布尔值")
        now = self._clock()
        with self._database.transaction() as connection:
            current = read_policy(connection, group)
            if not current.exists:
                raise PolicyRefused("该群没有群策略行")
            if current.paused is paused:
                return current
            revision = current.revision + 1
            connection.execute(
                "UPDATE group_policy SET paused = ?, revision = ?, updated_at = ? "
                "WHERE platform_id = ? AND self_id = ? AND group_id = ?",
                (int(paused), revision, now, *key_params(group)),
            )
            return GroupPolicy(
                exists=True,
                context_enabled=current.context_enabled,
                paused=paused,
                notice_version=current.notice_version,
                revision=revision,
            )


def _is_plain_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()
