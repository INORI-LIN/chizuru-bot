"""S1-14/S1-15 装配级离线测试：假提供商 + 假时钟，不触网、不建后台任务。

覆盖 A01—A04、A15—A17 的**离线部分**。真实传输（真实 401/402/429、重连回放、计费）
仍归 S4-06；S1-16 阶段门未通过。

测试引导与 `tests/test_plugin.py` 同构：真实 AstrBot 模块 + 隔离 `ASTRBOT_ROOT` +
作用域守卫（K12）。`event.send` 一律替换为记录器——真实实现会建 Metric 上传任务并联网。
"""

import asyncio
import ast
import importlib
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".runtime" / "astrbot"
PLUGIN_PATH = ROOT / "astrbot_plugin_chizuru"
MODULE_NAME = "astrbot_plugin_chizuru.main"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402（tests/ 先就位）

test_root = None
guard = None
factory = None
plugin_module = None
_config_seq = 0


def setUpModule():
    global test_root, guard, factory, plugin_module
    resources = ExitStack()
    unittest.addModuleCleanup(resources.close)
    test_root = Path(
        resources.enter_context(
            tempfile.TemporaryDirectory(prefix="offline-assembly-", dir=ROOT / ".runtime")
        )
    )
    resources.enter_context(patch.dict(os.environ, {"ASTRBOT_ROOT": str(test_root)}))
    resources.enter_context(patch.object(sys, "path", [str(SOURCE), *sys.path]))
    guard = fakes.install_scope_guard(allow_dir=ROOT / ".runtime")

    # AstrBot 的导入会启动 SharedPreferences 定时器；仅替换这一外部调度边界。
    from apscheduler.schedulers.background import BackgroundScheduler

    resources.enter_context(patch.object(BackgroundScheduler, "start"))
    core = importlib.import_module("astrbot.core")
    resources.enter_context(
        patch.object(
            core.pip_installer,
            "install",
            new=AsyncMock(side_effect=AssertionError("pip installer forbidden")),
        )
    )
    core.astrbot_config["trace_enable"] = False
    plugin_module = importlib.import_module(MODULE_NAME)
    factory = fakes.EventFactory()
    unittest.addModuleCleanup(_unregister_plugin)
    unittest.addModuleCleanup(_finish_guard)


def _unregister_plugin():
    """把本模块注册进全局登记表的内容收回，避免影响同进程的其他测试模块。"""
    from astrbot.core.star.star import star_map, star_registry
    from astrbot.core.star.star_handler import star_handlers_registry

    for handler in star_handlers_registry.get_handlers_by_module_name(MODULE_NAME):
        star_handlers_registry.remove(handler)
    metadata = star_map.pop(MODULE_NAME, None)
    if metadata in star_registry:
        star_registry.remove(metadata)
    for name in list(sys.modules):
        if name == MODULE_NAME or name.startswith("astrbot_plugin_chizuru."):
            sys.modules.pop(name)


def _finish_guard():
    guard.enabled = False
    if guard.violations:
        raise AssertionError(f"离线测试出现被拦截的操作：{guard.violations}")


def make_config(**overrides):
    global _config_seq
    _config_seq += 1
    values = {
        "platform_id": "qq-local",
        "self_id": "10001",
        "allowed_group_ids": ["20001"],
        "group_maintainers": {"20001": ["30001"]},
    }
    values.update(overrides)
    return fakes.make_config(test_root / f"config-{_config_seq}.json", **values)


def text_of(message):
    return "".join(part.text for part in message.chain if hasattr(part, "text"))


