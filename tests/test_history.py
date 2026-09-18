import json
import unittest

from astrbot_plugin_chizuru import history
from astrbot_plugin_chizuru.history import (
    AT_KEY,
    Turn,
    flatten,
    make_turn,
    parse,
    select,
    should_record,
    storage_entries,
    trim_stored,
)

NOW = 1_760_000_000


def pair(user_text="今天在学做菜", assistant_text="听起来不错", at=NOW):
    """构造存储形态的两条条目（测试多数断言直接面向存储 JSON）。"""
    return storage_entries([make_turn(user_text=user_text, assistant_text=assistant_text, at_epoch=at)])


class ParseTests(unittest.TestCase):
    def test_round_trip_from_stored_json(self):
        entries = list(pair()) + list(pair(user_text="第二条", at=NOW + 10))
        turns = parse(json.dumps(entries))
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].at, NOW)
        self.assertEqual(turns[0].user["content"], "今天在学做菜")
        self.assertEqual(turns[0].assistant["content"], "听起来不错")
        self.assertEqual(turns[1].at, NOW + 10)

    def test_malformed_input_is_dropped(self):
        for raw in (None, "", "   ", "not json", "{}", "[]", "123"):
            with self.subTest(raw=raw):
                self.assertEqual(parse(raw), ())

    def test_entries_without_timestamp_are_dropped(self):
        raw = json.dumps(
            [
                {"role": "user", "content": "没有时间戳"},
                {"role": "assistant", "content": "也不该出现"},
            ]
        )
        self.assertEqual(parse(raw), ())

    def test_dangling_and_mismatched_pairs_are_dropped(self):
        user, assistant = pair()
        cases = {
            "悬挂单条": [user],
            "只有 assistant": [assistant],
            "时间戳不一致": [user, {**assistant, AT_KEY: NOW + 1}],
            "插入外部条目": [user, {"role": "system", "content": "越权"}, assistant],
            "重复 user": [user, user, assistant],
            "空正文": [{**user, "content": " "}, assistant],
        }
        for name, entries in cases.items():
            with self.subTest(case=name):
                self.assertEqual(parse(json.dumps(entries)), ())

    def test_checkpoint_segments_are_ignored(self):
        user, assistant = pair()
        raw = json.dumps([user, assistant, {"role": "_checkpoint", "content": {"a": 1}}])
        turns = parse(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].assistant["content"], "听起来不错")


class SelectTests(unittest.TestCase):
    def test_ttl_is_enforced_with_strict_boundary(self):
        turns = parse(json.dumps(list(pair(at=NOW - 100)) + list(pair(at=NOW - 49))))
        kept = select(turns, max_turns=20, ttl_seconds=50, now_epoch=NOW)
        self.assertEqual([turn.at for turn in kept], [NOW - 49])

    def test_keeps_only_the_newest_turns(self):
        entries = []
        for index in range(25):
            entries.extend(pair(user_text=f"第 {index} 条", at=NOW + index))
        turns = parse(json.dumps(entries))
        kept = select(turns, max_turns=20, ttl_seconds=86_400, now_epoch=NOW + 24)
        self.assertEqual(len(kept), 20)
        self.assertEqual(kept[0].at, NOW + 5)
        self.assertEqual(kept[-1].at, NOW + 24)

    def test_future_timestamps_are_not_dropped(self):
        turns = parse(json.dumps(list(pair(at=NOW + 5))))
        self.assertEqual(len(select(turns, max_turns=20, ttl_seconds=10, now_epoch=NOW)), 1)

    def test_invalid_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            select((), max_turns=0, ttl_seconds=10, now_epoch=NOW)
        with self.assertRaises(ValueError):
            select((), max_turns=True, ttl_seconds=10, now_epoch=NOW)
        with self.assertRaises(ValueError):
            select((), max_turns=20, ttl_seconds=0, now_epoch=NOW)


