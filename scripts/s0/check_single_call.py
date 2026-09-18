"""S1-10 单次模型调用语义（离线部分）。

核验什么：

1. **K4 的第二条默认 LLM 路径确实存在**，以及它运行的条件——用桩子替身驱动**真实**
   的 `ProcessStage.process`，记录 agent 子阶段被调用了几次。
2. **`should_call_llm` 的真实方向（K5 修正）**：`event.call_llm` 初值为 `False`，
   第二路径的门槛是 `not event.call_llm`，因此只有 `should_call_llm(True)` 才抑制
   默认链路；`should_call_llm(False)` 什么也不改变。`stop_event()` 也能抑制，但它同时
   会挡住 RespondStage 的回复，不能用在需要回话的路径上。
3. **`provider_settings.enable=false`（附录 D.2）是"直调提供商"路径的前提**：它关闭
   ProcessStage 的第二条默认路径，并在 `AgentRequestSubStage` 入口处短路插件自身的
   `event.request_llm` 请求（注意：ProcessStage 的 handler 分支本身不检查该键，
   短路发生在它委托的子阶段里）。于是框架侧不可能再发起模型调用，O-03"一次 @ 一次
   调用"在必关配置下结构性成立，而不是靠收口技巧。
4. **`llm.py` 的请求计划与真实 `Provider.text_chat` 对齐**：`LLMRequestPlan.call_kwargs()`
   的键都是真实参数并含 `request_max_retries`，而 `event.request_llm` 根本没有重试
   参数——这是选择直调路径的依据之一。
5. **`call_llm` 只有一个写入点**：全上游 `self.call_llm = ` 仅出现在 `should_call_llm`。

不覆盖：真实提供商与 SDK 叠加后的实际请求次数（R19，只能在线测量）、NapCat/QQ、
真实 `cmd_config.json`、流式发送、WakingCheck 唤醒矩阵（`check_gating.py` 已覆盖）。

做法说明：`ProcessStage.initialize()` 会构造重量级子阶段并读取框架配置，因此这里直接
替换 `ctx` 与两个子阶段替身，只跑**真实的** `ProcessStage.process` 控制流。若上游以后
无法导入该模块（docs/03 §14.4 第 5 条记录过循环导入），控制流用例会被跳过并打印 note，
其余（源码指纹与签名）照常执行——不做"复刻一遍判定"的伪核验。
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, Harness  # noqa: E402
from _harness import run as run_async  # noqa: E402

SOURCE = Path(__file__).resolve().parents[2] / ".runtime" / "astrbot"
STAGE_PATH = SOURCE / "astrbot/core/pipeline/process_stage/stage.py"
AGENT_REQUEST_PATH = SOURCE / "astrbot/core/pipeline/process_stage/method/agent_request.py"


class AgentStub:
    """替换真实 AgentRequestSubStage：只数被调用了几次。

    `leave_result=True` 模拟真实 agent 结束后留下一个未停止的结果。
    """

    def __init__(self, *, leave_result: bool = False) -> None:
        self.calls = 0
        self.leave_result = leave_result

    async def process(self, event):
        self.calls += 1
        if self.leave_result:
            from astrbot.core.message.message_event_result import MessageEventResult

            event.set_result(MessageEventResult().message("(stub)"))
        if False:  # pragma: no cover - 让本方法成为异步生成器
            yield None


class StarStub:
    """替换真实 StarRequestSubStage：按 star_request.py 的语义交出 handler 返回值。"""

    def __init__(self, result: object = None) -> None:
        self.result = result

    async def process(self, event):
        if self.result is not None:
            yield self.result


def build_stage(harness, *, request: object = None, leave_result: bool = False):
    """搭一个只跑真实 process() 控制流的 ProcessStage。"""
    from astrbot.core.pipeline.process_stage.stage import ProcessStage

    stage = ProcessStage()
    stage.ctx = SimpleNamespace(astrbot_config=harness.config)
    stage.star_request_sub_stage = StarStub(request)
    stage.agent_sub_stage = AgentStub(leave_result=leave_result)
    return stage


async def drive(stage, event) -> None:
    """消费 Stage.process 的输出：它是 async def，但带 yield，因此直接返回异步生成器
    （WakingCheckStage 那类无 yield 的才是可 await 的协程），两种形态都要能处理。"""
    outcome = stage.process(event)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    if outcome is None:
        return
    async for _ in outcome:
        pass


def wake_event(harness):
    """构造一个"@ 了机器人"的事件。

    `is_at_or_wake_command` 线上由 WakingCheckStage 设置，这里显式赋值——唤醒矩阵
    本身由 check_gating.py 覆盖。
    """
    event = harness.make_event([harness.At(qq="10001"), harness.Plain("你好")])
    event.is_at_or_wake_command = True
    return event


async def run_flow(
    harness,
    *,
    enabled: bool,
    request: object = None,
    suppress: bool | None = None,
    stop: bool = False,
    leave_result: bool = False,
    activated: bool = False,
) -> int:
    """跑一次真实 ProcessStage.process，返回 agent 子阶段被调用的次数。

    `activated=True` 模拟"有 handler 被激活"——线上由 WakingCheckStage 写入
    `activated_handlers`，ProcessStage 只有看到它才会走 handler 返回值那一支。
    """
    harness.set_config("provider_settings.enable", enabled)
    stage = build_stage(harness, request=request, leave_result=leave_result)
    event = wake_event(harness)
    if activated:
        event.set_extra("activated_handlers", [object()])
    if suppress is not None:
        event.should_call_llm(suppress)
    if stop:
        event.stop_event()
    await drive(stage, event)
    return stage.agent_sub_stage.calls


def make_request():
    from astrbot.core.provider.entities import ProviderRequest

    return ProviderRequest(prompt="你好", system_prompt="你是千鹤")


async def check_stage_flow(checker: Checker, harness: Harness) -> None:
    checker.check("call_llm 初值为 False", wake_event(harness).call_llm is False)

    event = wake_event(harness)
    event.should_call_llm(False)
    checker.check("should_call_llm(False) 不改变初值（K5 修正）", event.call_llm is False)
    event.should_call_llm(True)
    checker.check("should_call_llm(True) 置位", event.call_llm is True)

    default_calls = await run_flow(harness, enabled=True)
    checker.check(
        "默认档：@ + enable=true → 默认路径跑一次（K4 第二条路径存在）",
        default_calls == 1,
        evidence=f"agent 调用 {default_calls} 次",
    )
    checker.check(
        "should_call_llm(True) 抑制默认路径",
        await run_flow(harness, enabled=True, suppress=True) == 0,
    )
    checker.check(
        "should_call_llm(False) 不抑制默认路径",
        await run_flow(harness, enabled=True, suppress=False) == 1,
    )
    checker.check(
        "stop_event() 抑制默认路径（但会挡住回复，不能用于聊天）",
        await run_flow(harness, enabled=True, stop=True) == 0,
    )
    checker.check(
        "留下未停止的结果不能抑制默认路径",
        await run_flow(harness, enabled=True, leave_result=True) == 1,
    )
    suppressed_calls = await run_flow(
        harness,
        enabled=True,
        request=make_request(),
        suppress=True,
        activated=True,
    )
    checker.check(
        "handler 返回 ProviderRequest 且抑制默认路径 → 恰好 1 次",
        suppressed_calls == 1,
        evidence=f"agent 调用 {suppressed_calls} 次",
    )
    double_calls = await run_flow(harness, enabled=True, request=make_request(), activated=True)
    checker.check(
        "handler 返回 ProviderRequest 且不抑制 → 2 次（O-03 双次调用复现）",
        double_calls == 2,
        evidence=f"agent 调用 {double_calls} 次",
    )
    checker.check(
        "enable=false → 默认路径完全关闭（0 次）",
        await run_flow(harness, enabled=False) == 0,
    )
    handler_branch_calls = await run_flow(
        harness,
        enabled=False,
        request=make_request(),
        activated=True,
    )
    checker.check(
        "enable=false 时 handler 分支仍会进入子阶段（真正的短路在 AgentRequestSubStage）",
        handler_branch_calls == 1,
        evidence=f"agent 子阶段被调用 {handler_branch_calls} 次",
    )

    # 真实 AgentRequestSubStage 的入口门：enable=false 时立即返回。
    from astrbot.core.pipeline.process_stage.method.agent_request import AgentRequestSubStage

    sub_stage = AgentRequestSubStage()
    sub_stage.ctx = SimpleNamespace(astrbot_config=harness.config)
    harness.set_config("provider_settings.enable", False)
    yielded = [item async for item in sub_stage.process(wake_event(harness))]
    checker.check(
        "AgentRequestSubStage 在 enable=false 时立即返回",
        yielded == [],
        evidence=str(yielded),
    )
    harness.set_config("provider_settings.enable", True)


def check_sources(checker: Checker) -> None:
    stage_src = STAGE_PATH.read_text()
    checker.check(
        "第二路径的三条件仍在（_has_send_oper / is_at_or_wake_command / not call_llm）",
        "not event._has_send_oper" in stage_src
        and "event.is_at_or_wake_command" in stage_src
        and "not event.call_llm" in stage_src,
    )
    checker.check(
        "第二路径的内层条件仍为（有结果且未停止）或（无结果）",
        "event.get_result() and not event.is_stopped()" in stage_src
        and ") or not event.get_result():" in stage_src,
    )
    checker.check(
        "ProcessStage 中仍有 provider_settings.enable 的检查",
        'if not self.ctx.astrbot_config["provider_settings"].get("enable", True):' in stage_src,
    )
    agent_src = AGENT_REQUEST_PATH.read_text()
    checker.check(
        "AgentRequestSubStage 仍以 provider_settings.enable 为入口门",
        'if not self.ctx.astrbot_config["provider_settings"]["enable"]:' in agent_src,
    )

    writers = [
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.glob("astrbot/**/*.py")
        if re.search(r"self\.call_llm\s*=", path.read_text(errors="ignore"))
    ]
    checker.check(
        "call_llm 只有 should_call_llm 一个写入点",
        writers == ["astrbot/core/platform/astr_message_event.py"],
        evidence=str(writers),
    )


def check_signatures(checker: Checker) -> None:
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.provider.provider import Provider

    llm = importlib.import_module("data.plugins.astrbot_plugin_chizuru.llm")

    request_params = inspect.signature(AstrMessageEvent.request_llm).parameters
    checker.check(
        "event.request_llm 没有任何重试参数",
        not any("retr" in name for name in request_params),
        evidence=str(list(request_params)),
    )

    chat_params = inspect.signature(Provider.text_chat).parameters
    checker.check("Provider.text_chat 接受 request_max_retries", "request_max_retries" in chat_params)
    checker.check("Provider.text_chat 接受 func_tool", "func_tool" in chat_params)
    checker.check(
        "Provider.text_chat 接受 extra_user_content_parts",
        "extra_user_content_parts" in chat_params,
    )

    kwargs = llm.LLMRequestPlan(prompt="你好", system_prompt="你是千鹤", max_retries=1).call_kwargs()
    unknown = sorted(set(kwargs) - set(chat_params))
    checker.check(
        "LLMRequestPlan.call_kwargs 的键都是真实参数",
        not unknown,
        evidence=f"未知参数：{unknown}",
    )
    checker.check(
        "请求计划不含任何工具参数",
        not [key for key in kwargs if "tool" in key],
        evidence=str(sorted(kwargs)),
    )


async def main() -> int:
    checker = Checker("S1-10", "单次模型调用语义（离线部分）")

    with Harness(with_plugin=False) as harness:
        try:
            import astrbot.core.pipeline.process_stage.stage  # noqa: F401
        except BaseException as exc:  # pragma: no cover - 依赖上游导入链
            checker.note(f"无法导入真实 ProcessStage（{exc!r}），跳过控制流用例")
        else:
            await check_stage_flow(checker, harness)
        check_signatures(checker)

    check_sources(checker)
    return checker.report()


if __name__ == "__main__":
    raise SystemExit(run_async(main()))
