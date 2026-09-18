"""S1-15 的离线假件：假时钟、假提供商、假平台、事件工厂与作用域守卫。

本文件**不是测试模块**（`test*.py` 才被 discover 收集），也不属于产品插件包。
用法见 `tests/test_assembly.py` 的 `setUpModule`：先完成框架引导（patch
`ASTRBOT_ROOT`、把 `.runtime/astrbot` 注入 `sys.path`、建立作用域守卫），再构造
`EventFactory`。假件只在本文件内实现，不 import `scripts/s0/`（S0 脚本不参与产品与测试）。

**时钟注意**：`FakeClock` 只喂给 `Scheduler`/`DedupStore`/`BudgetLedger`/重试判定，
它不影响 `asyncio.wait_for` 使用的事件循环时钟。因此测试不得在 await 期间推进假时钟：
超时路径要显式构造（例如直接打桩 `submit_chat` 抛 `DeadlineExceeded`）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".runtime" / "astrbot"
PLUGIN_PATH = ROOT / "astrbot_plugin_chizuru"
_ALLOWED_SQLITE_ROOT = (ROOT / ".runtime").resolve()

_BLOCKED_EVENTS = frozenset(
    {
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
)


class GuardViolation(AssertionError):
    """越界操作被离线守卫拒绝。"""


class ScopeGuard:
    """K12 作用域守卫：子进程/网络/pip 硬拒绝，SQLite 只放行 `.runtime` 下的路径。

    与 `tests/test_plugin.py` 同一策略。守卫只**收窄**既有保护，不删除也不整体停用。
    `sys.addaudithook` 不可卸载，因此用 `enabled` 开关控制生效区间。
    """

    def __init__(self, *, allow_dir: Path | None = None) -> None:
        self.allow_dir = (allow_dir or _ALLOWED_SQLITE_ROOT).resolve()
        self.enabled = False
        self.violations: list[str] = []

    def __call__(self, event: str, args: tuple) -> None:
        if not self.enabled:
            return
        if event == "sqlite3.connect":
            try:
                target = Path(str(args[0])).resolve() if args else None
                allowed = target is not None and target.is_relative_to(self.allow_dir)
            except (OSError, ValueError):
                allowed = False
            if not allowed:
                self.violations.append(event)
                raise GuardViolation(f"离线测试禁止 {event}（{self.allow_dir} 之外）")
            return
        blocked = event in _BLOCKED_EVENTS
        if event == "import" and args[0].split(".")[0] == "pip":
            blocked = True
        if blocked:
            self.violations.append(event)
            raise GuardViolation(f"离线测试禁止 {event}")


def install_scope_guard(*, allow_dir: Path | None = None) -> ScopeGuard:
    """安装并启用作用域守卫。返回守卫对象，调用方负责在模块结束时关闭并核对。"""
    guard = ScopeGuard(allow_dir=allow_dir)
    sys.addaudithook(guard)
    guard.enabled = True
    return guard


# ---- 时钟 ----


class FakeClock:
    """单调时钟与墙上时钟同源推进；两者都交给装配层注入。"""

    def __init__(
        self,
        *,
        start: float = 1_000.0,
        start_datetime: datetime | None = None,
    ) -> None:
        self._monotonic = start
        self._datetime = start_datetime or datetime(2026, 9, 18, 12, 0, 0)

    def monotonic(self) -> float:
        return self._monotonic

    def now(self) -> datetime:
        return self._datetime

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._datetime += timedelta(seconds=seconds)


# ---- 提供商 ----


class FakeUsage:
    """形状对齐框架的 `TokenUsage`：只实现 `llm.map_usage` 会读的三个字段。"""

    def __init__(self, *, input_other: int = 0, input_cached: int = 0, output: int = 0) -> None:
        self.input_other = input_other
        self.input_cached = input_cached
        self.output = output


class FakeLLMResponse:
    """只提供 `role` / `completion_text` / `usage`；读推理字段即失败。"""

    def __init__(self, *, role: str = "assistant", text: str = "", usage: object = None) -> None:
        self.role = role
        self._text = text
        self.usage = usage

    @property
    def completion_text(self) -> str:
        return self._text

    @property
    def reasoning_content(self) -> str:
        raise AssertionError("插件不得读取推理字段（R-CHAT）")


class FakeApiError(Exception):
    """带状态码的提供商异常；`llm.classify_exception` 按 `status_code` 分类。"""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"provider error {status_code}")
        self.status_code = status_code


class FakeProvider:
    """按脚本返回响应或抛异常，并原样记录每次调用的 kwargs。"""

    def __init__(self, *, model: str = "fake-model", script: list | None = None) -> None:
        self._model = model
        self.calls: list[dict] = []
        self._script = list(script or ())

    def get_model(self) -> str:
        return self._model

    def queue(self, *items: object) -> None:
        self._script.extend(items)

    async def text_chat(self, **kwargs: object) -> FakeLLMResponse:
        self.calls.append(dict(kwargs))
        item = self._script.pop(0) if self._script else FakeLLMResponse(text="嗯，我在听。")
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]


# ---- 平台与上下文 ----


class FakePlatform:
    """平台适配器替身；`.config` 故意带假 token，用于"报告不泄漏凭据"的反向断言。"""

    def __init__(self, *, status: str = "running", errors: tuple = ()) -> None:
        self.status = status
        self.errors = list(errors)
        self.config = {"ws_reverse_token": "SHOULD-NEVER-APPEAR"}


class FakeContext:
    """只实现装配层会用的两个读取入口；其余属性一律不存在（fail-closed）。"""

    def __init__(
        self,
        *,
        provider: object = None,
        provider_error: BaseException | None = None,
        platform: object = None,
        platform_error: BaseException | None = None,
    ) -> None:
        self._provider = provider
        self._provider_error = provider_error
        self._platform = platform
        self._platform_error = platform_error
        self.provider_calls: list[object] = []
        self.platform_queries: list[str] = []

    async def get_using_provider_async(self, umo: object = None) -> object:
        self.provider_calls.append(umo)
        if self._provider_error is not None:
            raise self._provider_error
        return self._provider

    def get_platform_inst(self, platform_id: str) -> object:
        self.platform_queries.append(platform_id)
        if self._platform_error is not None:
            raise self._platform_error
        return self._platform


def make_config(path: Path, **values: object) -> object:
    """按真实 `_conf_schema.json` 构造插件配置，并覆盖给定键。需先完成框架引导。"""
    import json

    from astrbot.api import AstrBotConfig

    schema = json.loads((PLUGIN_PATH / "_conf_schema.json").read_text(encoding="utf-8"))
    config = AstrBotConfig(str(path), schema=schema)
    if values:
        config.update(**values)
    return config


# ---- 事件 ----


class EventFactory:
    """构造真实 AstrBot 事件对象；框架类型在首次使用时才解析。"""

    def __init__(
        self,
        *,
        platform: str = "aiocqhttp",
        platform_id: str = "qq-local",
        self_id: str = "10001",
        group_id: str = "20001",
        sender_id: str = "30001",
    ) -> None:
        from astrbot.api.event import AstrMessageEvent
        from astrbot.api.message_components import At, AtAll, File, Image, Plain, Record, Reply, Video
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata

        self._event_cls = AstrMessageEvent
        self._message_cls = AstrBotMessage
        self._member_cls = MessageMember
        self._message_type = MessageType
        self._platform_metadata = PlatformMetadata
        self.At = At
        self.AtAll = AtAll
        self.Plain = Plain
        self.Reply = Reply
        self.Image = Image
        self.Record = Record
        self.Video = Video
        self.File = File
        self.defaults = {
            "platform": platform,
            "platform_id": platform_id,
            "self_id": self_id,
            "group_id": group_id,
            "sender_id": sender_id,
        }

    def event(self, chain: list, **overrides: object) -> object:
        """构造事件；`message_str` 故意是伪造串，用于验证插件不信任拼接文本。"""
        fields = {**self.defaults, **overrides}
        private = bool(fields.pop("private", False))
        nickname = str(fields.pop("nickname", "同名"))
        message_id = fields.pop("message_id", "event-1")
        message_str = fields.pop("message_str", "伪造的拼接文本不应成为直接输入")

        message = self._message_cls()
        message.type = (
            self._message_type.FRIEND_MESSAGE if private else self._message_type.GROUP_MESSAGE
        )
        message.self_id = fields["self_id"]
        message.group_id = fields["group_id"]
        message.sender = self._member_cls(user_id=fields["sender_id"], nickname=nickname)
        message.message = chain
        message.message_str = message_str
        message.message_id = message_id
        message.raw_message = {}
        return self._event_cls(
            message_str,
            message,
            self._platform_metadata(fields["platform"], "offline", fields["platform_id"]),
            fields["group_id"],
        )


def attach_send_sink(event: object, *, error: BaseException | None = None) -> list:
    """替换 `event.send` 为记录器（可选：抛出给定异常模拟发送不确定）。

    真实的 `send` 会 `asyncio.create_task(Metric.upload(...))` 并联网（基类
    `astr_message_event.py:483-497`），在线下会被守卫拒绝并留下悬挂任务。记录器同时
    复刻 `_has_send_oper = True` 的语义。
    """
    sent: list = []

    async def sink(message: object) -> None:
        sent.append(message)
        if error is not None:
            raise error

    event.send = sink
    event._has_send_oper = True
    return sent


def make_plugin(context: object, config: object, *, clock: FakeClock | None = None) -> object:
    """构造插件实例并覆写测试注入点（不新增构造参数）。"""
    from astrbot_plugin_chizuru.main import ChizuruPlugin

    plugin = ChizuruPlugin(context, config)
    if clock is not None:
        plugin.clock = clock.monotonic
        plugin.datetime_clock = clock.now
    plugin.redactor_salt = b"offline-test-salt-0123456789ab"
    return plugin
