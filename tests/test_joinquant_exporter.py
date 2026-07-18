import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import a_share_strategy
import exit_policy
import joinquant_exporter
from ml_store import MlCapacityError, MlStore
from trading_store import TradingStore


class JoinQuantExporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._ledger_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._ledger_tmp.cleanup)
        self._db_patch = patch.object(
            joinquant_exporter.app_config,
            "TRADING_DB_FILE",
            Path(self._ledger_tmp.name) / "trading.db",
        )
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)

    def test_confirmed_gap_reentry_creates_new_signal(self) -> None:
        row = pd.Series({
            "code": "002432", "name": "九安医疗", "price": 76.95,
            "entry_price": 76.95, "stop_loss": 72.0, "take_profit": 86.85,
            "position_pct": 9.0, "final_score": 90, "market_state": "NORMAL",
            "atr14": 2.0, "amount": 100_000_000,
            "entry_path": "gap_reentry", "gap_reentry_state": "OPEN_CONFIRMED",
            "parent_signal_id": "old-signal", "original_entry_price": 74.72,
            "original_stop_price": 69.49, "reentry_cap_price": 77.335,
        })
        with patch.object(joinquant_exporter.app_config, "GAP_REENTRY_ENABLE_DEFAULT", True):
            self.assertEqual(joinquant_exporter._buy_reject_reason(row, 75), "")
            signal = joinquant_exporter._buy_signal(row, "new-run", 0)
        self.assertEqual(signal["entry_path"], "gap_reentry")
        self.assertEqual(signal["parent_signal_id"], "old-signal")
        self.assertNotEqual(signal["id"], "old-signal")
        self.assertEqual(signal["position_pct"], 3.0)

    def test_gap_reentry_does_not_bypass_risk_off_or_cap(self) -> None:
        row = pd.Series({
            "code": "002432", "price": 76.95, "entry_price": 74.72,
            "stop_loss": 69.49, "take_profit": 85.0, "position_pct": 9.0,
            "final_score": 90, "market_state": "RISK_OFF", "atr14": 2.0,
            "amount": 100_000_000, "entry_path": "gap_reentry",
            "gap_reentry_state": "OPEN_CONFIRMED", "parent_signal_id": "old",
            "reentry_cap_price": 77.335,
        })
        with patch.object(joinquant_exporter.app_config, "GAP_REENTRY_ENABLE_DEFAULT", True):
            self.assertEqual(joinquant_exporter._buy_reject_reason(row, 75), "buy_disabled")
            self.assertEqual(
                joinquant_exporter._buy_reject_reason(
                    pd.Series({**row.to_dict(), "market_state": "NORMAL", "price": 77.40}),
                    75,
                ),
                "gap_reentry_too_far",
            )

    def test_gap_reentry_allows_one_lot_within_risk_budget(self) -> None:
        row = pd.Series({
            "code": "002432", "price": 76.0, "entry_price": 76.0,
            "stop_loss": 72.0, "take_profit": 84.0, "position_pct": 1.5,
            "final_score": 90, "market_state": "NORMAL", "atr14": 2.0,
            "amount": 100_000_000, "entry_path": "gap_reentry",
            "gap_reentry_state": "OPEN_CONFIRMED", "parent_signal_id": "old",
            "reentry_cap_price": 77.0,
        })
        with patch.object(joinquant_exporter.app_config, "GAP_REENTRY_ENABLE_DEFAULT", True):
            reason = joinquant_exporter._buy_reject_reason(
                row, 75, account_total_value=100_000, available_cash=10_000,
            )
            signal = joinquant_exporter._buy_signal(row, "run", 0)
        self.assertEqual(reason, "")
        self.assertEqual(signal["target_qty"], 100)
        self.assertAlmostEqual(signal["position_pct"], 7.6)

    def test_gap_reentry_one_lot_cannot_exceed_cash_or_risk(self) -> None:
        row = pd.Series({
            "code": "002432", "price": 76.0, "entry_price": 76.0,
            "stop_loss": 69.0, "take_profit": 90.0, "position_pct": 1.5,
            "final_score": 90, "market_state": "NORMAL", "atr14": 2.0,
            "amount": 100_000_000, "entry_path": "gap_reentry",
            "gap_reentry_state": "OPEN_CONFIRMED", "parent_signal_id": "old",
            "reentry_cap_price": 77.0,
        })
        with patch.object(joinquant_exporter.app_config, "GAP_REENTRY_ENABLE_DEFAULT", True):
            self.assertEqual(joinquant_exporter._buy_reject_reason(
                row.copy(), 75, account_total_value=100_000, available_cash=7_000,
            ), "gap_reentry_insufficient_cash")
            self.assertEqual(joinquant_exporter._buy_reject_reason(
                row.copy(), 75, account_total_value=100_000, available_cash=10_000,
                current_open_risk_pct=3.6,
            ), "gap_reentry_min_lot_risk_exceeded")

    def test_export_confirms_gap_only_on_second_distinct_scan(self) -> None:
        store = TradingStore(Path(self._ledger_tmp.name) / "trading.db")
        store.initialize()
        with store.transaction() as conn:
            store.record_strategy_run(conn, joinquant_exporter.StrategyRunRecord(
                "parent-run", "2026-07-16", "2026-07-16 10:00:00", "v", "p"
            ))
            parent = {
                "id": "parent-signal", "code": "002432", "jq_code": "002432.XSHE",
                "action": "buy", "entry_price": 10.0, "stop_loss": 9.0,
                "take_profit": 12.0, "position_pct": 5.0,
            }
            store.record_signal(conn, joinquant_exporter.SignalRecord(
                "parent-signal", "parent-run", "2026-07-16", "002432",
                "002432.XSHE", "buy", 5.0, "2026-07-16 10:00:00", "",
                json.dumps(parent),
            ))
        rows = pd.DataFrame([{
            "code": "002432", "price": 10.3, "prev_close": 10.0, "high": 11.0, "pct_chg": 9.7,
            "limit_quality": "炸板/回落", "entry_price": 10.3,
            "stop_loss": 9.5, "take_profit": 11.9, "position_pct": 9.0,
            "final_score": 90, "market_state": "NORMAL", "atr14": 0.4,
            "amount": 100_000_000,
        }])
        payloads = [
            {"schema_version": 1, "trade_date": "2026-07-17",
             "generated_at": "2026-07-17 10:00:00", "run_id": "scan-1",
             "source": "a_share_strategy", "dry_run": False, "signals": []},
            {"schema_version": 1, "trade_date": "2026-07-17",
             "generated_at": "2026-07-17 10:05:00", "run_id": "scan-2",
             "source": "a_share_strategy", "dry_run": False, "signals": []},
            {"schema_version": 1, "trade_date": "2026-07-17",
             "generated_at": "2026-07-17 10:10:00", "run_id": "scan-3",
             "source": "a_share_strategy", "dry_run": False, "signals": []},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter.app_config, "GAP_REENTRY_ENABLE_DEFAULT", True
        ), patch.object(joinquant_exporter, "_base_payload", side_effect=payloads):
            first = joinquant_exporter.export_signals(
                rows, run_id="scan-1", store=store, output_path=Path(tmp) / "one.json"
            )
            second = joinquant_exporter.export_signals(
                rows, run_id="scan-2", store=store, output_path=Path(tmp) / "two.json"
            )
            self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["signals"], [])
            signals = json.loads(second.read_text(encoding="utf-8"))["signals"]
            third = joinquant_exporter.export_signals(
                rows, run_id="scan-3", store=store, output_path=Path(tmp) / "three.json"
            )
            self.assertEqual(json.loads(third.read_text(encoding="utf-8"))["signals"], [])
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["entry_path"], "gap_reentry")
        self.assertEqual(signals[0]["parent_signal_id"], "parent-signal")

    def test_blocked_gap_signal_is_not_marked_as_published(self) -> None:
        store = TradingStore(Path(self._ledger_tmp.name) / "trading.db")
        store.initialize()
        with store.transaction() as conn:
            store.record_strategy_run(conn, joinquant_exporter.StrategyRunRecord(
                "parent-run", "2026-07-16", "2026-07-16 10:00:00", "v", "p"
            ))
            parent = {
                "id": "parent-signal", "code": "002432", "jq_code": "002432.XSHE",
                "action": "buy", "entry_price": 10.0, "stop_loss": 9.0,
                "take_profit": 12.0, "position_pct": 5.0,
            }
            store.record_signal(conn, joinquant_exporter.SignalRecord(
                "parent-signal", "parent-run", "2026-07-16", "002432",
                "002432.XSHE", "buy", 5.0, "2026-07-16 10:00:00", "",
                json.dumps(parent),
            ))
        rows = pd.DataFrame([{
            "code": "002432", "price": 10.3, "prev_close": 10.0,
            "high": 11.0, "pct_chg": 9.7, "limit_quality": "炸板/回落",
            "entry_price": 10.3, "stop_loss": 9.5, "take_profit": 11.9,
            "position_pct": 9.0, "final_score": 90,
            "market_state": "NORMAL", "atr14": 0.4, "amount": 100_000_000,
        }])
        payloads = [
            {"schema_version": 1, "trade_date": "2026-07-17",
             "generated_at": "2026-07-17 10:00:00", "run_id": "scan-1",
             "source": "a_share_strategy", "dry_run": False, "signals": []},
            {"schema_version": 1, "trade_date": "2026-07-17",
             "generated_at": "2026-07-17 10:05:00", "run_id": "scan-2",
             "source": "a_share_strategy", "dry_run": False, "signals": []},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter.app_config, "GAP_REENTRY_ENABLE_DEFAULT", True
        ), patch.object(joinquant_exporter, "_base_payload", side_effect=payloads):
            joinquant_exporter.export_signals(
                rows, run_id="scan-1", store=store,
                output_path=Path(tmp) / "one.json",
            )
            with store.transaction() as conn:
                store.set_system_state(conn, "kill_switch", "1", "test")
            result = joinquant_exporter.export_signals(
                rows, run_id="scan-2", store=store,
                output_path=Path(tmp) / "two.json",
            )
            published = json.loads(result.read_text(encoding="utf-8"))["signals"]

        self.assertEqual(published, [])
        opportunity = store.get_gap_reentry_for_stock_date("2026-07-17", "002432")
        self.assertIsNotNone(opportunity)
        self.assertIsNone(opportunity["new_signal_id"])

    def test_invalid_gap_risk_updates_candidate_and_ledger_to_rejected(self) -> None:
        store = TradingStore(Path(self._ledger_tmp.name) / "trading.db")
        store.initialize()
        with store.transaction() as conn:
            store.record_strategy_run(conn, joinquant_exporter.StrategyRunRecord(
                "parent-run", "2026-07-16", "2026-07-16 10:00:00", "v", "p"
            ))
            store.record_signal(conn, joinquant_exporter.SignalRecord(
                "parent-signal", "parent-run", "2026-07-16", "002432",
                "002432.XSHE", "buy", 5.0, "2026-07-16 10:00:00", "",
                json.dumps({
                    "id": "parent-signal", "code": "002432", "action": "buy",
                    "entry_price": 10.0, "stop_loss": 9.0,
                }),
            ))
        row = pd.Series({
            "code": "002432", "price": 10.3, "prev_close": 10.0,
            "stop_loss": 10.4, "final_score": 90, "market_state": "NORMAL",
        })
        with patch.object(
            joinquant_exporter.app_config, "GAP_REENTRY_ENABLE_DEFAULT", True
        ):
            first = joinquant_exporter._prepare_gap_reentry_row(
                row, store=store, run_id="scan-1", trade_date="2026-07-17",
                generated_at="2026-07-17 10:00:00", min_score=75,
                allow_buy=True,
            )
            second = joinquant_exporter._prepare_gap_reentry_row(
                row, store=store, run_id="scan-2", trade_date="2026-07-17",
                generated_at="2026-07-17 10:05:00", min_score=75,
                allow_buy=True,
            )

        self.assertEqual(first["gap_reentry_state"], "OPEN_OBSERVING")
        self.assertEqual(second["gap_reentry_state"], "RISK_REJECTED")
        self.assertEqual(second["gap_reentry_reason"], "gap_reentry_rr_invalid")
        opportunity = store.get_gap_reentry_for_stock_date("2026-07-17", "002432")
        self.assertEqual(opportunity["state"], "RISK_REJECTED")
        self.assertEqual(
            joinquant_exporter.rejection_stage("gap_reentry_rr_invalid"),
            "execution",
        )

    def test_rejection_stage_exhaustively_maps_current_buy_reasons(self) -> None:
        expected = {
            "": "selected",
            "buy_low_score": "score",
            "buy_suspended": "tradability", "buy_st": "tradability",
            "buy_delisting": "tradability", "buy_special_listing_stage": "tradability",
            "buy_quote_stale": "tradability", "buy_chasing": "tradability",
            "buy_illiquid": "tradability", "buy_invalid_price": "tradability",
            "buy_near_limit_up": "tradability",
            "buy_disabled": "risk", "buy_max_positions": "risk",
            "buy_daily_new_positions_limit": "risk", "buy_daily_orders_limit": "risk",
            "buy_daily_turnover_limit": "risk", "buy_daily_loss_limit": "risk",
            "buy_account_drawdown_limit": "risk", "buy_consecutive_loss_limit": "risk",
            "buy_cooldown": "risk", "buy_risk_disallowed": "risk",
            "buy_bad_position": "risk", "buy_open_risk_limit": "risk",
            "buy_sector_limit": "risk", "buy_theme_limit": "risk",
            "buy_uncategorized_limit": "risk", "buy_insufficient_available_cash": "risk",
            "buy_total_position_limit": "risk", "buy_too_small_for_board_lot": "risk",
            "not_buy_sell_signal": "execution", "buy_execution_plan_missing": "execution",
            "buy_execution_plan_invalid": "execution", "buy_invalid_take_profit": "execution",
            "buy_invalid_stop_loss": "execution", "buy_not_reached_entry": "execution",
        }
        self.assertEqual(
            {reason: joinquant_exporter.rejection_stage(reason) for reason in expected},
            expected,
        )
        with self.assertRaisesRegex(ValueError, "UNKNOWN_REJECTION_CODE"):
            joinquant_exporter.rejection_stage("buy_future_reason")

    def test_export_records_selected_and_rejected_candidates(self) -> None:
        rows = pd.DataFrame([
            {"code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
             "take_profit": 11, "position_pct": 5, "final_score": 90},
            {"code": "600001", "price": 10, "entry_price": 10, "stop_loss": 9.5,
             "take_profit": 11, "position_pct": 5, "final_score": 70},
            {"code": "600002", "price": 10, "entry_price": 10, "stop_loss": 9.5,
             "take_profit": 11, "position_pct": 5, "final_score": 95,
             "execution_allowed": False},
            {"code": "600003", "price": 10, "signal_action": "sell", "has_holding": True},
        ])
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter, "_base_payload",
            return_value={"schema_version": 1, "trade_date": "2026-07-15",
                          "generated_at": "2026-07-15 10:05:00", "run_id": "r1",
                          "source": "a_share_strategy", "dry_run": False, "signals": []},
        ):
            base = Path(tmp)
            ml_store = MlStore(base / "ml.db")
            joinquant_exporter.export_signals(
                rows, run_id="r1", trade_date="2026-07-15",
                output_path=base / "signals.json", ml_store=ml_store,
                cohort_mode="intraday", cohort_interval_sec=300,
            )
            with ml_store.transaction() as conn:
                saved = conn.execute(
                    "SELECT code, selected, rejection_stage, rejection_code, final_action, "
                    "universe_hash, market_data_version, code_hash, generator_hash, "
                    "decision_at, parameter_version, features_json "
                    "FROM ml_candidate_samples ORDER BY code"
                ).fetchall()
        self.assertEqual(len(saved), 4)
        self.assertEqual(sum(int(row["selected"]) for row in saved), 1)
        self.assertEqual(
            {row["rejection_code"] for row in saved},
            {"", "buy_low_score", "buy_risk_disallowed", "not_buy_sell_signal"},
        )
        self.assertEqual(
            {row["code"]: row["final_action"] for row in saved},
            {"600000": "buy_published", "600001": "rule_rejected",
             "600002": "rule_rejected", "600003": "sell_published"},
        )
        self.assertTrue(all(row["universe_hash"] and row["market_data_version"] for row in saved))
        self.assertTrue(all(row["code_hash"] and row["generator_hash"] for row in saved))
        self.assertEqual({row["decision_at"] for row in saved}, {"2026-07-15T10:05:00+08:00"})
        eligibility = {
            row["code"]: json.loads(row["features_json"])["training_eligible"]["value"]
            for row in saved
        }
        self.assertEqual(eligibility, {"600000": True, "600001": True, "600002": True, "600003": False})
        snapshot = json.loads(saved[0]["features_json"])["parameter_snapshot"]["value"]
        self.assertEqual(snapshot["min_score"], 75.0)
        self.assertFalse(snapshot["enforce_execution_contract"])
        self.assertTrue(saved[0]["parameter_version"].startswith("risk-observe-v1:"))

    def test_implementation_hash_covers_all_live_decision_modules_and_is_cached(self) -> None:
        expected = (
            "a_share_strategy.py", "candidate_core.py", "joinquant_exporter.py",
            "ml_dataset.py", "trade_safety.py", "exit_policy.py",
            "trading_store.py", "gap_reentry.py", "config.py",
        )
        self.assertEqual(joinquant_exporter.IMPLEMENTATION_HASH_FILES, expected)
        joinquant_exporter._ml_code_hash.cache_clear()
        self.addCleanup(joinquant_exporter._ml_code_hash.cache_clear)
        with patch.object(Path, "read_bytes", autospec=True, return_value=b"same") as read:
            first = joinquant_exporter._ml_code_hash()
            second = joinquant_exporter._ml_code_hash()
        self.assertEqual(first, second)
        self.assertEqual(read.call_count, len(expected))

    def test_parameter_snapshot_covers_buy_thresholds_without_runtime_secrets(self) -> None:
        snapshot = joinquant_exporter._ml_parameter_snapshot(81.5, True)
        self.assertEqual(snapshot["min_score"], 81.5)
        self.assertTrue(snapshot["enforce_execution_contract"])
        self.assertEqual(set(snapshot), {
            "min_score", "caution_min_score", "near_limit_up_pct", "board_lot_size",
            "special_listing_days", "quote_stale_sec", "chasing_max_pct",
            "chasing_atr_multiplier", "min_tradable_amount", "enforce_execution_contract",
            "portfolio_risk_enabled", "max_positions", "max_new_positions_per_day",
            "max_orders_per_day", "max_daily_turnover_pct", "daily_loss_warn_pct",
            "account_drawdown_warn_pct", "max_consecutive_losses", "exit_cooldown_enabled",
            "tradability_filter_enabled", "max_uncategorized_position_pct",
            "max_open_risk_caution_pct", "max_open_risk_normal_pct",
            "max_industry_position_pct", "max_theme_position_pct",
            "max_total_position_pct",
        })
        self.assertFalse(any(token in json.dumps(snapshot).lower() for token in (
            "token", "webhook", "url", "private_key",
        )))

    def test_ledger_controls_are_final_actions_and_make_batch_audit_only(self) -> None:
        rows = pd.DataFrame([
            {"code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
             "take_profit": 11, "position_pct": 5, "final_score": 90},
            {"code": "000001", "price": 12, "signal_action": "sell", "has_holding": True},
        ])
        for state_key, expected in (
            ("buy_enabled", {"600000": "buy_blocked_disabled", "000001": "sell_published"}),
            ("kill_switch", {"600000": "buy_blocked_kill_switch", "000001": "sell_blocked_kill_switch"}),
        ):
            with self.subTest(state_key=state_key), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                trading = TradingStore(base / "trading.db")
                trading.initialize()
                with trading.transaction() as conn:
                    trading.set_system_state(conn, state_key, "1" if state_key == "kill_switch" else "0", "test")
                ml_store = MlStore(base / "ml.db")
                joinquant_exporter.export_signals(
                    rows, run_id=f"control-{state_key}", output_path=base / "signals.json",
                    store=trading, ml_store=ml_store, cohort_mode="intraday",
                    cohort_interval_sec=300,
                )
                with ml_store.transaction() as conn:
                    saved = conn.execute(
                        "SELECT code, final_action, features_json FROM ml_candidate_samples"
                    ).fetchall()
                self.assertEqual({row["code"]: row["final_action"] for row in saved}, expected)
                self.assertTrue(all(
                    not json.loads(row["features_json"])["training_eligible"]["value"]
                    for row in saved
                ))

    def test_ledger_error_blocks_buy_and_does_not_create_ml_audit_data(self) -> None:
        class LockedStore:
            def initialize(self):
                pass

            @contextmanager
            def transaction(self):
                raise sqlite3.OperationalError("secret-ish database detail")
                yield

        rows = pd.DataFrame([
            {"code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
             "take_profit": 11, "position_pct": 5, "final_score": 90},
            {"code": "000001", "price": 12, "signal_action": "sell", "has_holding": True},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ml_store = MlStore(base / "ml.db")
            joinquant_exporter.export_signals(
                rows, run_id="ledger-error", output_path=base / "signals.json",
                store=LockedStore(), ml_store=ml_store, cohort_mode="intraday",
                cohort_interval_sec=300,
            )
            self.assertFalse((base / "ml.db").exists())

    def test_rolled_back_new_run_does_not_write_ml_candidates(self) -> None:
        class FailingSignalStore(TradingStore):
            def record_signal(self, conn, signal):
                raise sqlite3.OperationalError("signal write failed")

        rows = pd.DataFrame([{
            "code": "600000", "price": 10, "entry_price": 10,
            "stop_loss": 9.5, "take_profit": 11, "position_pct": 5,
            "final_score": 90,
        }])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            trading_store = FailingSignalStore(base / "trading.db")
            joinquant_exporter.export_signals(
                rows, run_id="rolled-back-run", output_path=base / "signals.json",
                store=trading_store, ml_store=MlStore(base / "ml.db"),
                cohort_mode="intraday", cohort_interval_sec=300,
            )
            self.assertFalse((base / "ml.db").exists())
            with trading_store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM strategy_runs WHERE run_id='rolled-back-run'"
                ).fetchone()[0], 0)

    def test_sell_disabled_and_no_holding_have_explicit_audit_actions(self) -> None:
        rows = pd.DataFrame([
            {"code": "000001", "price": 12, "signal_action": "sell", "has_holding": True},
            {"code": "000002", "price": 12, "signal_action": "sell", "has_holding": False},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ml_store = MlStore(base / "ml.db")
            joinquant_exporter.export_signals(
                rows, run_id="sell-audit", output_path=base / "signals.json",
                ml_store=ml_store, allow_sell=False, cohort_mode="intraday",
                cohort_interval_sec=300,
            )
            with ml_store.transaction() as conn:
                saved = conn.execute(
                    "SELECT code, final_action, features_json FROM ml_candidate_samples"
                ).fetchall()
        self.assertEqual(
            {row["code"]: row["final_action"] for row in saved},
            {"000001": "sell_blocked_disabled", "000002": "sell_rejected_no_holding"},
        )
        self.assertTrue(all(
            not json.loads(row["features_json"])["training_eligible"]["value"]
            for row in saved
        ))

    def test_repeated_export_run_is_idempotent_despite_later_wall_clock(self) -> None:
        rows = pd.DataFrame([{
            "code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
            "take_profit": 11, "position_pct": 5, "final_score": 90,
        }])
        generated = iter(("2026-07-15 10:05:00", "2026-07-15 10:06:00"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter, "_base_payload",
            side_effect=lambda run_id, trade_date, dry_run: {
                "schema_version": 1, "trade_date": "2026-07-15",
                "generated_at": next(generated), "run_id": run_id,
                "source": "a_share_strategy", "dry_run": dry_run, "signals": [],
            },
        ):
            base = Path(tmp)
            ml_store = MlStore(base / "ml.db")
            trading_store = TradingStore(base / "trading.db")
            for output in ("first.json", "second.json"):
                joinquant_exporter.export_signals(
                    rows, run_id="same-run", trade_date="2026-07-15",
                    output_path=base / output, store=trading_store, ml_store=ml_store,
                    cohort_mode="intraday",
                )
            self.assertEqual(ml_store.counts()["ml_candidate_samples"], 1)

    def test_replayed_run_never_backfills_ml_from_later_market_data(self) -> None:
        first_rows = pd.DataFrame([{
            "code": "600000", "price": 10, "entry_price": 10,
            "stop_loss": 9.5, "take_profit": 11, "position_pct": 5,
            "final_score": 70,
        }])
        later_rows = first_rows.assign(price=12, final_score=72)
        generated = iter(("2026-07-15 10:05:00", "2026-07-15 10:06:00"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter, "_base_payload",
            side_effect=lambda run_id, trade_date, dry_run: {
                "schema_version": 1, "trade_date": "2026-07-15",
                "generated_at": next(generated), "run_id": run_id,
                "source": "a_share_strategy", "dry_run": dry_run, "signals": [],
            },
        ):
            base = Path(tmp)
            trading_store = TradingStore(base / "trading.db")
            joinquant_exporter.export_signals(
                first_rows, run_id="same-run", trade_date="2026-07-15",
                output_path=base / "first.json", store=trading_store,
            )
            ml_store = MlStore(base / "ml.db")
            joinquant_exporter.export_signals(
                later_rows, run_id="same-run", trade_date="2026-07-15",
                output_path=base / "second.json", store=trading_store,
                ml_store=ml_store, cohort_mode="intraday", cohort_interval_sec=300,
            )
            self.assertFalse((base / "ml.db").exists())
            with trading_store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM strategy_runs WHERE run_id='same-run'"
                ).fetchone()[0], 1)

    def test_failed_first_ml_write_is_not_backfilled_by_run_replay(self) -> None:
        class FailingMlStore:
            def initialize(self):
                raise sqlite3.OperationalError("ml locked")

        rows = pd.DataFrame([{
            "code": "600000", "price": 10, "entry_price": 10,
            "stop_loss": 9.5, "take_profit": 11, "position_pct": 5,
            "final_score": 70,
        }])
        generated = iter(("2026-07-15 10:05:00", "2026-07-15 10:06:00"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter, "_base_payload",
            side_effect=lambda run_id, trade_date, dry_run: {
                "schema_version": 1, "trade_date": "2026-07-15",
                "generated_at": next(generated), "run_id": run_id,
                "source": "a_share_strategy", "dry_run": dry_run, "signals": [],
            },
        ):
            base = Path(tmp)
            trading_store = TradingStore(base / "trading.db")
            joinquant_exporter.export_signals(
                rows, run_id="failed-run", trade_date="2026-07-15",
                output_path=base / "first.json", store=trading_store,
                ml_store=FailingMlStore(), cohort_mode="intraday",
                cohort_interval_sec=300,
            )
            ml_store = MlStore(base / "ml.db")
            joinquant_exporter.export_signals(
                rows.assign(price=12), run_id="failed-run",
                trade_date="2026-07-15", output_path=base / "second.json",
                store=trading_store, ml_store=ml_store,
                cohort_mode="intraday", cohort_interval_sec=300,
            )
            self.assertFalse((base / "ml.db").exists())

    def test_ml_write_failure_keeps_export_bytes_equal_to_disabled_path(self) -> None:
        class FailingMlStore:
            def initialize(self):
                raise sqlite3.OperationalError("ml locked")

        rows = pd.DataFrame([{
            "code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
            "take_profit": 11, "position_pct": 5, "final_score": 90,
        }])
        fixed = {"schema_version": 1, "trade_date": "2026-07-15",
                 "generated_at": "2026-07-15 10:05:00", "run_id": "r1",
                 "source": "a_share_strategy", "dry_run": False, "signals": []}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter, "_base_payload", side_effect=lambda *args: dict(fixed, signals=[])
        ):
            base = Path(tmp)
            no_ml = joinquant_exporter.export_signals(
                rows, run_id="r1", output_path=base / "no-ml.json",
                store=TradingStore(base / "no-ml-trading.db"),
            ).read_bytes()
            failed_ml = joinquant_exporter.export_signals(
                rows, run_id="r1", output_path=base / "failed-ml.json",
                store=TradingStore(base / "failed-ml-trading.db"), ml_store=FailingMlStore(),
                cohort_mode="intraday", cohort_interval_sec=300,
            ).read_bytes()
        self.assertEqual(failed_ml, no_ml)

    def test_ml_capacity_refusal_keeps_rule_export_available(self) -> None:
        class FullMlStore:
            def initialize(self):
                raise MlCapacityError("full")

        rows = pd.DataFrame([{
            "code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
            "take_profit": 11, "position_pct": 5, "final_score": 90,
        }])
        with tempfile.TemporaryDirectory() as tmp:
            path = joinquant_exporter.export_signals(
                rows, run_id="capacity", output_path=Path(tmp) / "signals.json",
                store=TradingStore(Path(tmp) / "trading.db"), ml_store=FullMlStore(),
                cohort_mode="intraday",
            )
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["signals"]), 1)

    def test_legacy_ml_append_runtime_failure_does_not_block_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter, "append_signal_samples", side_effect=RuntimeError("append failed")
        ):
            path = joinquant_exporter.export_signals(
                pd.DataFrame(), output_path=Path(tmp) / "signals.json",
            )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["signals"], [])

    def test_ml_memory_error_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter, "append_signal_samples", side_effect=MemoryError("oom")
        ):
            with self.assertRaises(MemoryError):
                joinquant_exporter.export_signals(
                    pd.DataFrame(), output_path=Path(tmp) / "signals.json",
                )

    def test_ml_disabled_does_not_create_default_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter.app_config, "ML_TRAINED_SHADOW_ENABLE", False
        ), patch.object(joinquant_exporter.app_config, "ML_DB_FILE", Path(tmp) / "ml.db"):
            joinquant_exporter.export_signals(
                pd.DataFrame(), output_path=Path(tmp) / "signals.json",
            )
            self.assertFalse((Path(tmp) / "ml.db").exists())

    def test_enabled_default_ml_store_is_created_lazily(self) -> None:
        rows = pd.DataFrame([{
            "code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
            "take_profit": 11, "position_pct": 5, "final_score": 90,
        }])
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter.app_config, "ML_TRAINED_SHADOW_ENABLE", True
        ), patch.object(joinquant_exporter.app_config, "ML_DB_FILE", Path(tmp) / "ml.db"):
            joinquant_exporter.export_signals(
                rows, run_id="lazy-ml", output_path=Path(tmp) / "signals.json",
                store=TradingStore(Path(tmp) / "trading.db"), cohort_mode="intraday",
            )
            store = MlStore(Path(tmp) / "ml.db")
            self.assertEqual(store.counts()["ml_candidate_samples"], 1)

    def test_unknown_rejection_code_skips_ml_without_changing_rule_export(self) -> None:
        class RecordingMlStore:
            initialized = False

            def initialize(self):
                self.initialized = True

            def record_candidates(self, samples):
                raise AssertionError("invalid cohort must not be written")

        rows = pd.DataFrame([{
            "code": "600000", "price": 10, "position_pct": 5, "final_score": 90,
        }])
        fixed = {"schema_version": 1, "trade_date": "2026-07-15",
                 "generated_at": "2026-07-15 10:05:00", "run_id": "r1",
                 "source": "a_share_strategy", "dry_run": False, "signals": []}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter, "_base_payload", side_effect=lambda *args: dict(fixed, signals=[])
        ), patch.object(joinquant_exporter, "_buy_reject_reason", return_value="buy_future_reason"):
            base = Path(tmp)
            disabled = joinquant_exporter.export_signals(
                rows, run_id="r1", output_path=base / "disabled.json",
                store=TradingStore(base / "disabled.db"),
            ).read_bytes()
            enabled = joinquant_exporter.export_signals(
                rows, run_id="r1", output_path=base / "enabled.json",
                store=TradingStore(base / "enabled.db"), ml_store=RecordingMlStore(),
                cohort_mode="intraday",
            ).read_bytes()
        self.assertEqual(enabled, disabled)

    def test_buy_signal_persists_normalized_classification(self) -> None:
        row = pd.Series({
            "code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
            "take_profit": 11, "position_pct": 5, "final_score": 90,
            "industry": "银行", "theme_label": "中特估",
        })

        signal = joinquant_exporter._buy_signal(row, "run", 0)

        self.assertEqual(signal["industry"], "银行")
        self.assertEqual(signal["theme"], "中特估")

    def test_uncategorized_positions_share_one_aggregate_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{
                "code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
                "take_profit": 11, "position_pct": 4, "final_score": 90,
            }])
            path = joinquant_exporter.export_signals(
                rows, output_path=Path(tmp) / "signals.json",
                account_total_value=100000, available_cash=100000,
                sector_exposure_pct={"__UNCATEGORIZED__": 8},
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"], {"buy_uncategorized_limit": 1})

    def test_hard_position_boundaries_cannot_be_disabled_with_observation_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            joinquant_exporter.app_config, "JOINQUANT_PORTFOLIO_RISK_ENABLE_DEFAULT", False
        ):
            rows = pd.DataFrame([{
                "code": "600000", "price": 10, "entry_price": 10, "stop_loss": 9.5,
                "take_profit": 11, "position_pct": 2, "final_score": 90,
            }])
            path = joinquant_exporter.export_signals(
                rows, output_path=Path(tmp) / "signals.json",
                account_total_value=100000, available_cash=100000,
                current_position_count=5, current_position_pct=79,
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"], {"buy_max_positions": 1})

    def test_conflicting_signal_id_blocks_buy_and_preserves_sell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = TradingStore(base / "trading.db")
            first = pd.DataFrame([{"code": "600000", "price": 10, "position_pct": 12, "final_score": 90, "signal_action": "continue"}])
            joinquant_exporter.export_signals(first, run_id="run-1", trade_date="2026-07-07", output_path=base / "first.json", store=store)
            changed = pd.DataFrame([
                {"code": "600000", "price": 11, "position_pct": 12, "final_score": 90, "signal_action": "continue"},
                {"code": "000001", "price": 12, "final_score": 80, "signal_action": "sell", "has_holding": True},
            ])
            result = joinquant_exporter.export_signals(changed, run_id="run-1", trade_date="2026-07-07", output_path=base / "second.json", store=store)
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual([item["action"] for item in payload["signals"]], ["sell"])
            self.assertTrue(payload["diagnostics"]["buy_publication_blocked"])
            self.assertIn("immutable signal conflict", payload["diagnostics"]["ledger_error"])
    def test_ledger_and_json_signal_ids_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            store = TradingStore(Path(tmp) / "trading.db")
            rows = pd.DataFrame([
                {"code": "600000", "price": 10, "entry_price": 10, "take_profit": 11,
                 "position_pct": 12, "final_score": 90, "signal_action": "continue"},
                {"code": "000001", "price": 12, "final_score": 80,
                 "signal_action": "sell", "has_holding": True},
            ])

            result = joinquant_exporter.export_signals(
                rows, run_id="run-1", trade_date="2026-07-07",
                output_path=output_path, store=store,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["id"] for item in payload["signals"]],
                ["run-1-600000-buy-0000", "run-1-000001-sell-0001"],
            )
            with store.connect() as conn:
                db_ids = [row[0] for row in conn.execute("SELECT signal_id FROM signals ORDER BY signal_id")]
                decision_count = conn.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0]
            self.assertEqual(db_ids, sorted(item["id"] for item in payload["signals"]))
            self.assertEqual(decision_count, 2)
            self.assertTrue(payload["diagnostics"]["ledger_ok"])
            self.assertEqual(payload["diagnostics"]["ledger_signal_count"], 2)

    def test_ledger_signal_count_reports_only_new_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            store = TradingStore(Path(tmp) / "trading.db")
            rows = pd.DataFrame([{
                "code": "600000", "price": 10, "entry_price": 10, "take_profit": 11,
                "position_pct": 12, "final_score": 90, "signal_action": "continue",
            }])
            first = joinquant_exporter.export_signals(
                rows, run_id="run-1", trade_date="2026-07-07",
                output_path=output_path, store=store,
            )
            self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["diagnostics"]["ledger_signal_count"], 1)

            second = joinquant_exporter.export_signals(
                rows, run_id="run-1", trade_date="2026-07-07",
                output_path=output_path, store=store,
            )
            self.assertEqual(json.loads(second.read_text(encoding="utf-8"))["diagnostics"]["ledger_signal_count"], 0)

    def test_observation_decision_uses_configured_warning_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            store = TradingStore(Path(tmp) / "trading.db")
            rows = pd.DataFrame([{
                "code": "600000", "price": 10, "entry_price": 10, "take_profit": 11,
                "position_pct": 12, "final_score": 90, "signal_action": "continue",
            }])
            with (
                patch.object(joinquant_exporter.app_config, "MIN_CASH_RESERVE_PCT", 101),
                patch.object(joinquant_exporter.app_config, "DAILY_LOSS_WARN_PCT", 0),
                patch.object(joinquant_exporter.app_config, "ACCOUNT_DRAWDOWN_WARN_PCT", 0),
            ):
                joinquant_exporter.export_signals(
                    rows, run_id="run-limits", trade_date="2026-07-07",
                    output_path=output_path, store=store, daily_pnl_pct=1,
                    account_drawdown_pct=1,
                )
            with store.connect() as conn:
                raw = json.loads(conn.execute("SELECT raw_json FROM risk_decisions").fetchone()[0])
            self.assertIn("CASH_RESERVE_LIMIT", raw["soft_warnings"])
            self.assertIn("DAILY_LOSS_WARNING", raw["soft_warnings"])
            self.assertIn("ACCOUNT_DRAWDOWN_WARNING", raw["soft_warnings"])

    def test_ledger_error_publishes_sells_and_blocks_buys(self) -> None:
        class LockedStore:
            def initialize(self) -> None:
                pass

            @contextmanager
            def transaction(self):
                raise sqlite3.OperationalError("database is locked")
                yield

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([
                {"code": "600000", "price": 10, "entry_price": 10, "take_profit": 11,
                 "position_pct": 12, "final_score": 90, "signal_action": "continue"},
                {"code": "000001", "price": 12, "final_score": 80,
                 "signal_action": "sell", "has_holding": True},
            ])
            result = joinquant_exporter.export_signals(
                rows, run_id="run-1", trade_date="2026-07-07",
                output_path=output_path, store=LockedStore(),
            )
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual([item["action"] for item in payload["signals"]], ["sell"])
            self.assertFalse(payload["diagnostics"]["ledger_ok"])
            self.assertEqual(payload["diagnostics"]["ledger_signal_count"], 0)
            self.assertTrue(payload["diagnostics"]["buy_publication_blocked"])
            self.assertIn("database is locked", payload["diagnostics"]["ledger_error"])

    def test_ledger_error_replaces_prior_buy_file_with_empty_signals(self) -> None:
        class LockedStore:
            def initialize(self) -> None:
                pass

            @contextmanager
            def transaction(self):
                raise sqlite3.OperationalError("database is locked")
                yield

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            output_path.write_text('{"signals":[{"action":"buy"}]}', encoding="utf-8")
            rows = pd.DataFrame([{
                "code": "600000", "price": 10, "entry_price": 10, "take_profit": 11,
                "position_pct": 12, "final_score": 90, "signal_action": "continue",
            }])
            result = joinquant_exporter.export_signals(
                rows, run_id="run-1", trade_date="2026-07-07",
                output_path=output_path, store=LockedStore(),
            )
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertTrue(payload["diagnostics"]["buy_publication_blocked"])

    def test_trading_controls_block_buys_or_all_automatic_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = TradingStore(base / "trading.db")
            store.initialize()
            rows = pd.DataFrame([
                {"code": "600000", "price": 10, "entry_price": 10, "take_profit": 11,
                 "position_pct": 12, "final_score": 90, "signal_action": "continue"},
                {"code": "000001", "price": 12, "final_score": 80,
                 "signal_action": "sell", "has_holding": True},
            ])
            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "0", "reconciliation")
            first = joinquant_exporter.export_signals(
                rows, run_id="controls-1", trade_date="2026-07-14",
                output_path=base / "first.json", store=store,
            )
            payload = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual([item["action"] for item in payload["signals"]], ["sell"])
            self.assertEqual(payload["diagnostics"]["buy_enabled"], "0")

            with store.transaction() as conn:
                store.set_system_state(conn, "kill_switch", "1", "critical")
            second = joinquant_exporter.export_signals(
                rows, run_id="controls-2", trade_date="2026-07-14",
                output_path=base / "second.json", store=store,
            )
            payload = json.loads(second.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["kill_switch"], "1")

    def test_exports_buy_and_sell_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "PF Bank",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 12,
                        "final_score": 90,
                        "enhanced_score": 94,
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                    },
                    {
                        "code": "000001",
                        "name": "PA Bank",
                        "price": 12.0,
                        "final_score": 80,
                        "signal_action": "sell",
                        "has_holding": True,
                    },
                    {
                        "code": "300001",
                        "price": 20.0,
                        "position_pct": 10,
                        "final_score": 60,
                        "signal_action": "continue",
                    },
                ]
            )

            result = joinquant_exporter.export_signals(
                rows,
                run_id="run-1",
                trade_date="2026-07-07",
                output_path=output_path,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["run_id"], "run-1")
            self.assertEqual([item["action"] for item in payload["signals"]], ["buy", "sell"])
            self.assertEqual(payload["signals"][0]["jq_code"], "600000.XSHG")
            self.assertEqual(payload["signals"][0]["enhanced_score"], 94)
            self.assertEqual(payload["signals"][1]["jq_code"], "000001.XSHE")
            for signal in payload["signals"]:
                self.assertTrue(signal["created_at"])
                self.assertEqual(signal["validated_at"], payload["generated_at"])
                self.assertEqual(signal["published_at"], payload["generated_at"])

    def test_empty_export_keeps_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"

            result = joinquant_exporter.export_signals(
                pd.DataFrame(),
                trade_date="2026-07-07",
                output_path=output_path,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["trade_date"], "2026-07-07")

    def test_buy_signal_requires_trade_window_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "PF Bank",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "position_pct": 12,
                        "final_score": 90,
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                    },
                    {
                        "code": "000001",
                        "name": "PA Bank",
                        "price": 12.0,
                        "final_score": 80,
                        "signal_action": "sell",
                        "has_holding": True,
                    },
                ]
            )

            result = joinquant_exporter.export_signals(
                rows,
                run_id="run-1",
                trade_date="2026-07-07",
                output_path=output_path,
                allow_buy=False,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual([item["action"] for item in payload["signals"]], ["sell"])

    def test_buy_signal_requires_current_price_to_reach_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "PF Bank",
                        "price": 10.0,
                        "entry_price": 10.5,
                        "position_pct": 12,
                        "final_score": 90,
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                    }
                ]
            )

            result = joinquant_exporter.export_signals(
                rows,
                run_id="run-1",
                trade_date="2026-07-07",
                output_path=output_path,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_not_reached_entry"], 1)

    def test_buy_signal_requires_take_profit_above_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "PF Bank",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 10.0,
                        "position_pct": 12,
                        "final_score": 90,
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                    }
                ]
            )

            result = joinquant_exporter.export_signals(
                rows,
                run_id="run-1",
                trade_date="2026-07-07",
                output_path=output_path,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_invalid_take_profit"], 1)

    def test_export_records_reject_reason_when_buy_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "PF Bank",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "take_profit": 11.0,
                        "position_pct": 12,
                        "final_score": 90,
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                    }
                ]
            )

            result = joinquant_exporter.export_signals(
                rows,
                run_id="run-1",
                trade_date="2026-07-07",
                output_path=output_path,
                allow_buy=False,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_disabled"], 1)

    def test_export_appends_ml_signal_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            sample_path = Path(tmp) / "ml" / "signal_samples.jsonl"
            rows = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "PF Bank",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 12,
                        "final_score": 90,
                        "enhanced_score": 94,
                        "shadow_rank": 1,
                        "global_risk_score": 0,
                        "shadow_reason": "消息+3.2；题材+4.0",
                        "trade_score": 83,
                        "news_score": 4,
                        "risk_reward": 2.5,
                        "pressure_pct": 1.2,
                        "theme_heat_level": "高",
                        "market_state": "强势进攻",
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                        "amount": 120_000_000,
                        "turnover": 3.4,
                        "ma5": 9.8,
                        "atr14": 0.33,
                    },
                    {
                        "code": "000001",
                        "name": "PA Bank",
                        "price": 12.0,
                        "final_score": 80,
                        "signal_action": "sell",
                        "has_holding": True,
                    },
                ]
            )

            joinquant_exporter.export_signals(
                rows,
                run_id="run-ml",
                trade_date="2026-07-07",
                output_path=output_path,
                ml_sample_path=sample_path,
            )

            samples = [json.loads(line) for line in sample_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(samples), 2)
            self.assertEqual(samples[0]["sample_version"], 1)
            self.assertEqual(samples[0]["run_id"], "run-ml")
            self.assertEqual(samples[0]["signal"]["action"], "buy")
            self.assertEqual(samples[0]["signal"]["id"], "run-ml-600000-buy-0000")
            self.assertEqual(samples[0]["features"]["final_score"], 90.0)
            self.assertEqual(samples[0]["features"]["enhanced_score"], 94.0)
            self.assertEqual(samples[0]["features"]["shadow_rank"], 1)
            self.assertEqual(samples[0]["features"]["shadow_reason"], "消息+3.2；题材+4.0")
            self.assertEqual(samples[0]["features"]["market_state"], "强势进攻")
            self.assertEqual(samples[0]["features"]["ma5"], 9.8)
            self.assertEqual(samples[0]["labels"]["order_status"], "")
            self.assertEqual(samples[1]["signal"]["action"], "sell")

    def test_sell_signal_requires_existing_holding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "000001",
                        "name": "PA Bank",
                        "price": 12.0,
                        "final_score": 80,
                        "signal_action": "sell",
                    }
                ]
            )

            result = joinquant_exporter.export_signals(
                rows,
                run_id="run-1",
                trade_date="2026-07-07",
                output_path=output_path,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])

    def test_sell_signal_exports_when_holding_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "000001",
                        "name": "PA Bank",
                        "price": 12.0,
                        "final_score": 80,
                        "signal_action": "sell",
                        "has_holding": True,
                    }
                ]
            )

            result = joinquant_exporter.export_signals(
                rows,
                run_id="run-1",
                trade_date="2026-07-07",
                output_path=output_path,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual([item["action"] for item in payload["signals"]], ["sell"])

    def test_partial_exit_keeps_stable_id_and_target_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([{
                "code": "600000", "name": "PF Bank", "price": 12.0,
                "signal_action": "take_profit_1", "has_holding": True,
                "exit_signal_id": "cycle-1-take_profit_1-0", "target_qty": 500,
            }])
            result = joinquant_exporter.export_signals(
                rows, run_id="run-1", trade_date="2026-07-13",
                output_path=output_path,
            )
            signal = json.loads(result.read_text(encoding="utf-8"))["signals"][0]
            self.assertEqual(signal["id"], "cycle-1-take_profit_1-0")
            self.assertEqual(signal["action"], "sell")
            self.assertEqual(signal["target_qty"], 500)

    def test_sell_signal_creates_durable_exit_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([{"code": "600000", "price": 12, "signal_action": "hard_stop",
                                  "has_holding": True, "exit_signal_id": "cycle-hard-stop", "target_qty": 0}])
            joinquant_exporter.export_signals(rows, run_id="run-exit", output_path=output_path, store=store)
            self.assertEqual(store.get_open_exit_intents()["600000"]["signal_id"], "cycle-hard-stop")


    def test_buy_signal_rejects_target_value_below_board_lot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "688347",
                        "name": "High Price",
                        "price": 120.0,
                        "entry_price": 120.0,
                        "position_pct": 5,
                        "final_score": 95,
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                    }
                ]
            )

            result = joinquant_exporter.export_signals(
                rows,
                run_id="run-1",
                trade_date="2026-07-07",
                output_path=output_path,
                account_total_value=100000,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_too_small_for_board_lot"], 1)

    def test_weak_market_requires_high_score_and_halves_risk_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([
                {
                    "code": "600000", "price": 10.0, "entry_price": 10.0,
                    "support_level": 9.7, "atr14": 0.25, "position_pct": 20,
                    "final_score": 80, "signal_action": "continue",
                    "market_state": "弱势震荡",
                },
                {
                    "code": "600001", "price": 10.0, "entry_price": 10.0,
                    "support_level": 9.7, "atr14": 0.25, "position_pct": 20,
                    "final_score": 90, "signal_action": "continue",
                    "market_state": "弱势震荡",
                },
            ])
            result = joinquant_exporter.export_signals(
                rows, run_id="run-risk", trade_date="2026-07-13", output_path=output_path,
            )
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual([item["code"] for item in payload["signals"]], ["600001"])
            self.assertLessEqual(payload["signals"][0]["position_pct"], 10.0)

    def test_risk_release_blocks_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([{
                "code": "600000", "price": 10.0, "entry_price": 10.0,
                "support_level": 9.7, "atr14": 0.25, "position_pct": 20,
                "final_score": 95, "signal_action": "continue", "market_state": "风险释放",
            }])
            result = joinquant_exporter.export_signals(
                rows, run_id="run-risk-off", trade_date="2026-07-13", output_path=output_path,
            )
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])

    def test_high_score_risk_disallowed_row_cannot_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([{
                "code": "600000",
                "price": 10.0,
                "entry_price": 10.0,
                "stop_loss": 9.5,
                "take_profit": 11.0,
                "risk_per_share": 0.5,
                "risk_reward": 2.0,
                "position_pct": 10.0,
                "final_score": 99,
                "signal_action": "continue",
                "execution_plan_version": exit_policy.EXECUTION_PLAN_VERSION,
                "execution_allowed": False,
                "execution_reject_reason": "趋势走弱",
            }])

            result = joinquant_exporter.export_signals(
                rows,
                run_id="risk-disallowed",
                trade_date="2026-07-14",
                output_path=output_path,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(
                payload["diagnostics"]["reject_reasons"]["buy_risk_disallowed"],
                1,
            )

    def test_strict_export_rejects_missing_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([{
                "code": "600000",
                "price": 10.0,
                "entry_price": 10.0,
                "position_pct": 10.0,
                "final_score": 99,
                "signal_action": "continue",
            }])

            result = joinquant_exporter.export_signals(
                rows,
                run_id="missing-contract",
                output_path=output_path,
                enforce_execution_contract=True,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(
                payload["diagnostics"]["reject_reasons"]["buy_execution_plan_missing"],
                1,
            )

    def test_strict_export_rejects_invalid_current_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([{
                "code": "600000", "price": 10.0, "entry_price": 10.0,
                "stop_loss": 10.1, "take_profit": 10.2, "position_pct": 10.0,
                "final_score": 99, "signal_action": "continue",
                "execution_plan_version": exit_policy.EXECUTION_PLAN_VERSION,
                "execution_allowed": True,
            }])

            result = joinquant_exporter.export_signals(
                rows, output_path=output_path, enforce_execution_contract=True,
            )

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(
                payload["diagnostics"]["reject_reasons"]["buy_execution_plan_invalid"], 1,
            )

    def test_final_plan_matches_scan_json_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            row = pd.Series({
                "code": "600000",
                "name": "PF Bank",
                "price": 10.0,
                "amount": 100_000_000,
                "pct_chg": 1.0,
                "support_level": 9.6,
                "pressure_level": 12.0,
                "atr14": 0.2,
                "trend_state": "趋势修复",
                "market_state": "NORMAL",
                "industry": "银行",
                "theme_label": "中特估",
                "final_score": 95,
                "signal_action": "continue",
                "signal_state": "fresh",
                "signal_first_seen": "2026-07-14",
                "has_holding": False,
            })
            bundle = a_share_strategy.build_risk_bundle(row, {"state": "NORMAL"}, "")
            for key, value in bundle.items():
                row[key] = value
            anchor = a_share_strategy.build_signal_anchor_bundle(
                row,
                a_share_strategy.DiskCache(base / "anchor.json"),
            )
            for key, value in anchor.items():
                row[key] = value
            store = TradingStore(base / "trading.db")

            output = joinquant_exporter.export_signals(
                pd.DataFrame([row]),
                run_id="contract-equality",
                trade_date="2026-07-14",
                output_path=base / "signals.json",
                enforce_execution_contract=True,
                store=store,
            )

            signal = json.loads(output.read_text(encoding="utf-8"))["signals"][0]
            with store.connect() as conn:
                ledger = json.loads(conn.execute(
                    "SELECT raw_json FROM signals WHERE signal_id=?",
                    (signal["id"],),
                ).fetchone()[0])
            for field in ("entry_price", "stop_loss", "take_profit", "position_pct"):
                self.assertEqual(float(row[field]), float(signal[field]))
                self.assertEqual(float(signal[field]), float(ledger[field]))

    def test_board_lot_check_uses_risk_adjusted_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([{
                "code": "600000", "price": 100.0, "entry_price": 100.0,
                "support_level": 98.0, "atr14": 1.0, "position_pct": 20,
                "final_score": 95, "signal_action": "continue",
                "market_state": "CAUTION",
            }])
            result = joinquant_exporter.export_signals(
                rows, run_id="run-small", trade_date="2026-07-13",
                output_path=output_path, account_total_value=90000,
            )
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(
                payload["diagnostics"]["reject_reasons"]["buy_too_small_for_board_lot"], 1,
            )

    def test_buy_uses_real_current_portfolio_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame([{"code": "600000", "price": 10, "entry_price": 10,
                                  "support_level": 9.7, "atr14": 0.2, "position_pct": 20,
                                  "final_score": 95, "signal_action": "continue"}])
            result = joinquant_exporter.export_signals(
                rows, run_id="run-full", output_path=output_path,
                account_total_value=100000, current_position_pct=94,
            )
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_total_position_limit"], 1)

    def test_buy_rejects_sixth_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{
                "code": "600000", "price": 10, "entry_price": 10,
                "stop_loss": 9.5, "take_profit": 11.0,
                "position_pct": 2, "final_score": 95,
                "signal_action": "continue",
                "execution_plan_version": exit_policy.EXECUTION_PLAN_VERSION,
                "execution_allowed": True,
            }])
            path = joinquant_exporter.export_signals(
                rows,
                run_id="max-positions",
                output_path=Path(tmp) / "signals.json",
                current_position_count=5,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_max_positions"], 1)

    def test_buy_uses_joinquant_eighty_percent_total_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{
                "code": "600000", "price": 10, "entry_price": 10,
                "stop_loss": 9.5, "take_profit": 11.0,
                "position_pct": 2, "final_score": 95,
                "signal_action": "continue",
                "execution_plan_version": exit_policy.EXECUTION_PLAN_VERSION,
                "execution_allowed": True,
                "industry": "银行",
            }])
            path = joinquant_exporter.export_signals(
                rows,
                run_id="total-eighty",
                output_path=Path(tmp) / "signals.json",
                account_total_value=100000,
                current_position_pct=79,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_total_position_limit"], 1)

    def test_allow_sell_false_records_explicit_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{
                "code": "600000", "price": 10, "signal_action": "hard_stop",
                "has_holding": True, "target_qty": 0,
            }])
            path = joinquant_exporter.export_signals(
                rows,
                run_id="sell-disabled",
                output_path=Path(tmp) / "signals.json",
                allow_sell=False,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["sell_disabled"], 1)

    def test_buy_enforces_open_risk_and_sector_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{"code": "600000", "price": 10, "entry_price": 10, "support_level": 9.7,
                "atr14": 0.2, "position_pct": 20, "final_score": 95, "signal_action": "continue", "industry": "银行"}])
            path = joinquant_exporter.export_signals(
                rows, run_id="risk-cap", output_path=Path(tmp) / "signals.json", account_total_value=100000,
                current_open_risk_pct=3.9, sector_exposure_pct={"银行": 24},
            )
            reason = next(iter(json.loads(path.read_text(encoding="utf-8"))["diagnostics"]["reject_reasons"]))
            self.assertIn(reason, {"buy_open_risk_limit", "buy_sector_limit"})

    def test_buy_cannot_exceed_available_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{"code": "600000", "price": 10, "entry_price": 10, "support_level": 9.7,
                "atr14": 0.2, "position_pct": 30, "final_score": 95, "signal_action": "continue"}])
            path = joinquant_exporter.export_signals(rows, run_id="cash", output_path=Path(tmp) / "s.json",
                account_total_value=100000, available_cash=5000)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_insufficient_available_cash"], 1)

    def test_buy_enforces_daily_activity_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{"code": "600000", "price": 10, "entry_price": 10, "support_level": 9.7,
                "atr14": 0.2, "position_pct": 10, "final_score": 95, "signal_action": "continue"}])
            path = joinquant_exporter.export_signals(rows, run_id="daily", output_path=Path(tmp) / "s.json",
                account_total_value=100000, orders_today=joinquant_exporter.app_config.MAX_ORDERS_PER_DAY)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_daily_orders_limit"], 1)

    def test_buy_enforces_theme_exposure_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{"code": "600000", "price": 10, "entry_price": 10, "support_level": 9.7,
                "atr14": 0.2, "position_pct": 10, "final_score": 95, "signal_action": "continue",
                "theme": "AI"}])
            path = joinquant_exporter.export_signals(
                rows, run_id="theme", output_path=Path(tmp) / "s.json", account_total_value=100000,
                theme_exposure_pct={"AI": 15},
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_theme_limit"], 1)

    def test_buy_freezes_after_consecutive_losses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{"code": "600000", "price": 10, "entry_price": 10, "support_level": 9.7,
                "atr14": 0.2, "position_pct": 10, "final_score": 95, "signal_action": "continue"}])
            path = joinquant_exporter.export_signals(
                rows, run_id="losses", output_path=Path(tmp) / "s.json",
                consecutive_losses=joinquant_exporter.app_config.MAX_CONSECUTIVE_LOSSES,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_consecutive_loss_limit"], 1)

    def test_uncategorized_buy_uses_stricter_single_position_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([{"code": "600000", "price": 10, "entry_price": 10, "support_level": 9.7,
                "atr14": 0.2, "position_pct": 30, "final_score": 95, "signal_action": "continue"}])
            path = joinquant_exporter.export_signals(
                rows, run_id="uncategorized", output_path=Path(tmp) / "s.json", account_total_value=100000,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"][0]["position_pct"], 10)

    def test_higher_score_consumes_limited_cash_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = pd.DataFrame([
                {"code": "600001", "price": 10, "entry_price": 10, "support_level": 9.7,
                 "atr14": 0.2, "position_pct": 10, "final_score": 90, "signal_action": "continue"},
                {"code": "600002", "price": 10, "entry_price": 10, "support_level": 9.7,
                 "atr14": 0.2, "position_pct": 10, "final_score": 99, "signal_action": "continue"},
            ])
            path = joinquant_exporter.export_signals(
                rows, run_id="ranked", output_path=Path(tmp) / "s.json",
                account_total_value=100000, available_cash=10000,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([item["code"] for item in payload["signals"]], ["600002"])

    def test_portfolio_risk_and_tradability_modules_have_independent_switches(self) -> None:
        row = pd.Series({"code": "600000", "name": "*ST Test", "price": 10, "entry_price": 10,
                         "position_pct": 10, "final_score": 95, "signal_action": "continue"})
        with patch.object(joinquant_exporter.app_config, "JOINQUANT_TRADABILITY_FILTER_ENABLE_DEFAULT", False):
            self.assertNotEqual(joinquant_exporter._buy_reject_reason(row, 75), "buy_st")
        safe = pd.Series({"code": "600000", "price": 10, "entry_price": 10, "position_pct": 10,
                          "final_score": 95, "signal_action": "continue"})
        with patch.object(joinquant_exporter.app_config, "JOINQUANT_PORTFOLIO_RISK_ENABLE_DEFAULT", False):
            self.assertEqual(joinquant_exporter._buy_reject_reason(safe, 75, orders_today=999), "")
if __name__ == "__main__":
    unittest.main()
