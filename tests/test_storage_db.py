"""S2-01 + S3-01 存储基础（db/schema）离线测试：延迟建库、短事务、失败不恢复、升版。"""

import ast
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.storage import DB_FILE_NAME, Database, StorageFailure, schema

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "astrbot_plugin_chizuru"
WORK_ROOT = ROOT / ".runtime"

EXPECTED_TABLES = {
    "group_policy",
    "member_state",
    "memory_state",
    "memory_fact",
    "memory_source",
}
"""结构版本 2 的**完整**表集合；精确相等（而不是包含），多一张少一张都要改这里。"""

_VERSION_1_GROUP_POLICY = """
CREATE TABLE group_policy (
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
"""结构版本 1 的群策略表原文（冻结自 0.19 版 ``schema.py``），用于升版用例。"""


class StorageDbTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="storage-db-", dir=WORK_ROOT)
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.path = self.dir / DB_FILE_NAME
        self.database = Database(self.path)

    def table_names(self) -> set[str]:
        with self.database.read() as connection:
            if connection is None:
                return set()
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }


class LazyCreationTests(StorageDbTestCase):
    def test_read_of_missing_file_returns_none_and_creates_nothing(self):
        with self.database.read() as connection:
            self.assertIsNone(connection)
        self.assertFalse(self.path.exists())
        # probe 同样只校验，不建库。
        self.database.probe()
        self.assertFalse(self.path.exists())

    def test_first_write_creates_schema_and_version(self):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO group_policy (platform_id, self_id, group_id, updated_at) "
                "VALUES ('qq-local', '10001', '20001', 1)"
            )
        self.assertTrue(self.path.exists())
        self.assertEqual(self.table_names(), EXPECTED_TABLES)
        with self.database.read() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, schema.SCHEMA_VERSION)

    def test_reopening_keeps_data(self):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO group_policy (platform_id, self_id, group_id, updated_at) "
                "VALUES ('qq-local', '10001', '20001', 1)"
            )
        reopened = Database(self.path)
        with reopened.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM group_policy").fetchone()[0]
        self.assertEqual(count, 1)

    def test_empty_file_is_treated_as_uninitialized(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        with self.database.read() as connection:
            self.assertIsNone(connection)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO member_state (platform_id, self_id, group_id, member_id, updated_at) "
                "VALUES ('qq-local', '10001', '20001', '30001', 1)"
            )
        self.assertEqual(self.table_names(), EXPECTED_TABLES)


class MigrationTests(StorageDbTestCase):
    def make_version_1_database(self) -> None:
        """手工造一个结构版本 1 的库（只有群策略表与一行数据）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.execute(_VERSION_1_GROUP_POLICY)
        connection.execute(
            "INSERT INTO group_policy (platform_id, self_id, group_id, context_enabled, "
            "notice_version, revision, updated_at) VALUES ('qq-local', '10001', '20001', 1, "
            "'notice-1', 7, 1)"
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()

    def test_version_1_is_uninitialized_before_the_first_write(self):
        self.make_version_1_database()
        # 降级为"无状态"而不是报错：版本不等即未初始化（schema 模块头记录的代价）。
        self.database.probe()
        with self.database.read() as connection:
            self.assertIsNone(connection)

    def test_first_write_adds_the_new_tables_and_keeps_the_old_rows(self):
        self.make_version_1_database()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO member_state (platform_id, self_id, group_id, member_id, updated_at) "
                "VALUES ('qq-local', '10001', '20001', '30001', 1)"
            )
        self.assertEqual(self.table_names(), EXPECTED_TABLES)
        with self.database.read() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            policy = connection.execute(
                "SELECT revision, context_enabled FROM group_policy WHERE group_id = '20001'"
            ).fetchone()
        self.assertEqual(version, schema.SCHEMA_VERSION)
        self.assertEqual((policy["revision"], policy["context_enabled"]), (7, 1))


class FailureTests(StorageDbTestCase):
    def test_garbage_file_marks_failed_and_stays_failed(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(StorageFailure):
            self.database.probe()
        self.assertTrue(self.database.failed)
        # 粘滞：后续所有读写都被拒绝，不会"下次也许就好了"。
        with self.assertRaises(StorageFailure):
            with self.database.read():
                pass
        with self.assertRaises(StorageFailure):
            with self.database.transaction():
                pass

    def test_future_schema_version_is_refused(self):
        connection = sqlite3.connect(str(self.path))
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        with self.assertRaises(StorageFailure):
            self.database.probe()

    def test_closed_database_refuses_operations(self):
        self.database.close()
        with self.assertRaises(StorageFailure):
            with self.database.read():
                pass
        with self.assertRaises(StorageFailure):
            with self.database.transaction():
                pass

    def test_unwritable_file_fails_closed(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO group_policy (platform_id, self_id, group_id, updated_at) "
                "VALUES ('qq-local', '10001', '20001', 1)"
            )
        os.chmod(self.path, 0o444)
        self.addCleanup(os.chmod, self.path, 0o644)
        readonly = Database(self.path)
        self.assertTrue(readonly.failed is False)
        with self.assertRaises(StorageFailure):
            with readonly.transaction() as connection:
                connection.execute(
                    "UPDATE group_policy SET updated_at = 2 WHERE group_id = '20001'"
                )
        self.assertTrue(readonly.failed)


class TransactionTests(StorageDbTestCase):
    def test_failed_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO group_policy (platform_id, self_id, group_id, updated_at) "
                    "VALUES ('qq-local', '10001', '20001', 1)"
                )
                raise RuntimeError("业务异常")
        with self.database.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM group_policy").fetchone()[0]
        self.assertEqual(count, 0)

    def test_transaction_closes_its_connection(self):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO group_policy (platform_id, self_id, group_id, updated_at) "
                "VALUES ('qq-local', '10001', '20001', 1)"
            )
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_read_closes_its_connection(self):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO group_policy (platform_id, self_id, group_id, updated_at) "
                "VALUES ('qq-local', '10001', '20001', 1)"
            )
        with self.database.read() as connection:
            self.assertIsNotNone(connection)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_schema_creation_is_idempotent(self):
        for _ in range(3):
            with self.database.transaction() as connection:
                connection.execute("SELECT 1")


class PathValidationTests(unittest.TestCase):
    def test_rejects_empty_path(self):
        for value in (Path(""), Path("/")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Database(value)


class StructuralTests(unittest.TestCase):
    def test_storage_modules_are_synchronous_and_framework_free(self):
        for name in ("__init__.py", "db.py", "schema.py", "groups.py", "members.py", "memories.py"):
            with self.subTest(module=name):
                tree = ast.parse((PLUGIN_ROOT / "storage" / name).read_text(encoding="utf-8"))
                imports = {
                    alias.name.split(".")[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                }
                imports |= {
                    (node.module or "").split(".")[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                }
                self.assertNotIn("asyncio", imports)
                self.assertNotIn("astrbot", imports)
                awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
                self.assertEqual(awaits, [])

    def test_gitignore_covers_sqlite_artifacts(self):
        patterns = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        for pattern in ("*.db", "*.db-journal", "*.db-wal", "*.db-shm"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, patterns)


if __name__ == "__main__":
    unittest.main()
