"""@ 互动历史的映射、筛选与清理（S2-07）：纯逻辑，不导入框架、不做 IO、不落盘。

**存储归属**：轮次保存在 AstrBot 的会话存储（`ConversationManager`），本模块只处理
"读回来的 JSON 文本"与"要写回去的条列表"。框架侧没有每条消息的时间戳，也不提供
轮数/TTL 裁剪（R16），因此每条条目带一个私有键 ``_at``（epoch 秒）：

- 写入：`make_turn()` 让一问一答同带一个 ``_at``；
- 读取：`parse()` 把成对且带 ``_at`` 的条目还原为 `Turn`，**其余一律保守丢弃**
  （框架自身的 `_checkpoint` 段、缺时间戳的旧数据都不参与）；
- 送往模型前：`flatten()` 剥离 `_at` 等私有键，只留 `role` 与 `content`。

**排除规则**（需求 §4.2、架构 §5.1）：控制命令不进历史（由调用方只在聊天路径写入来保证）；
使用长期记忆的整轮不写共享历史（`should_record()` 的 `memory_assisted` 参数，S3-08 接线）。

时间语义：``_at`` 是**墙上时钟** epoch 秒，与 `context_buffer.BufferEntry.at`（单调时钟）
不是同一种时间，两者不得互换。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

AT_KEY = "_at"
"""时间戳私有键。与框架自身的 `_checkpoint` / `_no_save` 同属"存储里有、请求里没有"的键；
`flatten()` 只放行 `role` 与 `content`，因此任何私有键都不会进入模型请求。"""

_ROLES = ("user", "assistant")


@dataclass(frozen=True)
class Turn:
    """一问一答（同一 `_at`）；`user` / `assistant` 是存储里的原始条目。"""

    at: int
    user: Mapping[str, object]
    assistant: Mapping[str, object]

    def entries(self) -> tuple[dict[str, object], dict[str, object]]:
        """存储形态：键集固定为 `role` / `content` / `_at`，未知字段与框架私有键不写回。"""
        return (
            {"role": "user", "content": self.user.get("content"), AT_KEY: self.at},
            {"role": "assistant", "content": self.assistant.get("content"), AT_KEY: self.at},
        )


def _valid_entry(item: object, role: str) -> bool:
    if not isinstance(item, dict) or item.get("role") != role:
        return False
    content = item.get("content")
    return isinstance(content, str) and bool(content.strip())


def _valid_at(item: Mapping[str, object]) -> int | None:
    at = item.get(AT_KEY)
    if isinstance(at, int) and not isinstance(at, bool):
        return at
    return None


@dataclass(frozen=True)
class _Pair:
    """一对已识别的 `_at` 条目在原始列表中的位置（启动裁剪按位置删除）。"""

    user_index: int
    assistant_index: int
    at: int


class _Timed(Protocol):
    """带 `_at` 的对象：`Turn` 与 `_Pair` 都满足，供保留规则共用。"""

    at: int


def _pair_positions(items: Sequence[object]) -> tuple[_Pair, ...]:
    """识别"带同一 `_at` 的 user→assistant 相邻对"，返回其位置（不复制内容）。

    解析与裁剪共用同一次遍历：**只有这里能决定什么是"本插件的轮次"**。
    """
    pairs: list[_Pair] = []
    pending: tuple[int, int] | None = None  # (index, at)
    for index, item in enumerate(items):
        at = _valid_at(item) if isinstance(item, dict) else None
        if pending is not None and at == pending[1] and _valid_entry(item, "assistant"):
            pairs.append(_Pair(user_index=pending[0], assistant_index=index, at=pending[1]))
            pending = None
            continue
        if at is not None and _valid_entry(item, "user"):
            # 连续两条 user 说明序列已损坏：两条都不采用（保守），后续 assistant 也不配对。
            pending = None if pending is not None else (index, at)
            continue
        pending = None
    return tuple(pairs)


def _decode(raw: str | None) -> list | None:
    """解析会话存储的 JSON 原文；畸形输入返回 `None`（"读不出来"，不是"空"）。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        items = json.loads(raw)
    except ValueError:
        return None
    return items if isinstance(items, list) else None


