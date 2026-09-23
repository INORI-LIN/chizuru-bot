"""S4-03 待维护错误登记离线测试：跨重启保留、按群隔离、成功清理后清除（R23）。"""

import ast
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey
from astrbot_plugin_chizuru.storage import (
    Database,
    MaintenanceStore,
    StorageFailure,
    open_storage,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "astrbot_plugin_chizuru"
WORK_ROOT = ROOT / ".runtime"
INSTANCE = BotInstanceKey("qq-local", "10001")


class EpochClock:
    def __init__(self, now: int = 1_700_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


class MaintenanceStoreTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="storage-maintenance-", dir=WORK_ROOT)
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.db_path = self.dir / "chizuru.db"
        self.clock = EpochClock()
        self.storage = open_storage(self.db_path, clock=self.clock)
        self.addCleanup(self.storage.close)

    def group(self, group_id: str = "20001", self_id: str = "10001") -> GroupKey:
        return GroupKey(BotInstanceKey(INSTANCE.platform_id, self_id), group_id)


class RecordTests(MaintenanceStoreTestCase):
    def test_absent_registry_reads_as_zero_without_creating_the_file(self):
        self.assertFalse(self.storage.maintenance.failures(self.group()).exists)
        self.assertEqual(self.storage.maintenance.total(), 0)
        # 只读不建库：延迟建库的口径与其它仓储一致。
        self.assertFalse(self.db_path.exists())

    def test_repeated_failures_accumulate_and_keep_the_first_timestamp(self):
        first_at = self.clock.now
        self.assertEqual(self.storage.maintenance.record_failure(self.group()), 1)
        self.clock.advance(60)
        self.assertEqual(self.storage.maintenance.record_failure(self.group()), 2)

        pending = self.storage.maintenance.failures(self.group())
        self.assertTrue(pending.exists)
        self.assertEqual(pending.failures, 2)
        self.assertEqual(pending.first_at, first_at)
        self.assertEqual(pending.last_at, first_at + 60)
        self.assertEqual(self.storage.maintenance.total(), 2)

    def test_groups_and_instances_are_isolated(self):
        self.storage.maintenance.record_failure(self.group("20001"))
        self.storage.maintenance.record_failure(self.group("20001"))
        self.storage.maintenance.record_failure(self.group("20002"))

        self.assertEqual(self.storage.maintenance.failures(self.group("20001")).failures, 2)
        self.assertEqual(self.storage.maintenance.failures(self.group("20002")).failures, 1)
        self.assertEqual(self.storage.maintenance.failures(self.group("20003")).failures, 0)
        # self_id 也是键的一部分：另一个机器人实例的登记不会串进来。
        self.assertEqual(self.storage.maintenance.failures(self.group("20001", "10002")).failures, 0)
        self.assertEqual(self.storage.maintenance.total(), 3)

    def test_registry_survives_a_reopen(self):
        """R23 的核心：登记跨重启保留，不再归零。"""
        self.storage.maintenance.record_failure(self.group())
        self.storage.close()

        reopened = open_storage(self.db_path, clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.maintenance.failures(self.group()).failures, 1)
        self.assertEqual(reopened.maintenance.total(), 1)


class ClearTests(MaintenanceStoreTestCase):
    def test_clear_removes_only_that_group_and_reports_removal(self):
        self.storage.maintenance.record_failure(self.group("20001"))
        self.storage.maintenance.record_failure(self.group("20002"))

        self.assertTrue(self.storage.maintenance.clear_group(self.group("20001")))
        self.assertFalse(self.storage.maintenance.failures(self.group("20001")).exists)
        self.assertEqual(self.storage.maintenance.failures(self.group("20002")).failures, 1)
        self.assertEqual(self.storage.maintenance.total(), 1)

    def test_clear_is_idempotent_and_does_not_write_when_there_is_nothing_to_clear(self):
        self.assertFalse(self.storage.maintenance.clear_group(self.group()))
        # 没有登记时不该因为一次成功清理而创建（或改写）库文件。
        self.assertFalse(self.db_path.exists())

        self.storage.maintenance.record_failure(self.group())
        self.storage.close()
        before = self.db_path.read_bytes()
        reopened = open_storage(self.db_path, clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.maintenance.clear_group(self.group("20002")))
        self.assertEqual(self.db_path.read_bytes(), before)


class FailureTests(MaintenanceStoreTestCase):
    def test_empty_file_is_uninitialized_and_a_write_builds_the_table(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.touch()
        self.assertFalse(self.storage.maintenance.failures(self.group()).exists)
        self.storage.maintenance.record_failure(self.group())
        self.assertEqual(self.storage.maintenance.total(), 1)

    def test_broken_database_refuses_writes_and_reads(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(b"this is not a sqlite database")
        database = Database(self.db_path)
        store = MaintenanceStore(database, clock=self.clock)
        with self.assertRaises(StorageFailure):
            store.record_failure(self.group())
        # 失败粘滞：读侧也不返回"看起来已恢复"的零。
        self.assertTrue(database.failed)
        with self.assertRaises(StorageFailure):
            store.total()


class StructuralTests(unittest.TestCase):
    def test_table_has_no_content_columns(self):
        """登记表只有计数与时间：不存正文、路径或错误文本。"""
        source = (PLUGIN_ROOT / "storage" / "schema.py").read_text(encoding="utf-8")
        start = source.index("CREATE_CLEANUP_FAILURE")
        end = source.index("_TABLES", start)
        block = source[start:end]
        for forbidden in ("content", "text", "message", "path", "error", "traceback", "detail"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, block)

    def test_module_is_synchronous_and_framework_free(self):
        tree = ast.parse((PLUGIN_ROOT / "storage" / "maintenance.py").read_text(encoding="utf-8"))
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
        for forbidden in ("asyncio", "astrbot", "logging"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imports)
        awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
        self.assertEqual(awaits, [])


if __name__ == "__main__":
    unittest.main()
