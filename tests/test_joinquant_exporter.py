import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import joinquant_exporter
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
