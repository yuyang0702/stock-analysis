from __future__ import annotations

import unittest

from gap_reentry import (
    GapReentryInput, estimated_limit_up_price, evaluate_gap_reentry,
    minimum_lot_position,
)


def case(**overrides: object) -> GapReentryInput:
    values = {
        "trade_date": "2026-07-17",
        "code": "002432",
        "parent_signal_id": "parent-1",
        "batch_id": "batch-2",
        "now": "2026-07-17 10:05:00",
        "price": 76.95,
        "limit_up_price": 79.20,
        "original_entry_price": 74.72,
        "original_stop_price": 69.49,
        "market_state": "NORMAL",
        "current_score": 88.0,
        "required_score": 75.0,
        "quote_age_sec": 10.0,
        "first_open_at": "2026-07-17 10:00:00",
        "first_open_price": 76.90,
        "first_batch_id": "batch-1",
        "confirmation_count": 1,
        "attempt_count": 1,
    }
    values.update(overrides)
    return GapReentryInput(**values)


class GapReentryTest(unittest.TestCase):
    def test_limit_price_uses_board_rules(self) -> None:
        self.assertEqual(estimated_limit_up_price("600000", 10), 11.0)
        self.assertEqual(estimated_limit_up_price("300001", 10), 12.0)
        self.assertEqual(estimated_limit_up_price("830001", 10), 13.0)
    def test_locked_limit_is_observed_without_buying(self) -> None:
        result = evaluate_gap_reentry(case(price=79.20, at_limit=True))
        self.assertEqual(result.state, "LOCKED_LIMIT")
        self.assertEqual(result.reason, "gap_reentry_locked_limit")
        self.assertFalse(result.allowed)

    def test_two_distinct_scans_confirm_below_half_r_cap(self) -> None:
        result = evaluate_gap_reentry(case())
        self.assertEqual(result.state, "OPEN_CONFIRMED")
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.cap_price, 77.335)

    def test_reentry_above_half_r_cap_is_rejected(self) -> None:
        result = evaluate_gap_reentry(case(price=77.35))
        self.assertEqual(result.reason, "gap_reentry_too_far")

    def test_reseal_resets_confirmation(self) -> None:
        result = evaluate_gap_reentry(case(at_limit=True, resealed=True, price=79.20))
        self.assertEqual(result.state, "RESEALED")
        self.assertEqual(result.confirmation_count, 0)

    def test_lunch_break_does_not_count_as_five_minutes(self) -> None:
        result = evaluate_gap_reentry(case(
            first_open_at="2026-07-17 11:29:00",
            now="2026-07-17 13:03:00",
        ))
        self.assertEqual(result.state, "OPEN_OBSERVING")

    def test_risk_off_and_late_entries_are_blocked(self) -> None:
        self.assertEqual(
            evaluate_gap_reentry(case(market_state="RISK_OFF")).reason,
            "gap_reentry_current_risk_disallowed",
        )
        self.assertEqual(
            evaluate_gap_reentry(case(now="2026-07-17 14:46:00", first_open_at="")).reason,
            "gap_reentry_too_late",
        )

    def test_minimum_lot_uses_truthful_position_and_risk(self) -> None:
        result = minimum_lot_position(
            entry_price=76.0, stop_price=72.0, account_value=100_000,
            available_cash=10_000, risk_budget_pct=0.5,
            current_position_pct=20.0, max_total_position_pct=80.0,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.qty, 100)
        self.assertAlmostEqual(result.position_pct, 7.6)

    def test_minimum_lot_rejects_cash_and_risk_excess(self) -> None:
        self.assertEqual(minimum_lot_position(
            76, 72, 100_000, 7_000, 0.5, 20, 80,
        ).reason, "gap_reentry_insufficient_cash")
        self.assertEqual(minimum_lot_position(
            76, 69, 100_000, 10_000, 0.5, 20, 80,
        ).reason, "gap_reentry_min_lot_risk_exceeded")


if __name__ == "__main__":
    unittest.main()
