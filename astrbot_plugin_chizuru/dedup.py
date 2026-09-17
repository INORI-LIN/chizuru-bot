"""事件与动作去重：同一事件不重复回复，也不重复产生记忆任务。

**本模块只保留标识与状态，不保存任何正文。** 键由可信事件元数据（平台连接
实例 + 群 + message_id）与动作类别构成（架构 §6.1 去重元数据），值只有三种：
处理中、已完成、发送不确定。不做持久化——按有界窗口持久化属 S2。

两条上界都必须成立，禁止用"无限保存"代替上界：

- **窗口由调用方显式注入，模块内不设默认值。** 取值依赖 S0-08 / O-07 的在线
  结论（message_id 是否稳定、重连回放窗口多长，见 docs/03 风险 R10）；在那之前
  发明一个数字只会把未核验的假设伪装成参数。
- 容量上限：满时按插入顺序淘汰最旧项。TTL 按插入顺序惰性清除，均摊 O(1)。

纪律：

- ``begin()`` 是原子的"查 + 占用"：单事件循环内没有 await 间隙，两个并发事件
  竞争同一键时只有一个拿到 ``FIRST``。
- ``SEND_UNCERTAIN`` 与 ``DONE`` 同样不重发（架构 §8.2"对发送不确定状态不盲目
  重发，不虚称 exactly-once"），但状态可区分、可观察。
- ``release()`` 是唯一的逃生口，只允许在**确认未产生任何副作用**时使用（例如
  任务被队列或预算拒绝）。否则重连回放会让用户永远得不到回复。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .keys import GroupKey

_ONE_MILLISECOND = 0.001


class ActionKind(StrEnum):
    """同一条消息可以分别对聊天与抽取去重，互不牵连。"""

    CHAT_REPLY = "chat_reply"
    MEMORY_EXTRACT = "memory_extract"


class Claim(StrEnum):
    """``begin()`` 的结果：只有 ``FIRST`` 允许继续执行。"""

    FIRST = "first"
    """首次出现：已占用，接下来必须 finish 或 release。"""

    IN_FLIGHT = "in_flight"
    DONE = "done"
    UNCERTAIN = "uncertain"


class Outcome(StrEnum):
    COMPLETED = "completed"
    """已产生确定结果（发送成功、写入完成等）。"""

    SEND_UNCERTAIN = "send_uncertain"
    """发送结果不确定：不盲目重发，状态保留到窗口结束。"""


class _State(StrEnum):
    IN_FLIGHT = "in_flight"
    DONE = "done"
    UNCERTAIN = "uncertain"


_CLAIM_OF = {
    _State.IN_FLIGHT: Claim.IN_FLIGHT,
    _State.DONE: Claim.DONE,
    _State.UNCERTAIN: Claim.UNCERTAIN,
}


@dataclass(frozen=True)
class DedupKey:
    """平台连接实例 + 群 + message_id + 动作类别。

    四段都来自可信事件元数据；不带昵称、文本或任何内容字段。
    """

    group: GroupKey
    message_id: str
    action: ActionKind

    def __post_init__(self) -> None:
        if not isinstance(self.group, GroupKey):
            raise ValueError("DedupKey 需要 GroupKey")
        if not isinstance(self.message_id, str) or not self.message_id:
            raise ValueError("DedupKey 需要非空 message_id")
        if self.message_id != self.message_id.strip():
            raise ValueError("message_id 不得带首尾空白")
        if not isinstance(self.action, ActionKind):
            raise ValueError("DedupKey 需要 ActionKind")


@dataclass(frozen=True)
class DedupStats:
    size: int
    capacity: int
    window_seconds: float
    in_flight: int
    done: int
    uncertain: int
    evicted: int


@dataclass
class _Entry:
    state: _State
    recorded_at: float


class DedupStore:
    """有界的内存去重表。所有方法都是同步的，只在事件循环内使用。"""

    def __init__(
        self,
        *,
        window_seconds: float,
        capacity: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(window_seconds, (int, float)) or isinstance(window_seconds, bool):
            raise ValueError("window_seconds 必须是数字")
        if not window_seconds >= _ONE_MILLISECOND:
            raise ValueError("window_seconds 必须为正（毫秒级下限）")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity 必须是正整数")
        self._window_seconds = float(window_seconds)
        self._capacity = capacity
        self._clock = clock
        self._entries: dict[DedupKey, _Entry] = {}
        self._evicted = 0

    def begin(self, key: DedupKey) -> Claim:
        """查询并占用；只有返回 ``FIRST`` 时调用方才能执行动作。"""
        now = self._clock()
        self._purge_expired(now)
        entry = self._entries.get(key)
        if entry is not None:
            return _CLAIM_OF[entry.state]
        self._entries[key] = _Entry(_State.IN_FLIGHT, now)
        self._evict_overflow()
        return Claim.FIRST

    def finish(self, key: DedupKey, outcome: Outcome = Outcome.COMPLETED) -> bool:
        """记录终态。条目已过窗或被淘汰时返回 False，不做任何补救。"""
        entry = self._entries.get(key)
        if entry is None:
            return False
        entry.state = _State.DONE if outcome is Outcome.COMPLETED else _State.UNCERTAIN
        return True

    def release(self, key: DedupKey) -> bool:
        """撤销占用。**仅限确认未产生任何副作用时**（见模块文档）。"""
        return self._entries.pop(key, None) is not None

    def purge(self) -> int:
        """清除过期条目，返回清除数量。"""
        before = len(self._entries)
        self._purge_expired(self._clock())
        return before - len(self._entries)

    def stats(self) -> DedupStats:
        counts = {state: 0 for state in _State}
        for entry in self._entries.values():
            counts[entry.state] += 1
        return DedupStats(
            size=len(self._entries),
            capacity=self._capacity,
            window_seconds=self._window_seconds,
            in_flight=counts[_State.IN_FLIGHT],
            done=counts[_State.DONE],
            uncertain=counts[_State.UNCERTAIN],
            evicted=self._evicted,
        )

    def _purge_expired(self, now: float) -> None:
        while self._entries:
            key = next(iter(self._entries))
            if now - self._entries[key].recorded_at < self._window_seconds:
                return
            del self._entries[key]

    def _evict_overflow(self) -> None:
        while len(self._entries) > self._capacity:
            del self._entries[next(iter(self._entries))]
            self._evicted += 1