class BlockingProvider(fakes.FakeProvider):
    """挂起第一次调用，用于构造"同群并发 1 + 聊天队列已满"的场景。"""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def text_chat(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().text_chat(**kwargs)


class AssemblyTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = fakes.FakeClock()
        self.plugin = None
        self.sent = []
        self.tasks_before = set(asyncio.all_tasks())

    async def asyncTearDown(self):
        if self.plugin is not None:
            await self.plugin.terminate()
        self.assertEqual(set(asyncio.all_tasks()) - self.tasks_before, {asyncio.current_task()})

    async def start(
        self,
        *,
        provider=None,
        platform=None,
        provider_error=None,
        platform_error=None,
        config=None,
        storage_path=None,
        history_cleaner=None,
    ):
        self.context = fakes.FakeContext(
            provider=provider,
            platform=platform,
            provider_error=provider_error,
            platform_error=platform_error,
        )
        self.plugin = fakes.make_plugin(self.context, config or make_config(), clock=self.clock)
        if storage_path is not None:
            self.plugin.storage_path = storage_path
        if history_cleaner is not None:
            self.plugin.history_cleaner = history_cleaner
        await self.plugin.initialize()
        return self.plugin

    def new_storage_path(self, name: str = "chizuru.db") -> Path:
        """每次调用给出独立的临时库路径，避免用例之间互相污染持久状态。"""
        directory = Path(tempfile.mkdtemp(prefix="storage-", dir=test_root))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory / name

    def group_key(self, group_id: str = "20001"):
        return plugin_module.GroupKey(
            plugin_module.BotInstanceKey("qq-local", "10001"),
            group_id,
        )

    def open_group(self, *, notice: str = "notice-1", group_id: str = "20001"):
        """直接写策略仓储模拟 S2-02 完成告知；命令面仍由 S2-02/S2-05 交付。"""
        return self.plugin.services.storage.groups.record_notice_confirmed(
            self.group_key(group_id),
            version=notice,
            actor_id="30001",
        )

    def collected(self, group_id: str = "20001"):
        return self.plugin.services.context_buffer.entries(self.group_key(group_id))

    def plain(self, text="今天天气不错", **overrides):
        return self.event([factory.Plain(text)], **overrides)

    def event(self, chain, **overrides):
        event = factory.event(chain, **overrides)
        self.sent = fakes.attach_send_sink(event)
        return event

    def mention(self, text="你好", **overrides):
        return self.event([factory.At(qq="10001"), factory.Plain(text)], **overrides)


class ZeroOutboundTests(AssemblyTestCase):
    """需求 §4.1：无有效 @ 时不聊天、不采集、不发提示（A02/A03）。"""

    async def test_non_trigger_shapes_produce_no_output(self):
        provider = fakes.FakeProvider()
        await self.start(provider=provider)
        cases = [
            ("普通群聊", [factory.Plain("今天天气不错")], {}),
            ("私聊", [factory.At(qq="10001"), factory.Plain("你好")], {"private": True}),
            ("非允许群", [factory.At(qq="10001"), factory.Plain("你好")], {"group_id": "20002"}),
            ("机器人自身消息", [factory.At(qq="10001"), factory.Plain("你好")], {"sender_id": "10001"}),
            ("@他人", [factory.At(qq="10002"), factory.Plain("你好")], {}),
            ("@全体", [factory.AtAll(), factory.At(qq="10001"), factory.Plain("你好")], {}),
            ("引用内历史 @", [factory.Reply(id="old", chain=[factory.At(qq="10001"), factory.Plain("旧")]), factory.Plain("在吗")], {}),
            ("空 @", [factory.At(qq="10001")], {}),
            ("@ 后仅附件", [factory.At(qq="10001"), factory.Image(file="unused.png")], {}),
            ("错平台实例", [factory.At(qq="10001"), factory.Plain("你好")], {"platform_id": "other"}),
        ]
        for name, chain, overrides in cases:
            with self.subTest(case=name):
                event = self.event(chain, **overrides)
                await self.plugin.on_message(event)
                self.assertEqual(self.sent, [])
                self.assertTrue(event.is_stopped())
                self.assertEqual(provider.calls, [])
                self.assertIn(
                    event.get_extra("chizuru.classification"),
                    {"ignore", "empty_or_unsupported", "unsupported_attachment"},
                )

    async def test_other_platform_is_left_alone(self):
        await self.start(provider=fakes.FakeProvider())
        event = self.event([factory.Plain("hi")], platform="telegram")
        await self.plugin.on_message(event)
        self.assertFalse(event.is_stopped())
        self.assertIsNone(event.get_result())
        self.assertEqual(self.sent, [])

    async def test_missing_message_id_is_dropped(self):
        provider = fakes.FakeProvider()
        await self.start(provider=provider)
        for value in ("", "  ", None, 12345):
            with self.subTest(message_id=value):
                event = self.mention(message_id=value)
                await self.plugin.on_message(event)
                self.assertEqual(self.sent, [])
        self.assertEqual(provider.calls, [])

    async def test_before_initialize_is_silent(self):
        self.context = fakes.FakeContext(provider=fakes.FakeProvider())
        self.plugin = fakes.make_plugin(self.context, make_config(), clock=self.clock)
        event = self.mention()
        await self.plugin.on_message(event)
        self.assertEqual(self.sent, [])


class ChatFlowTests(AssemblyTestCase):
    """一次 @ = 一次调用 = 至多一条出站（A01 假提供商、A16 错误注入）。"""

    async def test_single_call_and_layered_prompt(self):
        provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="嗯，我在。")])
        await self.start(provider=provider)
        event = self.mention("今天有点累")
        await self.plugin.on_message(event)

        self.assertEqual(len(provider.calls), 1)
        kwargs = provider.calls[0]
        # prompt 只来自顶层 Plain，message_str 是伪造串却从未被使用。
        self.assertEqual(kwargs["prompt"], "今天有点累")
        self.assertEqual(kwargs["system_prompt"], plugin_module.context_assembly.STATIC_RULES)
        self.assertEqual(kwargs["request_max_retries"], 1)
        self.assertNotIn("func_tool", kwargs)
        self.assertNotIn("tool_choice", kwargs)

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(text_of(self.sent[0]), "嗯，我在。")
        self.assertTrue(event.is_stopped())
        self.assertIsNone(event.get_result())

    async def test_provider_unavailable_releases_the_event(self):
        await self.start(provider=None)
        first = self.mention("你好")
        await self.plugin.on_message(first)
        self.assertEqual(self.sent, [])
        self.assertIn(
            plugin_module.health.Degradation.PROVIDER_DISABLED,
            self.plugin.services.health.degradations(),
        )

        # 释放后可重来：同一 message_id 不会因"曾经失败"被永久判为已处理。
        self.context._provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="回复")])
        second = self.mention("你好")
        await self.plugin.on_message(second)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn(
            plugin_module.health.Degradation.PROVIDER_DISABLED,
            self.plugin.services.health.degradations(),
        )

    async def test_provider_lookup_error_is_contained(self):
        await self.start(provider_error=AttributeError("Mock(spec=[]) 没有这个方法"))
        event = self.mention()
        await self.plugin.on_message(event)
        self.assertEqual(self.sent, [])

    async def test_transient_error_retries_once(self):
        provider = fakes.FakeProvider(
            script=[
                fakes.FakeApiError(429, "rate limit"),
                fakes.FakeLLMResponse(text="重试成功。"),
            ]
        )
        await self.start(provider=provider)
        await self.plugin.on_message(self.mention())
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(self.sent), 1)

    async def test_transient_error_twice_gives_up_quietly(self):
        provider = fakes.FakeProvider(
            script=[fakes.FakeApiError(429, "rate limit"), fakes.FakeApiError(503, "bad gateway")]
        )
        await self.start(provider=provider)
        await self.plugin.on_message(self.mention())
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(self.sent, [])

    async def test_auth_failure_is_not_retried_and_never_logged_verbatim(self):
        secret = "sk-should-never-appear-9f2"
        provider = fakes.FakeProvider(script=[fakes.FakeApiError(401, f"invalid api key {secret}")])
        await self.start(provider=provider)
        logger = Mock()
        self.plugin.logger = logger
        await self.plugin.on_message(self.mention())
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.sent, [])
        rendered = " ".join(
            str(call) for call in logger.info.call_args_list + logger.debug.call_args_list
        )
        self.assertNotIn(secret, rendered)

    async def test_error_role_text_is_not_forwarded(self):
        # 错误响应按标记表分类；UNCLASSIFIED 不是暂时故障，不重试也不发送。
        provider = fakes.FakeProvider(
            script=[fakes.FakeLLMResponse(role="err", text="奇怪的失败 sk-leak")]
        )
        await self.start(provider=provider)
        await self.plugin.on_message(self.mention())
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.sent, [])

    async def test_empty_reply_produces_no_output(self):
        provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="   ")])
        await self.start(provider=provider)
        await self.plugin.on_message(self.mention())
        self.assertEqual(self.sent, [])

    async def test_duplicate_message_id_is_not_reprocessed(self):
        provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="一次")])
        await self.start(provider=provider)
        first = self.mention("你好")
        first_sent = self.sent
        await self.plugin.on_message(first)
        await self.plugin.on_message(self.mention("你好"))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(first_sent), 1)

    async def test_send_failure_marks_uncertain(self):
        provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="不好意思")])
        await self.start(provider=provider)
        event = self.mention()
        self.sent = fakes.attach_send_sink(event, error=RuntimeError("协议端断开"))
        await self.plugin.on_message(event)
        stats = self.plugin.services.dedup.stats()
        self.assertEqual(stats.uncertain, 1)
        self.assertEqual(stats.in_flight, 0)

    async def test_queue_full_releases_budget_and_dedup(self):
        provider = BlockingProvider()
        await self.start(provider=provider, config=make_config(chat_queue_per_group=1))
        running = asyncio.create_task(self.plugin.on_message(self.mention("第一条", message_id="event-1")))
        waiting = None
        try:
            await provider.started.wait()
            # 第二条占满唯一等待位；第三条被立即拒绝（不等待 30 秒准入超时）。
            waiting = asyncio.create_task(
                self.plugin.on_message(self.mention("第二条", message_id="event-2"))
            )
            self.assertTrue(await self._wait_until(lambda: self._waiting_chat() == 1))
            # 第一条在跑、第二条在等：两者都已预留。
            self.assertEqual(self.plugin.services.budget.snapshot().outstanding, 2)
            third = self.mention("第三条", message_id="event-3")
            third_sent = self.sent
            await self.plugin.on_message(third)
            self.assertEqual(third_sent, [])
            # 被拒绝的那次不留预留：在途额度仍只有前两条。
            self.assertEqual(self.plugin.services.budget.snapshot().outstanding, 2)
        finally:
            provider.release.set()
            await running
            if waiting is not None:
                await waiting
        self.assertEqual(self.plugin.services.budget.snapshot().outstanding, 0)
        self.assertEqual([call["prompt"] for call in provider.calls], ["第一条", "第二条"])

    def _waiting_chat(self) -> int:
        return sum(self.plugin.services.scheduler.stats().waiting_chat.values())

    async def _wait_until(self, predicate, tries: int = 200) -> bool:
        for _ in range(tries):
            if predicate():
                return True
            await asyncio.sleep(0)
        return False

    async def test_deadline_exceeded_settles_by_estimate(self):
        provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="来不及")])
        await self.start(provider=provider)
        self.plugin.services.scheduler.submit_chat = AsyncMock(
            side_effect=plugin_module.DeadlineExceeded("超期")
        )
        await self.plugin.on_message(self.mention())
        self.assertEqual(self.sent, [])
        snapshot = self.plugin.services.budget.snapshot()
        # 缺失 usage 按预留估算入账，绝不记零。
        self.assertEqual(snapshot.day_tokens.input_tokens, 8192)
        self.assertEqual(snapshot.day_tokens.output_tokens, 512)
        self.assertEqual(self.plugin.services.dedup.stats().in_flight, 0)

    async def test_configured_budget_without_prices_refuses_conservatively(self):
        provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="不该发生")])
        await self.start(provider=provider, config=make_config(daily_budget_amount=5))
        await self.plugin.on_message(self.mention())
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.sent, [])


