import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import a_share_strategy
from trading_store import TradingStore


class StrategyRunLedgerTest(unittest.TestCase):
    def _run(self, result=None, error: Exception | None = None) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            if error is None:
                patcher = mock.patch("a_share_strategy.run_once", return_value=result)
            else:
                patcher = mock.patch("a_share_strategy.run_once", side_effect=error)
            with patcher:
                if error is None:
                    a_share_strategy._run_once_with_ledger(
                        mock.Mock(), mock.Mock(), store=store, run_id="scan-run",
                    )
                else:
                    with self.assertRaises(type(error)):
                        a_share_strategy._run_once_with_ledger(
                            mock.Mock(), mock.Mock(), store=store, run_id="scan-run",
                        )
            with store.connect() as conn:
                return dict(conn.execute(
                    "SELECT * FROM strategy_runs WHERE run_id='scan-run'"
                ).fetchone())

    def test_successful_scan_is_recorded(self) -> None:
        row = self._run(pd.DataFrame([{"code": "600000"}]))
        self.assertEqual(row["result"], "success")
        self.assertEqual(row["data_status"], "complete")
        self.assertTrue(row["finished_at"])
        self.assertIsNone(row["error_message"])

    def test_empty_scan_is_successful_but_marked_empty(self) -> None:
        row = self._run(None)
        self.assertEqual(row["result"], "success")
        self.assertEqual(row["data_status"], "empty")

    def test_failure_before_export_is_recorded_and_reraised(self) -> None:
        row = self._run(error=ValueError("market source failed"))
        self.assertEqual(row["result"], "failed")
        self.assertEqual(row["data_status"], "failed")
        self.assertIn("ValueError: market source failed", row["error_message"])


if __name__ == "__main__":
    unittest.main()
