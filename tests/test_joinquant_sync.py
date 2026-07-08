import json
import tempfile
import unittest
from pathlib import Path

import joinquant_sync


class JoinQuantSyncTest(unittest.TestCase):
    def test_syncs_snapshot_to_portfolio_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_file = Path(tmp) / "account.json"
            positions_file = Path(tmp) / "positions.json"
            events_file = Path(tmp) / "events.jsonl"
            account_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "trade_date": "2026-07-07",
                        "generated_at": "2026-07-07 15:05:00",
                        "source": "joinquant",
                        "positions": [
                            {
                                "code": "600000",
                                "jq_code": "600000.XSHG",
                                "name": "PF Bank",
                                "qty": 1000,
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

            count = joinquant_sync.sync_account_snapshot(account_file, positions_file, events_file)

            payload = json.loads(positions_file.read_text(encoding="utf-8"))
            self.assertEqual(count, 1)
            self.assertEqual(payload["positions"][0]["code"], "600000")
            self.assertEqual(payload["positions"][0]["source"], "joinquant")
            self.assertEqual(payload["positions"][0]["current_price"], 10.5)
            self.assertTrue(events_file.exists())


if __name__ == "__main__":
    unittest.main()
