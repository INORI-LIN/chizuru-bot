import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from astrbot_plugin_chizuru.budget import (
    BudgetLedger,
    BudgetRefusal,
    BudgetRefused,
    EXTRACTION_STOP_RATIO,
    PriceTable,
    TokenPrice,
    TokenUsage,
    UsageKind,
)
from astrbot_plugin_chizuru.config import BudgetSettings

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "astrbot_plugin_chizuru"
MODEL = "deepseek-flash"
# 每百万 token 1 / 2：估算用量 1000 输入 + 100 输出 ⇒ 每次 0.0012
PRICE = TokenPrice(Decimal("1"), Decimal("2"))
ESTIMATE_COST = Decimal("0.0012")


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 17, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def settings(**overrides) -> BudgetSettings:
    values = {
        "input_token_budget": 1000,
        "output_token_budget": 100,
        "daily_amount": 0.0,
        "monthly_amount": 0.0,
        "require_budget_for_extraction": True,
    }
    values.update(overrides)
    return BudgetSettings(**values)


def ledger(*, prices=None, clock=None, **overrides) -> BudgetLedger:
    return BudgetLedger(
        settings(**overrides),
        prices=PriceTable({MODEL: PRICE}) if prices is None else prices,
        clock=clock,
    )


class UnconfiguredAmountTests(unittest.TestCase):
    """B2：暂不设金额。此时抽取保持关闭，聊天不受金额限制。"""

    def test_chat_runs_without_amounts_and_only_tokens_are_tracked(self):
        book = ledger(prices=PriceTable())
        reservation = book.reserve(UsageKind.CHAT, MODEL)
        book.settle(reservation, TokenUsage(100, 50))

        snapshot = book.snapshot()
        self.assertFalse(snapshot.configured)
        self.assertEqual(snapshot.day_tokens, TokenUsage(100, 50))
        self.assertIsNone(snapshot.day_amount)
        self.assertIsNone(snapshot.day_limit)
        self.assertFalse(snapshot.paused)

    def test_extraction_stays_off_until_budget_is_configured(self):
        book = ledger()
        self.assertFalse(book.extraction_allowed)
        with self.assertRaises(BudgetRefused) as caught:
            book.reserve(UsageKind.EXTRACTION, MODEL)
        self.assertEqual(caught.exception.reason, BudgetRefusal.EXTRACTION_DISABLED)
        self.assertFalse(book.snapshot().extraction_allowed)

    def test_the_requirement_can_be_relaxed_explicitly(self):
        book = ledger(require_budget_for_extraction=False, prices=PriceTable())
        self.assertTrue(book.extraction_allowed)
        book.reserve(UsageKind.EXTRACTION, MODEL)


class SettlementTests(unittest.TestCase):
    def test_usage_settles_the_amount_exactly(self):
        book = ledger(daily_amount=1.0, prices=PriceTable({MODEL: PRICE}))
        reservation = book.reserve(UsageKind.CHAT, MODEL)
        self.assertEqual(reservation.estimated_cost, ESTIMATE_COST)
        book.settle(reservation, TokenUsage(1000, 100))

        snapshot = book.snapshot()
        self.assertEqual(snapshot.day_amount, ESTIMATE_COST)
        self.assertFalse(snapshot.day_estimated)
        self.assertEqual(snapshot.outstanding, 0)

    def test_missing_usage_is_estimated_not_zero(self):
        book = ledger(daily_amount=1.0)
        reservation = book.reserve(UsageKind.CHAT, MODEL)
        book.settle(reservation, None)

        snapshot = book.snapshot()
        self.assertEqual(snapshot.day_tokens, TokenUsage(1000, 100))
        self.assertEqual(snapshot.day_amount, ESTIMATE_COST)
        self.assertTrue(snapshot.day_estimated)

    def test_explicit_cost_wins_over_the_price_table(self):
        book = ledger(daily_amount=1.0)
        reservation = book.reserve(UsageKind.CHAT, MODEL)
        book.settle(reservation, TokenUsage(1000, 100), cost=Decimal("0.5"))
        self.assertEqual(book.snapshot().day_amount, Decimal("0.5"))

    def test_unknown_or_repeated_settlement_is_a_programming_error(self):
        book = ledger(daily_amount=1.0)
        reservation = book.reserve(UsageKind.CHAT, MODEL)
        book.settle(reservation)
        with self.assertRaises(ValueError):
            book.settle(reservation)
        with self.assertRaises(ValueError):
            book.cancel(reservation)
        with self.assertRaises(ValueError):
            book.cancel(reservation)

    def test_invalid_inputs_are_rejected(self):
        book = ledger(daily_amount=1.0)
        for kind in ("chat", None):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    book.reserve(kind, MODEL)
        for model in ("", " deepseek-flash", None):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    book.reserve(UsageKind.CHAT, model)

        reservation = book.reserve(UsageKind.CHAT, MODEL)
        with self.assertRaises(ValueError):
            book.settle(reservation, TokenUsage(1, 1), cost=Decimal("-1"))
        book.settle(reservation, TokenUsage(1, 1))
        with self.assertRaises(ValueError):
            TokenUsage(-1, 0)
        with self.assertRaises(ValueError):
            TokenUsage(True, 0)
        with self.assertRaises(ValueError):
            TokenPrice(Decimal("-1"), Decimal("0"))
        with self.assertRaises(ValueError):
            TokenPrice(1.0, Decimal("0"))


