"""插件 SQLite 基础：延迟建库、短事务、失败不恢复。

**本模块只做存储基础设施**：连接、事务边界与结构版本校验。它不知道群、成员与
消息，也不做任何业务判断——策略语义在 ``groups.py`` / ``members.py``。

三条硬性约定：

- **延迟建库**：读操作在库文件不存在时返回"无状态"，绝不创建文件；只有真实写入
  才建库建表。这样"重装 / 状态不完整"自然回到关闭状态，也让只做分类与聊天的
  部署不会因为加载插件而产生磁盘文件。
- **短事务**：写操作在单个 ``transaction()`` 内完成并立即提交；连接不跨调用存活。
  模块是纯同步的（不 import asyncio），因此"模型请求在事务外完成"是结构性的。
- **失败不恢复**：一旦出现 SQL 错误，``failed`` 置位且不自动清除。上层按架构
  §8.3 保持相关能力关闭，而不是带着未知状态继续读写。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import schema

DB_FILE_NAME = "chizuru.db"
"""库文件名；位于 AstrBot 插件数据目录（由 main 解析，见 `main._resolve_storage_path`）。"""

BUSY_TIMEOUT_SECONDS = 5.0
"""连接等待锁的最长时间。**建议参数待评审**：单进程短事务，取值远大于实际需要。"""


class StorageFailure(RuntimeError):
    """存储不可用：结构版本不认识、文件损坏或读写失败。

    异常文本固定，不携带 SQL、路径或数据；调用方只据此降级，不做补救。
    """


class Database:
    """单文件 SQLite 的门面。所有方法同步，调用方负责不把它放进长事务。"""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.name:
            raise ValueError("Database 需要指向文件的 Path（不接受空路径）")
        self._path = path
        self._failed = False
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def failed(self) -> bool:
        """是否已进入失败状态；一旦为真，所有读写都被拒绝。"""
        return self._failed

    def probe(self) -> None:
        """校验既有库（结构版本、可读性）；库文件不存在视为未初始化，不建库。"""
        with self.read() as connection:
            del connection

    def close(self) -> None:
        """幂等关闭；关闭后一切读写都被拒绝（fail-closed）。"""
        self._closed = True

    # ---- 读取 ----

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection | None]:
        """只读上下文；库未初始化（文件缺失或空库）时产出 ``None``。

        调用方把 ``None`` 当作"没有任何持久状态"，按 fail-closed 默认处理。
        """
        connection = self._open_read()
        if connection is None:
            yield None
            return
        try:
            if not self._is_initialized(connection):
                yield None
                return
            yield connection
        except sqlite3.Error as error:
            raise self._stuck() from error
        finally:
            connection.close()

    # ---- 写入 ----

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """短事务上下文：进入时确保结构存在，退出时提交或回滚并关闭连接。"""
        connection = self._open_write()
        try:
            schema.ensure_schema(connection)
            with connection:
                yield connection
        except sqlite3.Error as error:
            raise self._stuck() from error
        finally:
            connection.close()

    # ---- 内部 ----

    def _open_read(self) -> sqlite3.Connection | None:
        self._guard()
        if not self._path.exists():
            return None
        return self._connect()

    def _open_write(self) -> sqlite3.Connection:
        self._guard()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise self._stuck() from error
        return self._connect()

    def _guard(self) -> None:
        if self._failed or self._closed:
            raise StorageFailure("存储不可用")

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                str(self._path),
                timeout=BUSY_TIMEOUT_SECONDS,
            )
        except sqlite3.Error as error:
            raise self._stuck() from error
        connection.row_factory = sqlite3.Row
        return connection

    def _is_initialized(self, connection: sqlite3.Connection) -> bool:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > schema.SCHEMA_VERSION:
            # 比插件认识的更新：绝不猜测结构，保持关闭等维护侧处理。
            self._failed = True
            raise StorageFailure("库结构版本高于插件支持的版本")
        return version == schema.SCHEMA_VERSION

    def _stuck(self) -> StorageFailure:
        self._failed = True
        return StorageFailure("存储操作失败，相关能力保持关闭")
