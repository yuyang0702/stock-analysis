import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

import a_share_strategy


class JoinQuantExportRuntimeTest(unittest.TestCase):
    def test_pending_buy_codes_only_include_active_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_file = Path(tmp) / "account.json"
            account_file.write_text(json.dumps({"orders": [
                {"action": "buy", "code": "600000", "status": "submitted"},
                {"action": "buy", "code": "000001", "status": "partial"},
                {"action": "buy", "code": "300001", "status": "filled"},
                {"action": "sell", "code": "600519", "status": "submitted"},
            ]}), encoding="utf-8")

            self.assertEqual(
                a_share_strategy.load_pending_buy_codes(account_file),
                {"600000", "000001"},
            )

    def test_cached_industry_map_is_read_without_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "industry.json"
            path.write_text(json.dumps({"600000": "银行"}), encoding="utf-8")

            self.assertEqual(a_share_strategy.load_cached_industry_map(path), {"600000": "银行"})

    def test_runtime_counts_ledger_classifications_for_existing_and_pending_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            output_path.write_text("{}", encoding="utf-8")
            store = MagicMock()
            store.get_active_position_cycles.return_value = {}
            store.get_active_position_classifications.return_value = {
                "600000": {"industry": "银行", "theme": "高股息"},
            }
            store.get_pending_buy_classification_exposures.return_value = [{
                "code": "000001", "industry": "银行", "theme": "高股息", "position_pct": 5,
            }]
            store.get_daily_factor_openings.return_value = []
            store.active_cooldown_codes.return_value = set()
            store.daily_activity.return_value = (0, 0)
            rows = pd.DataFrame([{"code": "600519"}])
            positions = {"600000": {"code": "600000", "market_value": 20000}}
            fresh_spot = pd.DataFrame([{
                "code": "600519", "price": 1500.0,
                "quote_time": "2026-08-01T10:00:00+08:00",
            }])

            with patch("a_share_strategy.TradingStore", return_value=store), patch(
                "a_share_strategy.is_a_share_trading_time", return_value=True
            ), patch(
                "a_share_strategy.fetch_spot_data", return_value=fresh_spot
            ), patch("a_share_strategy.load_portfolio_positions", return_value=positions), patch(
                "a_share_strategy.load_pending_buy_codes", return_value={"000001"}
            ), patch("a_share_strategy.load_portfolio_account_total_value", return_value=100000), patch(
                "a_share_strategy.load_portfolio_available_cash", return_value=80000
            ), patch("a_share_strategy.load_portfolio_account_metrics", return_value={}), patch(
                "joinquant_exporter.export_signals", return_value=output_path
            ) as export:
                a_share_strategy.run_joinquant_export(a_share_strategy.Config(), rows)

            kwargs = export.call_args.kwargs
            exported_rows = export.call_args.args[0]
            self.assertIs(kwargs["store"], store)
            self.assertEqual(kwargs["cohort_mode"], "after")
            self.assertEqual(kwargs["cohort_interval_sec"], 300)
            self.assertEqual(kwargs["current_position_count"], 2)
            self.assertEqual(kwargs["sector_exposure_pct"], {"银行": 25.0})
            self.assertEqual(kwargs["theme_exposure_pct"], {"高股息": 25.0})
            self.assertEqual(kwargs["factor_position_counts"], {})
            self.assertEqual(kwargs["factor_new_positions_today"], {})
            self.assertEqual(exported_rows.iloc[0]["price"], 1500.0)
            self.assertEqual(
                exported_rows.iloc[0]["quote_time"],
                "2026-08-01T10:00:00+08:00",
            )

    def test_execution_quote_refresh_preserves_strategy_fields_and_missing_codes(self) -> None:
        rows = pd.DataFrame([
            {
                "code": "600000", "price": 10.0, "quote_time": "old",
                "final_score": 91, "entry_price": 9.8,
            },
            {
                "code": "000001", "price": 12.0, "quote_time": "old",
                "final_score": 88, "entry_price": 11.8,
            },
        ])
        fresh = pd.DataFrame([{
            "code": "600000", "price": 10.2, "pct_chg": 2.0,
            "quote_time": "2026-08-01T10:00:00+08:00",
        }])

        refreshed = a_share_strategy._refresh_execution_quote_rows(rows, fresh)

        self.assertEqual(refreshed.iloc[0]["price"], 10.2)
        self.assertEqual(
            refreshed.iloc[0]["quote_time"], "2026-08-01T10:00:00+08:00",
        )
        self.assertEqual(refreshed.iloc[0]["final_score"], 91)
        self.assertEqual(refreshed.iloc[0]["entry_price"], 9.8)
        self.assertEqual(refreshed.iloc[1]["price"], 12.0)
        self.assertEqual(refreshed.iloc[1]["quote_time"], "old")

    def test_spot_quotes_keep_the_real_fetch_timestamp(self) -> None:
        raw = pd.DataFrame([{
            "代码": "600000", "名称": "PF Bank", "最新价": 10.0,
            "涨跌幅": 1.0, "成交额": 100000000,
        }])
        with patch.object(a_share_strategy.ak, "stock_zh_a_spot_em", return_value=raw):
            result = a_share_strategy.fetch_spot_data()

        self.assertIn("quote_time", result.columns)
        self.assertRegex(str(result.iloc[0]["quote_time"]), r"\+08:00$")

    def test_runtime_blocks_buy_export_outside_a_share_trading_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "PF Bank",
                        "price": 10.5,
                        "entry_price": 10.5,
                        "position_pct": 12,
                        "final_score": 90,
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                    }
                ]
            )

            with patch("joinquant_exporter.app_config.JOINQUANT_SIGNAL_FILE", output_path), patch(
                "a_share_strategy.app_config.TRADING_DB_FILE",
                Path(tmp) / "trading.db",
            ), patch(
                "a_share_strategy.is_a_share_trading_time", return_value=False
            ):
                result = a_share_strategy.run_joinquant_export(a_share_strategy.Config(), rows)

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])


if __name__ == "__main__":
    unittest.main()
