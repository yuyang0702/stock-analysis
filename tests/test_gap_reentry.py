from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal, Inexact, ROUND_DOWN, ROUND_UP, Rounded, localcontext

from execution_contracts import FeeSchedule, InstrumentRules
from gap_reentry import (
    GapReentryInput, estimated_limit_up_price, evaluate_gap_reentry,
    evaluate_minimum_lot_risk,
    minimum_lot_position,
)


FEES = FeeSchedule.simulation()
RULES_100 = InstrumentRules.a_share("600000")
RULES_200 = InstrumentRules.a_share(
    "920001", buy_min_qty=200, buy_qty_step=200,
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
    def test_gap_one_lot_checks_trade_and_portfolio_budgets_separately(self) -> None:
        result = evaluate_minimum_lot_risk(
            per_trade_risk_yuan=Decimal("80"),
            remaining_open_risk_yuan=Decimal("200"),
            lot_loss_yuan=Decimal("100"),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.reasons, ("PER_TRADE_RISK_EXCEEDED",))

        portfolio = evaluate_minimum_lot_risk(
            per_trade_risk_yuan=Decimal("200"),
            remaining_open_risk_yuan=Decimal("80"),
            lot_loss_yuan=Decimal("100"),
        )
        both = evaluate_minimum_lot_risk(
            per_trade_risk_yuan=Decimal("80"),
            remaining_open_risk_yuan=Decimal("80"),
            lot_loss_yuan=Decimal("100"),
        )
        self.assertEqual(portfolio.reasons, ("PORTFOLIO_OPEN_RISK_EXCEEDED",))
        self.assertEqual(
            both.reasons,
            ("PER_TRADE_RISK_EXCEEDED", "PORTFOLIO_OPEN_RISK_EXCEEDED"),
        )
        self.assertTrue(evaluate_minimum_lot_risk(
            Decimal("100"), Decimal("100"), Decimal("100"),
        ).allowed)

        with self.assertRaises(ValueError):
            evaluate_minimum_lot_risk(Decimal("NaN"), Decimal("80"), Decimal("100"))

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

    def test_gap_input_rejects_non_finite_or_negative_state(self) -> None:
        for changes in (
            {"price": Decimal("NaN")},
            {"limit_up_price": Decimal("Infinity")},
            {"limit_up_price": Decimal("1e9999")},
            {"current_score": Decimal("NaN")},
            {"quote_age_sec": -1},
            {"attempt_count": -1},
            {"market_state": "BROKEN"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    case(**changes)

    def test_reentry_above_half_r_cap_is_rejected(self) -> None:
        result = evaluate_gap_reentry(case(price=77.35))
        self.assertEqual(result.reason, "gap_reentry_too_far")

    def test_derived_price_overflow_is_ineligible(self) -> None:
        result = evaluate_gap_reentry(case(
            original_entry_price=1.7e308,
            original_stop_price=1.0,
        ))

        self.assertEqual(result.reason, "gap_reentry_parent_invalid")
        self.assertFalse(result.allowed)

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
            available_cash=10_000, per_trade_risk_yuan=Decimal("500"),
            remaining_open_risk_yuan=Decimal("1000"),
            current_position_pct=20.0, max_total_position_pct=80.0,
            rules=RULES_100,
            fees=FEES,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.qty, 100)
        self.assertEqual(result.position_pct, Decimal("7.600"))
        self.assertIsInstance(result.risk_pct, Decimal)
        self.assertEqual(
            result.cash_required_yuan,
            Decimal("7600") + FEES.estimate("buy", Decimal("76"), 100).total_yuan,
        )

    def test_minimum_lot_uses_frozen_200_share_rules(self) -> None:
        result = minimum_lot_position(
            10, 9.9, 100_000, 5_000, Decimal("500"), Decimal("500"), 0, 80,
            rules=RULES_200,
            fees=FEES,
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.qty, 200)
        self.assertEqual(result.instrument_rules, RULES_200)
        self.assertEqual(
            result.cash_required_yuan,
            Decimal("2000") + FEES.estimate("buy", Decimal("10"), 200).total_yuan,
        )

    def test_minimum_lot_rejects_cash_and_risk_excess(self) -> None:
        self.assertEqual(minimum_lot_position(
            76, 72, 100_000, 7_000, Decimal("500"), Decimal("1000"), 20, 80,
            rules=RULES_100,
            fees=FEES,
        ).reason, "gap_reentry_insufficient_cash")
        per_trade = minimum_lot_position(
            76, 69, 100_000, 10_000, Decimal("500"), Decimal("1000"), 20, 80,
            rules=RULES_100,
            fees=FEES,
        )
        portfolio = minimum_lot_position(
            76, 72, 100_000, 10_000, Decimal("500"), Decimal("300"), 20, 80,
            rules=RULES_100,
            fees=FEES,
        )
        self.assertEqual(per_trade.reason, "gap_reentry_per_trade_risk_exceeded")
        self.assertEqual(per_trade.risk_reasons, ("PER_TRADE_RISK_EXCEEDED",))
        self.assertEqual(portfolio.reason, "gap_reentry_portfolio_open_risk_exceeded")
        self.assertEqual(portfolio.risk_reasons, ("PORTFOLIO_OPEN_RISK_EXCEEDED",))

    def test_minimum_lot_uses_full_round_trip_cost_and_keeps_both_reasons(self) -> None:
        result = minimum_lot_position(
            10, 9.9, 100_000, 2_000, Decimal("20"), Decimal("20"), 0, 80,
            rules=RULES_100,
            fees=FEES,
        )

        self.assertEqual(
            result.reason,
            "gap_reentry_per_trade_and_portfolio_risk_exceeded",
        )
        self.assertEqual(
            result.risk_reasons,
            ("PER_TRADE_RISK_EXCEEDED", "PORTFOLIO_OPEN_RISK_EXCEEDED"),
        )
        self.assertGreater(result.risk_pct, Decimal("0.02"))
        self.assertEqual(minimum_lot_position(
            10, 9.9, 100_000, 2_000, Decimal("100"), Decimal("100"), 0, 80,
            rules=RULES_100,
        ).reason, "gap_reentry_fee_schedule_required")

    def test_minimum_lot_decisions_cannot_be_flipped_without_input_evidence(self) -> None:
        cash = minimum_lot_position(
            76, 72, 100_000, 7_000, Decimal("500"), Decimal("1000"), 20, 80,
            rules=RULES_100,
            fees=FEES,
        )
        risk = evaluate_minimum_lot_risk(
            Decimal("80"), Decimal("200"), Decimal("100"),
        )
        allowed = minimum_lot_position(
            76, 72, 100_000, 10_000, Decimal("500"), Decimal("1000"), 20, 80,
            rules=RULES_100,
            fees=FEES,
        )

        with self.assertRaises(ValueError):
            replace(cash, allowed=True, reason="", qty=100)
        with self.assertRaises(ValueError):
            replace(risk, allowed=True, reasons=())
        with self.assertRaises(ValueError):
            replace(allowed, available_cash_yuan=Decimal("0"))

    def test_minimum_lot_is_independent_of_global_decimal_precision(self) -> None:
        results = []
        for precision in (8, 12, 28):
            for rounding in (ROUND_DOWN, ROUND_UP):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    results.append(minimum_lot_position(
                        Decimal("10"), Decimal("9.9"), Decimal("99999.99"),
                        Decimal("2000"), Decimal("100"), Decimal("100"),
                        Decimal("0"), Decimal("80"),
                        rules=RULES_100,
                        max_single_position_pct=Decimal("1.000000100000005"),
                        fees=FEES,
                    ))

        self.assertTrue(all(result == results[0] for result in results))
        with localcontext() as context:
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            trapped = minimum_lot_position(
                Decimal("10"), Decimal("9.9"), Decimal("99999.99"),
                Decimal("2000"), Decimal("100"), Decimal("100"),
                Decimal("0"), Decimal("80"),
                rules=RULES_100,
                max_single_position_pct=Decimal("1.000000100000005"),
                fees=FEES,
            )
        self.assertEqual(trapped, results[0])

        fine_tick_rules = InstrumentRules.a_share(
            "600000", price_tick=Decimal("0.00000001"),
        )
        with localcontext() as context:
            context.prec = 5
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            fine_tick = minimum_lot_position(
                Decimal("123456789.12345678"),
                Decimal("123456788.12345678"),
                Decimal("100000000000"),
                Decimal("20000000000"),
                Decimal("1000000000"),
                Decimal("1000000000"),
                Decimal("0"),
                Decimal("80"),
                rules=fine_tick_rules,
                fees=FEES,
            )
        self.assertTrue(fine_tick.allowed)


if __name__ == "__main__":
    unittest.main()
