"""低敏长期记忆的持久化（S3-01/S3-02）：授权状态、事实与来源去重。

本模块只做存储，不做判断：类别白名单属 ``memory/types.py``（S3-05），归属只由可信事件
元数据构造的 ``MemberKey`` 决定，长度与敏感过滤在候选层完成。它回答三个问题——
"这位成员在本群授权了吗""他有哪些未过期的记录""这条源消息的这类动作处理过了吗"。

三条结构性约定与 ``db.py`` 一致：延迟建库、短事务、失败粘滞。没有行就是没有授权、
没有记录，读取一律按关闭处理。

**上限与保留期由调用方注入**（与 ``context_buffer`` / ``dedup`` 同例）：本模块没有
"20 条""90 天"这类字面量——``MemoryLimits`` 与 ``retention_seconds`` 都必须由调用方
给出。它们是待复核的业务参数，写成默认值就等于把未核验的假设固化成行为。

**成员修订号复用 ``member_state.revision``**（架构 §6.2）：授权变更、纠正、删除都在同一
事务内 +1，让在途聊天与抽取结果失效；本模块不持第二个计数器，因此 ``RevisionSnapshot``
的两侧比较与 ``send_gate`` 都不需要改动。

**来源登记与事实分开存**：``memory_source`` 只记"成员 + 源消息 + 动作"，不含原文。它必须
在事实被删除之后仍然存在，否则同一条消息被重复投递时会把刚删掉的记录重新抽回来。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Sequence

from ..keys import MemberKey
from .db import Database
from .groups import PolicyRefused
from .members import bump_member_revision, key_params, read_member_state

ORIGIN_AUTO = "auto"
ORIGIN_MANUAL = "manual"
"""事实来源：自动抽取与人工纠正。人工纠正优先——同类别已有手工记录时不再自动写入。"""

_SELECT_STATE = (
    "SELECT authorized, auth_version, next_record_id FROM memory_state "
    "WHERE platform_id = ? AND self_id = ? AND group_id = ? AND member_id = ?"
)

_SELECT_FACTS = (
    "SELECT record_id, category, content, origin, source_message_id, "
    "created_at, updated_at, expires_at FROM memory_fact "
    "WHERE platform_id = ? AND self_id = ? AND group_id = ? AND member_id = ? "
    "AND expires_at > ? ORDER BY record_id"
)

_SELECT_FACT = (
    "SELECT record_id, category, content, origin, source_message_id, "
    "created_at, updated_at, expires_at FROM memory_fact "
    "WHERE platform_id = ? AND self_id = ? AND group_id = ? AND member_id = ? "
    "AND record_id = ?"
)

_INSERT_FACT = (
    "INSERT INTO memory_fact (platform_id, self_id, group_id, member_id, record_id, "
    "category, content, origin, source_message_id, created_at, updated_at, expires_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_UPSERT_STATE = (
    "INSERT INTO memory_state (platform_id, self_id, group_id, member_id, authorized, "
    "auth_version, next_record_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
    "ON CONFLICT (platform_id, self_id, group_id, member_id) DO UPDATE SET "
    "authorized = excluded.authorized, auth_version = excluded.auth_version, "
    "updated_at = excluded.updated_at"
)


@dataclass(frozen=True)
class MemoryLimits:
    """每成员的事实条数上限与保留期；**无默认值**，必须由调用方注入。"""

    max_records: int
    ttl_seconds: int

    def __post_init__(self) -> None:
        if not _is_bounded_int(self.max_records, 1, 1_000):
            raise ValueError("max_records 必须是 1..1000 的整数")
        if not _is_bounded_int(self.ttl_seconds, 1, 3_650 * 86_400):
            raise ValueError("ttl_seconds 必须是 1..3650 天的整数秒")


@dataclass(frozen=True)
class MemoryState:
    """成员在本群的记忆状态；``absent()`` 表示从未授权（即未授权）。"""

    authorized: bool
    auth_version: str
    next_record_id: int

    @classmethod
    def absent(cls) -> "MemoryState":
        return cls(authorized=False, auth_version="", next_record_id=1)


@dataclass(frozen=True)
class MemoryFact:
    """一条低敏事实。``origin`` 取 ``ORIGIN_AUTO`` 或 ``ORIGIN_MANUAL``。"""

    record_id: int
    category: str
    content: str
    origin: str
    source_message_id: str
    created_at: int
    updated_at: int
    expires_at: int


@dataclass(frozen=True)
class AuthorizationTransition:
    """一次授权/撤回的结果：新状态、成员修订号，以及状态是否真的变化。"""

    state: MemoryState
    revision: int
    changed: bool


# ---- 既有连接内的读写（供需要多步同事务的调用方复用） ----


def read_memory_state(connection: sqlite3.Connection, member: MemberKey) -> MemoryState:
    """在既有连接/事务内读取记忆状态；无行即未授权。"""
    row = connection.execute(_SELECT_STATE, key_params(member)).fetchone()
    if row is None:
        return MemoryState.absent()
    return MemoryState(
        authorized=bool(row["authorized"]),
        auth_version=str(row["auth_version"]),
        next_record_id=int(row["next_record_id"]),
    )


def read_facts(
    connection: sqlite3.Connection,
    member: MemberKey,
    *,
    now: int,
) -> tuple[MemoryFact, ...]:
    """未过期的事实，按记录号升序。

    **读不续期**：本函数不修改 ``expires_at``（需求 §4.3"检索不自动续期"）。
    """
    rows = connection.execute(_SELECT_FACTS, (*key_params(member), now)).fetchall()
    return tuple(_fact(row) for row in rows)


def purge_expired(connection: sqlite3.Connection, member: MemberKey, *, now: int) -> int:
    """删掉这位成员已过期的事实，返回条数。机会性清理，不引入后台任务。"""
    cursor = connection.execute(
        "DELETE FROM memory_fact WHERE platform_id = ? AND self_id = ? AND group_id = ? "
        "AND member_id = ? AND expires_at <= ?",
        (*key_params(member), now),
    )
    return int(cursor.rowcount)


def add_auto_facts(
    connection: sqlite3.Connection,
    member: MemberKey,
    *,
    facts: Sequence[tuple[str, str]],
    source_message_id: str,
    limits: MemoryLimits,
    at: int,
) -> tuple[MemoryFact, ...]:
    """把一批候选写成本人的自动事实，返回真正写入的部分。

    四条拒绝规则，全部按"宁可少记"：
    未授权即抛 ``PolicyRefused``；同类别已有**人工**记录时跳过（人工纠正优先）；
    同类别同内容已存在时跳过；写满 ``limits.max_records`` 后**放弃剩余候选而不是淘汰旧记录**。
    只在真的写入时增加成员修订号。
    """
    if not isinstance(source_message_id, str) or not source_message_id.strip():
        raise ValueError("source_message_id 必须是非空文本")
    if not _is_bounded_int(at, 0, 2**62):
        raise ValueError("at 必须是 Unix 秒整数")

    state = read_memory_state(connection, member)
    if not state.authorized:
        raise PolicyRefused("成员未授权长期记忆，拒绝写入")

    purge_expired(connection, member, now=at)
    existing = read_facts(connection, member, now=at)
    manual_categories = {
        fact.category for fact in existing if fact.origin == ORIGIN_MANUAL
    }
    seen = {(fact.category, fact.content) for fact in existing}
    count = len(existing)
    next_id = state.next_record_id
    expires_at = at + limits.ttl_seconds
    written: list[MemoryFact] = []

    for category, content in facts:
        if not _is_plain_text(category) or not _is_plain_text(content):
            raise ValueError("候选必须是（非空且无首尾空白的）类别与内容")
        if category in manual_categories or (category, content) in seen:
            continue
        if count >= limits.max_records:
            break
        connection.execute(
            _INSERT_FACT,
            (
                *key_params(member),
                next_id,
                category,
                content,
                ORIGIN_AUTO,
                source_message_id,
                at,
                at,
                expires_at,
            ),
        )
        seen.add((category, content))
        written.append(
            MemoryFact(
                record_id=next_id,
                category=category,
                content=content,
                origin=ORIGIN_AUTO,
                source_message_id=source_message_id,
                created_at=at,
                updated_at=at,
                expires_at=expires_at,
            )
        )
        count += 1
        next_id += 1

    if written:
        connection.execute(
            "UPDATE memory_state SET next_record_id = ?, updated_at = ? "
            "WHERE platform_id = ? AND self_id = ? AND group_id = ? AND member_id = ?",
            (next_id, at, *key_params(member)),
        )
        bump_member_revision(connection, member, at=at)
    return tuple(written)


def mark_source(
    connection: sqlite3.Connection,
    member: MemberKey,
    *,
    source_message_id: str,
    action: str,
    retention_seconds: int,
    at: int,
) -> bool:
    """登记"这条源消息的这类动作已处理"；重复登记返回 ``False``。

    同时按注入的保留期清掉本人的过期登记——去重元数据必须有界（架构 §6.1），
    而窗口取值与 ``dedup`` 同例，由调用方给出，不写默认值。
    """
    if not _is_plain_text(source_message_id) or not _is_plain_text(action):
        raise ValueError("source_message_id 与 action 必须是非空且无首尾空白的文本")
    if not _is_bounded_int(retention_seconds, 1, 3_650 * 86_400):
        raise ValueError("retention_seconds 必须是 1..3650 天的整数秒")
    connection.execute(
        "DELETE FROM memory_source WHERE platform_id = ? AND self_id = ? AND group_id = ? "
        "AND member_id = ? AND created_at <= ?",
        (*key_params(member), at - retention_seconds),
    )
    cursor = connection.execute(
        "INSERT OR IGNORE INTO memory_source (platform_id, self_id, group_id, member_id, "
        "source_message_id, action, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*key_params(member), source_message_id, action, at),
    )
    return int(cursor.rowcount) == 1


# ---- 短事务入口 ----


class MemoryStore:
    """记忆授权与事实的读写入口。全部方法同步、短事务、fail-closed。

    需要把写入与别的动作放进同一个事务时（抽取管线要在同一事务内重核修订号与来源），
    调用本模块的模块级函数；这里的每个方法各自开一个短事务，供单步操作使用。
    """

    def __init__(self, database: Database, *, clock: Callable[[], int]) -> None:
        self._database = database
        self._clock = clock

    def state(self, member: MemberKey) -> MemoryState:
        with self._database.read() as connection:
            if connection is None:
                return MemoryState.absent()
            return read_memory_state(connection, member)

    def facts(self, member: MemberKey) -> tuple[MemoryFact, ...]:
        now = self._clock()
        with self._database.read() as connection:
            if connection is None:
                return ()
            return read_facts(connection, member, now=now)

    def set_authorized(
        self,
        member: MemberKey,
        *,
        authorized: bool,
        auth_version: str,
    ) -> AuthorizationTransition:
        """授权 / 撤回授权，并在同一事务内把成员修订号 +1；等值重复调用幂等。

        撤回时清空授权版本（没有生效中的说明）；不删除事实——"关闭"与"删除全部"是否
        连动属命令语义（S3-04），由调用方组合本方法与 :meth:`clear`。
        """
        if not isinstance(authorized, bool):
            raise ValueError("authorized 必须是布尔值")
        if not _is_plain_text(auth_version):
            raise ValueError("auth_version 必须是非空且无首尾空白的文本")
        now = self._clock()
        with self._database.transaction() as connection:
            current = read_memory_state(connection, member)
            settled_version = auth_version if authorized else ""
            if current.authorized is authorized and current.auth_version == settled_version:
                return AuthorizationTransition(
                    state=current,
                    revision=read_member_state(connection, member).revision,
                    changed=False,
                )
            connection.execute(
                _UPSERT_STATE,
                (*key_params(member), int(authorized), settled_version, now),
            )
            revision = bump_member_revision(connection, member, at=now)
            return AuthorizationTransition(
                state=MemoryState(
                    authorized=authorized,
                    auth_version=settled_version,
                    next_record_id=current.next_record_id,
                ),
                revision=revision,
                changed=True,
            )

    def record_auto_facts(
        self,
        member: MemberKey,
        *,
        facts: Sequence[tuple[str, str]],
        source_message_id: str,
        limits: MemoryLimits,
    ) -> tuple[MemoryFact, ...]:
        """自动抽取的写入面（单事务版）；写入规则见 :func:`add_auto_facts`。"""
        now = self._clock()
        with self._database.transaction() as connection:
            return add_auto_facts(
                connection,
                member,
                facts=facts,
                source_message_id=source_message_id,
                limits=limits,
                at=now,
            )

    def remember_source(
        self,
        member: MemberKey,
        *,
        source_message_id: str,
        action: str,
        retention_seconds: int,
    ) -> bool:
        """来源登记（单事务版）；重复返回 ``False``。"""
        now = self._clock()
        with self._database.transaction() as connection:
            return mark_source(
                connection,
                member,
                source_message_id=source_message_id,
                action=action,
                retention_seconds=retention_seconds,
                at=now,
            )

    def correct(self, member: MemberKey, record_id: int, *, content: str) -> MemoryFact | None:
        """人工纠正：改内容、标记为人工、增加成员修订号。

        返回修改后的事实；记录不存在或已过期返回 ``None``（已过期视同不存在）。
        **不续期**：``expires_at`` 保持原值（以"宁可少记"为准）。
        """
        _require_record_id(record_id)
        if not _is_plain_text(content):
            raise ValueError("content 必须是非空且无首尾空白的文本")
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(_SELECT_FACT, (*key_params(member), record_id)).fetchone()
            if row is None or int(row["expires_at"]) <= now:
                return None
            connection.execute(
                "UPDATE memory_fact SET content = ?, origin = ?, updated_at = ? "
                "WHERE platform_id = ? AND self_id = ? AND group_id = ? AND member_id = ? "
                "AND record_id = ?",
                (
                    content,
                    ORIGIN_MANUAL,
                    now,
                    *key_params(member),
                    record_id,
                ),
            )
            bump_member_revision(connection, member, at=now)
            return MemoryFact(
                record_id=record_id,
                category=str(row["category"]),
                content=content,
                origin=ORIGIN_MANUAL,
                source_message_id=str(row["source_message_id"]),
                created_at=int(row["created_at"]),
                updated_at=now,
                expires_at=int(row["expires_at"]),
            )

    def delete(self, member: MemberKey, record_id: int) -> bool:
        """删除单条记录；不存在或已过期返回 ``False``；删除成功时成员修订号 +1。"""
        _require_record_id(record_id)
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_fact WHERE platform_id = ? AND self_id = ? "
                "AND group_id = ? AND member_id = ? AND record_id = ? AND expires_at > ?",
                (*key_params(member), record_id, now),
            )
            deleted = int(cursor.rowcount) == 1
            if deleted:
                bump_member_revision(connection, member, at=now)
            return deleted

    def clear(self, member: MemberKey) -> int:
        """清空这位成员的全部记录（含已过期行），返回条数；有删除时成员修订号 +1。

        **不动 ``memory_source``**：来源登记不含原文，且必须活过事实本身，否则同一条
        消息再次投递会把刚删掉的记录重新抽回来。
        """
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_fact WHERE platform_id = ? AND self_id = ? "
                "AND group_id = ? AND member_id = ?",
                key_params(member),
            )
            removed = int(cursor.rowcount)
            if removed:
                bump_member_revision(connection, member, at=now)
            return removed


# ---- 内部 ----


def _fact(row: sqlite3.Row) -> MemoryFact:
    return MemoryFact(
        record_id=int(row["record_id"]),
        category=str(row["category"]),
        content=str(row["content"]),
        origin=str(row["origin"]),
        source_message_id=str(row["source_message_id"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        expires_at=int(row["expires_at"]),
    )


def _require_record_id(record_id: object) -> None:
    if not _is_bounded_int(record_id, 1, 2**62):
        raise ValueError("record_id 必须是正整数")


def _is_plain_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _is_bounded_int(value: object, low: int, high: int) -> bool:
    # bool 是 int 的子类，必须显式排除。
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
