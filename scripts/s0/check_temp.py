"""S0-04 临时材料不持久化（离线部分）。

核验两层机制：
1. ContentPart.mark_as_temp()  —— 把注入片段从持久化内容里剔除（part 级）
2. Message._no_save = True     —— 让 _save_to_history 整条跳过（message 级）

结论：单靠 mark_as_temp() 只能剔除注入文本，本轮的用户提问与助手回复仍会入库；
要做到"记忆辅助整轮不入共享历史"，两层都必须用。

不覆盖：真实 pipeline 里 ProcessStage → DB 的端到端落库、重启后复查。
_save_to_history 因上游循环导入无法离线导入，这里按 internal.py:574-582 的条件
在真实 Message 对象上复现该过滤，并另由 check_outbound.py 钉住源码条件本身。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import Checker, Harness  # noqa: E402
from _harness import run as run_async  # noqa: E402

# 与 internal.py:574-582 一致的条件；check_outbound.py 负责钉住该源码行仍存在。
def save_filter(messages: list) -> list:
    kept = []
    skipped_initial_system = False
    for message in messages:
        if message.role == "system" and not skipped_initial_system:
            skipped_initial_system = True
            continue
        if message.role in ["assistant", "user"] and message._no_save:
            continue
        kept.append(message)
    return kept


async def main() -> int:
    checker = Checker("S0-04", "临时材料不持久化（离线部分）")

    with Harness(with_plugin=False) as h:
        from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints

        # ---- 第 1 层：ContentPart 级 ----
        injected = TextPart(text="SECRET-MEMORY").mark_as_temp()
        checker.check("mark_as_temp() 置位 ContentPart._no_save", injected._no_save is True)

        user_msg = Message(
            role="user",
            content=[TextPart(text="用户本轮提问"), injected],
        )
        checker.check(
            "part 标记不会传播为 Message._no_save",
            user_msg._no_save is False,
            evidence=f"Message._no_save={user_msg._no_save}",
        )

        dumped = dump_messages_with_checkpoints([user_msg])
        content_text = str(dumped)
        checker.check(
            "持久化内容中不含被标记的注入片段",
            "SECRET-MEMORY" not in content_text,
            evidence=content_text[:160],
        )
        checker.check(
            "用户本轮提问仍留在持久化内容中（单靠 part 级不够）",
            "用户本轮提问" in content_text,
            evidence=content_text[:160],
        )

        # ---- 第 2 层：Message 级 ----
        assistant_msg = Message(role="assistant", content=[TextPart(text="机器人本轮回复")])
        assistant_msg._no_save = True
        kept = save_filter([user_msg, assistant_msg])
        checker.check(
            "Message._no_save=True 时 _save_to_history 整条跳过",
            all(m is not assistant_msg for m in kept),
            evidence=f"保留 {len(kept)} 条",
        )

        # 只有两层同时使用时，整轮才完全不落库。
        user_msg._no_save = True
        kept = save_filter([user_msg, assistant_msg])
        checker.check(
            "两层同时使用时整轮不落库（user + assistant 都被跳过）",
            kept == [],
            evidence=f"保留 {len(kept)} 条",
        )

        # ---- 该标记无法经序列化传入 ----
        plain = Message(role="user", content=[TextPart(text="x")])
        plain._no_save = True
        serialized_has_flag = "_no_save" in str(plain.model_dump())
        checker.check(
            "Message._no_save 是 PrivateAttr，不参与序列化",
            not serialized_has_flag,
            evidence="若可序列化，就必须重新评估能否通过 req.contexts 注入",
        )
        checker.note(
            "因此 message 级标记必须在活对象上设置（on_agent_done 里对 run_context.messages 操作），"
            "不能通过构造 dict 传入",
        )

        # ---- 注入通道 ----
        from astrbot.core.provider.entities import ProviderRequest

        request_fields = getattr(ProviderRequest, "model_fields", None) or getattr(
            ProviderRequest,
            "__annotations__",
            {},
        )
        checker.check(
            "ProviderRequest 提供 extra_user_content_parts 注入通道",
            "extra_user_content_parts" in request_fields,
            evidence=str(list(request_fields))[:200],
        )

    return checker.report()


if __name__ == "__main__":
    raise SystemExit(run_async(main()))
