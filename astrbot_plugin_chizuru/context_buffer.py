"""普通群聊环形缓冲：纯内存、有界、可失效（R-CTX）。

只保存**近期普通群聊文本**，供有效 @ 时理解共同话题。本模块不发消息、不请求模型、
不落盘，也不持有后台任务——条数与 TTL 在写入、读取与清理时惰性淘汰。

排除规则在 ``ingest`` 处一次性表达（架构 §4.2、需求 §4.1/§4.2）：

- 非 ``TEXT_ONLY`` 形状一律不收（@、附件、可识别引用、合并转发等）；
- 命令文本不收（复用 ``commands.parse``，不另立一份指令表）；
- 机器人自身输出不收（调用方已由 ``is_trusted_scope`` 排除，这里是第二道）；
- 明显敏感文本**整条丢弃**（宁可少采，不做局部脱敏）；
- 同群重复 ``message_id`` 不收。

清理入口（``clear_group`` / ``clear_member`` / ``clear_all``）由调用方在退出、暂停
与关闭时触发：退出在 S2-04 已接线，暂停/关闭随 S2-05 的命令落地。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

from .commands import is_command
from .keys import GroupKey, MemberKey

MAX_TEXT_LENGTH = 300
"""单条文本上限（字符）。**建议参数待评审**：架构 §4.2 只要求"有限长度直接文本"。"""

SENSITIVE_KEYWORDS = (
    "验证码",
    "密码",
    "口令",
    "身份证",
    "银行卡",
    "信用卡",
    "住址",
    "手机号",
)
"""敏感关键词。**建议参数待评审**：命中即整条丢弃。"""

_SENSITIVE_PATTERN = re.compile(
    r"\d{11,}"  # 长数字串：手机号 / 证件号 / 银行卡等
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"  # 邮箱
    r"|sk-[A-Za-z0-9]{8,}",  # 常见 API Key 前缀
    re.IGNORECASE,
)


class BufferShape(StrEnum):
    """消息形状分类；只有 ``TEXT_ONLY`` 可入缓冲。

    由调用方从可信消息组件判定——本模块不导入框架，因而读不到原始消息对象。
    """

    TEXT_ONLY = "text_only"
    HAS_MENTION = "has_mention"
    HAS_ATTACHMENT = "has_attachment"
    HAS_QUOTE = "has_quote"
    HAS_FORWARD = "has_forward"
    OTHER = "other"


class IngestOutcome(StrEnum):
    """一次入库尝试的结果；除 ``STORED`` 外都是"没有保存"的确定原因。"""

    STORED = "stored"
    REJECTED_SHAPE = "rejected_shape"
    REJECTED_EMPTY = "rejected_empty"
    REJECTED_TOO_LONG = "rejected_too_long"
    REJECTED_SENSITIVE = "rejected_sensitive"
    REJECTED_COMMAND = "rejected_command"
    REJECTED_DUPLICATE = "rejected_duplicate"
    REJECTED_SELF = "rejected_self"


@dataclass(frozen=True)
class BufferEntry:
    """一条缓冲消息。正文不进 ``repr``（与 ``MessageFacts.direct_text`` 同例）。"""

    member_id: str
    message_id: str
    text: str = field(repr=False)
    at: float


@dataclass(frozen=True)
class BufferStats:
    groups: int
    entries: int
    evicted_capacity: int
    evicted_expired: int


def is_sensitive(text: str) -> bool:
    """文本是否命中明显敏感规则；命中即整条丢弃。"""
    if _SENSITIVE_PATTERN.search(text):
        return True
    return any(keyword in text for keyword in SENSITIVE_KEYWORDS)


class ContextBuffer:
    """每群一个 deque 的内存环形缓冲；所有方法同步，只在事件循环内使用。"""

    def __init__(
        self,
        *,
        max_messages: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(max_messages, int) or isinstance(max_messages, bool) or max_messages < 1:
            raise ValueError("max_messages 必须是正整数")
        if (
            not isinstance(ttl_seconds, (int, float))
            or isinstance(ttl_seconds, bool)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds 必须是正数")
        self._max = max_messages
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._groups: dict[GroupKey, deque[BufferEntry]] = {}
        self._evicted_capacity = 0
        self._evicted_expired = 0

    def ingest(
        self,
        *,
        group: GroupKey,
        member: MemberKey,
        message_id: str,
        text: str,
        shape: BufferShape,
    ) -> IngestOutcome:
        """尝试写入一条普通群聊；返回未保存的确定原因，调用方无需再判一遍。"""
        if member.group != group:
            raise ValueError("member 必须属于 group")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id 必须是非空文本")
        if shape is not BufferShape.TEXT_ONLY:
            return IngestOutcome.REJECTED_SHAPE
        if member.member_id == group.instance.self_id:
            return IngestOutcome.REJECTED_SELF
        if not isinstance(text, str) or not text.strip():
            return IngestOutcome.REJECTED_EMPTY
        if len(text) > MAX_TEXT_LENGTH:
            return IngestOutcome.REJECTED_TOO_LONG
        if is_command(text):
            return IngestOutcome.REJECTED_COMMAND
        if is_sensitive(text):
            return IngestOutcome.REJECTED_SENSITIVE

        now = self._clock()
        entries = self._groups.get(group)
        if entries is not None:
            self._evict(entries, now)
            if any(entry.message_id == message_id for entry in entries):
                return IngestOutcome.REJECTED_DUPLICATE
        else:
            entries = deque()
            self._groups[group] = entries
        entries.append(BufferEntry(member_id=member.member_id, message_id=message_id, text=text, at=now))
        while len(entries) > self._max:
            entries.popleft()
            self._evicted_capacity += 1
        return IngestOutcome.STORED

    def entries(self, group: GroupKey) -> tuple[BufferEntry, ...]:
        """按写入顺序返回未过期条目；读取本身也会淘汰过期项。"""
        entries = self._groups.get(group)
        if entries is None:
            return ()
        self._evict(entries, self._clock())
        if not entries:
            self._groups.pop(group, None)
            return ()
        return tuple(entries)

    def clear_group(self, group: GroupKey) -> int:
        """清空一个群并移除其缓冲；返回被清掉的条数。"""
        entries = self._groups.pop(group, None)
        return len(entries) if entries is not None else 0

    def clear_member(self, member: MemberKey) -> int:
        """清掉某成员在本群的条目（其他成员不受影响）；返回被清掉的条数。"""
        entries = self._groups.get(member.group)
        if entries is None:
            return 0
        before = len(entries)
        remaining = deque(entry for entry in entries if entry.member_id != member.member_id)
        if remaining:
            self._groups[member.group] = remaining
        else:
            self._groups.pop(member.group, None)
        return before - len(remaining)

    def clear_all(self) -> None:
        self._groups.clear()

    def stats(self) -> BufferStats:
        now = self._clock()
        total = 0
        for group in list(self._groups):
            entries = self._groups[group]
            self._evict(entries, now)
            if not entries:
                del self._groups[group]
            total += len(entries)
        return BufferStats(
            groups=len(self._groups),
            entries=total,
            evicted_capacity=self._evicted_capacity,
            evicted_expired=self._evicted_expired,
        )

    def _evict(self, entries: deque[BufferEntry], now: float) -> None:
        """惰性淘汰：T 时刻恰好到期即过期（取严）。"""
        while entries and now - entries[0].at >= self._ttl:
            entries.popleft()
            self._evicted_expired += 1
