"""待维护错误登记（S4-03，闭合 R23）：清理未能确认完成的次数**跨重启保留**。

背景（R23）：S2-08 的登记只在内存，重启后归零——维护者可能误判"已经恢复"，实际旧
历史可能仍在会话存储里。本模块把**群级删除**的失败落成按群聚合的计数行（只有计数与
时间，没有正文、路径或错误文本），启动时由调用方读回并据此恢复计数与 `DELETION_FAILED`；
一次**成功的重跑**（`上下文 清空` / `群上下文 关闭` / `上下文 退出`）清除该群的行，
全部清空后由调用方解除降级（架构 §8.3"恢复前不重启相关能力"）。

**只覆盖群级删除**：启动裁剪（`history.trim_stored`）的失败不落这里——它在每次启动都会
自动重跑，没有需要维护者介入的"未恢复"状态；那类失败仍只计本次运行的计数。

键与其它仓储一致：平台连接实例 + 机器人 self_id + 群。本模块不做业务判断、不重试、
不发送、不发通知；库未初始化时读取返回"没有登记"（fail-closed），写入失败抛
``StorageFailure`` 交由调用方降级。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..keys import GroupKey
from .db import Database
from .groups import key_params

_SELECT_GROUP = (
    "SELECT failures, first_at, last_at FROM cleanup_failure "
    "WHERE platform_id = ? AND self_id = ? AND group_id = ?"
)

_SELECT_TOTAL = "SELECT COALESCE(SUM(failures), 0) FROM cleanup_failure"

_DELETE_GROUP = (
    "DELETE FROM cleanup_failure WHERE platform_id = ? AND self_id = ? AND group_id = ?"
)


@dataclass(frozen=True)
class CleanupFailures:
    """一个群尚未确认恢复的清理失败；``absent()`` 表示没有登记。"""

    exists: bool
    failures: int
    first_at: int
    last_at: int

    @classmethod
    def absent(cls) -> "CleanupFailures":
        return cls(exists=False, failures=0, first_at=0, last_at=0)


class MaintenanceStore:
    """待维护错误登记读写入口。全部方法同步、短事务、fail-closed。"""

    def __init__(self, database: Database, *, clock: Callable[[], int]) -> None:
        self._database = database
        self._clock = clock

    def failures(self, group: GroupKey) -> CleanupFailures:
        with self._database.read() as connection:
            if connection is None:
                return CleanupFailures.absent()
            row = connection.execute(_SELECT_GROUP, key_params(group)).fetchone()
        if row is None:
            return CleanupFailures.absent()
        return CleanupFailures(
            exists=True,
            failures=int(row["failures"]),
            first_at=int(row["first_at"]),
            last_at=int(row["last_at"]),
        )

    def total(self) -> int:
        """所有群"尚未确认恢复"的失败次数之和；供启动时恢复计数与降级。"""
        with self._database.read() as connection:
            if connection is None:
                return 0
            row = connection.execute(_SELECT_TOTAL).fetchone()
        return int(row[0]) if row is not None else 0

    def record_failure(self, group: GroupKey) -> int:
        """登记一次清理失败（同一群重复失败累加），返回该群的累计值。

        ``first_at`` 只在首次登记时写入，``last_at`` 每次刷新：维护者据此判断
        "是不是刚刚才失败过"。
        """
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(_SELECT_GROUP, key_params(group)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO cleanup_failure (platform_id, self_id, group_id, failures, "
                    "first_at, last_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (*key_params(group), now, now),
                )
                return 1
            failures = int(row["failures"]) + 1
            connection.execute(
                "UPDATE cleanup_failure SET failures = ?, last_at = ? "
                "WHERE platform_id = ? AND self_id = ? AND group_id = ?",
                (failures, now, *key_params(group)),
            )
            return failures

    def clear_group(self, group: GroupKey) -> bool:
        """清除该群的登记（一次**成功**的清理后调用）；返回是否真的删掉了行。

        没有登记时**不写库**：DELETE 也是一次写入，而"延迟建库"要求只有真实变更才
        创建文件——一个从未失败过的部署不该因为一次成功清理而多出一个空库。重复调用
        幂等（第二次返回 `False`，因为没有可清的行）。
        """
        if not self.failures(group).exists:
            return False
        with self._database.transaction() as connection:
            cursor = connection.execute(_DELETE_GROUP, key_params(group))
            return cursor.rowcount > 0
