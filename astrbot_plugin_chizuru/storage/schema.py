"""SQLite 结构定义与版本。

五张表分属两个阶段：S2 的 ``group_policy`` / ``member_state``（群策略与成员上下文），
S3 的 ``memory_state`` / ``memory_fact`` / ``memory_source``（记忆授权、低敏事实、
来源去重）。**两张 S2 表仍然没有"记忆授权"列**——记忆授权只在 ``memory_state``
里，"退出普通群上下文不改动记忆授权"因此仍是表结构的事实，而不是实现纪律。

"无有效行默认关闭"同样落在结构上：本模块不插入任何默认行，行只在真实动作
（告知确认、暂停、成员退出、成员授权、抽取写回）发生时创建。

**结构版本 2 的迁移口径**：``ensure_schema`` 一直是幂等的 ``IF NOT EXISTS`` 建表加写
版本，因此老库（v1）在**首次写入**时自动补齐三张新表并升到 2，既有行原样保留，不需要
显式的迁移分支。代价写在明处：首次写入之前 ``Database.read`` 按"未初始化"处理
（``db._is_initialized`` 只在版本相等时为真），群策略与成员状态因此短暂回到"无状态"
——采集关闭、暂停视为未暂停、聊天照常。这是 fail-closed，不是数据丢失。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2
"""结构版本（``PRAGMA user_version``）。比它新的库一律拒绝（``db.StorageFailure``）。"""

CREATE_GROUP_POLICY = """
CREATE TABLE IF NOT EXISTS group_policy (
    platform_id     TEXT    NOT NULL,
    self_id         TEXT    NOT NULL,
    group_id        TEXT    NOT NULL,
    context_enabled INTEGER NOT NULL DEFAULT 0 CHECK (context_enabled IN (0, 1)),
    paused          INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    notice_version  TEXT    NOT NULL DEFAULT '',
    notice_at       INTEGER,
    notice_by       TEXT    NOT NULL DEFAULT '',
    revision        INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (platform_id, self_id, group_id)
) STRICT
"""

CREATE_MEMBER_STATE = """
CREATE TABLE IF NOT EXISTS member_state (
    platform_id     TEXT    NOT NULL,
    self_id         TEXT    NOT NULL,
    group_id        TEXT    NOT NULL,
    member_id       TEXT    NOT NULL,
    context_opt_out INTEGER NOT NULL DEFAULT 0 CHECK (context_opt_out IN (0, 1)),
    revision        INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (platform_id, self_id, group_id, member_id)
) STRICT
"""

CREATE_MEMORY_STATE = """
CREATE TABLE IF NOT EXISTS memory_state (
    platform_id    TEXT    NOT NULL,
    self_id        TEXT    NOT NULL,
    group_id       TEXT    NOT NULL,
    member_id      TEXT    NOT NULL,
    authorized     INTEGER NOT NULL DEFAULT 0 CHECK (authorized IN (0, 1)),
    auth_version   TEXT    NOT NULL DEFAULT '',
    next_record_id INTEGER NOT NULL DEFAULT 1 CHECK (next_record_id >= 1),
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (platform_id, self_id, group_id, member_id)
) STRICT
"""

CREATE_MEMORY_FACT = """
CREATE TABLE IF NOT EXISTS memory_fact (
    platform_id       TEXT    NOT NULL,
    self_id           TEXT    NOT NULL,
    group_id          TEXT    NOT NULL,
    member_id         TEXT    NOT NULL,
    record_id         INTEGER NOT NULL CHECK (record_id >= 1),
    category          TEXT    NOT NULL,
    content           TEXT    NOT NULL,
    origin            TEXT    NOT NULL CHECK (origin IN ('auto', 'manual')),
    source_message_id TEXT    NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    PRIMARY KEY (platform_id, self_id, group_id, member_id, record_id)
) STRICT
"""

CREATE_MEMORY_SOURCE = """
CREATE TABLE IF NOT EXISTS memory_source (
    platform_id       TEXT    NOT NULL,
    self_id           TEXT    NOT NULL,
    group_id          TEXT    NOT NULL,
    member_id         TEXT    NOT NULL,
    source_message_id TEXT    NOT NULL,
    action            TEXT    NOT NULL,
    created_at        INTEGER NOT NULL,
    PRIMARY KEY (
        platform_id, self_id, group_id, member_id, source_message_id, action
    )
) STRICT
"""

_TABLES = (
    CREATE_GROUP_POLICY,
    CREATE_MEMBER_STATE,
    CREATE_MEMORY_STATE,
    CREATE_MEMORY_FACT,
    CREATE_MEMORY_SOURCE,
)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """建表并写入结构版本；幂等，重复调用不改变既有数据。"""
    for statement in _TABLES:
        connection.execute(statement)
    # PRAGMA 不接受参数绑定；SCHEMA_VERSION 是模块内整数常量，不来自外部输入。
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
