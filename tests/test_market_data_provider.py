import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from data_source_registry import load_registry, register_dataset
from cross_source_compare import compare_sources
from historical_data import HistoricalStore
from market_data_provider import (
    BrokerHistoricalProvider,
    DataSourceSettings,
    JQDataProvider,
    load_data_source_settings,
    read_env_file,
)


class MarketDataProviderTest(unittest.TestCase):
    def test_env_file_loader_does_not_expose_or_mutate_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock-analysis.env"
            path.write_text("JQDATA_USERNAME='user'\nJQDATA_PASSWORD=secret\n# ignored\n", encoding="utf-8")
            self.assertEqual(read_env_file(path), {"JQDATA_USERNAME": "user", "JQDATA_PASSWORD": "secret"})
            with patch.dict("os.environ", {}, clear=True):
                settings = load_data_source_settings(env_file=path, provider="jqdata")
            self.assertEqual(settings.username, "user")
            self.assertEqual(settings.password, "secret")
            self.assertIn("password_configured=True", repr(settings))
            self.assertNotIn("secret", repr(settings))

    def test_jqdata_provider_normalizes_frame(self) -> None:
        class FakeJQ:
            @staticmethod
            def auth(_username, _password):
                return True

            @staticmethod
            def get_price(*_args, **_kwargs):
                return pd.DataFrame([
                    {"time": "2025-01-02", "open": 10, "high": 11, "low": 9, "close": 10.5,
                     "pre_close": 10, "volume": 100, "money": 1000, "factor": 1,
                     "high_limit": 11, "low_limit": 9, "paused": 0},
                ])

        with patch("market_data_provider.importlib.import_module", return_value=FakeJQ):
            provider = JQDataProvider("u", "p")
            provider.connect()
            rows = provider.fetch_daily("000001", "2025-01-02", "2025-01-02")
        self.assertEqual(rows[0]["code"], "000001")
        self.assertEqual(rows[0]["amount"], 1000.0)
        self.assertEqual(rows[0]["prev_close"], 10.0)

    def test_jqdata_provider_fetches_historical_status_and_universe(self) -> None:
        class FakeJQ:
            @staticmethod
            def auth(_username, _password):
                return True

            @staticmethod
            def get_extras(_field, securities, **_kwargs):
                return pd.DataFrame(
                    {securities[0]: [False, True]},
                    index=pd.to_datetime(["2025-01-02", "2025-01-03"]),
                )

            @staticmethod
            def get_all_securities(**_kwargs):
                return pd.DataFrame(
                    {
                        "start_date": pd.to_datetime(["2020-01-01"]),
                        "end_date": pd.to_datetime(["2200-01-01"]),
                    },
                    index=["000001.XSHE"],
                )

            @staticmethod
            def get_industry(securities, **_kwargs):
                return {securities[0]: {"sw_l1": {"industry_name": "银行"}}}

        with patch("market_data_provider.importlib.import_module", return_value=FakeJQ):
            provider = JQDataProvider("u", "p")
            provider.connect()
            flags = provider.fetch_st_flags(["000001"], "2025-01-02", "2025-01-03")
            universe = provider.fetch_universe_membership(["000001"], ["2025-01-02"])
            industry = provider.fetch_industry(["000001"], ["2025-01-02"])
        self.assertFalse(flags[("2025-01-02", "000001")])
        self.assertTrue(flags[("2025-01-03", "000001")])
        self.assertEqual(universe["2025-01-02"], {"000001"})
        self.assertEqual(industry[("2025-01-02", "000001")], "银行")

    def test_broker_export_provider_reads_canonical_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bars.csv").write_text(
                "trade_date,code,open,high,low,close,prev_close,volume,amount,adjust_factor\n"
                "2025-01-02,600000,10,11,9,10.5,10,100,1000,1\n",
                encoding="utf-8",
            )
            provider = BrokerHistoricalProvider(root)
            provider.connect()
            self.assertEqual(provider.latest_trade_date(), "2025-01-02")
            self.assertEqual(len(provider.fetch_daily("600000", "2025-01-01", "2025-01-03")), 1)

    def test_dataset_registry_is_bounded_and_provenance_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "datasets.json"
            entry = register_dataset(
                {
                    "dataset_id": "jqdata-price-core-test",
                    "source": "jqdata",
                    "strict_eligible": False,
                    "proxy_only": True,
                    "start": "2025-01-01",
                    "end": "2025-01-02",
                    "rows": {"bars": 1},
                    "warnings": ["point_in_time_features_not_collected"],
                },
                output_dir=Path(tmp) / "data",
                registry_path=registry,
            )
            self.assertEqual(entry["provider"], "jqdata")
            payload = load_registry(registry)
            self.assertEqual(payload["datasets"]["jqdata-price-core-test"]["proxy_only"], True)

    def test_cross_source_compare_reports_metric_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "history.db"
            store = HistoricalStore(db)
            store.initialize()
            with store.connect() as connection:
                for index, dataset in enumerate(("ak", "jq"), start=1):
                    connection.execute(
                        "INSERT INTO backtest_runs "
                        "(run_id,dataset_id,dataset_hash,start_date,end_date,mode,config_json,status,summary_json,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"run-{dataset}", dataset, f"hash-{dataset}", "2025-01-01", "2025-01-02",
                            "price_core", "{}", "complete",
                            json.dumps({"metrics": {"net_return": index / 100, "max_drawdown": 0.01, "profit_factor": 1.1, "turnover": 1, "win_rate": 0.5, "average_holding_days": 2}}),
                            f"2025-01-0{index}T00:00:00Z",
                        ),
                    )
            report = compare_sources(db, ["ak", "jq"], registry_path=Path(tmp) / "missing.json")
            self.assertEqual(report["comparisons"][0]["delta_vs_first"]["net_return"], 0.01)


if __name__ == "__main__":
    unittest.main()