class MakeTurnTests(unittest.TestCase):
    def test_turn_carries_the_same_timestamp(self):
        user, assistant = pair()
        self.assertEqual((user["role"], assistant["role"]), ("user", "assistant"))
        self.assertEqual(user[AT_KEY], assistant[AT_KEY])
        self.assertIsInstance(user[AT_KEY], int)

    def test_storage_entries_have_a_fixed_key_set(self):
        turn = make_turn(user_text="问", assistant_text="答", at_epoch=NOW)
        for entry in turn.entries():
            with self.subTest(entry=entry):
                self.assertEqual(set(entry), {"role", "content", AT_KEY})

    def test_parsed_turn_drops_unknown_fields_on_write_back(self):
        user, assistant = pair()
        turns = parse(json.dumps([{**user, "tool_calls": [1]}, {**assistant, "_no_save": True}]))
        entries = storage_entries(turns)
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertEqual(set(entry), {"role", "content", AT_KEY})

    def test_empty_texts_are_rejected(self):
        for kwargs in (
            {"user_text": "", "assistant_text": "x", "at_epoch": NOW},
            {"user_text": "x", "assistant_text": "  ", "at_epoch": NOW},
            {"user_text": "x", "assistant_text": "y", "at_epoch": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    make_turn(**kwargs)


class FlattenTests(unittest.TestCase):
    def test_private_keys_never_reach_contexts(self):
        turns = parse(json.dumps(list(pair())))
        contexts = flatten(turns)
        self.assertEqual(len(contexts), 2)
        for entry in contexts:
            with self.subTest(entry=entry):
                self.assertEqual(set(entry), {"role", "content"})
        self.assertEqual([entry["role"] for entry in contexts], ["user", "assistant"])

    def test_unknown_fields_are_stripped(self):
        user, assistant = pair()
        turns = parse(json.dumps([{**user, "tool_calls": [1]}, {**assistant, "_no_save": True}]))
        contexts = flatten(turns)
        self.assertEqual([set(entry) for entry in contexts], [{"role", "content"}] * 2)

    def test_order_is_chronological(self):
        entries = list(pair(user_text="第一问", assistant_text="第一答", at=NOW))
        entries += list(pair(user_text="第二问", assistant_text="第二答", at=NOW + 1))
        contexts = flatten(parse(json.dumps(entries)))
        self.assertEqual([entry["content"] for entry in contexts], ["第一问", "第一答", "第二问", "第二答"])


class TrimStoredTests(unittest.TestCase):
    """S2-08：启动裁剪的写回计划——只动 `_at` 成对条目，其余原样保留。"""

    def trim(self, entries, *, max_turns=20, ttl_seconds=86_400, now_epoch=NOW):
        return trim_stored(
            json.dumps(entries),
            max_turns=max_turns,
            ttl_seconds=ttl_seconds,
            now_epoch=now_epoch,
        )

    def test_expired_turns_are_dropped_and_fresh_ones_kept(self):
        entries = list(pair(user_text="过期", at=NOW - 90_000)) + list(pair(user_text="新鲜", at=NOW - 60))
        trimmed = self.trim(entries)
        self.assertIsNotNone(trimmed)
        assert trimmed is not None
        self.assertEqual([entry["content"] for entry in trimmed], ["新鲜", "听起来不错"])

    def test_boundary_is_strict(self):
        # 恰好到期即过期：整轮被裁掉是**真实改动**（写回空列表），不是"无需改动"。
        entries = list(pair(at=NOW - 86_400))
        self.assertEqual(self.trim(entries), ())

    def test_over_limit_pairs_are_dropped_oldest_first(self):
        entries = []
        for index in range(5):
            entries.extend(pair(user_text=f"第 {index} 问", at=NOW + index))
        trimmed = self.trim(entries, max_turns=2)
        assert trimmed is not None
        self.assertEqual(
            [entry["content"] for entry in trimmed],
            ["第 3 问", "听起来不错", "第 4 问", "听起来不错"],
        )

    def test_unrecognised_entries_are_preserved(self):
        entries = [
            {"role": "_checkpoint", "content": {"a": 1}},
            *pair(user_text="过期", at=NOW - 90_000),
            {"role": "user", "content": "没有时间戳的历史"},
            *pair(user_text="新鲜", at=NOW - 60),
        ]
        trimmed = self.trim(entries)
        assert trimmed is not None
        self.assertEqual(
            trimmed,
            (
                {"role": "_checkpoint", "content": {"a": 1}},
                {"role": "user", "content": "没有时间戳的历史"},
                {"role": "user", "content": "新鲜", AT_KEY: NOW - 60},
                {"role": "assistant", "content": "听起来不错", AT_KEY: NOW - 60},
            ),
        )

    def test_no_change_means_no_write(self):
        for entries in (
            [],
            [{"role": "_checkpoint", "content": {}}],
            [{"role": "user", "content": "框架原生的历史"}],
            list(pair(at=NOW - 60)),
        ):
            with self.subTest(entries=entries):
                self.assertIsNone(self.trim(entries))

    def test_malformed_input_is_not_a_write_plan(self):
        for raw in (None, "", "   ", "not json", "{}", "[1, 2]"):
            with self.subTest(raw=raw):
                self.assertIsNone(
                    trim_stored(raw, max_turns=20, ttl_seconds=86_400, now_epoch=NOW)
                )

    def test_invalid_window_is_rejected(self):
        for kwargs in ({"max_turns": 0}, {"max_turns": True}, {"ttl_seconds": 0}, {"ttl_seconds": -1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    trim_stored(
                        json.dumps(list(pair(at=NOW))),
                        max_turns=kwargs.get("max_turns", 20),
                        ttl_seconds=kwargs.get("ttl_seconds", 86_400),
                        now_epoch=NOW,
                    )


class ShouldRecordTests(unittest.TestCase):
    def test_only_delivered_non_memory_rounds_are_recorded(self):
        self.assertTrue(should_record(memory_assisted=False, delivered=True))
        self.assertFalse(should_record(memory_assisted=True, delivered=True))
        self.assertFalse(should_record(memory_assisted=False, delivered=False))
        self.assertFalse(should_record(memory_assisted=True, delivered=False))


class ModuleBoundaryTests(unittest.TestCase):
    def test_module_is_pure_logic(self):
        import ast
        from pathlib import Path

        source = Path(history.__file__).read_text(encoding="utf-8")
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertEqual(imported, ["__future__", "json", "dataclasses", "typing"])
        for forbidden in ("astrbot", "asyncio", "sqlite3", "time", "logging"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
