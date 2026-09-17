"""S0 离线核验共享引导。

本目录不属于产品插件包，不参与插件运行时；脚本只在离线开发环境中执行。

约定与 tests/ 保持一致：隔离 ASTRBOT_ROOT、注入 .runtime/astrbot 到 sys.path、
阻止子进程/网络/pip，替换后台调度边界。区别是这些脚本在独立进程里运行，
因此可以按需放行 sqlite3（check_history.py 需要建临时库）。

典型用法：

    from _harness import Checker, Harness

    checker = Checker("S0-0X", "任务名")
    with Harness(allow_sqlite=False) as h:
        event = h.make_event([At(qq="10001"), Plain("你好")])
        sends = h.attach_sink(event)
        await h.run_waking(event)
        checker.check("非 @ 零出站", sends == [], evidence=str(sends))
    raise SystemExit(checker.report())
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / ".runtime" / "astrbot"

# audit hook 无法卸载，同一个进程只能建立一次 Harness。
_HOOK_INSTALLED = False

# 与 tests/test_plugin.py 相同的阻止集合；sqlite3.connect 单独处理。
_ALWAYS_BLOCKED = {
    "subprocess.Popen",
    "os.system",
    "os.posix_spawn",
    "os.exec",
    "os.fork",
    "socket.connect",
    "socket.bind",
    "socket.getaddrinfo",
    "socket.sendto",
}


class GuardViolation(AssertionError):
    """守卫生效期间发生了被禁止的操作。"""


def _describe(args: tuple) -> str:
    try:
        return " ".join(repr(a)[:120] for a in args)
    except Exception:  # pragma: no cover - 仅用于诊断信息
        return "<unprintable>"


class _AuditGuard:
    """进程级 audit hook。

    白名单只有本次运行的临时根目录：SQLite 仅允许落在其中（框架的
    WakingCheckStage 会经 SessionPluginManager→shared_preferences 读写数据库，
    无法规避）。子进程、网络、pip 一律无条件拒绝。
    """

    def __init__(self) -> None:
        self.allow_dir: Path | None = None
        self.enabled = False
        self.violations: list[str] = []

    def __call__(self, event: str, args: tuple) -> None:
        if not self.enabled:
            return
        if event in _ALWAYS_BLOCKED:
            self._reject(event, args)
        if event == "import" and args and str(args[0]).split(".")[0] == "pip":
            self._reject(event, args)
        if event == "sqlite3.connect":
            target = Path(str(args[0])).resolve() if args else None
            if self.allow_dir is None or target is None:
                self._reject(event, args)
            else:
                try:
                    target.relative_to(self.allow_dir)
                except ValueError:
                    self._reject(event, args)

    def _reject(self, event: str, args: tuple) -> None:
        detail = f"{event}({_describe(args)})"
        self.violations.append(detail)
        raise GuardViolation(f"离线核验禁止的操作：{detail}")


class Harness:
    """建立一次隔离的离线运行环境并暴露框架对象。

    每个脚本进程只使用一次：audit hook 无法卸载，重复进入会叠加钩子。
    """

    def __init__(self, *, with_plugin: bool = True) -> None:
        self.with_plugin = with_plugin
        self._stack = ExitStack()
        self._guard = _AuditGuard()
        self.tmpdir: Path | None = None
        self.config = None
        self.ctx = None
        self.waking = None
        # 框架对象在 _enter 中导入后填充
        self.AstrBotConfig = None
        self.AstrBotMessage = None
        self.AstrMessageEvent = None
        self.MessageMember = None
        self.MessageType = None
        self.PlatformMetadata = None
        self.At = None
        self.AtAll = None
        self.Plain = None
        self.Reply = None
        self.Image = None
        self.core = None
        self.plugin_module = None
        self.builtin_astrbot = None
        self.builtin_commands = None

    # ---- 生命周期 -----------------------------------------------------

    def __enter__(self) -> "Harness":
        global _HOOK_INSTALLED
        if _HOOK_INSTALLED:
            raise RuntimeError("每个进程只允许建立一个 Harness（audit hook 不可卸载）")
        _HOOK_INSTALLED = True

        parent = SOURCE / "data" if (SOURCE / "data").is_dir() else SOURCE
        self.tmpdir = Path(
            self._stack.enter_context(
                tempfile.TemporaryDirectory(prefix="s0-", dir=parent),
            ),
        )
        self._guard.allow_dir = self.tmpdir.resolve()
        self._stack.enter_context(patch.dict(os.environ, {"ASTRBOT_ROOT": str(self.tmpdir)}))
        self._stack.enter_context(patch.object(sys, "path", [str(SOURCE), *sys.path]))
        sys.addaudithook(self._guard)

        # AstrBot 导入会启动 SharedPreferences 定时器；只替换这一外部调度边界。
        from apscheduler.schedulers.background import BackgroundScheduler

        self._stack.enter_context(patch.object(BackgroundScheduler, "start"))

        self._guard.enabled = True
        self._load_modules()
        return self

    def __exit__(self, *exc_info) -> None:
        self._guard.enabled = False
        self._stack.close()

    def _load_modules(self) -> None:
        import importlib

        from astrbot.core import pip_installer

        self._stack.enter_context(
            patch.object(
                pip_installer,
                "install",
                new=Mock(side_effect=AssertionError("pip installer forbidden")),
            ),
        )

        self.core = importlib.import_module("astrbot.core")
        self.core.astrbot_config["trace_enable"] = False

        from astrbot.api import AstrBotConfig
        from astrbot.api.event import AstrMessageEvent
        from astrbot.api.message_components import At, AtAll, Image, Plain, Reply
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.pipeline.context import PipelineContext
        from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata

        self.AstrBotConfig = AstrBotConfig
        self.AstrMessageEvent = AstrMessageEvent
        self.AstrBotMessage = AstrBotMessage
        self.MessageMember = MessageMember
        self.MessageType = MessageType
        self.PlatformMetadata = PlatformMetadata
        self.At, self.AtAll, self.Image, self.Plain, self.Reply = At, AtAll, Image, Plain, Reply

        # default_config 用深拷贝，避免脚本改配置时污染框架模块级对象。
        self.config = AstrBotConfig(
            str(self.tmpdir / "cmd_config.json"),
            default_config=copy.deepcopy(DEFAULT_CONFIG),
        )
        self.ctx = PipelineContext(
            astrbot_config=self.config,
            plugin_manager=Mock(),
            astrbot_config_id="s0-offline",
            db_helper=None,
        )
        self.WakingCheckStage = WakingCheckStage

        if self.with_plugin:
            self._register_plugin()
        self._activate_modules()

    def _register_plugin(self) -> None:
        """导入真实插件模块，使其 handler 进入注册表（与线上组合一致）。"""
        import importlib

        module_name = "data.plugins.astrbot_plugin_chizuru.main"
        self.plugin_module = importlib.import_module(module_name)

    def _activate_modules(self) -> None:
        """补齐 star_map 元数据。

        线上由 StarManager 完成这两件事，缺了就会失真：
        - `name` 为空时 SessionPluginManager.filter_handlers_by_session 会直接丢弃
          该插件的全部 handler（session_plugin_manager.py:89-90），导致 activated_handlers
          恒为空；
        - 保留内置插件必须 `reserved=True`，否则 plugin_set 收窄会误伤它们
          （star_handler.py:175-186）。
        """
        from astrbot.core.star.star import star_map

        for module_path, name in (
            ("astrbot.builtin_stars.astrbot.main", "astrbot"),
            ("astrbot.builtin_stars.builtin_commands.main", "builtin_commands"),
        ):
            md = star_map.get(module_path)
            if md is not None:
                md.name = name
                md.reserved = True
                md.activated = True

        if self.plugin_module is None:
            return
        from astrbot.core.star.star_manager import PluginManager

        md = star_map.get("data.plugins.astrbot_plugin_chizuru.main")
        if md is None:
            return
        try:
            meta = PluginManager._load_plugin_metadata(str(ROOT / "astrbot_plugin_chizuru"))
            md.name = meta.name
        except BaseException:
            md.name = "astrbot_plugin_chizuru"
        md.reserved = False
        md.activated = True

    def load_builtins(self) -> None:
        """导入保留内置插件。

        它们位于 astrbot/builtin_stars/ 下，plugin_set 无法排除，线上必然加载，
        因此 S0-02 的门控矩阵必须把它们算进来。
        """
        import importlib

        self.builtin_astrbot = importlib.import_module("astrbot.builtin_stars.astrbot.main")
        self.builtin_commands = importlib.import_module(
            "astrbot.builtin_stars.builtin_commands.main",
        )
        self._activate_modules()

    def build_instances(self, plugin_settings: dict | None = None) -> tuple[dict, list]:
        """构造各 handler_module_path 对应的 Star 实例。

        返回 (instances, failures)；构造失败的模块不会进入 instances，调用方应据
        failures 判断哪些处理器实际未被执行。
        """
        import json

        instances: dict = {}
        failures: list[str] = []

        if self.plugin_module is not None:
            plugin_path = ROOT / "astrbot_plugin_chizuru"
            schema = json.loads((plugin_path / "_conf_schema.json").read_text())
            plugin_config = self.AstrBotConfig(
                str(self.tmpdir / "plugin-config.json"),
                schema=schema,
            )
            if plugin_settings:
                plugin_config.update(plugin_settings)
            instances["data.plugins.astrbot_plugin_chizuru.main"] = (
                self.plugin_module.ChizuruPlugin(Mock(spec=[]), plugin_config)
            )

        builtin_ctx = Mock()
        builtin_ctx.get_config = lambda **kwargs: self.config
        # 会话管理用真异步替身：若返回不可 await 的 Mock，处理器会走异常分支，
        # 就看不出它原本会不会发起模型调用。
        builtin_ctx.conversation_manager = Mock()
        builtin_ctx.conversation_manager.get_curr_conversation_id = AsyncMock(return_value=None)
        builtin_ctx.conversation_manager.new_conversation = AsyncMock(return_value="s0-cid")
        builtin_ctx.conversation_manager.get_conversation = AsyncMock(return_value=None)
        builtin_ctx.get_event_queue = Mock()

        for module, module_path, class_name in (
            (getattr(self, "builtin_astrbot", None), "astrbot.builtin_stars.astrbot.main", "Main"),
            (
                getattr(self, "builtin_commands", None),
                "astrbot.builtin_stars.builtin_commands.main",
                "Main",
            ),
        ):
            if module is None:
                continue
            try:
                instances[module_path] = getattr(module, class_name)(builtin_ctx)
            except BaseException as exc:  # 构造失败必须显式记录，不能当作已执行
                failures.append(f"{module_path}: {type(exc).__name__}: {exc}")

        return instances, failures

    # ---- 便捷方法 -----------------------------------------------------

    async def init_waking(self) -> None:
        """按当前配置新建并初始化一个 WakingCheckStage。

        Stage 在 initialize 时读取配置，所以切换配置档必须重新初始化；这与框架
        为每个配置档维护独立 Stage 实例的方式一致。
        """
        self.waking = self.WakingCheckStage()
        await self.waking.initialize(self.ctx)

    def set_config(self, dotted: str, value) -> None:
        """按点号路径写配置，例如 set_config("platform_settings.no_permission_reply", False)。"""
        node = self.config
        parts = dotted.split(".")
        for key in parts[:-1]:
            node = node[key]
        node[parts[-1]] = value

    def apply_closed_config(self) -> None:
        """应用 S0-02 记录的必关配置（附录 D.2）。"""
        self.set_config("disable_builtin_commands", True)
        self.set_config("plugin_set", ["astrbot_plugin_chizuru"])
        self.set_config("provider_settings.enable", False)
        self.set_config("provider_settings.streaming_response", False)
        self.set_config("provider_settings.proactive_capability.add_cron_tools", False)
        self.set_config("platform_settings.no_permission_reply", False)
        self.set_config("platform_settings.empty_mention_waiting", False)
        self.set_config("platform_settings.empty_mention_waiting_need_reply", False)
        self.set_config("platform_settings.friend_message_needs_wake_prefix", True)
        self.set_config("platform_settings.ignore_at_all", True)
        self.set_config("platform_settings.ignore_bot_self_message", True)
        self.set_config("platform_settings.unique_session", False)
        self.set_config("provider_ltm_settings.group_icl_enable", False)
        self.set_config("provider_ltm_settings.group_message_history_enable", False)
        self.set_config("provider_ltm_settings.active_reply.enable", False)

    def make_event(
        self,
        chain,
        *,
        platform: str = "aiocqhttp",
        platform_id: str = "qq-local",
        self_id: str = "10001",
        group_id: str = "20001",
        sender_id: str = "30001",
        nickname: str = "成员",
        private: bool = False,
        message_id: str = "event-1",
        message_str: str | None = None,
    ):
        """构造一个 aiocqhttp 消息事件；message_str 默认取 Plain 文本拼接。"""
        message = self.AstrBotMessage()
        message.type = self.MessageType.FRIEND_MESSAGE if private else self.MessageType.GROUP_MESSAGE
        message.self_id = self_id
        message.group_id = group_id
        message.sender = self.MessageMember(user_id=sender_id, nickname=nickname)
        message.message = list(chain)
        if message_str is None:
            message_str = "".join(
                part.text for part in chain if isinstance(part, self.Plain)
            )
        message.message_str = message_str
        message.message_id = message_id
        message.raw_message = {}
        event = self.AstrMessageEvent(
            message.message_str,
            message,
            self.PlatformMetadata(platform, "offline", platform_id),
            group_id,
        )
        return event

    def attach_sink(self, event) -> list:
        """把 event.send 换成本地记录器，返回记录列表。"""
        sends: list = []

        async def _record(message_chain):
            sends.append(message_chain)
            event._has_send_oper = True

        event.send = _record
        return sends

    def attach_llm_recorder(self, event) -> list:
        """记录 event.request_llm 调用（模型调用意图），返回记录列表。"""
        calls: list = []

        def _request_llm(**kwargs):
            calls.append(kwargs)
            return Mock()

        event.request_llm = _request_llm
        return calls

    async def run_waking(self, event) -> None:
        """跑 WakingCheckStage.process，把返回的异步生成器消费掉。

        Stage.process 是 async def，但返回 None 或异步生成器，需要先 await。
        """
        result = await self.waking.process(event)
        if result is None:
            return
        async for _ in result:
            pass

    async def run_pipeline_handlers(self, event, instances: dict) -> list:
        """按 star_request.py:31-70 的语义执行 activated_handlers。

        instances 把 handler_module_path 映射到已构造的 Star 实例；无法提供实例的
        处理器会被跳过并在返回值中标记，避免"跳过了却当成跑过"。
        """
        from astrbot.core.pipeline.context_utils import call_handler

        activated = event.get_extra("activated_handlers") or []
        executed: list[str] = []
        skipped: list[str] = []
        for handler in activated:
            if event.is_stopped():
                break
            instance = instances.get(handler.handler_module_path)
            if instance is None:
                skipped.append(handler.handler_full_name)
                continue
            bound = handler.handler.__get__(instance)
            async for _ in call_handler(event, bound):
                pass
            executed.append(handler.handler_full_name)
            if event.is_stopped():
                break
            # 与 star_request.py:52 一致：未停止则清除上一个 handler 的结果。
            event.clear_result()
        event.set_extra("s0.executed_handlers", executed)
        event.set_extra("s0.skipped_handlers", skipped)
        return activated


class Checker:
    """逐项打印 PASS/FAIL 并汇总退出码。"""

    def __init__(self, task_id: str, title: str) -> None:
        self.task_id = task_id
        self.title = title
        self.passed = 0
        self.failures: list[str] = []
        print(f"[{task_id}] {title}")

    def check(self, name: str, condition: bool, evidence: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failures.append(name)
            print(f"  FAIL  {name}" + (f"  <- {evidence}" if evidence else ""))
        return bool(condition)

    def note(self, text: str) -> None:
        print(f"  note  {text}")

    def report(self) -> int:
        total = self.passed + len(self.failures)
        if self.failures:
            print(f"{self.task_id}: {self.passed}/{total} PASS，失败项：{', '.join(self.failures)}")
            return 1
        print(f"{self.task_id}: {self.passed}/{total} PASS")
        return 0


def run(coro):
    """脚本入口用的 asyncio 运行包装。"""
    return asyncio.run(coro)
