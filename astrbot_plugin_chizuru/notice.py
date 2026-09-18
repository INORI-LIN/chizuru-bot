"""群告知文案与两步确认窗口（S2-02）：纯逻辑，不导入框架、不做 IO、不判权限。

**交付机制**（第 9.1 节 B1a）：维护者 @ 机器人发「群上下文 开启」→ 机器人**只回复**
`NOTICE_TEXT` 全文，并记录一个待确认窗口；维护者把该回复设为群公告或转发置顶完成告知；
同一维护者在窗口内、同一群内发「群上下文 确认开启」才写入告知版本并开启采集。

| 事项 | 本模块的处理 |
|---|---|
| 文案 | `NOTICE_TEXT` **逐字**取自 docs/03 附录 C.1（2026-09-18 用户定稿），只去掉代码块末尾的换行；测试用字面量副本与 SHA-256 指纹钉住 |
| 版本 | `NOTICE_VERSION` 锚定本模块内文本的版本；**写入策略行的版本由调用方传入**（与 `_maybe_collect` 的判定同源），调用方必须先确认两者一致 |
| 窗口 | 每群**单槽**：任何维护者重新发起都覆盖刷新（换人即作废前一次）；`now - requested_at < window` 才有效——**恰好到期算过期**（取严） |
| 失败 | `confirm()` **无论成败都消费**窗口；失败的五种原因只用于审计与测试，对群内行为完全一致（重发全文并重开窗口） |

**必须不做**：不导入框架/sqlite/asyncio、不做 IO、不调模型、不发消息、不判权限、
不读配置（版本由调用方传入）、不保存正文或昵称。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from .keys import GroupKey

NOTICE_VERSION = "notice-1"
"""告知文案版本。**文本任何改动（含标点与空白）都必须递增本值**——测试用字面量钉住
版本与文本指纹，改动而未递增会直接失败（B1a"文案一旦修改必须递增版本并重新告知"）。"""

NOTICE_TEXT = """【关于本群启用的聊天机器人】

本群将启用一个参考《租借女友》水原千鹤设计的聊天机器人。
它不是真人，也不是官方账号，是仅在本群使用的非官方角色机器人。

它出现的方式很有限：
· 只有被 @ 时才会回复
· 你主动发送控制指令时，它会按权限执行

为了让对话能接上群里的话题，它会在内存中短暂保留本群近期的普通聊天
文本（每群最多 30 条、不超过 10 分钟）。这些内容不写入数据库，服务重
启即丢失。只有有人 @ 它时，它才会把必要的部分发送给 DeepSeek 处理，
不会逐条分析，也不会给群成员做画像。

你可以随时 @ 它并发送「上下文 退出」，停止采集你自己的发言，并清除
相关缓冲。退出后你仍然可以正常 @ 它聊天。

长期记忆默认关闭。除非你本人主动开启并确认，它不会记住任何关于你的
事情。

请不要向它发送密码、证件、电话、住址、财务或健康信息。群消息会经由
DeepSeek 处理，而且已经发出的群消息其他成员都能看到，无法撤回。