class StatusCommandTests(AssemblyTestCase):
    """`千鹤 状态`：唯一允许的群内控制出口（A04 离线权限、R17）。"""

    async def test_maintainer_receives_report_without_credentials(self):
        provider = fakes.FakeProvider()
        platform = fakes.FakePlatform(status="running", errors=["boom", "boom2"])
        await self.start(provider=provider, platform=platform)
        await self.plugin.on_message(self.mention("千鹤 状态"))

        self.assertEqual(len(self.sent), 1)
        report = text_of(self.sent[0])
        self.assertIn("平台连接：运行中；累计错误 2 次", report)
        self.assertIn(plugin_module.health.ACCOUNT_STATE_NOTE, report)
        self.assertIn("队列：", report)
        self.assertNotIn("SHOULD-NEVER-APPEAR", report)
        self.assertEqual(provider.calls, [])

    async def test_missing_platform_instance_is_reported(self):
        await self.start(provider=fakes.FakeProvider(), platform=None)
        await self.plugin.on_message(self.mention("千鹤 状态"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("未找到平台实例", text_of(self.sent[0]))

    async def test_platform_lookup_error_is_contained(self):
        await self.start(provider=fakes.FakeProvider(), platform_error=AttributeError("no such method"))
        await self.plugin.on_message(self.mention("千鹤 状态"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("未找到平台实例", text_of(self.sent[0]))

    async def test_non_maintainer_gets_nothing(self):
        provider = fakes.FakeProvider()
        await self.start(provider=provider, platform=fakes.FakePlatform())
        await self.plugin.on_message(self.mention("千鹤 状态", sender_id="30002"))
        self.assertEqual(self.sent, [])
        self.assertEqual(provider.calls, [])

    async def test_other_commands_are_silent(self):
        provider = fakes.FakeProvider()
        await self.start(provider=provider, platform=fakes.FakePlatform())
        for text in ("帮助", "上下文 退出", "记忆 开启", "群上下文 开启", "千鹤 暂停"):
            with self.subTest(command=text):
                await self.plugin.on_message(self.mention(text, message_id=f"event-{text}"))
                self.assertEqual(self.sent, [])
        self.assertEqual(provider.calls, [])

    async def test_model_failure_does_not_block_control(self):
        provider = fakes.FakeProvider(script=[fakes.FakeApiError(401, "invalid api key")])
        await self.start(provider=provider, platform=fakes.FakePlatform())
        await self.plugin.on_message(self.mention("你好", message_id="event-chat"))
        self.assertEqual(self.sent, [])

        await self.plugin.on_message(self.mention("千鹤 状态", message_id="event-status"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("平台连接：运行中", text_of(self.sent[0]))


class LifecycleAndStructureTests(AssemblyTestCase):
    async def test_terminate_releases_scheduler_and_is_idempotent(self):
        await self.start(provider=fakes.FakeProvider())
        services = self.plugin.services
        await self.plugin.terminate()
        self.assertTrue(services.scheduler.stats().closed)
        self.assertIsNone(self.plugin.services)
        await self.plugin.terminate()

        event = self.mention()
        await self.plugin.on_message(event)
        self.assertEqual(self.sent, [])

    def test_single_outbound_path_in_source(self):
        source = (PLUGIN_PATH / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        sends = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send"
        ]
        self.assertEqual(len(sends), 1)
        self.assertEqual(ast.unparse(sends[0].func), "event.send")

        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for forbidden in ("request_llm", "send_streaming", "create_task", "send_message"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, names)

    def test_single_registered_handler(self):
        from astrbot.core.star.star_handler import star_handlers_registry

        handlers = star_handlers_registry.get_handlers_by_module_name(MODULE_NAME)
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].extras_configs["priority"], 1000)


class ContextCollectionTests(AssemblyTestCase):
    """S2-03：普通群聊采集的准入与排除。

    生产环境默认关闭（策略仓储无行即关闭，S2-02 未落地）；测试直接写策略模拟
    S2-02 完成告知后的状态。采集不产生任何出站或模型调用。
    """

    async def test_collection_stays_off_without_storage(self):
        provider = fakes.FakeProvider()
        await self.start(provider=provider)
        self.assertIsNone(self.plugin.services.storage)
        await self.plugin.on_message(self.plain())
        self.assertEqual(self.collected(), ())
        self.assertEqual(self.sent, [])
        self.assertEqual(provider.calls, [])

    async def test_open_group_collects_plain_text(self):
        provider = fakes.FakeProvider()
        await self.start(provider=provider, storage_path=self.new_storage_path())
        self.open_group()
        await self.plugin.on_message(self.plain("今天天气不错"))
        entries = self.collected()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].member_id, "30001")
        self.assertEqual(entries[0].text, "今天天气不错")
        self.assertEqual(self.sent, [])
        self.assertEqual(provider.calls, [])

    async def test_excluded_shapes_and_scopes_are_not_collected(self):
        provider = fakes.FakeProvider()
        await self.start(provider=provider, storage_path=self.new_storage_path())
        self.open_group()
        cases = (
            ("私聊", [factory.Plain("你好")], {"private": True}),
            ("未允许群", [factory.Plain("你好")], {"group_id": "20002"}),
            ("机器人自身消息", [factory.Plain("你好")], {"sender_id": "10001"}),
            ("@全体", [factory.AtAll(), factory.Plain("你好")], {}),
            ("@他人", [factory.At(qq="10002"), factory.Plain("你好")], {}),
            ("空 @", [factory.At(qq="10001")], {}),
            ("引用", [factory.Reply(id="old", chain=[factory.Plain("旧")]), factory.Plain("在吗")], {}),
            ("合并转发", [factory.Forward(id="forward-1")], {}),
            ("附件", [factory.Image(file="unused.png")], {}),
            ("命令", [factory.Plain("千鹤 状态")], {}),
            ("敏感文本", [factory.Plain("我的手机号是13800138000")], {}),
        )
        for name, chain, overrides in cases:
            with self.subTest(case=name):
                await self.plugin.on_message(self.event(chain, **overrides))
                self.assertEqual(self.collected(), ())
        self.assertEqual(self.sent, [])
        self.assertEqual(provider.calls, [])

    async def test_closed_paused_or_opted_out_groups_stop_collection(self):
        await self.start(storage_path=self.new_storage_path())
        self.open_group()
        group = self.group_key()
        storage = self.plugin.services.storage

        storage.groups.set_paused(group, paused=True)
        await self.plugin.on_message(self.plain(message_id="paused"))
        self.assertEqual(self.collected(), ())

        storage.groups.set_paused(group, paused=False)
        storage.members.opt_out(plugin_module.MemberKey(group, "30001"))
        await self.plugin.on_message(self.plain(message_id="opted-out"))
        self.assertEqual(self.collected(), ())

        storage.groups.set_context_enabled(
            group, enabled=False, required_notice_version="notice-1"
        )
        await self.plugin.on_message(self.plain(message_id="disabled"))
        self.assertEqual(self.collected(), ())

    async def test_degradation_stops_collection(self):
        await self.start(storage_path=self.new_storage_path())
        self.open_group()
        self.plugin.services.health.set_degraded(
            plugin_module.health.Degradation.MEMORY_STORE_FAILED
        )
        await self.plugin.on_message(self.plain())
        self.assertEqual(self.collected(), ())

    async def test_buffer_ttl_follows_the_injected_clock(self):
        await self.start(storage_path=self.new_storage_path())
        self.open_group()
        await self.plugin.on_message(self.plain())
        self.assertEqual(len(self.collected()), 1)
        self.clock.advance(600)
        self.assertEqual(self.collected(), ())


class ContextCommandTests(AssemblyTestCase):
    """S2-04：`上下文 退出` / `上下文 加入` 的状态变更与清理（静默、不虚报）。"""

    def leave(self, **overrides):
        return self.mention("上下文 退出", **overrides)

    def join(self, **overrides):
        return self.mention("上下文 加入", **overrides)

    def member(self, member_id: str = "30001"):
        return plugin_module.MemberKey(self.group_key(), member_id)

    async def start_with_storage(self, *, provider=None, cleaner=None):
        path = self.new_storage_path()
        await self.start(
            provider=provider or fakes.FakeProvider(),
            storage_path=path,
            history_cleaner=cleaner or fakes.FakeHistoryCleaner(),
        )
        return path

    async def test_leave_persists_clears_buffer_and_history(self):
        provider = fakes.FakeProvider()
        cleaner = fakes.FakeHistoryCleaner()
        path = await self.start_with_storage(provider=provider, cleaner=cleaner)
        self.open_group()
        await self.plugin.on_message(self.plain("旧消息"))
        self.assertEqual(len(self.collected()), 1)

        event = self.leave()
        await self.plugin.on_message(event)
        self.assertTrue(self.plugin.services.storage.members.state(self.member()).opted_out)
        self.assertEqual(self.collected(), ())
        self.assertEqual(cleaner.calls, [event.unified_msg_origin])
        self.assertEqual(self.sent, [])
        self.assertEqual(provider.calls, [])

        # 重启不复活。
        reopened = plugin_module.storage.open_storage(path, clock=lambda: 0)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.members.state(self.member()).opted_out)

    async def test_leave_only_affects_the_sender(self):
        cleaner = fakes.FakeHistoryCleaner()
        await self.start_with_storage(cleaner=cleaner)
        self.open_group()
        await self.plugin.on_message(self.plain("甲的消息", sender_id="30001", message_id="m1"))
        await self.plugin.on_message(self.plain("乙的消息", sender_id="30002", message_id="m2"))
        await self.plugin.on_message(self.leave(message_id="leave-1"))

        self.assertTrue(self.plugin.services.storage.members.state(self.member("30001")).opted_out)
        self.assertFalse(self.plugin.services.storage.members.state(self.member("30002")).opted_out)
        self.assertEqual([entry.text for entry in self.collected()], ["乙的消息"])

    async def test_join_needs_an_open_group_and_restores_nothing(self):
        await self.start_with_storage()
        await self.plugin.on_message(self.leave(message_id="leave-1"))
        self.assertTrue(self.plugin.services.storage.members.state(self.member()).opted_out)

        # 未告知/未开启：加入不生效。
        await self.plugin.on_message(self.join(message_id="join-1"))
        self.assertTrue(self.plugin.services.storage.members.state(self.member()).opted_out)

        # 告知并开启后可以加入；旧材料不恢复。
        self.open_group()
        await self.plugin.on_message(self.join(message_id="join-2"))
        self.assertFalse(self.plugin.services.storage.members.state(self.member()).opted_out)
        self.assertEqual(self.collected(), ())

    async def test_leave_then_addressed_chat_still_works(self):
        provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="嗯，我在听。")])
        await self.start_with_storage(provider=provider)
        self.open_group()
        await self.plugin.on_message(self.leave(message_id="leave-1"))
        await self.plugin.on_message(self.mention("你好", message_id="chat-1"))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(self.sent), 1)

    async def test_history_failure_is_reported_without_faking_deletion(self):
        cleaner = fakes.FakeHistoryCleaner(error=RuntimeError("delete failed"))
        await self.start_with_storage(cleaner=cleaner)
        self.open_group()
        await self.plugin.on_message(self.leave())

        self.assertTrue(self.plugin.services.storage.members.state(self.member()).opted_out)
        self.assertIn(
            plugin_module.health.Degradation.DELETION_FAILED,
            self.plugin.services.health.degradations(),
        )
        self.assertEqual(self.sent, [])
        # 删除失败后采集保持关闭（架构 §8.3）。
        await self.plugin.on_message(self.plain("新消息", message_id="after"))
        self.assertEqual(self.collected(), ())

    async def test_storage_unavailable_is_silent_and_fails_closed(self):
        broken = self.new_storage_path()
        broken.write_bytes(b"not a sqlite database")
        provider = fakes.FakeProvider(script=[fakes.FakeLLMResponse(text="嗯。")])
        await self.start(provider=provider, storage_path=broken, history_cleaner=fakes.FakeHistoryCleaner())
        self.assertIsNone(self.plugin.services.storage)
        self.assertIn(
            plugin_module.health.Degradation.MEMORY_STORE_FAILED,
            self.plugin.services.health.degradations(),
        )
        await self.plugin.on_message(self.leave())
        self.assertEqual(self.sent, [])
        self.assertEqual(provider.calls, [])

        # 聊天不依赖存储：退出/加入不可用不影响正常 @ 回复。
        await self.plugin.on_message(self.mention("你好", message_id="chat-1"))
        self.assertEqual(len(self.sent), 1)

    async def test_duplicate_event_runs_once(self):
        cleaner = fakes.FakeHistoryCleaner()
        await self.start_with_storage(cleaner=cleaner)
        self.open_group()
        await self.plugin.on_message(self.leave(message_id="same"))
        await self.plugin.on_message(self.leave(message_id="same"))
        self.assertEqual(len(cleaner.calls), 1)

    async def test_out_of_scope_events_change_nothing(self):
        await self.start_with_storage()
        self.open_group()
        cases = (
            ("私聊", {"private": True}),
            ("未允许群", {"group_id": "20002"}),
            ("机器人自身消息", {"sender_id": "10001"}),
        )
        for index, (name, overrides) in enumerate(cases):
            with self.subTest(case=name):
                await self.plugin.on_message(self.leave(message_id=f"m{index}", **overrides))
        self.assertFalse(self.plugin.services.storage.members.state(self.member()).opted_out)
        self.assertFalse(
            self.plugin.services.storage.members.state(
                plugin_module.MemberKey(self.group_key("20002"), "30001")
            ).opted_out
        )
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
