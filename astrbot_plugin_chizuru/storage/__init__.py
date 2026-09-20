"""插件持久化层（S2-01/S2-04 + S3-01/S3-02）：群策略、成员上下文与长期记忆。

``Storage`` 是唯一门面，``open_storage`` 是唯一构造入口——库路径必须由调用方
注入（``main._resolve_storage_path``），本包内没有任何默认路径。

本层只保存**运行时状态与成员数据**：群告知版本与开关、暂停、成员退出、记忆授权、
低敏事实与来源登记。配置（允许群、维护者映射、文案版本）仍在 ``config.py``；
事实的条数上限与保留期属于业务参数，由调用方在写入时注入
（``MemoryLimits``），本层不写默认值。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .db import BUSY_TIMEOUT_SECONDS, DB_FILE_NAME, Database, StorageFailure
from .groups import (
    GroupPolicy,
    GroupPolicyStore,
    PolicyRefused,
    bump_group_revision,
    key_params,
    read_policy,
)
from .members import MemberState, MemberStore, Transition, read_member_state
from .memories import (
    ORIGIN_AUTO,
    ORIGIN_MANUAL,
    AuthorizationTransition,
    MemoryFact,
    MemoryLimits,
    MemoryState,
    MemoryStore,
    add_auto_facts,
    mark_source,
    purge_expired,
    read_facts,
    read_memory_state,
)

__all__ = [
    "BUSY_TIMEOUT_SECONDS",
    "DB_FILE_NAME",
    "ORIGIN_AUTO",
    "ORIGIN_MANUAL",
    "AuthorizationTransition",
    "Database",
    "GroupPolicy",
    "GroupPolicyStore",
    "MemberState",
    "MemberStore",
    "MemoryFact",
    "MemoryLimits",
    "MemoryState",
    "MemoryStore",
    "PolicyRefused",
    "Storage",
    "StorageFailure",
    "Transition",
    "add_auto_facts",
    "bump_group_revision",
    "key_params",
    "mark_source",
    "open_storage",
    "purge_expired",
    "read_facts",
    "read_member_state",
    "read_memory_state",
    "read_policy",
]


@dataclass(frozen=True)
class Storage:
    """数据库与三个仓储的组合；``close`` 幂等，关闭后一切读写都被拒绝。"""

    database: Database
    groups: GroupPolicyStore
    members: MemberStore
    memories: MemoryStore

    def close(self) -> None:
        self.database.close()


def open_storage(path: Path, *, clock: Callable[[], int]) -> Storage:
    """构造存储门面并校验既有库；库文件不存在时不创建（延迟建库）。

    ``clock`` 返回 Unix 秒，由调用方注入（生产为墙上时钟）。校验失败抛
    ``StorageFailure``，调用方按架构 §8.3 保持相关能力关闭。
    """
    database = Database(path)
    storage = Storage(
        database=database,
        groups=GroupPolicyStore(database, clock=clock),
        members=MemberStore(database, clock=clock),
        memories=MemoryStore(database, clock=clock),
    )
    database.probe()
    return storage
