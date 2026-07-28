import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backtest_engine import BacktestConfig, BacktestEngine
from execution_contracts import FeeSchedule

from historical_backtest import (
    EquityPoint,
    HistoricalBacktestConfig,
    HistoricalBacktestResult,
    HistoricalTrade,
    build_walk_forward_windows,
    compare_results,
    compute_metrics,
    group_metrics,
    run_historical_backtest,
    sensitivity_matrix,
)
from historical_data import HistoricalStore
from historical_strategy import Candidate
from paper_trading import apply_paper_trades, new_account, summarize_account


class HistoricalBacktestTest(unittest.TestCase):
    def test_reports_versioned_fee_components(self) -> None:
        fees = FeeSchedule(
            version="test-v1", effective_from="2026-01-01",
            buy_commission_rate=Decimal("0.0003"), sell_commission_rate=Decimal("0.0003"),
            buy_minimum_commission_yuan=Decimal("5"),
            sell_minimum_commission_yuan=Decimal("5"), stamp_tax_rate=Decimal("0.0005"),
            transfer_fee_rate=Decimal("0.00001"), other_fee_rate=Decimal("0"),
            buy_slippage_rate=Decimal("0.001"), sell_slippage_rate=Decimal("0.001"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [("2025-01-02", 9.8, 10.2, 9.7, 10, 0, 11, 9), ("2025-01-03", 10, 10.5, 9.8, 10.4, 0, 11, 9)],
            )
            config = HistoricalBacktestConfig(initial_cash=11_000, fee_schedule=fees)
            with patch("historical_backtest.generate_daily_candidates", side_effect=[[self._candidate()], []]):
                result = run_historical_backtest(store, "d1", "2025-01-02", "2025-01-03", config)

        self.assertEqual(result.metadata["fee_schedule_version"], "test-v1")
        self.assertEqual(result.metadata["fee_schedule_sha256"], fees.contract_sha256)
        self.assertEqual(result.trades[0].price, 10.0)
        self.assertEqual(result.trades[0].fee, 6.01)
        self.assertEqual(result.trades[0].fee_components, {
            "commission_yuan": 5.0,
            "stamp_tax_yuan": 0.0,
            "transfer_fee_yuan": 0.01,
            "other_fee_yuan": 0.0,
            "slippage_yuan": 1.0,
        })

    def _store(self, root: Path, days: list[tuple]) -> HistoricalStore:
        store = HistoricalStore(root / "history.db")
        store.initialize()
        with store.connect() as connection:
            for day, open_, high, low, close, suspended, limit_up, limit_down in days:
                connection.execute(
                    "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("d1", day, "600000", open_, high, low, close, close, 100000, close * 100000, 1),
                )
                connection.execute(
                    "INSERT INTO daily_status VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("d1", day, "600000", 1, 0, suspended, limit_up, limit_down),
                )
                connection.execute(
                    "INSERT INTO daily_universe VALUES (?, ?, ?)", ("d1", day, "600000")
                )
        return store

    def _candidate(
        self, stop: float = 9.0, *, take_profit: float = 12.0, position_pct: float = 10.0
    ) -> Candidate:
        return Candidate(
            "600000", 90, position_pct, 10, stop, take_profit, 0.3,
            "short", "NORMAL", "bank", "value", {"proxy_only": True},
        )

    def test_partial_exits_allocate_all_entry_cost_exactly_once(self) -> None:
        fees = FeeSchedule.simulation(
            version="partial-v1", transfer_fee_rate=0, other_fee_rate=0
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [
                    ("2025-01-02", 10, 10.1, 9.9, 10, 0, 11, 9),
                    ("2025-01-03", 10, 10.2, 9.9, 10, 0, 11, 9),
                    ("2025-01-06", 11, 11.2, 10.5, 11, 0, 12, 9),
                    ("2025-01-07", 8.8, 9, 8.5, 8.8, 0, 9.6, 8),
                ],
            )
            candidate = self._candidate(take_profit=11, position_pct=20)
            with patch(
                "historical_backtest.generate_daily_candidates",
                side_effect=[[candidate], [], [], []],
            ):
                result = run_historical_backtest(
                    store, "d1", "2025-01-02", "2025-01-07",
                    HistoricalBacktestConfig(initial_cash=10_000, fee_schedule=fees),
                )

        buy = next(trade for trade in result.trades if trade.action == "buy")
        sells = [trade for trade in result.trades if trade.action == "sell"]
        self.assertEqual([trade.quantity for trade in sells], [100, 100])
        self.assertEqual(
            round(sum(trade.entry_fee_allocated_yuan for trade in sells), 2), buy.fee
        )

    def test_three_engines_share_one_lot_and_round_trip_net_pnl(self) -> None:
        fees = FeeSchedule.simulation(
            version="engine-equality-v1", transfer_fee_rate=0, other_fee_rate=0
        )
        signal = BacktestEngine(
            BacktestConfig(initial_cash=10_000, fee_schedule=fees)
        ).run(
            [
                {"date": "2025-01-03", "code": "600000", "action": "buy", "price": 10,
                 "entry_price": 10, "position_pct": 10},
                {"date": "2025-01-06", "code": "600000", "action": "sell", "price": 10.1},
            ]
        )

        account = new_account(10_000)
        apply_paper_trades(
            account,
            pd.DataFrame([{"code": "600000", "price": 10, "entry_price": 10,
                           "stop_loss": 9, "take_profit": 10.1, "position_pct": 10,
                           "final_score": 90}]),
            trade_date="2025-01-03", fee_schedule=fees,
        )
        paper_sell = apply_paper_trades(
            account,
            pd.DataFrame([{"code": "600000", "price": 10.1,
                           "stop_loss": 9, "take_profit": 10.1}]),
            trade_date="2025-01-06", fee_schedule=fees,
        )[0]

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [
                    ("2025-01-02", 10, 10, 9.9, 10, 0, 11, 9),
                    ("2025-01-03", 10, 10, 9.9, 10, 0, 11, 9),
                    ("2025-01-06", 10.1, 10.1, 10, 10.1, 0, 11, 9),
                ],
            )
            candidate = self._candidate(take_profit=10.1)
            with patch(
                "historical_backtest.generate_daily_candidates",
                side_effect=[[candidate], [], []],
            ):
                historical = run_historical_backtest(
                    store, "d1", "2025-01-02", "2025-01-06",
                    HistoricalBacktestConfig(initial_cash=10_000, fee_schedule=fees),
                )

        quantities = [
            signal.trades[0]["qty"],
            account["trades"][0]["qty"],
            historical.trades[0].quantity,
        ]
        pnls = [
            signal.trades[-1]["pnl"],
            paper_sell["pnl"],
            historical.trades[-1].pnl,
        ]
        self.assertEqual(quantities, [100, 100, 100])
        self.assertEqual(pnls, [-2.52, -2.52, -2.52])
        metrics = compute_metrics(historical.equity, historical.trades)
        self.assertEqual(metrics.win_rate, 0)
        self.assertEqual(metrics.profit_factor, 0)

    def test_close_decision_executes_next_open_with_lot_slippage_and_fee(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [("2025-01-02", 9.8, 10.2, 9.7, 10, 0, 11, 9), ("2025-01-03", 10, 10.5, 9.8, 10.4, 0, 11, 9)],
            )
            with patch("historical_backtest.generate_daily_candidates", side_effect=[[self._candidate()], []]):
                result = run_historical_backtest(store, "d1", "2025-01-02", "2025-01-03", HistoricalBacktestConfig())

            trade = result.trades[0]
            self.assertEqual(trade.decision_date, "2025-01-02")
            self.assertEqual(trade.trade_date, "2025-01-03")
            self.assertEqual(trade.quantity, 1000)
            self.assertEqual(trade.price, 10.0)
            self.assertEqual(trade.fee, 15.0)
            self.assertEqual(trade.slippage_yuan, 10.0)

    def test_suspension_and_limit_up_block_buy(self) -> None:
        for suspended, limit_up in [(1, 11), (0, 10)]:
            with self.subTest(suspended=suspended, limit_up=limit_up), tempfile.TemporaryDirectory() as tmp:
                store = self._store(
                    Path(tmp),
                    [("2025-01-02", 9.8, 10, 9.7, 9.9, 0, 11, 9), ("2025-01-03", 10, 10, 10, 10, suspended, limit_up, 9)],
                )
                with patch("historical_backtest.generate_daily_candidates", side_effect=[[self._candidate()], []]):
                    result = run_historical_backtest(store, "d1", "2025-01-02", "2025-01-03", HistoricalBacktestConfig())
                self.assertEqual(result.trades, [])

    def test_hard_stop_gap_uses_tradable_open_not_stop_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [
                    ("2025-01-02", 9.8, 10.2, 9.7, 10, 0, 11, 9),
                    ("2025-01-03", 10, 10.4, 9.8, 10.2, 0, 11, 9),
                    ("2025-01-06", 8, 8.5, 7.8, 8.2, 0, 9, 7),
                ],
            )
            with patch("historical_backtest.generate_daily_candidates", side_effect=[[self._candidate(9)], [], []]):
                result = run_historical_backtest(store, "d1", "2025-01-02", "2025-01-06", HistoricalBacktestConfig())

            sell = result.trades[-1]
            self.assertEqual(sell.action, "sell")
            self.assertEqual(sell.reason, "HARD_STOP")
            self.assertEqual(sell.price, 8.0)
            self.assertEqual(sell.slippage_yuan, 8.0)

    def test_same_bar_stop_wins_and_first_profit_takes_only_half(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [
                    ("2025-01-02", 9.8, 10.2, 9.7, 10, 0, 11, 9),
                    ("2025-01-03", 10, 10.4, 9.8, 10.2, 0, 11, 9),
                    ("2025-01-06", 10.3, 12.5, 9.5, 11.8, 0, 13, 9),
                ],
            )
            with patch("historical_backtest.generate_daily_candidates", side_effect=[[self._candidate(9)], [], []]):
                result = run_historical_backtest(store, "d1", "2025-01-02", "2025-01-06", HistoricalBacktestConfig())

            sell = result.trades[-1]
            self.assertEqual(sell.reason, "TAKE_PROFIT_1")
            self.assertEqual(sell.quantity, 500)

            with store.connect() as connection:
                connection.execute(
                    "UPDATE daily_bars SET low = 8.5 WHERE dataset_id = 'd1' AND trade_date = '2025-01-06'"
                )
            with patch("historical_backtest.generate_daily_candidates", side_effect=[[self._candidate(9)], [], []]):
                conservative = run_historical_backtest(store, "d1", "2025-01-02", "2025-01-06", HistoricalBacktestConfig())
            self.assertEqual(conservative.trades[-1].reason, "HARD_STOP")

    def test_adjustment_factor_change_preserves_position_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [
                    ("2025-01-02", 9.8, 10.2, 9.7, 10, 0, 11, 9),
                    ("2025-01-03", 10, 10.4, 9.8, 10.2, 0, 11, 9),
                    ("2025-01-06", 5.1, 5.4, 5.0, 5.2, 0, 5.7, 4.6),
                ],
            )
            with store.connect() as connection:
                connection.execute(
                    "UPDATE daily_bars SET adjust_factor = 2 "
                    "WHERE dataset_id = 'd1' AND trade_date = '2025-01-06'"
                )
            with patch("historical_backtest.generate_daily_candidates", side_effect=[[self._candidate(8)], [], []]):
                result = run_historical_backtest(store, "d1", "2025-01-02", "2025-01-06", HistoricalBacktestConfig())

            self.assertGreater(result.equity[-1].equity, 99_000)

    def test_metrics_and_top_three_robustness_are_hand_computable(self) -> None:
        equity = [
            EquityPoint("2025-01-01", 100, 100),
            EquityPoint("2025-01-02", 110, 110),
            EquityPoint("2025-01-03", 99, 99),
            EquityPoint("2025-01-04", 120, 120),
        ]
        trades = [
            HistoricalTrade("2025-01-01", "2025-01-02", "600000", "sell", 100, 11, 0, "X", 10, holding_days=1),
            HistoricalTrade("2025-01-01", "2025-01-03", "600001", "sell", 100, 9, 0, "X", -5, holding_days=2),
            HistoricalTrade("2025-01-01", "2025-01-04", "600002", "sell", 100, 12, 0, "X", 20, holding_days=3),
        ]

        metrics = compute_metrics(equity, trades)

        self.assertAlmostEqual(metrics.net_return, 0.2)
        self.assertAlmostEqual(metrics.max_drawdown, 0.1)
        self.assertAlmostEqual(metrics.win_rate, 2 / 3)
        self.assertAlmostEqual(metrics.profit_factor, 6.0)
        self.assertEqual(metrics.average_holding_days, 2.0)
        self.assertEqual(metrics.net_profit_without_top3, -5.0)

    def test_walk_forward_windows_are_ordered_and_non_overlapping(self) -> None:
        dates = [f"2025-01-{day:02d}" for day in range(1, 17)]
        windows = build_walk_forward_windows(dates, count=3)

        self.assertEqual(len(windows), 3)
        for window in windows:
            self.assertLess(window.training_end, window.validation_start)
        self.assertLess(windows[0].validation_end, windows[1].validation_start)

    def test_comparison_contract_mismatch_is_rejected(self) -> None:
        baseline = HistoricalBacktestResult(metadata={"dataset_hash": "a", "window": "w", "fees": 1})
        candidate = HistoricalBacktestResult(metadata={"dataset_hash": "b", "window": "w", "fees": 1})

        comparison = compare_results(baseline, candidate)

        self.assertEqual(comparison["status"], "COMPARISON_CONTRACT_MISMATCH")
        self.assertIn("dataset_hash", comparison["mismatches"])

    def test_comparison_rejects_different_fee_contract_hashes(self) -> None:
        common = {"dataset_hash": "a", "window": "w"}
        baseline = HistoricalBacktestResult(
            metadata={**common, "fee_schedule_sha256": "a" * 64}
        )
        candidate = HistoricalBacktestResult(
            metadata={**common, "fee_schedule_sha256": "b" * 64}
        )

        comparison = compare_results(baseline, candidate)

        self.assertEqual(comparison["status"], "COMPARISON_CONTRACT_MISMATCH")
        self.assertIn("fee_schedule_sha256", comparison["mismatches"])

        version_only = compare_results(
            HistoricalBacktestResult(metadata={**common, "fee_schedule_version": "v1"}),
            HistoricalBacktestResult(metadata={**common, "fee_schedule_version": "v2"}),
        )
        self.assertEqual(version_only["status"], "COMPARISON_CONTRACT_MISMATCH")
        self.assertIn("fee_schedule_version", version_only["mismatches"])

        for fee_hash in (None, "x"):
            with self.subTest(fee_hash=fee_hash):
                metadata = {
                    **common, "fee_schedule_version": "v1",
                    "fee_schedule_sha256": fee_hash,
                }
                invalid = compare_results(
                    HistoricalBacktestResult(metadata=metadata),
                    HistoricalBacktestResult(metadata=metadata),
                )
                self.assertEqual(
                    invalid["status"], "COMPARISON_CONTRACT_MISMATCH"
                )
                self.assertIn("fee_schedule_sha256", invalid["mismatches"])

    def test_group_metrics_is_bounded_and_sensitivity_changes_execution_only(self) -> None:
        trades = [
            HistoricalTrade("2025-01-01", "2025-01-02", f"{i:06d}", "sell", 100, 10, 0, "X", i - 2, strategy_mode="short", market_regime="NORMAL", score=80 + i, industry=str(i), theme="unknown")
            for i in range(25)
        ]
        grouped = group_metrics(trades, ("strategy_mode", "market_regime", "score_band", "industry", "theme"))
        self.assertIn("unknown", grouped["theme"])
        self.assertLessEqual(len(grouped["industry"]), 21)

        seen = []
        def factory(config):
            seen.append(config)
            return HistoricalBacktestResult(metadata={"dataset_hash": "a", "window": "w"})
        matrix = sensitivity_matrix(factory, HistoricalBacktestConfig())
        self.assertEqual(set(matrix), {"zero_slippage", "base", "double_slippage", "double_fees"})
        self.assertTrue(all(item.mode == HistoricalBacktestConfig().mode for item in seen))
        schedules = [item.resolved_fee_schedule() for item in seen]
        self.assertEqual(len({item.version for item in schedules}), 4)
        self.assertEqual(len({item.contract_sha256 for item in schedules}), 4)

    def test_short_time_stop_decides_at_close_and_sells_next_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            days = [
                ("2025-01-02", 9.8, 10.2, 9.7, 10.0, 0, 11, 9),
                ("2025-01-03", 10.0, 10.3, 9.8, 10.1, 0, 11, 9),
                ("2025-01-06", 10.1, 10.3, 9.9, 10.1, 0, 11, 9),
                ("2025-01-07", 10.1, 10.3, 9.9, 10.1, 0, 11, 9),
                ("2025-01-08", 10.1, 10.3, 9.9, 10.1, 0, 11, 9),
                ("2025-01-09", 10.0, 10.2, 9.8, 10.0, 0, 11, 9),
            ]
            store = self._store(Path(tmp), days)
            with patch("historical_backtest.generate_daily_candidates", side_effect=[[self._candidate(9)], [], [], [], [], []]):
                result = run_historical_backtest(store, "d1", "2025-01-02", "2025-01-09", HistoricalBacktestConfig())

            sell = result.trades[-1]
            self.assertEqual(sell.reason, "TIME_STOP")
            self.assertEqual(sell.decision_date, "2025-01-08")
            self.assertEqual(sell.trade_date, "2025-01-09")


if __name__ == "__main__":
    unittest.main()
