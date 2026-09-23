import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".runtime" / "astrbot"
PLUGIN_PATH = ROOT / "astrbot_plugin_chizuru"
MODULE_NAME = "data.plugins.astrbot_plugin_chizuru.main"
PACKAGE_NAME = MODULE_NAME.rpartition(".")[0]
_ALLOWED_SQLITE_ROOT = (ROOT / ".runtime").resolve()
_guard_enabled = False
_violations = []


def _audit(event, args):
    if not _guard_enabled:
        return
    if event == "sqlite3.connect":
        # K12：只放行 .runtime 下的临时库，其他路径仍硬拒绝——守卫是收窄而非移除。
        try:
            target = Path(str(args[0])).resolve() if args else None
            allowed = target is not None and target.is_relative_to(_ALLOWED_SQLITE_ROOT)
        except (OSError, ValueError):
            allowed = False
        if not allowed:
            _violations.append(event)
            raise AssertionError(f"Offline test forbids {event} outside {_ALLOWED_SQLITE_ROOT}")
        return
    blocked = event in {
        "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork",
        "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto",
    }
    if event == "import" and args[0].split(".")[0] == "pip":
        blocked = True
    if blocked:
        _violations.append(event)
        raise AssertionError(f"Offline test forbids {event}")


sys.addaudithook(_audit)


def setUpModule():
    global _guard_enabled, resources, plugin_module, core, metadata, handlers
    global AstrBotConfig, AstrMessageEvent, AstrBotMessage, MessageMember, MessageType
    global PlatformMetadata, At, AtAll, File, Image, Plain, Record, Reply, Video
    global call_handler, test_root
    resources = ExitStack()
    unittest.addModuleCleanup(resources.close)
    test_root = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="offline-", dir=ROOT / ".runtime")))
    resources.enter_context(patch.dict(os.environ, {"ASTRBOT_ROOT": str(test_root)}))
    resources.enter_context(patch.object(sys, "path", [str(SOURCE), *sys.path]))
    _guard_enabled = True
    unittest.addModuleCleanup(_finish_guard)

    # AstrBot 的导入会启动 SharedPreferences 定时器；仅替换这一外部调度边界。
    from apscheduler.schedulers.background import BackgroundScheduler
    resources.enter_context(patch.object(BackgroundScheduler, "start"))
    core = importlib.import_module("astrbot.core")
    resources.enter_context(patch.object(core.pip_installer, "install", new=AsyncMock(side_effect=AssertionError("pip installer forbidden"))))
    core.astrbot_config["trace_enable"] = False
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import At, AtAll, File, Image, Plain, Record, Reply, Video
    from astrbot.core.pipeline.context_utils import call_handler
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.star.star_manager import PluginManager

    plugin_module = importlib.import_module(MODULE_NAME)
    unittest.addModuleCleanup(_unregister_plugin)
    metadata = PluginManager._load_plugin_metadata(str(PLUGIN_PATH))
    registered = star_map[MODULE_NAME]
    registered.name = metadata.name
    registered.activated = True
    handlers = star_handlers_registry.get_handlers_by_module_name(MODULE_NAME)


def _unregister_plugin():
    from astrbot.core.star.star import star_map, star_registry
    from astrbot.core.star.star_handler import star_handlers_registry
    for handler in star_handlers_registry.get_handlers_by_module_name(MODULE_NAME):
        star_handlers_registry.remove(handler)
    metadata = star_map.pop(MODULE_NAME, None)
    if metadata in star_registry:
        star_registry.remove(metadata)
    for name in list(sys.modules):
        if name == MODULE_NAME.rpartition(".")[0] or name.startswith(MODULE_NAME.rpartition(".")[0] + "."):
            sys.modules.pop(name)
    assert not star_handlers_registry.get_handlers_by_module_name(MODULE_NAME)
    assert MODULE_NAME not in star_map


