import unittest
from dataclasses import replace

from astrbot_plugin_chizuru.config import Settings
from astrbot_plugin_chizuru.policy import Classification, MessageFacts, classify


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "platform_id": "qq-local",
            "self_id": "10001",
            "allowed_group_ids": ["20001"],
        }
        self.settings = Settings.from_mapping(self.config)
        self.message = MessageFacts(
            "qq-local", "10001", "20001", "30001", True, ("10001",), "你好"
        )

    def test_direct_mention_is_only_a_candidate(self):
        self.assertEqual(classify(self.message, self.settings), Classification.TEXT_CANDIDATE)

    def test_missing_config_denies_all(self):
        for config in ({}, {**self.config, "allowed_group_ids": []}):
            with self.subTest(config=config):
                self.assertEqual(classify(self.message, Settings.from_mapping(config)), Classification.IGNORE)

    def test_malformed_config_fails_closed(self):
        for key, value in (
            ("platform_id", None), ("platform_id", " qq-local"),
            ("self_id", 10001), ("self_id", "１０００１"), ("self_id", "0"),
            ("allowed_group_ids", "20001"), ("allowed_group_ids", [20001]),
            ("allowed_group_ids", ["20001", ""]), ("allowed_group_ids", [True]),
        ):
            with self.subTest(key=key, value=value):
                self.assertEqual(Settings.from_mapping({**self.config, key: value}), Settings())

    def test_identity_and_scope_rejections(self):
        for fields in (
            {"platform_id": "other"}, {"self_id": "10002"},
            {"group_id": "20002"}, {"group_id": ""},
            {"sender_id": ""}, {"sender_id": "010"},
            {"sender_id": "10001"}, {"is_group": False},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(classify(replace(self.message, **fields), self.settings), Classification.IGNORE)

    def test_text_is_not_a_mention(self):
        for text in ("千鹤", "@千鹤 你好", "[CQ:at,qq=10001] 你好", "/help"):
            with self.subTest(text=text):
                message = replace(self.message, mention_targets=(), direct_text=text)
                self.assertEqual(classify(message, self.settings), Classification.IGNORE)

    def test_other_mentions_and_at_all(self):
        for targets in (("10002",), ("all",), ("all", "10001"), ("10001", "all")):
            with self.subTest(targets=targets):
                self.assertEqual(classify(replace(self.message, mention_targets=targets), self.settings), Classification.IGNORE)

    def test_empty_mention_does_not_enable_followup(self):
        for text in ("", " \n\t"):
            self.assertEqual(classify(replace(self.message, direct_text=text), self.settings), Classification.EMPTY_OR_UNSUPPORTED)
        self.assertEqual(classify(replace(self.message, mention_targets=()), self.settings), Classification.IGNORE)

    def test_member_keys_include_every_scope(self):
        key = self.message.member_key
        for field in ("platform_id", "self_id", "group_id", "sender_id"):
            with self.subTest(field=field):
                self.assertNotEqual(replace(self.message, **{field: "40001"}).member_key, key)
        self.assertNotIn("你好", repr(self.message))


if __name__ == "__main__":
    unittest.main()