有问题可以 @ 它发送「帮助」，或联系本群维护者。"""

CONFIRM_WINDOW_SECONDS = 300
"""确认窗口（秒）。**建议参数待评审**：B1a 定为 5 分钟，取值与需求 §6"授权确认"一致。"""

PENDING_CAPACITY = 256
"""待确认窗口的群数上界。**建议参数待评审**：条目只含群键、维护者 ID、版本与时间。"""


class ConfirmOutcome(StrEnum):
    """一次确认尝试的结论；除 ``CONFIRMED`` 外都是"没有生效"。"""

    CONFIRMED = "confirmed"
    NO_PENDING = "no_pending"
    EXPIRED = "expired"
    ACTOR_MISMATCH = "actor_mismatch"
    VERSION_MISMATCH = "version_mismatch"


@dataclass(frozen=True)
class PendingNotice:
    """一次"已展示告知、等待确认"的记录（群键即表键，不重复存放）。"""

    actor_id: str
    version: str
    requested_at: float


def _is_plain_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


class NoticeGate:
    """每群一个待确认窗口的内存表；所有方法同步，只在事件循环内使用。

    与 `context_buffer` / `dedup` 同例：纯内存、有界、惰性淘汰、不持有后台任务。
    重启会丢弃待确认窗口——维护者重新发起一次即可，不构成状态丢失。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        window_seconds: float = CONFIRM_WINDOW_SECONDS,
        capacity: int = PENDING_CAPACITY,
    ) -> None:
        if not isinstance(window_seconds, (int, float)) or isinstance(window_seconds, bool) or window_seconds <= 0:
            raise ValueError("window_seconds 必须是正数")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity 必须是正整数")
        if not callable(clock):
            raise ValueError("clock 必须是可调用对象")
        self._clock = clock
        self._window = float(window_seconds)
        self._capacity = capacity
        self._pending: dict[GroupKey, PendingNotice] = {}
        self._evicted = 0

    def begin(self, group: GroupKey, *, actor_id: str, version: str) -> None:
        """记录（或刷新）本群的待确认窗口；同一维护者重复发起只是刷新时间。"""
        if not isinstance(group, GroupKey):
            raise ValueError("group 必须是 GroupKey")
        if not _is_plain_text(actor_id):
            raise ValueError("actor_id 必须是非空且无首尾空白的文本")
        if not _is_plain_text(version):
            raise ValueError("version 必须是非空且无首尾空白的文本")
        now = self._clock()
        self._drop_expired(now)
        self._pending.pop(group, None)
        self._pending[group] = PendingNotice(actor_id=actor_id, version=version, requested_at=now)
        while len(self._pending) > self._capacity:
            oldest = next(iter(self._pending), None)
            if oldest is None:
                break
            self._pending.pop(oldest, None)
            self._evicted += 1

    def confirm(self, group: GroupKey, *, actor_id: str, version: str) -> ConfirmOutcome:
        """核对一次确认；**无论成败都消费窗口**（失败后由调用方重新发起）。

        顺序：先过期、再换人、最后版本——三者对群内行为一致，区分只为审计可读。
        """
        if not isinstance(group, GroupKey):
            raise ValueError("group 必须是 GroupKey")
        now = self._clock()
        entry = self._pending.pop(group, None)
        if entry is None:
            return ConfirmOutcome.NO_PENDING
        if now - entry.requested_at >= self._window:
            return ConfirmOutcome.EXPIRED
        if entry.actor_id != actor_id:
            return ConfirmOutcome.ACTOR_MISMATCH
        if entry.version != version:
            return ConfirmOutcome.VERSION_MISMATCH
        return ConfirmOutcome.CONFIRMED

    def pending(self, group: GroupKey) -> PendingNotice | None:
        """当前待确认记录（只读；过期即视为没有）。"""
        entry = self._pending.get(group)
        if entry is None:
            return None
        if self._clock() - entry.requested_at >= self._window:
            self._pending.pop(group, None)
            return None
        return entry

    def stats(self) -> tuple[int, int]:
        """(当前群数, 因容量淘汰的累计条数)；读取本身也会淘汰过期项。"""
        self._drop_expired(self._clock())
        return len(self._pending), self._evicted

    def _drop_expired(self, now: float) -> None:
        for group in [key for key, entry in self._pending.items() if now - entry.requested_at >= self._window]:
            self._pending.pop(group, None)


def describe_policy(*, notice_version: str, context_enabled: bool, paused: bool) -> str:
    """`千鹤 状态` 的群上下文行取值（**维护者可见新文案，待评审**）。

    优先级：没有告知版本 → 未告知；暂停 → 已暂停；已开启；其余为已关闭。
    没有告知版本时**不看**开关与暂停：`bump_revision` 建出的行就是这种形态，
    显示"未告知"比显示"已关闭（）"更准确。
    """
    if not _is_plain_text(notice_version):
        return "群上下文：未告知"
    if paused:
        return f"群上下文：已暂停（{notice_version}）"
    if context_enabled:
        return f"群上下文：已开启（{notice_version}）"
    return f"群上下文：已关闭（{notice_version}）"
