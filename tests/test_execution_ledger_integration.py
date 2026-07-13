import json
import tempfile
import unittest
from pathlib import Path

from joinquant_signal_server import create_app
from joinquant_sync import ingest_snapshot_payload
from trading_control import unlock_eligibility
from trading_store import TradingStore


class ExecutionLedgerIntegrationTest(unittest.TestCase):
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
                    "schema_version": 1, "trade_date": "2026-07-14", "generated_at": generated_at,
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
                store.upsert_exit_intent(conn, "exit-1", "600000", 0, "hard_stop", "2026-07-14 10:01:30")
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