class LimitTests(unittest.TestCase):
    def test_reserved_amount_counts_toward_the_limit(self):
        book = ledger(daily_amount=0.002)
        first = book.reserve(UsageKind.CHAT, MODEL)
        second = book.reserve(UsageKind.CHAT, MODEL)
        # 两次在途预留（0.0024）已达 0.002：第三次拒绝，而不是无限叠加。
        with self.assertRaises(BudgetRefused) as caught:
            book.reserve(UsageKind.CHAT, MODEL)
        self.assertEqual(caught.exception.reason, BudgetRefusal.LIMIT_EXHAUSTED)
        self.assertEqual(book.snapshot().outstanding, 2)

        # 释放预留后额度恢复。
        book.cancel(first)
        book.cancel(second)
        book.reserve(UsageKind.CHAT, MODEL)

    def test_a_single_last_task_may_cross_the_limit(self):
        """上限小于一次估算时仍放行一次：超出量不超过一次预留估算，随后立即停用。"""
        book = ledger(daily_amount=0.001)
        book.reserve(UsageKind.CHAT, MODEL)
        with self.assertRaises(BudgetRefused):
            book.reserve(UsageKind.CHAT, MODEL)

    def test_unknown_price_blocks_spending_a_configured_budget(self):
        book = ledger(daily_amount=1.0, prices=PriceTable())
        with self.assertRaises(BudgetRefused) as caught:
            book.reserve(UsageKind.CHAT, MODEL)
        self.assertEqual(caught.exception.reason, BudgetRefusal.PRICE_UNKNOWN)

    def test_exhausted_budget_pauses_new_work(self):
        book = ledger(daily_amount=0.5)
        reservation = book.reserve(UsageKind.CHAT, MODEL)
        # 实际账单高于估算（价格变化/统计延迟）：按实际入账并如实显示。
        book.settle(reservation, TokenUsage(1000, 100), cost=Decimal("1.0"))
        snapshot = book.snapshot()
        self.assertEqual(snapshot.day_amount, Decimal("1.0"))
        self.assertTrue(snapshot.paused)
        with self.assertRaises(BudgetRefused) as caught:
            book.reserve(UsageKind.CHAT, MODEL)
        self.assertEqual(caught.exception.reason, BudgetRefusal.LIMIT_EXHAUSTED)

    def test_extraction_stops_before_chat(self):
        """抽取优先停用：抽取在 90% 即停，聊天到上限才停。"""
        book = ledger(daily_amount=0.01)
        first = book.reserve(UsageKind.CHAT, MODEL)
        book.settle(first, TokenUsage(1000, 100), cost=Decimal("0.009"))

        self.assertFalse(book.extraction_allowed)
        with self.assertRaises(BudgetRefused) as caught:
            book.reserve(UsageKind.EXTRACTION, MODEL)
        self.assertEqual(caught.exception.reason, BudgetRefusal.EXTRACTION_DISABLED)

        chat = book.reserve(UsageKind.CHAT, MODEL)
        book.settle(chat, TokenUsage(1000, 100), cost=Decimal("0.002"))
        self.assertTrue(book.snapshot().paused)
        with self.assertRaises(BudgetRefused) as caught:
            book.reserve(UsageKind.CHAT, MODEL)
        self.assertEqual(caught.exception.reason, BudgetRefusal.LIMIT_EXHAUSTED)

    def test_monthly_limit_is_enforced_too(self):
        book = ledger(monthly_amount=0.002)
        book.reserve(UsageKind.CHAT, MODEL)
        book.reserve(UsageKind.CHAT, MODEL)
        with self.assertRaises(BudgetRefused):
            book.reserve(UsageKind.CHAT, MODEL)

    def test_stop_ratio_is_a_reviewable_suggestion(self):
        self.assertEqual(EXTRACTION_STOP_RATIO, Decimal("0.9"))


class RolloverTests(unittest.TestCase):
    def test_day_and_month_aggregates_reset_lazily(self):
        clock = FakeClock()
        book = ledger(daily_amount=10.0, monthly_amount=100.0, clock=clock)
        book.settle(book.reserve(UsageKind.CHAT, MODEL), TokenUsage(1000, 100))
        self.assertEqual(book.snapshot().day_amount, ESTIMATE_COST)

        clock.advance(days=1)
        next_day = book.snapshot()
        self.assertEqual(next_day.day_tokens, TokenUsage())
        self.assertEqual(next_day.day_amount, Decimal(0))
        self.assertEqual(next_day.month_amount, ESTIMATE_COST)

        clock.advance(days=40)
        next_month = book.snapshot()
        self.assertEqual(next_month.month_tokens, TokenUsage())
        self.assertEqual(next_month.month_amount, Decimal(0))

    def test_in_flight_reservations_survive_rollover(self):
        clock = FakeClock()
        book = ledger(daily_amount=10.0, monthly_amount=100.0, clock=clock)
        reservation = book.reserve(UsageKind.CHAT, MODEL)
        clock.advance(days=1)
        self.assertEqual(book.snapshot().outstanding, 1)
        book.settle(reservation, TokenUsage(500, 50))
        self.assertEqual(book.snapshot().outstanding, 0)


class StructuralTests(unittest.TestCase):
    def test_price_table_has_no_fallback_price(self):
        """不硬编码价目：缺项就是未知，而不是默认价格。"""
        table = PriceTable()
        self.assertIsNone(table.cost(MODEL, TokenUsage(1000, 100)))
        self.assertFalse(table.configured)

    def test_module_does_not_import_the_framework(self):
        text = (PLUGIN_ROOT / "budget.py").read_text()
        self.assertNotIn("import astrbot", text)
        self.assertNotIn("from astrbot", text)


if __name__ == "__main__":
    unittest.main()
