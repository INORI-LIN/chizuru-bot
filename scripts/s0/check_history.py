"""S0-05 原生历史 TTL 与删除接口核实。

在隔离 ASTRBOT_ROOT 下建真实 SQLite 库，核实：
- 会话与平台消息历史都没有原生过期机制（插件必须自建保留策略）
- unique_session 关闭时 umo 只含群，任何按 umo 的删除都是整群范围
- 原生不提供成员级删除
- 20 轮上限依赖显式配置（max_turns 默认 -1，即不限）

不覆盖：真实运行库的删除与重启后复查（需求一三条在线前置）。
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import SOURCE, Checker, Harness  # noqa: E402
from _harness import run as run_async  # noqa: E402

GROUP_UMO = "aiocqhttp:GroupMessage:20001"
OTHER_GROUP_UMO = "aiocqhttp:GroupMessage:29999"

# 整词匹配：避免 delete_at 命中 delete_attachment、expire 命中 expire_on_commit。
EXPIRY_RE = re.compile(
    r"\b(expire|expires|expiry|expired|expires_at|expire_at|ttl|retention|purge|cleanup|clean_up|deleted_at)\b",
    re.IGNORECASE,
)


async def main() -> int:
    checker = Checker("S0-05", "原生历史 TTL 与删除接口核实")

    with Harness(with_plugin=False) as h:
        from astrbot.core.conversation_mgr import ConversationManager
        from astrbot.core.db.po import ConversationV2, PlatformMessageHistory

        # ---- 1. 无原生 TTL ----
        for model, label in (
            (ConversationV2, "conversations"),
            (PlatformMessageHistory, "platform_message_history"),
        ):
            columns = [c.name for c in model.__table__.columns]
            suspicious = [c for c in columns if EXPIRY_RE.search(c)]
            checker.check(
                f"{label} 表无过期语义列",
                not suspicious,
                evidence=f"可疑列 {suspicious}；全部列 {columns}",
            )

        # 只有创建/更新时间，没有过期时间。
        checker.check(
            "conversations 表仅有 TimestampMixin 的时间列",
            {"created_at", "updated_at"}.issubset(
                {c.name for c in ConversationV2.__table__.columns},
            ),
        )

        # 数据库抽象层：没有过期/清理类接口。
        # 注意不要用源码关键词模糊匹配——SQLAlchemy 的 expire_on_commit 会误伤。
        db_init = (SOURCE / "astrbot/core/db/__init__.py").read_text()
        method_names = re.findall(r"^\s*(?:async )?def (\w+)", db_init, re.MULTILINE)
        expiry_methods = [name for name in method_names if EXPIRY_RE.search(name)]
        checker.check(
            "BaseDatabase 未暴露过期/清理类接口",
            not expiry_methods,
            evidence=f"命中 {expiry_methods}；全部方法 {len(method_names)} 个",
        )

        # ---- 2. 真实库：删除作用域为整群 ----
        await h.core.db_helper.initialize()
        manager = ConversationManager(h.core.db_helper)

        ids = [
            await manager.new_conversation(GROUP_UMO, platform_id="aiocqhttp"),
            await manager.new_conversation(GROUP_UMO, platform_id="aiocqhttp"),
        ]
        other_id = await manager.new_conversation(OTHER_GROUP_UMO, platform_id="aiocqhttp")
        checker.check(
            "同一群 umo 下可存在多条会话（模拟群共享会话）",
            len(set(ids)) == 2,
            evidence=str(ids),
        )

        rows = await h.core.db_helper.get_conversations(GROUP_UMO)
        user_ids = {row.user_id for row in rows}
        checker.check(
            "会话行的 user_id 就是 umo，不含任何成员标识",
            user_ids == {GROUP_UMO},
            evidence=f"user_id={user_ids}；umo={GROUP_UMO}",
        )

        await manager.delete_conversations_by_user_id(GROUP_UMO)
        remaining = await h.core.db_helper.get_conversations(GROUP_UMO)
        checker.check(
            "delete_conversations_by_user_id(umo) 清空该群全部会话",
            remaining == [],
            evidence=f"残留 {len(remaining)} 条",
        )
        still_there = await h.core.db_helper.get_conversations(OTHER_GROUP_UMO)
        checker.check(
            "删除不波及其他群（群间隔离成立）",
            len(still_there) == 1,
            evidence=f"其他群残留 {len(still_there)} 条",
        )
        checker.note(
            "→ 原生无成员级删除：要满足「退出后清本人历史」，插件必须自行改写 history 内容"
            "或另存成员级数据，不能依赖原生接口",
        )

        # ---- 3. umo 只含群 ----
        checker.check(
            "umo 形如 platform:GroupMessage:group_id，不含成员 ID",
            GROUP_UMO.count(":") == 2 and "30001" not in GROUP_UMO,
            evidence=GROUP_UMO,
        )

        # ---- 4. 轮数上限需显式配置 ----
        compression = h.config["agent_runner"]["config"]["compression"]
        checker.check(
            "max_turns 默认 -1（不限轮数），20 轮上限必须显式配置",
            compression["max_turns"] == -1,
            evidence=f"max_turns={compression['max_turns']} trim_turns={compression['trim_turns']}",
        )

        # ---- 5. 平台消息历史删除的时间窗语义 ----
        from astrbot.core.platform_message_history_mgr import PlatformMessageHistoryManager

        sig = inspect.signature(PlatformMessageHistoryManager.delete)
        checker.check(
            "PlatformMessageHistoryManager.delete 删除的是最近窗口而非过期数据",
            "offset_sec" in sig.parameters and sig.parameters["offset_sec"].default == 86400,
            evidence=str(sig),
        )
        checker.note(
            "→ 该方法名易误导：默认只删最近 24 小时，不是「清理过期」",
        )

    return checker.report()


if __name__ == "__main__":
    raise SystemExit(run_async(main()))
