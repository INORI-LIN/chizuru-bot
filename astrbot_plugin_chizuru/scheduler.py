"""有界队列、并发与业务期限：聊天与抽取共用的准入层。

**本模块只回答"这次工作能否被接纳、何时开始、还剩多少时间"**：不做发送、
不调模型、不读存储、不判权限、不做重试。重试由 ``llm.py`` 在本模块给出的
剩余期限内自行约束（S1-10），总耗时不得重置（架构 §8.1）。

设计要点（对应 docs/03 §5.2 的 S1-07 判据）：

- 全局并发与同群并发由**集中准入**保证，而不是 ``asyncio.Semaphore``：信号量
  的唤醒顺序是 FIFO，无法表达"聊天优先于抽取"。
- 队列上界只约束**未被准入的等待者**：每群聊天等待 ≤ ``chat_queue_per_group``、
  全局抽取等待 ≤ ``extraction_queue_global``；运行中的任务另计。超界立即拒绝，
  不排队（架构 §8.1"聊天优先于自动提取""不持久化无限积压"）。
- 期限自**接受任务**起算：排队截止取 ``schedule_wait_seconds`` 与业务期限的更
  小者；准入后把剩余时间交给工作协程，超时即取消，结果不可得——"修订变化后
  旧结果不写回"因此是结构保证，而不是约定。
- 全部状态在内存：进程重启即丢弃在途任务（架构 §8.2）。调度器不持有后台任务，
  在途工作运行在调用方自己的任务里，停止时由框架取消调用方任务。
- 预算（S1-09）与本模块解耦：调度器同时服务聊天与抽取，"先预留再调度"的顺序
  由装配层保证（docs/03 §5.2 实施说明）。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from .config import LimitsSettings
from .keys import GroupKey

T = TypeVar("T")
Work = Callable[["Deadline"], Awaitable[T]]


class TaskKind(StrEnum):
    CHAT = "chat"
    EXTRACTION = "extraction"


class Refusal(StrEnum):
    """未接纳的原因。队列满与等待超时必须可区分：前者对应"忙碌"提示，
    后者对应"到期放弃"。"""

    QUEUE_FULL = "queue_full"
    WAIT_TIMEOUT = "wait_timeout"
    CLOSED = "closed"


class AdmissionRefused(Exception):
    """任务未被调度器接纳：没有开始执行，也不产生任何副作用。"""

    def __init__(self, reason: Refusal) -> None:
        super().__init__(f"任务未被接纳：{reason.value}")
        self.reason = reason


class DeadlineExceeded(Exception):
    """任务在业务期限内未完成，工作协程已被取消；结果不可得。"""


@dataclass(frozen=True)
class Deadline:
    """一次任务的期限依据。工作协程据此约束自己的网络超时与重试。"""

    accepted_at: float
    expires_at: float

    def remaining(self, now: float) -> float:
        """相对给定时刻的剩余秒数；不再为正即已过期。"""
        return self.expires_at - now

    @property
    def total(self) -> float:
        return self.expires_at - self.accepted_at


@dataclass(frozen=True)
class SchedulerStats:
    """运行中与等待中的计数快照，供测试与后续"千鹤 状态"使用。"""

    running_global: int
    running_per_group: Mapping[GroupKey, int]
    waiting_chat: Mapping[GroupKey, int]
    waiting_extraction: int
    closed: bool


@dataclass
class _Waiter:
    kind: TaskKind
    group: GroupKey
    deadline: Deadline
    future: asyncio.Future
    admitted: bool = False


class Scheduler:
    """准入层。所有公开方法都只在事件循环内使用，状态不跨进程。"""

    def __init__(
        self,
        limits: LimitsSettings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._monotonic = monotonic
        self._waiting_chat: dict[GroupKey, deque[_Waiter]] = {}
        self._waiting_extraction: deque[_Waiter] = deque()
        self._running_per_group: dict[GroupKey, int] = {}
        self._running_global = 0
        self._closed = False

    # ---- 提交 ----

    async def submit_chat(self, group: GroupKey, work: Work[T]) -> T:
        """按聊天优先级提交一次同群串行工作。"""
        return await self._submit(TaskKind.CHAT, group, work)

    async def submit_extraction(self, group: GroupKey, work: Work[T]) -> T:
        """提交一次后台抽取工作；与聊天共享全局与同群并发上限。"""
        return await self._submit(TaskKind.EXTRACTION, group, work)

    async def _submit(self, kind: TaskKind, group: GroupKey, work: Work[T]) -> T:
        if self._closed:
            raise AdmissionRefused(Refusal.CLOSED)

        now = self._monotonic()
        deadline = Deadline(
            accepted_at=now,
            expires_at=now + self._limits.task_deadline_seconds,
        )
        admit_by = min(now + self._limits.schedule_wait_seconds, deadline.expires_at)

        waiter = _Waiter(kind, group, deadline, asyncio.get_running_loop().create_future())
        self._enqueue(waiter)
        self._pump()

        try:
            await asyncio.wait_for(waiter.future, timeout=admit_by - now)
            remaining = deadline.remaining(self._monotonic())
            if remaining <= 0:
                raise AdmissionRefused(Refusal.WAIT_TIMEOUT)
            return await asyncio.wait_for(work(deadline), timeout=remaining)
        except TimeoutError as exc:
            if not waiter.admitted:
                raise AdmissionRefused(Refusal.WAIT_TIMEOUT) from exc
            if deadline.remaining(self._monotonic()) > 0:
                # 工作自身抛出的超时（如网络读取），按原样上抛，不冒充业务期限。
                raise
            raise DeadlineExceeded("任务超出业务期限，工作已被取消") from exc
        finally:
            # 准入与释放严格配对：admitted 由 _dispatch 在同步段内置位，且与
            # 槽位计数同时发生，因此这里不会漏放或重复释放。
            if waiter.admitted:
                self._release_slot(group)
            else:
                self._discard(waiter)

    def _enqueue(self, waiter: _Waiter) -> None:
        if waiter.kind is TaskKind.CHAT:
            queue = self._waiting_chat.setdefault(waiter.group, deque())
            if len(queue) >= self._limits.chat_queue_per_group:
                raise AdmissionRefused(Refusal.QUEUE_FULL)
            queue.append(waiter)
            return
        if len(self._waiting_extraction) >= self._limits.extraction_queue_global:
            raise AdmissionRefused(Refusal.QUEUE_FULL)
        self._waiting_extraction.append(waiter)

    # ---- 准入 ----

    def _pump(self) -> None:
        """把可准入的等待者交给执行。同步完成，不产生后台任务。"""
        if self._closed:
            return
        while True:
            waiter = self._take_chat()
            if waiter is None:
                waiter = self._take_extraction()
            if waiter is None:
                return
            self._dispatch(waiter)

    def _take_chat(self) -> _Waiter | None:
        if self._running_global >= self._limits.provider_concurrency_global:
            return None
        for group in list(self._waiting_chat):
            queue = self._waiting_chat[group]
            self._drop_finished(queue)
            if not queue:
                del self._waiting_chat[group]
                continue
            if self._running_per_group.get(group, 0) >= self._limits.provider_concurrency_per_group:
                continue
            return queue.popleft()
        return None

    def _take_extraction(self) -> _Waiter | None:
        if self._running_global >= self._limits.provider_concurrency_global:
            return None
        # 只轮转一圈：本轮没有可准入者就停下，避免被同群占满的队头拖住。
        for _ in range(len(self._waiting_extraction)):
            waiter = self._waiting_extraction.popleft()
            if waiter.future.done():
                continue
            if self._running_per_group.get(waiter.group, 0) >= self._limits.provider_concurrency_per_group:
                self._waiting_extraction.append(waiter)
                continue
            return waiter
        return None

    @staticmethod
    def _drop_finished(queue: deque[_Waiter]) -> None:
        """清掉已取消（排队超时）的等待者，避免它们虚占队列位。"""
        while queue and queue[0].future.done():
            queue.popleft()

    def _dispatch(self, waiter: _Waiter) -> None:
        self._running_global += 1
        self._running_per_group[waiter.group] = self._running_per_group.get(waiter.group, 0) + 1
        waiter.admitted = True
        waiter.future.set_result(None)

    def _release_slot(self, group: GroupKey) -> None:
        remaining = self._running_per_group.get(group, 0) - 1
        if remaining > 0:
            self._running_per_group[group] = remaining
        else:
            self._running_per_group.pop(group, None)
        self._running_global -= 1
        self._pump()

    def _discard(self, waiter: _Waiter) -> None:
        if waiter.kind is TaskKind.CHAT:
            queue = self._waiting_chat.get(waiter.group)
            if queue is not None and waiter in queue:
                queue.remove(waiter)
                if not queue:
                    del self._waiting_chat[waiter.group]
            return
        if waiter in self._waiting_extraction:
            self._waiting_extraction.remove(waiter)

    # ---- 观测与停止 ----

    def stats(self) -> SchedulerStats:
        return SchedulerStats(
            running_global=self._running_global,
            running_per_group=dict(self._running_per_group),
            waiting_chat={group: len(queue) for group, queue in self._waiting_chat.items() if queue},
            waiting_extraction=len(self._waiting_extraction),
            closed=self._closed,
        )

    async def aclose(self) -> None:
        """停止接纳：拒绝新提交，并以 ``Refusal.CLOSED`` 唤醒所有等待者。

        在途工作不在这里取消——它们运行在调用方的任务里，由框架停止时取消。
        幂等。
        """
        if self._closed:
            return
        self._closed = True
        waiters = [waiter for queue in self._waiting_chat.values() for waiter in queue]
        waiters += list(self._waiting_extraction)
        self._waiting_chat.clear()
        self._waiting_extraction.clear()
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(AdmissionRefused(Refusal.CLOSED))
