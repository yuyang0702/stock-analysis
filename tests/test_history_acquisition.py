import tempfile
import unittest
from pathlib import Path

import pandas as pd
from unittest.mock import patch

from history_acquisition import (
    AcquisitionConfig,
    acquire_akshare_daily,
    acquire_broker_historical,
    acquire_jqdata_daily,
    discover_akshare_a_share_codes,
    fetch_akshare_code,
    normalize_akshare_daily_frame,
)
from historical_data import HistoricalStore


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

    def test_default_fetch_falls_back_to_tencent_with_explicit_proxy_metadata(self) -> None:
        class FakeAkShare:
            @staticmethod
            def stock_zh_a_hist(**_kwargs):
                raise RuntimeError("eastmoney unavailable")

            @staticmethod
            def stock_zh_a_hist_tx(**_kwargs):
                return pd.DataFrame([
                    {"date": "2024-12-31", "open": 9.8, "high": 10.1,
                     "low": 9.7, "close": 10.0, "amount": 100},
                    {"date": "2025-01-02", "open": 10.1, "high": 10.5,
                     "low": 10.0, "close": 10.4, "amount": 110},
                ])

        report: dict[str, object] = {}
        with patch.dict("sys.modules", {"akshare": FakeAkShare()}):
            rows = fetch_akshare_code(
                "600000", "2025-01-02", "2025-01-03", source_report=report
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["tx_fallback_codes"], ["600000"])
        self.assertEqual(rows[0]["volume"], 110.0)
        self.assertEqual(rows[0]["amount"], 114400.0)

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

    def test_discover_current_universe_is_explicitly_not_historical_membership(self) -> None:
        frame = pd.DataFrame([{"代码": "600000"}, {"代码": "000001"}, {"代码": "600000"}])
        self.assertEqual(
            discover_akshare_a_share_codes(lambda: frame),
            ["000001", "600000"],
        )

    def test_normalized_output_keeps_akshare_source_when_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = AcquisitionConfig("ak-proxy", "2025-01-02", "2025-01-03", root / "export", sleep_seconds=0)
            acquire_akshare_daily(config, ["600000"], fetcher=lambda **_: self._frame())

            store = HistoricalStore(root / "history.db")
            store.initialize()
            for kind in ("bars", "status", "universe"):
                store.import_csv(
                    config.dataset_id,
                    kind,
                    config.output_dir / f"{kind}.csv",
                    "akshare_canonical",
                    "raw",
                )

            with store.connect() as connection:
                sources = connection.execute(
                    "SELECT DISTINCT source FROM dataset_manifests WHERE dataset_id = ?",
                    (config.dataset_id,),
                ).fetchall()
            self.assertEqual([row[0] for row in sources], ["akshare_canonical"])

    def test_jqdata_acquisition_writes_bounded_proxy_artifacts(self) -> None:
        class FakeProvider:
            @staticmethod
            def connect():
                return None

            @staticmethod
            def plain_code(value):
                return str(value).zfill(6)

            @staticmethod
            def fetch_daily(code, start, end, *, adjust=""):
                return [{
                    "trade_date": "2025-01-02", "code": str(code).zfill(6),
                    "open": 10, "high": 11, "low": 9, "close": 10.5,
                    "prev_close": 10, "volume": 100, "amount": 1000,
                    "adjust_factor": 1, "limit_up": 11, "limit_down": 9,
                    "suspended": 0,
                }]

        with tempfile.TemporaryDirectory() as tmp:
            config = AcquisitionConfig("jq-proxy", "2025-01-02", "2025-01-02", Path(tmp), sleep_seconds=0)
            metadata = acquire_jqdata_daily(
                config,
                ["600000"],
                username="u",
                password="p",
                provider=FakeProvider(),
            )
            self.assertEqual(metadata["source"], "jqdata")
            self.assertTrue(metadata["proxy_only"])
            self.assertEqual(metadata["rows"]["bars"], 1)

    def test_broker_historical_export_is_a_separate_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "broker"
            source.mkdir()
            (source / "bars.csv").write_text(
                "trade_date,code,open,high,low,close,prev_close,volume,amount,adjust_factor\n"
                "2025-01-02,600000,10,11,9,10.5,10,100,1000,1\n",
                encoding="utf-8",
            )
            output = Path(tmp) / "out"
            config = AcquisitionConfig("broker-proxy", "2025-01-02", "2025-01-02", output, sleep_seconds=0)
            metadata = acquire_broker_historical(config, input_dir=source)
            self.assertEqual(metadata["source"], "broker")
            self.assertTrue((output / "bars.csv").is_file())


if __name__ == "__main__":
    unittest.main()
