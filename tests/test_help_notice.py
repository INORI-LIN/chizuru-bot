"""S1-17 `帮助` 回执文案的离线测试：逐字副本、指纹、指令覆盖与模块纯度。"""

import ast
import hashlib
import unittest
from pathlib import Path

from astrbot_plugin_chizuru import commands
from astrbot_plugin_chizuru.help_notice import HELP_NOTICE_TEXT, HELP_NOTICE_VERSION

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "astrbot_plugin_chizuru"

# **批准来源的逐字副本**（2026-09-28 随批次 4c-离线定稿）：docs/03 附录 C.6。
# 改动任一侧都应让本测试失败；改文本必须同时递增 HELP_NOTICE_VERSION 与指纹。
HELP_BODY = """【使用说明】

我是一个参考《租借女友》水原千鹤设计的聊天机器人，不是真人，也不是官方账号，是仅在本群使用的非官方角色机器人。

我出现的方式很有限：
· 只有被 @ 时才会回复
· 你主动发送控制指令时，我会按权限执行

我目前只能读文字消息，还看不了图片、语音和文件。请把想说的内容用文字发给我。

【数据范围】
为了让对话能接上群里的话题，我会在内存中短暂保留本群近期的普通聊天文本（每群最多 30 条、不超过 10 分钟）。这些内容不写入数据库，服务重启即丢失。只有有人 @ 我时，我才会把必要的部分发送给 DeepSeek 处理，不会逐条分析，也不会给群成员做画像。

你可以随时 @ 我并发送「上下文 退出」，停止采集你自己的发言，并清除相关缓冲。退出后你仍然可以正常 @ 我聊天。

【长期记忆】
长期记忆默认关闭。除非你本人主动开启并确认，我不会记住任何关于你的事情。

是否允许我在本群记住一些关于你的事情？默认是关闭的，需要你确认后才会开启。

提取到的事实只用于我在本群回复你时让对话更连贯。提取在后台完成，我不会在群里主动说"我记住了"。

保存在本机数据库，最多 20 条，90 天后失效，到期即停止使用并清除。提取和回复时，必要内容会发送给 DeepSeek 处理。

只有你本人可以查看、纠正或删除自己的记录，别人问起我不会说。但请注意：你在本群查看记录时，内容会发在群里，其他成员可能看到。

随时 @ 我发送「记忆 关闭」或「记忆 删除全部」，即可撤回授权并清除已保存的记录。撤回后需要重新确认才能再次开启。确认开启请回复「记忆 确认开启」，5 分钟内有效。

【请注意】
请不要向我发送密码、证件、电话、住址、财务或健康信息。群消息会经由 DeepSeek 处理，而且已经发出的群消息其他成员都能看到，无法撤回。

【控制操作】
· 帮助（群成员）—— 说明能力、数据范围、DeepSeek 外部处理和控制操作
· 上下文 退出（本人）—— 停止采集本人的普通群聊，清除相关缓冲及受影响历史；不改变本人的长期记忆授权
· 上下文 加入（本人）—— 在本群已告知并开启的前提下恢复采集；不恢复旧材料
· 记忆 开启（本人）—— 展示授权说明；再次「记忆 确认开启」后启用；不送入 LLM
· 记忆 状态（本人）—— 查看当前授权、记录数与有效期，不展示他人信息
· 记忆 查看（本人）—— 在群中查看自己的低敏记忆列表；先提示这是群内可见操作，再次「记忆 查看 确认」后列出
· 记忆 纠正 <记录编号> <内容>（本人）—— 更新本人记录并使旧提取任务失效；不符合允许类型的内容不写入
· 记忆 删除 <记录编号>（本人）—— 删除指定记录及受影响衍生内容，避免旧任务复活
· 记忆 关闭 / 记忆 删除全部（本人）—— 均撤回授权、停止读写并清除已有记忆；重新开启需再次确认
· 群上下文 开启 / 群上下文 关闭（授权维护者）—— 完成告知后启用或停止普通群聊采集；群上下文 开启先展示告知全文，再次「群上下文 确认开启」后记录告知版本并启用；关闭时清空缓冲及受影响群历史
· 上下文 清空（授权维护者）—— 清空本群短期缓冲与互动历史，不自动删除已授权长期记忆
· 千鹤 暂停 / 千鹤 恢复（授权维护者）—— 暂停/恢复本群聊天；暂停时停止采集和自动抽取，清空普通缓冲，保留成员删除通道
· 千鹤 状态（授权维护者）—— 展示连接、聊天开关、队列/预算状态，不展示密钥、完整聊天或成员档案"""

# 规范串指纹：`名称=文本`（与 `test_fixed_notice.py` 同口径；单常量不再按名排序）。
CANONICAL = f"HELP_NOTICE_TEXT={HELP_BODY}"


class TextTests(unittest.TestCase):
    def test_text_is_verbatim_from_the_approved_copy(self):
        self.assertEqual(HELP_NOTICE_TEXT, HELP_BODY)

    def test_version_and_fingerprint_are_pinned(self):
        self.assertEqual(HELP_NOTICE_VERSION, "help-notice-1")
        self.assertEqual(
            hashlib.sha256(CANONICAL.encode("utf-8")).hexdigest(),
            "8d1b263efd38cc191a51ddc69cd075570acef8895b2833d7cbf0cf53c72aceb6",
        )

    def test_every_command_is_documented(self):
        """需求 §4.4：`帮助` 说明全部控制操作——与解析器清单逐字对齐，防两处漂移。"""
        for command in commands.COMMAND_TEXTS:
            with self.subTest(command=command):
                self.assertIn(command, HELP_NOTICE_TEXT)

    def test_required_disclosures_are_present(self):
        """需求 §4.4 的四个说明面：能力、数据范围、DeepSeek 外部处理、控制操作。"""
        for phrase in ("只有被 @ 时才会回复", "内存", "DeepSeek", "【控制操作】"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, HELP_NOTICE_TEXT)

    def test_help_text_leaks_nothing_sensitive(self):
        for word in ("http", "sk-", "token", "traceback", "ws_reverse"):
            with self.subTest(word=word):
                self.assertNotIn(word, HELP_NOTICE_TEXT)


class ModuleBoundaryTests(unittest.TestCase):
    def test_module_is_pure_logic(self):
        tree = ast.parse((PLUGIN_ROOT / "help_notice.py").read_text(encoding="utf-8"))
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
        for forbidden in ("asyncio", "astrbot", "sqlite3", "logging", "llm"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imports)
        awaits = [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
        self.assertEqual(awaits, [])


if __name__ == "__main__":
    unittest.main()
