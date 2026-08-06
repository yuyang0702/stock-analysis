import json
import tempfile
import unittest
from pathlib import Path

from joinquant_signal_server import create_app
from joinquant_sync import ingest_snapshot_payload
from trading_control import unlock_eligibility
from trading_store import TradingStore


class ExecutionLedgerIntegrationTest(unittest.TestCase):
    def test_take_profit_callback_advances_stage_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            initial = {
                "schema_version": 1,
                "trade_date": "2026-08-03",
                "generated_at": "2026-08-03 09:55:00",
                "cash": 97000,
                "available_cash": 97000,
                "total_value": 100000,
                "positions": [{
                    "code": "600000", "qty": 300, "closeable_amount": 300,
                    "locked_amount": 0, "today_amount": 0, "avg_cost": 10,
                    "price": 12, "market_value": 3600, "pnl": 600,
                }],
                "orders": [],
                "trades": [],
            }
            ingest_snapshot_payload(initial, store, "2026-08-03 09:55:01")
            cycle = store.get_active_position_cycles()["600000"]
            signal_id = f"{cycle['position_cycle_id']}-take_profit_1-0"
            reason = "take_profit_1: 达到2R；计划卖出100股（占当前持仓33.3%），目标保留200股"
            with store.transaction() as conn:
                conn.execute(
                    """INSERT INTO strategy_runs(
                       run_id,trade_date,started_at,created_at,updated_at
                       ) VALUES('tp-run','2026-08-03','2026-08-03 10:00:00',
                                '2026-08-03 10:00:00','2026-08-03 10:00:00')"""
                )
                conn.execute(
                    """INSERT INTO signals(
                       signal_id,run_id,trade_date,stock_code,jq_code,action,
                       generated_at,raw_json,created_at
                       ) VALUES(?,'tp-run','2026-08-03','600000','600000.XSHG',
                                'sell','2026-08-03 10:00:00','{}',
                                '2026-08-03 10:00:00')""",
                    (signal_id,),
                )
                store.upsert_exit_intent(
                    conn, signal_id, "600000", 200, reason,
                    "2026-08-03 10:00:00",
                )

            reduced = {
                **initial,
                "generated_at": "2026-08-03 10:01:00",
                "cash": 98200,
                "available_cash": 98200,
                "positions": [{
                    **initial["positions"][0],
                    "qty": 200,
                    "closeable_amount": 200,
                    "market_value": 2400,
                    "pnl": 400,
                }],
                "orders": [{
                    "id": signal_id, "signal_id": signal_id,
                    "order_id": "tp-order", "code": "600000",
                    "action": "sell", "amount": 100, "filled": 100,
                    "avg_price": 12, "status": "filled",
                    "datetime": "2026-08-03 10:01:00",
                }],
                "trades": [{
                    "trade_id": "tp-fill", "order_id": "tp-order",
                    "code": "600000", "action": "sell", "amount": 100,
                    "price": 12, "commission": 5,
                    "datetime": "2026-08-03 10:01:00",
                }],
            }
            ingest_snapshot_payload(reduced, store, "2026-08-03 10:01:01")
            ingest_snapshot_payload(reduced, store, "2026-08-03 10:01:02")

            self.assertEqual(
                store.get_active_position_cycles()["600000"]["take_profit_stage"],
                1,
            )
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM fills WHERE fill_id='tp-fill'"
                ).fetchone()[0], 1)

    def test_callback_partial_fill_replay_controls_and_recovery_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            store = TradingStore(base / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                conn.execute(
                    """INSERT INTO strategy_runs(run_id, trade_date, started_at, created_at, updated_at)
                       VALUES ('run-1','2026-07-14','2026-07-14 09:30:00','2026-07-14 09:30:00','2026-07-14 09:30:00')"""
                )
                conn.execute(
                    """INSERT INTO signals(signal_id, run_id, trade_date, stock_code, jq_code, action,
                       generated_at, raw_json, created_at)
                       VALUES ('sig-1','run-1','2026-07-14','600000','600000.XSHG','buy',
                       '2026-07-14 09:30:00','{}','2026-07-14 09:30:00')"""
                )
            app = create_app("secret", signal_file, account_file, store=store)
            client = app.test_client()

            def payload(generated_at: str, qty: int, filled: int, trades: list[dict]) -> dict:
                return {
                    "schema_version": 1, "trade_date": "2026-07-14",
                    "generated_at": generated_at, "received_at": generated_at,
                    "template_version": "ledger-v6", "cash": 100000 - qty * 10,
                    "available_cash": 100000 - qty * 10, "total_value": 100000,
                    "positions": [{
                        "code": "600000", "jq_code": "600000.XSHG", "qty": qty,
                        "closeable_amount": qty, "locked_amount": 0, "today_amount": 0,
                        "avg_cost": 10, "price": 10, "market_value": qty * 10, "pnl": 0,
                    }],
                    "orders": [{
                        "id": "sig-1", "order_id": "o-1", "code": "600000",
                        "action": "buy", "amount": 100, "filled": filled,
                        "avg_price": 10, "status": "filled" if filled == 100 else "partial",
                        "datetime": generated_at,
                    }],
                    "trades": trades,
                }

            t1 = {"trade_id": "t-1", "order_id": "o-1", "code": "600000", "action": "buy",
                  "amount": 50, "price": 10, "commission": 1, "datetime": "2026-07-14 10:00:00"}
            partial = payload("2026-07-14 10:00:00", 50, 50, [t1])
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=partial).status_code, 200)

            t2 = {"trade_id": "t-2", "order_id": "o-1", "code": "600000", "action": "buy",
                  "amount": 50, "price": 10, "commission": 1, "datetime": "2026-07-14 10:01:00"}
            filled = payload("2026-07-14 10:01:00", 100, 100, [t1, t2])
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=filled).status_code, 200)
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=filled).status_code, 200)

            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM fills").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT filled_qty FROM orders WHERE order_id='o-1'").fetchone()[0], 100)
                self.assertEqual(conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT count(*) FROM daily_equity").fetchone()[0], 1)

            with store.transaction() as conn:
                store.upsert_exit_intent(
                    conn, "exit-1", "600000", 0, "hard_stop",
                    "2026-07-14 09:58:00",
                )
            mismatch = dict(filled, generated_at="2026-07-14 10:02:00")
            mismatch["orders"] = []
            mismatch["trades"] = []
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=mismatch).status_code, 200)
            self.assertEqual(store.get_system_state("buy_enabled"), "0")

            with store.transaction() as conn:
                store.reconcile_exit_intents(conn, [], "2026-07-14 10:03:00")
            flat = {
                "schema_version": 1, "trade_date": "2026-07-14", "cash": 100000,
                "available_cash": 100000, "total_value": 100000,
                "positions": [], "orders": filled["orders"], "trades": [t1, t2],
            }
            ingest_snapshot_payload(
                dict(flat, generated_at="2026-07-14 10:04:00"), store,
                "2026-07-14 10:04:01", mode="full",
            )
            ingest_snapshot_payload(
                dict(flat, generated_at="2026-07-14 10:05:00"), store,
                "2026-07-14 10:05:01", mode="full",
            )
            self.assertEqual(unlock_eligibility(store, now="2026-07-14 10:05:02"), (True, []))
            self.assertEqual(store.get_system_state("buy_enabled"), "0")


if __name__ == "__main__":
    unittest.main()
