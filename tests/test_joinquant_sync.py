import json
import tempfile
import unittest
from pathlib import Path

import joinquant_sync
from trading_store import TradingStore


class JoinQuantSyncTest(unittest.TestCase):
    @staticmethod
    def _ledger_snapshot(generated_at: str = "2026-07-07 10:05:00") -> dict:
        return {
            "schema_version": 1,
            "trade_date": "2026-07-07",
            "generated_at": generated_at,
            "source": "joinquant",
            "template_version": "test-ledger-v6",
            "cash": 89500,
            "available_cash": 89500,
            "total_value": 100000,
            "daily_turnover_pct": 10.5,
            "daily_pnl_pct": 0.5,
            "account_drawdown_pct": -0.2,
            "positions": [{
                "code": "600000", "jq_code": "600000.XSHG", "qty": 1000,
                "closeable_amount": 1000, "locked_amount": 0, "today_amount": 0,
                "avg_cost": 10.0, "price": 10.5, "market_value": 10500, "pnl": 500,
            }],
            "orders": [{
                "order_id": "10", "code": "600000", "jq_code": "600000.XSHG",
                "action": "buy", "amount": 1000, "filled": 1000,
                "avg_price": 10.0, "status": "filled", "datetime": "2026-07-07 10:05:00",
            }],
            "trades": [{
                "trade_id": "20", "order_id": "10", "code": "600000",
                "action": "buy", "amount": 1000, "price": 10.0,
                "commission": 5.0, "stamp_tax": 0.0, "other_fee": 0.2,
                "datetime": "2026-07-07 10:05:00",
            }],
        }

    def test_ingest_persists_snapshot_order_fill_and_daily_equity_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()

            first = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:02")
            second = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:03")

            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM position_snapshots").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM fills").fetchone()[0], 1)
                equity = conn.execute("SELECT * FROM daily_equity WHERE trade_date='2026-07-07'").fetchone()
            self.assertEqual(equity["opening_equity"], 100000)
            self.assertEqual(equity["closing_equity"], 100000)
            self.assertAlmostEqual(equity["fees"], 5.2)
            self.assertEqual(equity["unrealized_pnl"], 500)

    def test_detail_retention_keeps_changes_and_hourly_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            joinquant_sync.ingest_snapshot_payload(
                self._ledger_snapshot("2026-07-07 10:05:00"), store, "2026-07-07 10:05:02"
            )
            joinquant_sync.ingest_snapshot_payload(
                self._ledger_snapshot("2026-07-07 10:06:00"), store, "2026-07-07 10:06:02"
            )
            joinquant_sync.ingest_snapshot_payload(
                self._ledger_snapshot("2026-07-07 11:00:00"), store, "2026-07-07 11:00:02"
            )
            changed = self._ledger_snapshot("2026-07-07 11:01:00")
            changed["positions"][0]["price"] = 10.6
            changed["positions"][0]["market_value"] = 10600
            joinquant_sync.ingest_snapshot_payload(changed, store, "2026-07-07 11:01:02")

            with store.connect() as conn:
                retained = conn.execute(
                    "SELECT generated_at FROM account_snapshots WHERE retained_details=1 ORDER BY generated_at"
                ).fetchall()
                details = conn.execute("SELECT count(*) FROM position_snapshots").fetchone()[0]
                controls = {
                    row["key"]: row["value"] for row in conn.execute(
                        "SELECT key, value FROM system_state WHERE key IN ('buy_enabled','kill_switch')"
                    )
                }
            self.assertEqual([row[0] for row in retained], [
                "2026-07-07 10:05:00", "2026-07-07 11:00:00", "2026-07-07 11:01:00",
            ])
            self.assertEqual(details, 3)
            self.assertEqual(controls.get("buy_enabled", "1"), "1")
            self.assertEqual(controls.get("kill_switch", "0"), "0")

    def test_syncs_snapshot_to_portfolio_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_file = Path(tmp) / "account.json"
            positions_file = Path(tmp) / "positions.json"
            events_file = Path(tmp) / "events.jsonl"
            migration_file = Path(tmp) / "migration.md"
            account_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "trade_date": "2026-07-07",
                        "generated_at": "2026-07-07 15:05:00",
                        "source": "joinquant",
                        "cash": 20000,
                        "available_cash": 18000,
                        "total_value": 100000,
                        "daily_turnover_pct": 12.5,
                        "daily_pnl_pct": -1.2,
                        "account_drawdown_pct": -3.4,
                        "consecutive_losses": 2,
                        "pending_buy_position_pct": 4.5,
                        "pending_buy_risk_pct": 0.4,
                        "positions": [
                            {
                                "code": "600000",
                                "jq_code": "600000.XSHG",
                                "name": "PF Bank",
                                "qty": 1000,
                                "closeable_amount": 600,
                                "locked_amount": 200,
                                "today_amount": 200,
                                "avg_cost": 10.0,
                                "price": 10.5,
                                "market_value": 10500,
                                "pnl": 500,
                            }
                        ],
                        "trades": [],
                    }
                ),
                encoding="utf-8",
            )

            store = TradingStore(Path(tmp) / "trading.db")
            count = joinquant_sync.sync_account_snapshot(
                account_file, positions_file, events_file, store=store, migration_report_file=migration_file
            )

            payload = json.loads(positions_file.read_text(encoding="utf-8"))
            self.assertEqual(count, 1)
            self.assertEqual(payload["positions"][0]["code"], "600000")
            self.assertEqual(payload["positions"][0]["source"], "joinquant")
            self.assertEqual(payload["positions"][0]["current_price"], 10.5)
            self.assertEqual(payload["positions"][0]["closeable_qty"], 600)
            self.assertEqual(payload["positions"][0]["locked_qty"], 200)
            self.assertEqual(payload["account"]["available_cash"], 18000)
            self.assertEqual(payload["account"]["daily_turnover_pct"], 12.5)
            self.assertEqual(payload["account"]["consecutive_losses"], 2)
            self.assertEqual(payload["account"]["pending_buy_position_pct"], 4.5)
            self.assertEqual(payload["account"]["pending_buy_risk_pct"], 0.4)
            self.assertTrue(events_file.exists())
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["current_qty"], 1000)
            self.assertEqual(cycle["initial_stop_price"], 9.65)

            report = joinquant_sync.build_position_migration_report(payload["positions"], store.get_active_position_cycles())
            self.assertIn("600000", report)
            self.assertIn("ATR", report)
            self.assertIn("enabled_rules", report)
            self.assertEqual(joinquant_sync.unsafe_migration_codes(
                payload["positions"], store.get_active_position_cycles(),
            ), [])
            self.assertIn("固定硬止损", report)
            self.assertTrue(migration_file.exists())

    def test_sync_defaults_missing_consecutive_losses_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_file = root / "account.json"
            positions_file = root / "positions.json"
            account_file.write_text(
                json.dumps({"schema_version": 1, "positions": []}), encoding="utf-8"
            )

            joinquant_sync.sync_account_snapshot(
                account_file,
                positions_file,
                root / "events.jsonl",
                store=TradingStore(root / "trading.db"),
                migration_report_file=root / "migration.md",
            )

            payload = json.loads(positions_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["account"]["consecutive_losses"], 0)


if __name__ == "__main__":
    unittest.main()
