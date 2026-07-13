import json
import tempfile
import unittest
from pathlib import Path

import joinquant_sync
from trading_store import TradingStore


class JoinQuantSyncTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
