"""S0-03 非唤醒群消息可采集且不触发模型（离线部分）。

要证明的是：普通群消息（没有 @）能被本插件的宽 filter 观测到，用于写入群缓冲，
同时不产生任何模型调用或出站。

关键点：WakingCheckStage 会因为"有 handler 的 filter 通过"把事件标记为 wake（K1）。
这在采集场景下是必要副作用——但不能连带放出模型调用。本脚本验证：
- 观测可行（handler 被激活并执行）
- 不以 is_at_or_wake_command 的形式获得模型调用资格（ProcessStage 第二条 LLM 路径的门槛）
- 全程零模型调用、零出站

不覆盖：真实入库缓冲（S2-03 才实现）、真实群消息回放。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, Harness  # noqa: E402
from _harness import run as run_async  # noqa: E402

PLUGIN_SETTINGS = {
    "platform_id": "qq-local",
    "self_id": "10001",
    "allowed_group_ids": ["20001"],
}


async def probe(h: Harness, chain: list, instances: dict, **kwargs) -> dict:
    event = h.make_event(chain, **kwargs)
    sends = h.attach_sink(event)
    llm_calls = h.attach_llm_recorder(event)
    await h.run_waking(event)
    await h.run_pipeline_handlers(event, instances)
    return {
        "wake": event.is_wake,
        "at_or_wake": event.is_at_or_wake_command,
        "call_llm": event.call_llm,
        "stopped": event.is_stopped(),
        "sends": len(sends),
        "llm": len(llm_calls),
        "executed": event.get_extra("s0.executed_handlers") or [],
        "skipped": event.get_extra("s0.skipped_handlers") or [],
    }


async def main() -> int:
    checker = Checker("S0-03", "非唤醒群消息可采集且不触发模型（离线部分）")

    with Harness() as h:
        h.load_builtins()
        instances, failures = h.build_instances(PLUGIN_SETTINGS)
        for item in failures:
            checker.note(f"Star 实例构造失败：{item}")
        h.apply_closed_config()
        await h.init_waking()

        plain = await probe(h, [h.Plain("今天午饭吃什么")], instances)
        print(f"  普通群消息观测：{plain}")

        checker.check(
            "普通群消息能被本插件 handler 观测到（采集可行性）",
            "data.plugins.astrbot_plugin_chizuru.main_on_message" in plain["executed"],
            evidence=f"executed={plain['executed']}",
        )

        checker.check(
            "普通群消息不以 is_at_or_wake_command 获得模型调用资格",
            plain["at_or_wake"] is False,
            evidence=f"is_at_or_wake_command={plain['at_or_wake']}",
        )

        checker.check(
            "普通群消息不产生模型调用",
            plain["llm"] == 0,
            evidence=f"llm={plain['llm']}",
        )

        checker.check(
            "普通群消息不产生出站",
            plain["sends"] == 0,
            evidence=f"sends={plain['sends']}",
        )

        # 2026-09-18 修正（K5）：`call_llm=False` 是无效调用，不抑制任何链路；真正关闭
        # 框架默认 LLM 路径的是 `should_call_llm(True)`（置 call_llm=True）。本脚本原断言
        # 期望 call_llm=False（S0 时点的骨架姿态），按修正后的语义改判据——判据变强而非变弱。
        checker.check(
            "本插件显式关闭框架默认 LLM 链路（should_call_llm(True) → call_llm=True）",
            plain["call_llm"] is True,
            evidence=f"call_llm={plain['call_llm']}",
        )

        # K1 是已知且被接受的副作用：采集依赖它，但它本身不构成模型调用。
        checker.check(
            "K1 副作用存在但无害（wake=True 却无模型调用）",
            plain["wake"] is True and plain["llm"] == 0,
            evidence=f"wake={plain['wake']} llm={plain['llm']}",
        )

        checker.note(
            "→ 结论：宽 filter 采集可行，但采集路径必须自己保证不请求模型；"
            "ProcessStage 第二条 LLM 路径的门槛是 is_at_or_wake_command，普通群消息不满足",
        )

        # 对照：真正被 @ 时才应获得模型调用资格（S1 才会实现调用，这里只验证门槛差异）。
        mention = await probe(h, [h.At(qq="10001"), h.Plain("你好")], instances)
        checker.check(
            "@ 机器人才获得模型调用资格",
            mention["at_or_wake"] is True,
            evidence=f"is_at_or_wake_command={mention['at_or_wake']}",
        )

        if plain["skipped"]:
            checker.note(f"未执行处理器：{plain['skipped']}")

    return checker.report()


if __name__ == "__main__":
    raise SystemExit(run_async(main()))