def parse(raw: str | None) -> tuple[Turn, ...]:
    """把会话存储的 JSON 原文解析成轮次；**任何不合格条目都保守丢弃**。

    只承认"带同一 `_at` 的 user→assistant 相邻对"；畸形 JSON、非列表、缺时间戳、
    单条悬挂、以及框架的 `_checkpoint` 段一律不进入结果。
    """
    items = _decode(raw)
    if items is None:
        return ()
    return tuple(
        Turn(at=pair.at, user=items[pair.user_index], assistant=items[pair.assistant_index])
        for pair in _pair_positions(items)
    )


def _validate_window(*, max_turns: int, ttl_seconds: float) -> None:
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        raise ValueError("max_turns 必须是正整数")
    if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds 必须是正数")


def _keep_last(
    entries: Sequence[_Timed],
    *,
    max_turns: int,
    ttl_seconds: float,
    now_epoch: int,
) -> list[_Timed]:
    """有效期内的最近 `max_turns` 项；**到期判定取严**（年龄恰等 TTL 即过期）。

    解析（`Turn`）与存储裁剪（`_Pair`）共用这一条规则，避免两侧漂移。
    """
    fresh = [entry for entry in entries if now_epoch - entry.at < ttl_seconds]
    return fresh[-max_turns:]


def trim_stored(
    raw: str | None,
    *,
    max_turns: int,
    ttl_seconds: float,
    now_epoch: int,
) -> tuple[dict, ...] | None:
    """启动裁剪的**写回计划**（S2-08）：返回新的条列表，`None` 表示无需改动。

    只移除过期或超轮的 `_at` 成对条目；**未识别条目原样保留**——同一张会话表里可能
    有框架的 `_checkpoint`、别的来源、或没有 `_at` 的历史，裁剪不得顺手删掉它们。
    一个可识别的轮次都没裁掉时返回 `None`（绝不因为"解析不出来"就整体覆盖）。
    """
    items = _decode(raw)
    if items is None:
        return None
    _validate_window(max_turns=max_turns, ttl_seconds=ttl_seconds)
    pairs = _pair_positions(items)
    if not pairs:
        return None
    kept = {pair.user_index for pair in _keep_last(pairs, max_turns=max_turns, ttl_seconds=ttl_seconds, now_epoch=now_epoch)}
    dropped = {
        index
        for pair in pairs
        if pair.user_index not in kept
        for index in (pair.user_index, pair.assistant_index)
    }
    if not dropped:
        return None
    return tuple(item for index, item in enumerate(items) if index not in dropped)


def select(
    turns: Sequence[Turn],
    *,
    max_turns: int,
    ttl_seconds: float,
    now_epoch: int,
) -> tuple[Turn, ...]:
    """保留有效期内的最近 `max_turns` 轮；**到期判定取严**（年龄恰等 TTL 即过期）。"""
    _validate_window(max_turns=max_turns, ttl_seconds=ttl_seconds)
    return tuple(_keep_last(turns, max_turns=max_turns, ttl_seconds=ttl_seconds, now_epoch=now_epoch))


def make_turn(*, user_text: str, assistant_text: str, at_epoch: int) -> Turn:
    """构造要写回会话存储的一轮；两条条目共享同一个墙上时间戳。"""
    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("user_text 必须是非空文本")
    if not isinstance(assistant_text, str) or not assistant_text.strip():
        raise ValueError("assistant_text 必须是非空文本")
    if not isinstance(at_epoch, int) or isinstance(at_epoch, bool):
        raise ValueError("at_epoch 必须是整数秒")
    return Turn(
        at=at_epoch,
        user={"role": "user", "content": user_text},
        assistant={"role": "assistant", "content": assistant_text},
    )


def storage_entries(turns: Sequence[Turn]) -> tuple[dict[str, object], ...]:
    """按存储形态展开轮次：写入会话存储用的条列表（时间升序）。"""
    return tuple(entry for turn in turns for entry in turn.entries())


def flatten(turns: Sequence[Turn]) -> tuple[dict[str, str], ...]:
    """转成 `contexts` 条目：只留 `role` 与 `content`，`_at` 等私有键与未知字段一律剥离。"""
    contexts: list[dict[str, str]] = []
    for turn in turns:
        for role, item in zip(_ROLES, (turn.user, turn.assistant)):
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            contexts.append({"role": role, "content": content})
    return tuple(contexts)


def should_record(*, memory_assisted: bool, delivered: bool) -> bool:
    """这一轮是否写进共享互动历史：**已送达**且**不是记忆辅助轮**（需求 §4.3）。

    控制命令的排除不在这里——它由调用方只在聊天路径调用本函数保证（架构 §4.4）。
    """
    return bool(delivered) and not bool(memory_assisted)
