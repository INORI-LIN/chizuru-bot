"""S0-06 出站路径全覆盖审计（离线部分）。

两件事：
1. 漂移检测——把上游全部出站调用点做指纹固化。上游升级导致发送面变化时本脚本
   会失败，强制重新审计，避免附录 D.1 的清单悄悄失效。
2. 假设钉住——本项目的设计与文档依赖若干上游行为，这里逐个断言它们仍然成立。

不覆盖：第三方插件、cron 任务、真实 NapodCat 投递、运行时才出现的发送路径。
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / ".runtime" / "astrbot"
PKG = SOURCE / "astrbot"

# 出站调用点模式：覆盖 event.send / 流式发送 / 平台适配器直发 / 会话直发。
SEND_PATTERNS = (
    r"await event\.send\(",
    r"event\.send\(",
    r"send_streaming\(",
    r"send_stream\(",
    r"context\.send_message\(",
    r"context_obj\.send_message\(",
    r"send_by_session\(",
    r"bot\.send_group_msg",
    r"bot\.send_private_msg",
)
_SEND_RE = re.compile("|".join(SEND_PATTERNS))

# 受控变更：上游升级后重新审计，确认无新增未审计出站路径，再用 --print-digest 更新。
EXPECTED_DIGEST = "5cf8044ad32970d9ed0c36e09b44d02b173c4e9abf7c795e081c00f39d5e917f"
EXPECTED_TOTAL = 123


def scan_send_sites() -> tuple[list[tuple[str, int]], int]:
    counts: dict[str, int] = {}
    total = 0
    for path in sorted(PKG.rglob("*.py")):
        if ".venv" in path.parts:
            continue
        hits = 0
        for line in path.read_text(errors="replace").splitlines():
            if _SEND_RE.search(line):
                hits += 1
        if hits:
            counts[str(path.relative_to(SOURCE))] = hits
            total += hits
    return sorted(counts.items()), total


def digest_of(inventory: list[tuple[str, int]]) -> str:
    payload = "\n".join(f"{path}\t{count}" for path, count in inventory)
    return hashlib.sha256(payload.encode()).hexdigest()


def main() -> int:
    failures: list[str] = []
    passed = 0

    def check(name: str, condition: bool, evidence: str = "") -> None:
        nonlocal passed
        if condition:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}" + (f"  <- {evidence}" if evidence else ""))

    print("[S0-06] 出站路径全覆盖审计（离线部分）")

    inventory, total = scan_send_sites()
    digest = digest_of(inventory)

    if "--print-digest" in sys.argv:
        print(f"  digest = {digest}")
        print(f"  total  = {total}")
        print(f"  files  = {len(inventory)}")
        for path, count in inventory:
            print(f"    {count:>3}  {path}")
        return 0

    check(
        f"出站调用点总数仍为 {EXPECTED_TOTAL}",
        total == EXPECTED_TOTAL,
        evidence=f"实际 {total}",
    )
    check(
        "出站调用点分布指纹未变",
        digest == EXPECTED_DIGEST,
        evidence=f"实际 {digest[:16]}；若为受控升级，请重新审计后用 --print-digest 更新",
    )

    # ---- 假设钉住：本项目设计与文档依赖的上游行为 ----
    waking = (PKG / "core/pipeline/waking_check/stage.py").read_text()
    check(
        "WakingCheckStage 仍存在 filter 异常时直接发送",
        "await event.send(" in waking,
        evidence="waking_check/stage.py",
    )
    check(
        "WakingCheckStage 仍在 no_permission_reply 时发送",
        "no_permission_reply" in waking,
        evidence="waking_check/stage.py",
    )
    check(
        "任一 handler filter 通过即置 is_wake（K1）",
        "is_wake = True" in waking,
        evidence="waking_check/stage.py",
    )

    star_handler = (PKG / "core/star/star_handler.py").read_text()
    check(
        "保留插件豁免 plugin_set 收窄的规则仍在",
        "not plugin.reserved" in star_handler,
        evidence="star_handler.py:169-186",
    )

    session_mgr = (PKG / "core/star/session_plugin_manager.py").read_text()
    check(
        "name 为空的插件其 handler 仍会被丢弃",
        "if plugin.name is None:" in session_mgr,
        evidence="session_plugin_manager.py:89-90",
    )

    stage_order = (PKG / "core/pipeline/stage_order.py").read_text()
    check(
        "STAGES_ORDER 仍是固定列表（K7：插件无法插入自定义 Stage）",
        "STAGES_ORDER = [" in stage_order,
        evidence="stage_order.py:3",
    )

    internal = (
        PKG / "core/pipeline/process_stage/method/agent_sub_stages/internal.py"
    ).read_text()
    check(
        "_save_to_history 仍只认消息级 _no_save（K6/S0-04 依赖）",
        'if message.role in ["assistant", "user"] and message._no_save:' in internal,
        evidence="internal.py:580",
    )

    message_py = (PKG / "core/agent/message.py").read_text()
    check(
        "mark_as_temp 仍只作用于 ContentPart",
        "def mark_as_temp" in message_py and "_no_save: bool = PrivateAttr(default=False)" in message_py,
        evidence="agent/message.py:25/68/215",
    )

    builtin = (PKG / "builtin_stars/astrbot/main.py").read_text()
    check(
        "保留内置插件的空 @ 处理器仍由 empty_mention_waiting 控制（S0-02 依赖）",
        'p_settings.get("empty_mention_waiting", True)' in builtin,
        evidence="builtin_stars/astrbot/main.py:67",
    )
    check(
        "空 @ 处理器仍会发起模型调用",
        "yield event.request_llm(" in builtin,
        evidence="builtin_stars/astrbot/main.py:99-109",
    )

    conv = (PKG / "core/conversation_mgr.py").read_text()
    check(
        "会话删除接口仍在（K9）",
        "async def delete_conversation(" in conv and "async def delete_conversations_by_user_id(" in conv,
        evidence="conversation_mgr.py:139/160",
    )

    history = (PKG / "core/platform_message_history_mgr.py").read_text()
    check(
        "平台消息历史删除接口仍在（K9）",
        "async def delete(" in history and "async def delete_by_id(" in history,
        evidence="platform_message_history_mgr.py:140/163",
    )

    default = (PKG / "core/config/default.py").read_text()
    check(
        "关键默认值未被上游改动（空 @ 等待、私聊唤醒、@全体）",
        '"empty_mention_waiting": True' in default
        and '"friend_message_needs_wake_prefix": False' in default
        and '"ignore_at_all": False' in default,
        evidence="config/default.py:103-107",
    )

    total_checks = passed + len(failures)
    if failures:
        print(f"S0-06: {passed}/{total_checks} PASS，失败项：{', '.join(failures)}")
        return 1
    print(f"S0-06: {passed}/{total_checks} PASS（出站调用点 {total} 处 / {len(inventory)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
