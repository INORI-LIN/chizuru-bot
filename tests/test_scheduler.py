import asyncio
import time
import unittest
from pathlib import Path

from astrbot_plugin_chizuru.config import LimitsSettings
from astrbot_plugin_chizuru.keys import BotInstanceKey, GroupKey
from astrbot_plugin_chizuru.scheduler import (
    AdmissionRefused,
    Deadline,
    DeadlineExceeded,
    Refusal,
    Scheduler,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot_plugin_chizuru"
INSTANCE = BotInstanceKey("qq-local", "10001")

# 测试时长取 0.01—0.1 秒：足以让事件循环推进，又不至于让回归变慢。
TICK = 0.01
WAIT = 0.05


def group(group_id: str) -> GroupKey:
    return GroupKey(INSTANCE, group_id)


def limits(**overrides) -> LimitsSettings:
    values = {
        "provider_concurrency_global": 2,
        "provider_concurrency_per_group": 1,
        "chat_queue_per_group": 3,
        "extraction_queue_global": 20,
        "schedule_wait_seconds": 1,
        "task_deadline_seconds": 2,
    }
    values.update(overrides)
    return LimitsSettings(**values)


def work(name, *, gate=None, order=None, delay=0.0):
    async def run(deadline: Deadline):
        if order is not None:
            order.append(name)
        if delay:
            await asyncio.sleep(delay)
        if gate is not None:
            await gate.wait()
        return name

    return run


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = Scheduler(limits())

    async def asyncTearDown(self):
        await self.scheduler.aclose()

    async def test_chat_work_returns_its_result(self):
        self.assertEqual(await self.scheduler.submit_chat(group("20001"), work("ok")), "ok")

    async def test_same_group_chat_is_serial_and_keeps_order(self):
        order = []
        gates = []

        async def serial(deadline: Deadline):
            order.append(len(order))
            gate = asyncio.Event()
            gates.append(gate)
            await gate.wait()

        tasks = [asyncio.create_task(self.scheduler.submit_chat(group("20001"), serial)) for _ in range(3)]
        await asyncio.sleep(TICK)
        self.assertEqual(self.scheduler.stats().running_global, 1)
        for gate in gates:
            gate.set()
            await asyncio.sleep(TICK)
        await asyncio.gather(*tasks)
        self.assertEqual(order, [0, 1, 2])

    async def test_global_concurrency_is_bounded(self):
        running = 0
        peak = 0

        async def counted(deadline: Deadline):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(WAIT)
            running -= 1

        await asyncio.gather(
            *(self.scheduler.submit_chat(group(f"2000{i}"), counted) for i in range(4))
        )
        self.assertEqual(peak, 2)

    async def test_group_slot_is_shared_by_chat_and_extraction(self):
        running = 0
        peak = 0
        gate = asyncio.Event()

        async def counted(deadline: Deadline):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await gate.wait()
            running -= 1

        chat = asyncio.create_task(self.scheduler.submit_chat(group("20001"), counted))
        await asyncio.sleep(TICK)
        extraction = asyncio.create_task(self.scheduler.submit_extraction(group("20001"), counted))
        await asyncio.sleep(TICK)
        self.assertEqual(self.scheduler.stats().waiting_extraction, 1)
        gate.set()
        await asyncio.gather(chat, extraction)
        self.assertEqual(peak, 1)

    async def test_chat_is_admitted_before_waiting_extraction(self):
        order = []
        gate = asyncio.Event()
        scheduler = Scheduler(limits(provider_concurrency_global=1))

        first = asyncio.create_task(scheduler.submit_chat(group("A"), work("A", gate=gate, order=order)))
        await asyncio.sleep(TICK)
        extraction = asyncio.create_task(scheduler.submit_extraction(group("B"), work("B", order=order)))
        chat = asyncio.create_task(scheduler.submit_chat(group("C"), work("C", order=order)))
        await asyncio.sleep(TICK)
        gate.set()
        await asyncio.gather(first, extraction, chat)
        self.assertEqual(order, ["A", "C", "B"])
        await scheduler.aclose()

    async def test_full_chat_queue_is_refused(self):
        gate = asyncio.Event()
        running = asyncio.create_task(self.scheduler.submit_chat(group("20001"), work("run", gate=gate)))
        await asyncio.sleep(TICK)
        waiting = [
            asyncio.create_task(self.scheduler.submit_chat(group("20001"), work(f"w{i}")))
            for i in range(3)
        ]
        await asyncio.sleep(TICK)

        with self.assertRaises(AdmissionRefused) as caught:
            await self.scheduler.submit_chat(group("20001"), work("overflow"))
        self.assertEqual(caught.exception.reason, Refusal.QUEUE_FULL)

        # 队列满只约束等待者：另一个群不受影响。
        self.assertEqual(await self.scheduler.submit_chat(group("20002"), work("other")), "other")
        gate.set()
        await asyncio.gather(running, *waiting)

    async def test_full_extraction_queue_is_refused(self):
        scheduler = Scheduler(limits(provider_concurrency_global=1, extraction_queue_global=2))
        gate = asyncio.Event()
        running = asyncio.create_task(scheduler.submit_chat(group("20001"), work("run", gate=gate)))
        await asyncio.sleep(TICK)
        waiting = [
            asyncio.create_task(scheduler.submit_extraction(group(f"2000{i}"), work(f"w{i}")))
            for i in range(2)
        ]
        await asyncio.sleep(TICK)

        with self.assertRaises(AdmissionRefused) as caught:
            await scheduler.submit_extraction(group("20009"), work("overflow"))
        self.assertEqual(caught.exception.reason, Refusal.QUEUE_FULL)
        gate.set()
        await asyncio.gather(running, *waiting)
        await scheduler.aclose()

    async def test_waiting_too_long_is_refused_and_frees_queue_slot(self):
        scheduler = Scheduler(
            limits(provider_concurrency_global=1, schedule_wait_seconds=WAIT),
        )
        gate = asyncio.Event()
        running = asyncio.create_task(scheduler.submit_chat(group("20001"), work("run", gate=gate)))
        await asyncio.sleep(TICK)

        with self.assertRaises(AdmissionRefused) as caught:
            await scheduler.submit_chat(group("20001"), work("late"))
        self.assertEqual(caught.exception.reason, Refusal.WAIT_TIMEOUT)
        # 超时者不虚占队列位：队列并未因此变满。
        self.assertEqual(scheduler.stats().waiting_chat, {})

        gate.set()
        await running
        self.assertEqual(await scheduler.submit_chat(group("20001"), work("next")), "next")
        await scheduler.aclose()

    async def test_failure_releases_the_slot(self):
        async def boom(deadline: Deadline):
            raise RuntimeError("失败也要释放槽位")

        with self.assertRaises(RuntimeError):
            await self.scheduler.submit_chat(group("20001"), boom)
        self.assertEqual(self.scheduler.stats().running_global, 0)
        self.assertEqual(await self.scheduler.submit_chat(group("20001"), work("ok")), "ok")


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_work_receives_the_remaining_deadline(self):
        scheduler = Scheduler(limits(schedule_wait_seconds=1, task_deadline_seconds=2))
        seen = {}

        async def work(deadline: Deadline):
            seen["remaining"] = deadline.remaining(time.monotonic())
            seen["total"] = deadline.total

        await scheduler.submit_chat(group("20001"), work)
        self.assertEqual(seen["total"], 2)
        self.assertTrue(0 < seen["remaining"] <= 2)
        await scheduler.aclose()

    async def test_expired_work_is_cancelled_and_reported(self):
        scheduler = Scheduler(limits(task_deadline_seconds=WAIT, schedule_wait_seconds=1))
        cancelled = False

        async def slow(deadline: Deadline):
            nonlocal cancelled
            try:
                await asyncio.sleep(5)
            finally:
                cancelled = True

        with self.assertRaises(DeadlineExceeded):
            await scheduler.submit_chat(group("20001"), slow)
        self.assertTrue(cancelled)
        self.assertEqual(scheduler.stats().running_global, 0)
        self.assertEqual(await scheduler.submit_chat(group("20001"), work("ok")), "ok")
        await scheduler.aclose()

    async def test_work_timeout_is_not_reported_as_deadline(self):
        """工作自身的网络超时按原样上抛，不冒充业务期限。"""

        async def timed_out(deadline: Deadline):
            raise TimeoutError("网络读取超时")

        with self.assertRaises(TimeoutError) as caught:
            await Scheduler(limits()).submit_chat(group("20001"), timed_out)
        self.assertNotIsInstance(caught.exception, DeadlineExceeded)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_reports_running_and_waiting(self):
        scheduler = Scheduler(limits())
        gate = asyncio.Event()
        running = asyncio.create_task(scheduler.submit_chat(group("20001"), work("run", gate=gate)))
        await asyncio.sleep(TICK)
        waiting = asyncio.create_task(scheduler.submit_chat(group("20001"), work("wait")))
        await asyncio.sleep(TICK)

        stats = scheduler.stats()
        self.assertEqual(stats.running_global, 1)
        self.assertEqual(stats.running_per_group, {group("20001"): 1})
        self.assertEqual(stats.waiting_chat, {group("20001"): 1})
        self.assertEqual(stats.waiting_extraction, 0)
        self.assertFalse(stats.closed)

        gate.set()
        await asyncio.gather(running, waiting)
        self.assertEqual(scheduler.stats().running_global, 0)
        self.assertEqual(scheduler.stats().waiting_chat, {})
        await scheduler.aclose()

    async def test_aclose_refuses_new_work_and_wakes_waiters(self):
        scheduler = Scheduler(limits())
        gate = asyncio.Event()
        running = asyncio.create_task(scheduler.submit_chat(group("20001"), work("run", gate=gate)))
        await asyncio.sleep(TICK)
        waiting = asyncio.create_task(scheduler.submit_chat(group("20001"), work("wait")))
        await asyncio.sleep(TICK)

        await scheduler.aclose()
        with self.assertRaises(AdmissionRefused) as caught:
            await waiting
        self.assertEqual(caught.exception.reason, Refusal.CLOSED)
        with self.assertRaises(AdmissionRefused) as caught:
            await scheduler.submit_chat(group("20001"), work("late"))
        self.assertEqual(caught.exception.reason, Refusal.CLOSED)
        self.assertTrue(scheduler.stats().closed)

        await scheduler.aclose()  # 幂等
        # 在途任务不被调度器取消：它运行在调用方的任务里，由框架停止时负责。
        gate.set()
        self.assertEqual(await running, "run")

    async def test_aclose_leaves_no_lingering_tasks(self):
        before = set(asyncio.all_tasks())
        scheduler = Scheduler(limits())
        await scheduler.submit_chat(group("20001"), work("ok"))
        await scheduler.aclose()
        # 调度器不创建后台任务；准入与执行都在调用方任务里完成。
        self.assertEqual(set(asyncio.all_tasks()) - before, set())


class StructuralTests(unittest.TestCase):
    def test_refusal_reasons_are_distinct(self):
        self.assertEqual(
            len({Refusal.QUEUE_FULL, Refusal.WAIT_TIMEOUT, Refusal.CLOSED}),
            3,
        )

    def test_module_does_not_import_the_framework(self):
        text = (PLUGIN_ROOT / "scheduler.py").read_text()
        self.assertNotIn("import astrbot", text)
        self.assertNotIn("from astrbot", text)


if __name__ == "__main__":
    unittest.main()
