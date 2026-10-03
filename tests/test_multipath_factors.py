from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from candidate_channels import build_factor_channel_rows, merge_candidate_channels
from factor_contracts import FactorContractError, normalize_factor_bars
from factor_limitdown import evaluate_limitdown_exhaustion_factor
from factor_research import evaluate_factor_release_gate
from factor_wave3 import evaluate_wave3_factor
from strategy_economics import estimate_round_trip_economics, expected_net_return_gate, size_strategy_order
from strategy_exit_runtime import evaluate_factor_exit


SHANGHAI = timezone(timedelta(hours=8))
DECISION = "2026-07-01T10:05:00+08:00"
FEE = {
    "buy_commission_rate": "0.0003",
    "sell_commission_rate": "0.0003",
    "buy_minimum_commission_yuan": "5",
    "sell_minimum_commission_yuan": "5",
    "stamp_tax_rate": "0.0005",
    "transfer_fee_rate": "0",
    "other_fee_rate": "0",
    "buy_slippage_rate": "0.001",
    "sell_slippage_rate": "0.001",
}


def _daily(closes: list[float], volumes: list[float] | None = None) -> list[dict]:
    start = date(2026, 4, 1)
    result = []
    for index, close in enumerate(closes):
        day = start + timedelta(days=index)
        volume = (volumes or [1000.0] * len(closes))[index]
        result.append({
            "available_at": datetime.combine(day, datetime.min.time()).replace(
                hour=15, tzinfo=SHANGHAI
            ).isoformat(),
            "open": close - 0.05,
            "high": close + 0.12,
            "low": close - 0.12,
            "close": close,
            "volume": volume,
            "money": close * volume,
            "factor": 1.0,
            "prev_close": closes[index - 1] if index else close,
            "low_limit": round((closes[index - 1] if index else close) * 0.9, 2),
        })
    return result


def _wave_history() -> list[dict]:
    closes = [
        12.0, 11.8, 11.6, 11.4, 11.2, 11.0, 10.8, 10.6, 10.4, 10.2,
        10.0, 10.3, 10.8, 11.6, 12.4, 13.0,
        12.8, 12.4, 11.9, 11.7,
        11.8, 12.4, 13.2, 14.0, 14.7, 15.0,
        14.7, 14.2, 13.8, 13.5,
        13.7, 14.0, 14.2, 14.3, 14.4,
    ]
    volumes = [800.0] * 10
    volumes += [1000.0] * 6
    volumes += [450.0] * 4
    volumes += [1000.0] * 6
    volumes += [450.0] * 4
    volumes += [700.0] * 5
    return _daily(closes, volumes)


def _intraday(base: float, prices: list[float], previous_close: float) -> list[dict]:
    start = datetime(2026, 7, 1, 9, 35, tzinfo=SHANGHAI)
    rows = []
    for index, close in enumerate(prices):
        volume = 1000.0
        rows.append({
            "available_at": (start + timedelta(minutes=index * 5)).isoformat(),
            "open": base if index == 0 else prices[index - 1],
            "high": close + 0.02,
            "low": min(close, base) - 0.02,
            "close": close,
            "volume": volume,
            "money": close * volume,
            "prev_close": previous_close,
            "low_limit": round(previous_close * 0.9, 2),
        })
    return rows