def _finish_guard():
    global _guard_enabled
    _guard_enabled = False
    if _violations:
        raise AssertionError(f"Blocked operations occurred: {_violations}")


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        schema = json.loads((PLUGIN_PATH / "_conf_schema.json").read_text())
        config = AstrBotConfig(str(test_root / "plugin-config.json"), schema=schema)
        config.update(platform_id="qq-local", self_id="10001", allowed_group_ids=["20001"])
        self.context = Mock(spec=[])
        self.plugin = plugin_module.ChizuruPlugin(self.context, config)
        self.tasks_before = set(asyncio.all_tasks())
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.assertEqual(set(asyncio.all_tasks()) - self.tasks_before, {asyncio.current_task()})
        self.assertEqual(self.context.mock_calls, [])
        self.assertFalse(core.db_helper.inited)
        self.assertFalse(list(test_root.rglob("*.db")))
        core.pip_installer.install.assert_not_called()

    def event(self, chain, *, platform="aiocqhttp", platform_id="qq-local", self_id="10001", group_id="20001", sender_id="30001", nickname="同名", private=False, message_id="event-1"):
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
        message.self_id = self_id
        message.group_id = group_id
        message.sender = MessageMember(user_id=sender_id, nickname=nickname)
        message.message = chain
        message.message_str = "伪造的拼接文本不应成为直接输入"
        message.message_id = message_id
        message.raw_message = {}
        event = AstrMessageEvent(message.message_str, message, PlatformMetadata(platform, "offline", platform_id), group_id)
        event.send = AsyncMock(side_effect=AssertionError("send forbidden"))
        return event

    def test_metadata_and_schema(self):
        self.assertEqual(metadata.name, "astrbot_plugin_chizuru")
        self.assertEqual(metadata.support_platforms, ["aiocqhttp"])
        self.assertEqual(metadata.astrbot_version, "==4.28.1")
        self.assertIsNone(metadata.repo)
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].extras_configs["priority"], 1000)
        schema = json.loads((PLUGIN_PATH / "_conf_schema.json").read_text())
        defaults = AstrBotConfig(str(test_root / "defaults.json"), schema=schema)
        # 从插件包内取 config 子模块：另起一条 import 路径会造出第二个模块实例，
        # 使 dataclass 相等判断失效。
        plugin_config = importlib.import_module(f"{PACKAGE_NAME}.config")
        Settings, default_fields = plugin_config.Settings, plugin_config.default_fields
        # schema 与 config.py 必须一一对应：多了是无人消费的假配置，少了是隐藏默认。
        self.assertEqual(set(defaults), set(default_fields()))
        # schema 默认值必须整体落回"拒绝全部"哨兵，而不是一条看似可用的配置。
        self.assertEqual(Settings.from_mapping(dict(defaults)), Settings())
        self.assertIs(defaults["memory_extraction_enabled"], False)
        self.assertIs(defaults["require_budget_for_extraction"], True)
        self.assertEqual(defaults["daily_budget_amount"], 0)
        self.assertEqual(defaults["monthly_budget_amount"], 0)
        plugin = plugin_module.ChizuruPlugin(self.context, defaults)
        self.assertEqual(plugin.classify_event(self.event([At(qq="10001"), Plain("hello")])), "ignore")
        self.assertEqual((SOURCE / "data/plugins/astrbot_plugin_chizuru").resolve(), PLUGIN_PATH)
        self.assertFalse((PLUGIN_PATH / "requirements.txt").exists())

    async def test_real_components_and_no_output(self):
        """无有效 @、空 @、@全体、引用内历史 @ 与命令一律静默（需求 §4.1）。"""
        cases = [
            ([Plain("@千鹤 /help [CQ:at,qq=10001]")], "ignore"),
            ([At(qq="10002"), Plain("hello")], "ignore"),
            ([AtAll(), At(qq="10001"), Plain("hello")], "ignore"),
            ([At(qq="10001"), At(qq="all"), Plain("hello")], "ignore"),
            ([Reply(id="old", chain=[At(qq="10001"), Plain("old")]), Plain("hello")], "ignore"),
            ([At(qq="10001"), Reply(id="old", chain=[Plain("old")])], "empty_or_unsupported"),
            ([At(qq="10001")], "empty_or_unsupported"),
            ([At(qq="10001"), Plain(" \n")], "empty_or_unsupported"),
        ]
        for chain, expected in cases:
            with self.subTest(expected=expected, types=[type(part).__name__ for part in chain]):
                event = self.event(chain)
                self.assertEqual(self.plugin.classify_event(event), expected)
                result = [item async for item in call_handler(event, self.plugin.on_message)]
                self.assertTrue(event.is_stopped())
                self.assertIsNone(event.get_result())
                self.assertEqual(result, [None])
                self.assertEqual(event.get_extra("chizuru.classification"), expected)
                event.send.assert_not_called()
        followup = self.event([Plain("hello")])
        await self.plugin.on_message(followup)
        self.assertEqual(followup.get_extra("chizuru.classification"), "ignore")

    async def test_mention_cases_leave_the_group_output_to_the_plugin(self):
        """真实组件下的两条出站档：附件回能力提示；纯文本因未装提供商回暂不可用提示。

        两者都**只**经装配层的固定提示路径：不解析附件、不调用模型、结果交回框架为空。
        """
        cases = [
            ([At(qq=10001), Plain("hello")], "text_candidate", "现在我暂时不可用，请稍后再试，或联系本群维护者。"),
            ([At(qq="10001"), Image(file="unused.png")], "unsupported_attachment", "我目前只能读文字消息，还看不了图片、语音和文件。请把想说的内容用文字发给我。"),
            ([At(qq="10001"), Record(file="unused.amr")], "unsupported_attachment", "我目前只能读文字消息，还看不了图片、语音和文件。请把想说的内容用文字发给我。"),
            ([At(qq="10001"), Video(file="unused.mp4")], "unsupported_attachment", "我目前只能读文字消息，还看不了图片、语音和文件。请把想说的内容用文字发给我。"),
            ([At(qq="10001"), File(name="unused.pdf")], "unsupported_attachment", "我目前只能读文字消息，还看不了图片、语音和文件。请把想说的内容用文字发给我。"),
            ([At(qq="10001"), Plain("   "), Image(file="unused.png")], "unsupported_attachment", "我目前只能读文字消息，还看不了图片、语音和文件。请把想说的内容用文字发给我。"),
        ]
        for index, (chain, expected, text) in enumerate(cases):
            with self.subTest(expected=expected, types=[type(part).__name__ for part in chain]):
                event = self.event(chain, message_id=f"case-{index}")
                self.assertEqual(self.plugin.classify_event(event), expected)
                result = [item async for item in call_handler(event, self.plugin.on_message)]
                self.assertTrue(event.is_stopped())
                self.assertIsNone(event.get_result())
                self.assertEqual(result, [None])
                self.assertEqual(event.get_extra("chizuru.classification"), expected)
                self.assertEqual(event.send.await_count, 1)
                sent = event.send.await_args.args[0]
                self.assertEqual(
                    "".join(part.text for part in sent.chain if hasattr(part, "text")), text
                )

    async def test_identity_rejections_and_other_platform(self):
        chain = [At(qq="10001"), Plain("hello")]
        for fields in ({"private": True}, {"sender_id": "10001"}, {"sender_id": ""}, {"platform_id": "other"}, {"self_id": "10002"}, {"group_id": "20002"}):
            with self.subTest(fields=fields):
                event = self.event(chain, **fields)
                await self.plugin.on_message(event)
                self.assertTrue(event.is_stopped())
                self.assertEqual(event.get_extra("chizuru.classification"), "ignore")
                event.send.assert_not_called()
        other = self.event(chain, platform="telegram")
        await self.plugin.on_message(other)
        self.assertFalse(other.is_stopped())
        self.assertIsNone(other.get_result())

    def test_nickname_does_not_control_identity(self):
        for nickname in ("同名", "system", "新昵称"):
            event = self.event([At(qq="10001"), Plain("hello")], nickname=nickname)
            self.assertEqual(self.plugin.classify_event(event), "text_candidate")

    def test_environment_matches_recorded_baseline(self):
        import hashlib
        from importlib.metadata import version

        self.assertEqual(version("astrbot"), "4.28.1")
        self.assertEqual(sys.version_info[:3], (3, 12, 9))
        self.assertEqual(Path(sys.prefix).resolve(), (SOURCE / ".venv").resolve())
        self.assertEqual(
            (ROOT / "environment/uv.lock").read_bytes(),
            (SOURCE / "uv.lock").read_bytes(),
        )
        self.assertEqual(
            hashlib.sha256((SOURCE / "pyproject.toml").read_bytes()).hexdigest(),
            "6d1adff107d35eb6f2b05da0a109cefde40928d312449f468d3b43e1ab3edae4",
        )

    def test_registered_filters_do_not_wake_other_platforms(self):
        handler = handlers[0]
        for platform, expected in (("aiocqhttp", True), ("telegram", False)):
            event = self.event([Plain("ordinary message")], platform=platform)
            passed = all(f.filter(event, core.astrbot_config) for f in handler.event_filters)
            self.assertEqual(passed, expected)

    async def test_registered_handler_invocation(self):
        event = self.event([At(qq="10001"), Plain("hello")])
        # 与 loader 一样绑定实际注册函数；不导入会启动额外设施的完整 ProcessStage。
        bound = handlers[0].handler.__get__(self.plugin)
        results = [item async for item in call_handler(event, bound)]
        self.assertEqual(results, [None])
        self.assertTrue(event.is_stopped())
        self.assertIsNone(event.get_result())
        # 未装提供商 → 固定提示一次（S4-01）；结果仍交回框架为空，不产生第二条默认链路回复。
        self.assertEqual(event.send.await_count, 1)

    async def test_invalid_live_configuration_still_stops(self):
        self.plugin.config["allowed_group_ids"] = [True]
        event = self.event([At(qq="10001"), Plain("hello")])
        await self.plugin.on_message(event)
        self.assertEqual(event.get_extra("chizuru.classification"), "ignore")
        self.assertTrue(event.is_stopped())
        event.send.assert_not_called()

    def test_sqlite_guard_is_scoped_not_removed(self):
        """K12：守卫只放行 .runtime 下的临时库，其他路径仍然硬拒绝。"""
        import shutil
        import sqlite3

        outside = Path(tempfile.gettempdir()) / "chizuru-guard-probe.db"
        before = len(_violations)
        with self.assertRaises(AssertionError):
            sqlite3.connect(str(outside))
        # 这次拒绝是本用例故意触发的；从模块级汇总里移除，避免污染 _finish_guard。
        del _violations[before:]
        self.assertFalse(outside.exists())

        # 放在 test_root 之外：test_root 另有"不得出现任何 .db"的不变量。
        scratch = Path(tempfile.mkdtemp(prefix="guard-", dir=ROOT / ".runtime"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        inside = scratch / "guard-probe.db"
        connection = sqlite3.connect(str(inside))
        connection.close()
        self.assertTrue(inside.exists())


if __name__ == "__main__":
    unittest.main()
