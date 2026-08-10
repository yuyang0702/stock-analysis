import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

import a_share_strategy
import joinquant_exporter
from trading_store import TradingStore


class HoldingStopLossTest(unittest.TestCase):
    def test_adds_stop_loss_for_holding_outside_candidate_pool(self) -> None:
        source = pd.DataFrame([{"code": "600000", "price": 10.0}])
        spot = pd.DataFrame([{"code": "000021", "price": 52.12}])
        portfolio = {
            "000021": {
                "code": "000021",
                "name": "深科技",
                "qty": 100,
                "status": "holding",
                "cost_price": 61.19,
                "current_price": 52.50,
                "stop_price": 59.05,
                "take_price": 65.47,
            }
        }

        result = a_share_strategy.merge_holding_stop_loss_rows(source, spot, portfolio)

        stop = result[result["code"] == "000021"].iloc[0]
        self.assertEqual(stop["signal_action"], "stop_loss")
        self.assertEqual(stop["signal_state"], "stop_hit")
        self.assertEqual(stop["price"], 52.12)
        self.assertEqual(stop["hold_stop_price"], 59.05)
        self.assertTrue(stop["has_holding"])

    def test_uses_portfolio_key_and_snapshot_price_when_spot_is_missing(self) -> None:
        portfolio = {
            "000021": {
                "name": "深科技",
                "qty": 100,
                "status": "holding",
                "current_price": 52.12,
                "stop_price": 59.05,
            }
        }

        result = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), pd.DataFrame(), portfolio
        )

        self.assertEqual(result.iloc[0]["code"], "000021")
        self.assertEqual(result.iloc[0]["price"], 52.12)

    def test_skips_inactive_invalid_and_untriggered_holdings(self) -> None:
        portfolio = {
            "000001": {"code": "000001", "qty": 100, "status": "holding", "current_price": 10, "stop_price": 9},
            "000002": {"code": "000002", "qty": 0, "status": "holding", "current_price": 8, "stop_price": 9},
            "000003": {"code": "000003", "qty": 100, "status": "closed", "current_price": 8, "stop_price": 9},
            "000004": {"code": "000004", "qty": 100, "status": "holding", "current_price": 8},
            "bad": {"qty": 100, "status": "holding", "current_price": 8, "stop_price": 9},
        }
        source = pd.DataFrame([{"code": "600000", "price": 10.0}])

        result = a_share_strategy.merge_holding_stop_loss_rows(source, pd.DataFrame(), portfolio)

        self.assertEqual(result["code"].tolist(), ["600000"])

    def test_stop_loss_replaces_same_code_buy_and_exports_sell(self) -> None:
        source = pd.DataFrame([{
            "code": "000021",
            "name": "深科技",
            "price": 60.0,
            "entry_price": 60.0,
            "take_profit": 66.0,
            "position_pct": 10,
            "final_score": 90,
            "signal_action": "continue",
        }])
        spot = pd.DataFrame([{"code": "000021", "price": 52.12}])
        portfolio = {
            "000021": {
                "code": "000021", "name": "深科技", "qty": 100,
                "status": "holding", "current_price": 52.50, "stop_price": 59.05,
            }
        }
        merged = a_share_strategy.merge_holding_stop_loss_rows(source, spot, portfolio)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            output = joinquant_exporter.export_signals(
                merged,
                run_id="run-stop",
                trade_date="2026-07-13",
                output_path=base / "signals.json",
                ml_sample_path=base / "samples.jsonl",
                store=TradingStore(base / "trading.db"),
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(len(merged), 1)
        self.assertEqual([item["action"] for item in payload["signals"]], ["sell"])

    def test_position_cycle_two_r_exports_partial_target(self) -> None:
        spot = pd.DataFrame([{"code": "600000", "price": 12.0}])
        portfolio = {
            "600000": {
                "code": "600000", "name": "浦发银行", "qty": 1000,
                "status": "holding", "cost_price": 10.0, "current_price": 12.0,
                "stop_price": 9.0,
            }
        }
        cycles = {
            "600000": {
                "position_cycle_id": "cycle-1", "mode": "short",
                "initial_qty": 1000, "current_qty": 1000, "entry_price": 10.0,
                "initial_stop_price": 9.0, "highest_price": 12.0, "atr14": 0.4,
                "take_profit_stage": 0, "opened_at": "2026-07-10 09:31:00",
            }
        }

        merged = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), spot, portfolio, cycles=cycles,
            market_state="NORMAL", current_day=date(2026, 7, 13),
        )

        self.assertEqual(merged.iloc[0]["signal_action"], "take_profit_1")
        self.assertEqual(merged.iloc[0]["target_qty"], 500)
        self.assertEqual(merged.iloc[0]["exit_signal_id"], "cycle-1-take_profit_1-0")

    def test_wave3_position_uses_path_specific_five_day_exit(self) -> None:
        portfolio = {"600000": {
            "code": "600000", "name": "PF Bank", "qty": 500,
            "status": "holding", "cost_price": 10.0,
            "current_price": 10.2, "stop_price": 9.0,
        }}
        cycles = {"600000": {
            "position_cycle_id": "wave-cycle", "mode": "mid",
            "initial_qty": 500, "current_qty": 500, "entry_price": 10.0,
            "initial_stop_price": 9.0, "highest_price": 10.6, "atr14": 0.3,
            "take_profit_stage": 0, "opened_at": "2026-08-03 09:35:00",
        }}

        merged = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(),
            pd.DataFrame(),
            portfolio,
            cycles=cycles,
            position_factor_paths={"600000": "wave3_v1"},
            market_state="NORMAL",
            current_day=date(2026, 8, 7),
        )

        self.assertEqual(merged.iloc[0]["signal_action"], "time_stop")
        self.assertEqual(merged.iloc[0]["target_qty"], 0)
        self.assertEqual(merged.iloc[0]["factor_path"], "wave3_v1")
        self.assertIn("5日", merged.iloc[0]["signal_note"])

    def test_two_r_uses_ceiling_half_targets_for_odd_lot_counts(self) -> None:
        for initial_qty, target_qty in ((300, 200), (500, 300)):
            with self.subTest(initial_qty=initial_qty):
                portfolio = {"600000": {
                    "code": "600000", "name": "PF Bank", "qty": initial_qty,
                    "status": "holding", "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }}
                cycles = {"600000": {
                    "position_cycle_id": f"cycle-{initial_qty}", "mode": "short",
                    "initial_qty": initial_qty, "current_qty": initial_qty,
                    "entry_price": 10, "initial_stop_price": 9,
                    "highest_price": 12, "atr14": 0.4,
                    "take_profit_stage": 0, "opened_at": "2026-08-03 09:31:00",
                }}

                result = a_share_strategy.merge_holding_stop_loss_rows(
                    pd.DataFrame(), pd.DataFrame(), portfolio, cycles=cycles,
                    current_day=date(2026, 8, 3),
                )

                self.assertEqual(result.iloc[0]["target_qty"], target_qty)

    def test_one_lot_protection_is_persisted_without_a_sell_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 100, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-08-03 09:55:00")
                conn.execute(
                    """UPDATE position_cycles SET mode='short', entry_price=10,
                       initial_stop_price=9, initial_r=1, atr14=0.4,
                       highest_price=13 WHERE stock_code='600000' AND status='active'"""
                )
            portfolio = {"600000": {
                "code": "600000", "name": "PF Bank", "qty": 100,
                "status": "holding", "cost_price": 10,
                "current_price": 12, "stop_price": 9,
            }}

            first = a_share_strategy.merge_holding_stop_loss_rows(
                pd.DataFrame(), pd.DataFrame(), portfolio,
                cycles=store.get_active_position_cycles(), store=store,
                decision_batch_at="2026-08-03T10:00:00+08:00",
                current_day=date(2026, 8, 3),
            )
            same_batch = a_share_strategy.merge_holding_stop_loss_rows(
                pd.DataFrame(), pd.DataFrame(), {
                    "600000": {**portfolio["600000"], "current_price": 12.1},
                },
                cycles=store.get_active_position_cycles(), store=store,
                decision_batch_at="2026-08-03T10:00:00+08:00",
                current_day=date(2026, 8, 3),
            )
            next_batch = a_share_strategy.merge_holding_stop_loss_rows(
                pd.DataFrame(), pd.DataFrame(), {
                    "600000": {**portfolio["600000"], "current_price": 12.1},
                },
                cycles=store.get_active_position_cycles(), store=store,
                decision_batch_at="2026-08-03T10:05:00+08:00",
                current_day=date(2026, 8, 3),
            )

            self.assertTrue(first.empty)
            self.assertTrue(same_batch.empty)
            self.assertEqual(next_batch.iloc[0]["signal_action"], "trailing_stop")
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM exit_intents"
                ).fetchone()[0], 0)

    def test_legacy_cycle_without_buy_signal_only_uses_fixed_stop(self) -> None:
        portfolio = {"600000": {"code": "600000", "qty": 1000, "status": "holding",
            "cost_price": 10, "current_price": 10.1, "stop_price": 9.65}}
        cycles = {"600000": {"position_cycle_id": "legacy", "mode": "legacy_fixed",
            "initial_qty": 1000, "entry_price": 10, "initial_stop_price": 9.65,
            "highest_price": 12, "atr14": 0, "take_profit_stage": 0,
            "opened_at": "2026-06-01"}}
        result = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), pd.DataFrame(), portfolio, cycles=cycles, current_day=date(2026, 7, 13),
        )
        self.assertTrue(result.empty)

    def test_stale_or_abnormal_spot_cannot_trigger_sell(self) -> None:
        portfolio = {"600000": {"code": "600000", "qty": 100, "status": "holding",
            "current_price": 10, "stop_price": 9}}
        stale = pd.DataFrame([{"code": "600000", "price": 8, "quote_age_sec": 121}])
        abnormal = pd.DataFrame([{"code": "600000", "price": 7}])

        self.assertTrue(a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), stale, portfolio,
        ).empty)
        self.assertTrue(a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), abnormal, portfolio,
        ).empty)

    def test_open_hard_stop_intent_republishes_after_price_recovers(self) -> None:
        portfolio = {"600000": {
            "code": "600000", "name": "浦发银行", "qty": 1000,
            "status": "holding", "current_price": 10.2, "stop_price": 9.0,
        }}
        intents = {"600000": {
            "signal_id": "cycle-hard_stop-0", "stock_code": "600000",
            "target_qty": 0, "reason": "hard_stop", "status": "active",
        }}

        result = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), pd.DataFrame(), portfolio, exit_intents=intents,
        )

        self.assertEqual(result.iloc[0]["exit_signal_id"], "cycle-hard_stop-0")
        self.assertEqual(result.iloc[0]["target_qty"], 0)

    def test_open_partial_exit_republishes_only_until_target_is_reached(self) -> None:
        intent = {"600000": {
            "signal_id": "cycle-take_profit_1-0", "stock_code": "600000",
            "target_qty": 500, "reason": "take_profit_1", "status": "active",
        }}
        pending = {"600000": {
            "code": "600000", "qty": 800, "status": "partial_sell",
            "current_price": 11.0, "stop_price": 9.0,
        }}
        completed = {"600000": {**pending["600000"], "qty": 500}}

        pending_result = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), pd.DataFrame(), pending, exit_intents=intent,
        )
        completed_result = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), pd.DataFrame(), completed, exit_intents=intent,
        )

        self.assertEqual(pending_result.iloc[0]["target_qty"], 500)
        self.assertTrue(completed_result.empty)

    def test_fresh_hard_stop_overrides_open_take_profit_intent(self) -> None:
        portfolio = {"600000": {
            "code": "600000", "qty": 1000, "status": "holding",
            "current_price": 8.9, "stop_price": 9.0,
        }}
        cycles = {"600000": {
            "position_cycle_id": "cycle-1", "mode": "short",
            "initial_qty": 1000, "current_qty": 1000, "entry_price": 10.0,
            "initial_stop_price": 9.0, "highest_price": 12.0, "atr14": 0.4,
            "take_profit_stage": 0, "opened_at": "2026-07-10 09:31:00",
        }}
        intents = {"600000": {
            "signal_id": "cycle-1-take_profit_1-0", "stock_code": "600000",
            "target_qty": 500, "reason": "take_profit_1", "status": "active",
        }}

        result = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), pd.DataFrame(), portfolio, cycles=cycles,
            exit_intents=intents, current_day=date(2026, 7, 14),
        )

        self.assertEqual(result.iloc[0]["signal_action"], "hard_stop")
        self.assertEqual(result.iloc[0]["target_qty"], 0)

    def test_one_lot_activation_does_not_hide_an_active_exit_intent(self) -> None:
        portfolio = {"600000": {
            "code": "600000", "qty": 100, "status": "holding",
            "cost_price": 10, "current_price": 12, "stop_price": 9,
        }}
        cycles = {"600000": {
            "position_cycle_id": "cycle-one-lot", "mode": "short",
            "initial_qty": 100, "current_qty": 100, "entry_price": 10,
            "initial_stop_price": 9, "highest_price": 12, "atr14": 0.4,
            "take_profit_stage": 0, "opened_at": "2026-08-03 09:31:00",
        }}
        intents = {"600000": {
            "signal_id": "cycle-one-lot-hard_stop-0", "stock_code": "600000",
            "target_qty": 0, "reason": "hard_stop", "status": "active",
        }}

        result = a_share_strategy.merge_holding_stop_loss_rows(
            pd.DataFrame(), pd.DataFrame(), portfolio, cycles=cycles,
            exit_intents=intents,
            decision_batch_at="2026-08-03T10:00:00+08:00",
            current_day=date(2026, 8, 3),
        )

        self.assertEqual(result.iloc[0]["signal_action"], "hard_stop")
        self.assertEqual(result.iloc[0]["target_qty"], 0)


if __name__ == "__main__":
    unittest.main()
