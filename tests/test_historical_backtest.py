import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backtest_engine import BacktestConfig, BacktestEngine
from execution_contracts import FeeSchedule

from historical_backtest import (
    DecisionTimeReplayResult,
    EquityPoint,
    HistoricalBacktestConfig,
    HistoricalBacktestResult,
    HistoricalTrade,
    build_walk_forward_windows,
    compare_results,
    compute_metrics,
    group_metrics,
    run_decision_time_replay,
    run_historical_backtest,
    sensitivity_matrix,
)
from historical_data import HistoricalDataValidationError, HistoricalStore, strict_table_hash
from historical_strategy import Candidate
from ml_contracts import CandidateSample, TimedFeature
from paper_trading import apply_paper_trades, new_account, summarize_account


class HistoricalBacktestTest(unittest.TestCase):
    @staticmethod
    def _strict_sample(decision_at: str, *, selected: bool = True) -> CandidateSample:
        return CandidateSample.from_values(
            source="strict_history",
            dataset_id="strict-1",
            decision_at=decision_at,
            code="600000",
            strategy_version="strategy-v1",
            parameter_version="params-v1",
            feature_schema_version="features-v1",
            features={
                "price": TimedFeature(10.5, decision_at),
                "market_regime": TimedFeature("NORMAL", decision_at),
            },
            selected=selected,
            rejection_stage="selected" if selected else "score",
            rejection_code="" if selected else "BELOW_SCORE",
            final_action="selected" if selected else "score_rejected",
            universe_hash="universe-sha",
            market_data_version="market-v1",
            code_hash="code-sha",
            generator_hash="generator-sha",
        )

    @staticmethod
    def _strict_manifest(samples: list[CandidateSample]) -> dict[str, object]:
        cohorts = {}
        for decision_at in sorted({sample.decision_at for sample in samples}):
            rows = [sample for sample in samples if sample.decision_at == decision_at]
            cohorts[decision_at] = {
                "codes": [sample.code for sample in rows],
                "universe_hash": rows[0].universe_hash,
            }
        return {
            "dataset_id": "strict-1",
            "source": "strict_history",
            "strategy_version": "strategy-v1",
            "parameter_version": "params-v1",
            "feature_schema_version": "features-v1",
            "market_data_version": "market-v1",
            "code_hash": "code-sha",
            "generator_hash": "generator-sha",
            "adjustment_version": "raw-v1",
            "cohorts": cohorts,
            "table_hashes": {
                "decision_candidates": strict_table_hash(
                    "decision_candidates", samples
                ),
                "candidate_prices": "",
            },
        }

    @staticmethod
    def _strict_replay_config() -> dict[str, str]:
        return {
            "strategy_version": "strategy-v1",
            "parameter_version": "params-v1",
            "feature_schema_version": "features-v1",
            "market_data_version": "market-v1",
            "code_hash": "code-sha",
            "generator_hash": "generator-sha",
        }

    def test_decision_time_replay_is_exact_hash_bound_and_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            samples = [
                self._strict_sample("2025-01-02T10:00:00+08:00"),
                self._strict_sample(
                    "2025-01-02T10:05:00+08:00", selected=False
                ),
            ]
            store.import_candidate_cohorts(
                samples, manifest=self._strict_manifest(samples)
            )
            expected_hash = store.dataset_hash("strict-1")

            with patch.object(
                store, "daily_slice", side_effect=AssertionError("current cache")
            ), patch(
                "historical_strategy.fetch_live_quotes",
                side_effect=AssertionError("network"),
            ):
                replay = run_decision_time_replay(
                    store,
                    "strict-1",
                    "2025-01-02T09:55:00+08:00",
                    "2025-01-02T10:10:00+08:00",
                    strategy_config=self._strict_replay_config(),
                    expected_dataset_hash=expected_hash,
                )

            self.assertIsInstance(replay, DecisionTimeReplayResult)
            self.assertEqual(replay.dataset_sha256, expected_hash)
            self.assertEqual(
                [batch.decision_at for batch in replay.batches],
                [sample.decision_at for sample in samples],
            )
            self.assertEqual(
                [batch.candidate_count for batch in replay.batches],
                [1, 1],
            )
            self.assertEqual(
                [batch.selected_count for batch in replay.batches],
                [1, 0],
            )
            self.assertEqual(replay.candidate_count, 2)
            self.assertEqual(replay.selected_count, 1)

    def test_decision_time_replay_rejects_dataset_hash_mismatch_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            with patch.object(
                store, "decision_times", side_effect=AssertionError("must not read")
            ), self.assertRaisesRegex(
                HistoricalDataValidationError, "STRICT_DATASET_HASH_MISMATCH"
            ):
                run_decision_time_replay(
                    store,
                    "strict-1",
                    "2025-01-02T09:55:00+08:00",
                    "2025-01-02T10:10:00+08:00",
                    strategy_config=self._strict_replay_config(),
                    expected_dataset_hash="0" * 64,
                )

    def test_daily_backtest_does_not_auto_switch_to_decision_time_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [("2025-01-02", 10, 10.2, 9.8, 10, 0, 11, 9)],
            )
            with patch(
                "historical_backtest.generate_daily_candidates", return_value=[]
            ) as daily, patch(
                "historical_backtest.generate_candidates_at",
                side_effect=AssertionError("decision-time replay must be explicit"),
            ):
                result = run_historical_backtest(
                    store,
                    "d1",
                    "2025-01-02",
                    "2025-01-02",
                    HistoricalBacktestConfig(mode="strict"),
                )

            daily.assert_called_once()
            self.assertEqual(result.trades, [])

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
                {"date": "2025-01-06", "code": "600000", "action": "sell", "price": 8.9},
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
            pd.DataFrame([{"code": "600000", "price": 8.9,
                           "stop_loss": 9, "take_profit": 12}]),
            trade_date="2025-01-06", fee_schedule=fees,
        )[0]

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(
                Path(tmp),
                [
                    ("2025-01-02", 10, 10, 9.9, 10, 0, 11, 9),
                    ("2025-01-03", 10, 10, 9.9, 10, 0, 11, 9),
                    ("2025-01-06", 8.9, 9, 8.8, 8.9, 0, 10, 8),
                ],
            )
            candidate = self._candidate(take_profit=12)
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
        self.assertEqual(pnls, [-122.34, -122.34, -122.34])
        metrics = compute_metrics(historical.equity, historical.trades)
        self.assertEqual(metrics.win_rate, 0)
        self.assertEqual(metrics.profit_factor, 0)

    def test_strict_backtest_uses_one_lot_and_odd_lot_profit_protection(self) -> None:
        days = [
            ("2025-01-02", 10, 10.1, 9.9, 10, 0, 11, 9),
            ("2025-01-03", 10, 10.1, 9.9, 10, 0, 11, 9),
            ("2025-01-06", 11.8, 12.2, 11.8, 12, 0, 13, 10),
            ("2025-01-07", 11.7, 11.9, 11.2, 11.8, 0, 13, 10),
            ("2025-01-08", 11.2, 11.4, 11, 11.1, 0, 12, 10),
        ]
        for initial_qty, first_sell_qty in ((100, 0), (300, 100), (500, 200)):
            with self.subTest(initial_qty=initial_qty), tempfile.TemporaryDirectory() as tmp:
                store = self._store(Path(tmp), days)
                candidate = self._candidate(take_profit=12, position_pct=100)
                with patch(
                    "historical_backtest.generate_daily_candidates",
                    side_effect=[[candidate], [], [], [], []],
                ):
                    result = run_historical_backtest(
                        store,
                        "d1",
                        "2025-01-02",
                        "2025-01-08",
                        HistoricalBacktestConfig(
                            initial_cash=initial_qty * 10,
                            commission_rate=0,
                            minimum_commission=0,
                            stamp_tax_rate=0,
                            slippage_bps=0,
                        ),
                    )

                first_profit_sells = [
                    trade for trade in result.trades
                    if trade.reason == "TAKE_PROFIT_1"
                ]
                self.assertEqual(
                    sum(trade.quantity for trade in first_profit_sells),
                    first_sell_qty,
                )
                if initial_qty == 100:
                    trailing = [
                        trade for trade in result.trades
                        if trade.reason == "TRAILING_STOP"
                    ]
                    self.assertEqual([trade.quantity for trade in trailing], [100])
                    self.assertEqual(trailing[0].trade_date, "2025-01-07")

    def test_trailing_stop_does_not_use_same_day_future_high(self) -> None:
        days = [
            ("2025-01-02", 10, 10.1, 9.9, 10, 0, 11, 9),
            ("2025-01-03", 10, 10.1, 9.9, 10, 0, 11, 9),
            ("2025-01-06", 11.8, 12.2, 11.8, 12, 0, 13, 10),
            ("2025-01-07", 12.3, 14, 12, 13.5, 0, 15, 11),
            ("2025-01-08", 13.3, 13.5, 13.2, 13.4, 0, 15, 12),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp), days)
            candidate = self._candidate(take_profit=12, position_pct=50)
            with patch(
                "historical_backtest.generate_daily_candidates",
                side_effect=[[candidate], [], [], [], []],
            ):
                result = run_historical_backtest(
                    store,
                    "d1",
                    "2025-01-02",
                    "2025-01-08",
                    HistoricalBacktestConfig(
                        initial_cash=4000,
                        commission_rate=0,
                        minimum_commission=0,
                        stamp_tax_rate=0,
                        slippage_bps=0,
                    ),
                )

        trailing = [trade for trade in result.trades if trade.reason == "TRAILING_STOP"]
        self.assertEqual([trade.trade_date for trade in trailing], ["2025-01-08"])

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
