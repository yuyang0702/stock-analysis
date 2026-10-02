import tempfile
import unittest
from pathlib import Path

import pandas as pd

from history_acquisition import (
    AcquisitionConfig,
    acquire_akshare_daily,
    fetch_akshare_code,
    normalize_akshare_daily_frame,
)


class HistoryAcquisitionTest(unittest.TestCase):
    @staticmethod
    def _frame() -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"日期": "2024-12-31", "开盘": 9.8, "最高": 10.1, "最低": 9.7, "收盘": 10.0, "成交量": 100, "成交额": 1000},
                {"日期": "2025-01-02", "开盘": 10.1, "最高": 10.5, "最低": 10.0, "收盘": 10.4, "成交量": 110, "成交额": 1100},
                {"日期": "2025-01-03", "开盘": 10.3, "最高": 10.6, "最低": 10.2, "收盘": 10.5, "成交量": 120, "成交额": 1200},
            ]
        )

    def test_normalize_derives_previous_close_only_from_earlier_row(self) -> None:
        rows = normalize_akshare_daily_frame(self._frame(), "600000", "2025-01-02", "2025-01-03")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["prev_close"], 10.0)
        self.assertEqual(rows[1]["prev_close"], 10.4)
        self.assertEqual(rows[0]["adjust_factor"], 1.0)

    def test_fetch_passes_bounded_lookback_to_provider(self) -> None:
        calls = []

        def fetcher(**kwargs):
            calls.append(kwargs)
            return self._frame()

        rows = fetch_akshare_code("600000", "2025-01-02", "2025-01-03", fetcher=fetcher)

        self.assertEqual(len(rows), 2)
        self.assertEqual(calls[0]["start_date"], "20241203")
        self.assertEqual(calls[0]["end_date"], "20250103")
        self.assertEqual(calls[0]["adjust"], "")

    def test_acquisition_writes_proxy_metadata_and_bounded_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AcquisitionConfig("ak-proxy", "2025-01-02", "2025-01-03", Path(tmp), sleep_seconds=0)
            metadata = acquire_akshare_daily(config, ["600000"], fetcher=lambda **_: self._frame())

            self.assertFalse(metadata["strict_eligible"])
            self.assertTrue(metadata["proxy_only"])
            self.assertEqual(metadata["rows"]["bars"], 2)
            self.assertTrue((Path(tmp) / "bars.csv").is_file())
            self.assertTrue((Path(tmp) / "acquisition_metadata.json").is_file())
            self.assertIn("historical_universe_membership_unavailable", metadata["warnings"])


if __name__ == "__main__":
    unittest.main()
