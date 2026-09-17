"""S0-02 全框架严格 @ 可行性（离线部分）。

证明在"只启用本插件 + 必关配置"下，非 @ 事件不产生任何出站与模型调用；
并记录默认配置下的泄漏点，作为必关清单（附录 D.2）的依据。

不覆盖：真实 NapCat 投递、第三方插件、cron 任务、真实延迟下的空 @ 等待。
"""

from __future__ import annotations

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from _harness import Checker, Harness  # noqa: E402
from _harness import run as run_async  # noqa: E402

PLUGIN_SETTINGS = {
    "platform_id": "qq-local",
    "self_id": "10001",
    "allowed_group_ids": ["20001"],
}


def cases(h: Harness) -> list[tuple[str, list, dict]]:
    return [
        ("非 @ 群消息", [h.Plain("今天天气不错")], {}),
        ("@ 机器人", [h.At(qq="10001"), h.Plain("你好")], {}),
        ("空 @", [h.At(qq="10001")], {}),
        ("@全体", [h.AtAll(), h.Plain("集合")], {}),
        ("@全体 + @机器人", [h.AtAll(), h.At(qq="10001"), h.Plain("集合")], {}),
        (
            "引用内 @",
            [h.Reply(id="old", chain=[h.At(qq="10001"), h.Plain("旧")]), h.Plain("新")],
            {},
        ),
        ("私聊", [h.Plain("你好")], {"private": True}),
        ("机器人自身消息", [h.Plain("我自己说的")], {"sender_id": "10001"}),
        ("未允许群", [h.At(qq="10001"), h.Plain("你好")], {"group_id": "99999"}),
        ("@ 后仅图片", [h.At(qq="10001"), h.Image(file="unused.png")], {}),
        ("非 @ 但以唤醒前缀开头", [h.Plain("/help")], {}),
    ]


async def observe(h: Harness, name: str, chain: list, kwargs: dict, instances: dict) -> dict:
    """跑一次完整门控：WakingCheckStage → activated_handlers → 处理器调用。

    "会被投递"包含两种形态：处理器直接 send（sends），或在事件上留下 result 交给
    RespondStage 发送（result）。只看 sends 会漏掉后者。
    """
    event = h.make_event(chain, **kwargs)
    sends = h.attach_sink(event)
    llm_calls = h.attach_llm_recorder(event)
    await h.run_waking(event)
    await h.run_pipeline_handlers(event, instances)
    return {
        "name": name,
        "wake": event.is_wake,
        "at_or_wake": event.is_at_or_wake_command,
        "stopped": event.is_stopped(),
        "sends": len(sends),
        "llm": len(llm_calls),
        "result": event.get_result() is not None,
        "deliverable": len(sends) > 0 or (event.get_result() is not None and not event.is_stopped()),
        "skipped": event.get_extra("s0.skipped_handlers") or [],
    }


async def main() -> int:
    checker = Checker("S0-02", "全框架严格 @ 可行性（离线部分）")

    with Harness() as h:
        h.load_builtins()
        instances, failures = h.build_instances(PLUGIN_SETTINGS)
        if failures:
            for item in failures:
                checker.note(f"Star 实例构造失败，对应处理器不会被执行：{item}")

        # --- 配置档 1：默认配置。记录泄漏点（不判失败），用于支撑必关清单。 ---
        await h.init_waking()
        default_rows = []
        for name, chain, kwargs in cases(h):
            default_rows.append(await observe(h, name, chain, kwargs, instances))
        print("  默认配置下的观测（仅记录，不判失败）：")
        for row in default_rows:
            print(
                f"    {row['name']:<20} wake={row['wake']!s:<5} at_or_wake={row['at_or_wake']!s:<5}"
                f" stopped={row['stopped']!s:<5} sends={row['sends']} llm={row['llm']}"
                f" result={row['result']}",
            )

        leaked = [r["name"] for r in default_rows if r["deliverable"] or r["llm"]]
        checker.check(
            "默认配置下存在泄漏（证明必关清单是必要的）",
            bool(leaked),
            evidence="默认配置未观测到任何泄漏，可能是矩阵失效",
        )
        checker.note(f"默认配置下的泄漏项：{leaked or '无'}")

        # --- 配置档 2：必关配置。这才是 S0-02 的结论所在。 ---
        h.apply_closed_config()
        await h.init_waking()
        for name, chain, kwargs in cases(h):
            row = await observe(h, name, chain, kwargs, instances)
            label = f"必关配置 · {name}"
            checker.check(
                f"{label} → 无出站、无模型调用、无可投递结果",
                row["sends"] == 0 and row["llm"] == 0 and not row["deliverable"],
                evidence=f"sends={row['sends']} llm={row['llm']} result={row['result']}",
            )
            if row["skipped"]:
                checker.note(f"{label} 有未执行处理器：{row['skipped']}")

    return checker.report()


if __name__ == "__main__":
    raise SystemExit(run_async(main()))