class MultipathFactorTest(unittest.TestCase):

    def test_expected_net_return_gate_subtracts_round_trip_costs(self) -> None:
        rejected = expected_net_return_gate(10, 100, 20, FEE, minimum_net_return_bps=20)
        accepted = expected_net_return_gate(10, 100, 200, FEE, minimum_net_return_bps=20)
        self.assertFalse(rejected["allowed"])
        self.assertEqual(rejected["reason"], "expected_net_return_below_threshold")
        self.assertTrue(accepted["allowed"])
        self.assertLess(accepted["expected_net_return_bps"], 200)
    def test_future_factor_bar_is_rejected(self) -> None:
        with self.assertRaisesRegex(FactorContractError, "FACTOR_BAR_FROM_FUTURE"):
            normalize_factor_bars(
                [{"available_at": "2026-07-01T10:10:00+08:00", "close": 10}],
                DECISION,
            )

    def test_candidate_channel_cannot_hide_future_factor_evidence(self) -> None:
        market = pd.DataFrame([{
            "code": "600001", "name": "A", "price": 10.0,
            "amount": 100_000_000, "pct_chg": 1.0,
            "listing_days": 500,
        }])
        with self.assertRaisesRegex(FactorContractError, "FACTOR_BAR_FROM_FUTURE"):
            build_factor_channel_rows(
                market,
                "2026-07-01T10:00:00+08:00",
                lambda _code: [{
                    "available_at": "2026-07-01T10:05:00+08:00",
                    "open": 10, "high": 10, "low": 10, "close": 10,
                    "volume": 1, "money": 10, "factor": 1,
                }],
            )

    def test_wave3_requires_structure_and_closed_five_minute_breakout(self) -> None:
        decision = evaluate_wave3_factor(
            "600001",
            DECISION,
            _wave_history(),
            _intraday(14.35, [14.40, 14.45, 14.50, 14.54], 14.4),
            industry_relative_strength=0.05,
            market_state="NORMAL",
            theme_heat_score=20,
        )
        self.assertTrue(decision.eligible)
        self.assertTrue(decision.triggered, decision.to_dict())
        self.assertGreaterEqual(decision.score, 75)
        self.assertLess(decision.stop_loss, decision.entry_price)
        self.assertGreater(decision.take_profit, decision.entry_price)
        self.assertEqual(decision.max_hold_days, 10)

    def test_wave3_never_triggers_without_four_closed_intraday_bars(self) -> None:
        decision = evaluate_wave3_factor(
            "600001",
            DECISION,
            _wave_history(),
            _intraday(14.35, [14.40, 14.45, 14.50], 14.4),
            industry_relative_strength=0.05,
        )
        self.assertTrue(decision.eligible)
        self.assertFalse(decision.triggered)
        self.assertEqual(decision.rejection_code, "wave3_trigger_pending")

    def test_limitdown_path_waits_for_next_day_0950_confirmation(self) -> None:
        closes = [10.0] * 22 + [9.0, 8.1, 8.0]
        daily = _daily(closes, [1000.0] * 22 + [1200.0, 1300.0, 2500.0])
        daily[22].update({"high": 9.0, "low": 9.0, "low_limit": 9.0})
        daily[23].update({"high": 8.1, "low": 8.1, "low_limit": 8.1})
        daily[24].update({
            "open": 7.30, "high": 8.20, "low": 7.29, "close": 8.0,
            "low_limit": 7.29, "money": 250_000_000.0, "volume": 30_000_000.0,
        })
        for row in daily[:24]:
            row["money"] = 100_000_000.0
            row["volume"] = 10_000_000.0
        waiting = evaluate_limitdown_exhaustion_factor(
            "600002", DECISION, daily, (), listing_days=300,
        )
        self.assertTrue(waiting.eligible)
        self.assertFalse(waiting.triggered)
        self.assertEqual(waiting.state, "open_confirmed")
        confirmed = evaluate_limitdown_exhaustion_factor(
            "600002",
            DECISION,
            daily,
            _intraday(8.05, [8.10, 8.15, 8.20, 8.25], 8.0),
            listing_days=300,
        )
        self.assertTrue(confirmed.triggered, confirmed.to_dict())
        self.assertEqual(confirmed.max_hold_days, 3)
        too_early = evaluate_limitdown_exhaustion_factor(
            "600002",
            "2026-07-01T09:45:00+08:00",
            daily,
            _intraday(8.05, [8.10, 8.15, 8.20], 8.0),
            listing_days=300,
        )
        self.assertFalse(too_early.triggered)
        self.assertEqual(too_early.rejection_code, "limitdown_wait_next_day_confirm")

    def test_channel_merge_dedupes_and_triggered_factor_overrides_momentum(self) -> None:
        momentum = pd.DataFrame([{
            "code": "600001", "name": "A", "price": 10, "amount": 1e8,
            "pct_chg": 5, "score": 90,
        }])
        factor = pd.DataFrame([{
            "code": "600001", "name": "A", "price": 10, "amount": 1e8,
            "pct_chg": 5, "score": 80, "factor_path": "wave3_v1",
            "candidate_channels": "wave3_v1", "factor_triggered": True,
            "factor_score": 82, "simulation_only": True,
        }])
        merged = merge_candidate_channels(momentum, factor)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged.iloc[0]["factor_path"], "wave3_v1")
        self.assertEqual(
            merged.iloc[0]["candidate_channels"], "momentum_v1|wave3_v1"
        )
        self.assertEqual(
            [item["factor_path"] for item in merged.iloc[0]["factor_attributions"]],
            ["wave3_v1", "momentum_v1"],
        )

    def test_exact_economic_gate_floors_lots_and_rejects_small_expensive_trade(self) -> None:
        expensive = size_strategy_order(
            10, 9, 12, 100_000, 100_000, 0.5, 1.0, FEE,
        )
        self.assertFalse(expensive["allowed"])
        self.assertEqual(expensive["reason"], "factor_round_trip_cost_rate_high")
        allowed = size_strategy_order(
            10, 9, 13, 1_000_000, 1_000_000, 0.5, 12.0, FEE,
        )
        self.assertTrue(allowed["allowed"], allowed)
        self.assertEqual(allowed["target_qty"] % 100, 0)
        economics = estimate_round_trip_economics(
            10, 13, allowed["target_qty"], FEE
        )
        self.assertGreater(economics["net_profit_yuan"], 0)

    def test_path_specific_exit_rules_and_release_gate(self) -> None:
        wave = evaluate_factor_exit(
            "wave3_v1", 10, 9, 10.2, 10.5, 0.3, 5, "NORMAL"
        )
        self.assertEqual(wave["action"], "exit_all")
        limitdown = evaluate_factor_exit(
            "limitdown_exhaustion_v1", 10, 9, 11.0, 11.2, 0.3, 1, "NORMAL"
        )
        self.assertEqual(limitdown["action"], "raise_stop")
        gate = evaluate_factor_release_gate(
            "wave3_v1", [0.02] * 40 + [-0.01] * 15,
            [0.01, 0.02, -0.01], 0.05, 0.052,
        )
        self.assertTrue(gate["passed"], gate)


if __name__ == "__main__":
    unittest.main()
