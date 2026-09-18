"""SQLite 结构定义与版本。

表只有两张，且都**没有"记忆授权"列**：成员是否允许长期记忆属 S3，本批的结构在
结构上无法表达这种授权——"退出普通群上下文不改动记忆授权"因此不是纪律，而是
表结构的事实。

"无有效策略默认关闭"同样落在结构上：本模块不插入任何默认行，行只在真实动作
（告知确认、暂停、成员退出）发生时创建。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1
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

_TABLES = (CREATE_GROUP_POLICY, CREATE_MEMBER_STATE)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """建表并写入结构版本；幂等，重复调用不改变既有数据。"""
    for statement in _TABLES:
        connection.execute(statement)
    # PRAGMA 不接受参数绑定；SCHEMA_VERSION 是模块内整数常量，不来自外部输入。
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
