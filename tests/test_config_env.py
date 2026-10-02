import importlib
import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import config


class ConfigEnvTest(unittest.TestCase):
    def test_simulation_fee_schedule_has_one_versioned_default(self) -> None:
        try:
            with patch.dict(os.environ, {}, clear=True):
                fees = importlib.reload(config).SIMULATION_FEE_SCHEDULE

                self.assertEqual(fees.version, "simulation-only-v1")
                self.assertEqual(fees.buy_commission_rate, Decimal("0.0003"))
                self.assertEqual(fees.sell_commission_rate, Decimal("0.0003"))
                self.assertEqual(fees.buy_minimum_commission_yuan, Decimal("5"))
                self.assertEqual(fees.sell_minimum_commission_yuan, Decimal("5"))
                self.assertEqual(fees.stamp_tax_rate, Decimal("0.0005"))
                self.assertEqual(fees.transfer_fee_rate, Decimal("0.00001"))
                self.assertEqual(fees.buy_slippage_rate, Decimal("0.001"))
                self.assertEqual(fees.sell_slippage_rate, Decimal("0.001"))
        finally:
            importlib.reload(config)

    def test_fee_schedule_environment_values_are_explicit(self) -> None:
        updates = {
            "FEE_SCHEDULE_VERSION": "broker-v2",
            "FEE_SCHEDULE_EFFECTIVE_FROM": "2026-07-01",
            "FEE_BUY_COMMISSION_RATE": "0.00021",
            "FEE_SELL_COMMISSION_RATE": "0.00022",
            "FEE_BUY_MINIMUM_COMMISSION_YUAN": "6",
            "FEE_SELL_MINIMUM_COMMISSION_YUAN": "7",
            "FEE_STAMP_TAX_RATE": "0.0005",
            "FEE_TRANSFER_FEE_RATE": "0.00001",
            "FEE_OTHER_FEE_RATE": "0.00002",
            "FEE_BUY_SLIPPAGE_RATE": "0.0008",
            "FEE_SELL_SLIPPAGE_RATE": "0.0009",
        }
        try:
            with patch.dict(os.environ, updates, clear=True):
                fees = importlib.reload(config).SIMULATION_FEE_SCHEDULE

                self.assertEqual(fees.version, "broker-v2")
                self.assertEqual(fees.effective_from, "2026-07-01")
                self.assertEqual(fees.buy_commission_rate, Decimal("0.00021"))
                self.assertEqual(fees.sell_commission_rate, Decimal("0.00022"))
                self.assertEqual(fees.buy_minimum_commission_yuan, Decimal("6"))
                self.assertEqual(fees.sell_minimum_commission_yuan, Decimal("7"))
                self.assertEqual(fees.other_fee_rate, Decimal("0.00002"))
                self.assertEqual(fees.sell_slippage_rate, Decimal("0.0009"))
        finally:
            importlib.reload(config)

    def test_legacy_minimum_commission_env_fills_both_explicit_sides(self) -> None:
        try:
            with patch.dict(
                os.environ, {"FEE_MINIMUM_COMMISSION_YUAN": "4"}, clear=True
            ):
                fees = importlib.reload(config).SIMULATION_FEE_SCHEDULE

                self.assertEqual(fees.buy_minimum_commission_yuan, Decimal("4"))
                self.assertEqual(fees.sell_minimum_commission_yuan, Decimal("4"))
        finally:
            importlib.reload(config)

    def test_signal_watchlist_retention_default_is_twenty_days(self) -> None:
        try:
            with patch.dict(os.environ, {}, clear=True):
                reloaded = importlib.reload(config)
                self.assertEqual(reloaded.SIGNAL_WATCHLIST_DAYS_DEFAULT, 20)
        finally:
            importlib.reload(config)

    def test_observation_risk_defaults(self) -> None:
        try:
            with patch.dict(os.environ, {}, clear=True):
                reloaded = importlib.reload(config)
                self.assertEqual(reloaded.RISK_MODE, "observe")
                self.assertEqual(reloaded.MAX_SINGLE_POSITION_PCT, 30)
                self.assertEqual(reloaded.MAX_TOTAL_POSITION_PCT, 95)
                self.assertEqual(reloaded.JOINQUANT_MAX_POSITIONS_DEFAULT, 5)
                self.assertEqual(reloaded.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT, 80)
                self.assertTrue(reloaded.JOINQUANT_ALLOW_BUY_DEFAULT)
                self.assertTrue(reloaded.JOINQUANT_ALLOW_SELL_DEFAULT)
                self.assertEqual(reloaded.JOINQUANT_EXECUTION_INTENT_TTL_SEC_DEFAULT, 120)
                self.assertEqual(reloaded.ML_DB_FILE, reloaded.CACHE_DIR / "ml" / "ml.db")
                self.assertEqual(reloaded.ML_MODEL_DIR, reloaded.CACHE_DIR / "ml" / "models")
                self.assertEqual(reloaded.ML_DB_MAX_BYTES, 2_000_000_000)
                self.assertEqual(
                    reloaded.ML_HISTORY_DB_FILE,
                    reloaded.CACHE_DIR / "backtest" / "history.db",
                )
                self.assertEqual(reloaded.ML_HISTORY_DB_MAX_BYTES, 3_000_000_000)
                self.assertFalse(reloaded.ML_TRAINED_SHADOW_ENABLE)
                self.assertEqual(reloaded.ML_PERMISSION_LEVEL_MAX, 0)
                self.assertEqual(reloaded.ML_INFERENCE_TIMEOUT_SEC, 1.0)
                self.assertEqual(reloaded.ML_LABEL_SOURCE, "strict_counterfactual_v2")
                self.assertEqual(reloaded.ML_LABEL_VERSION, "ml-label-v2")
                self.assertEqual(reloaded.ML_LABEL_LOOKBACK_DAYS, 45)
                self.assertEqual(reloaded.ML_MAINTENANCE_MAX_ROWS, 500_000)
                self.assertEqual(reloaded.ML_BACKUP_DAILY_KEEP, 7)
                self.assertEqual(reloaded.HISTORY_BACKUP_MONTHLY_KEEP, 12)
        finally:
            importlib.reload(config)

    def test_unsupported_risk_mode_is_rejected(self) -> None:
        try:
            with patch.dict(os.environ, {"RISK_MODE": "BLOCK"}, clear=True):
                with self.assertRaisesRegex(ValueError, "Unsupported RISK_MODE"):
                    importlib.reload(config)
        finally:
            importlib.reload(config)

    def test_enforce_risk_mode_is_supported(self) -> None:
        try:
            with patch.dict(os.environ, {"RISK_MODE": "ENFORCE"}, clear=True):
                self.assertEqual(importlib.reload(config).RISK_MODE, "enforce")
        finally:
            importlib.reload(config)

    def test_ml_database_hard_caps_cannot_be_raised_by_environment(self) -> None:
        try:
            with patch.dict(
                os.environ,
                {"ML_DB_MAX_BYTES": "2000000001"},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "ML_DB_MAX_BYTES"):
                    importlib.reload(config)
            with patch.dict(
                os.environ,
                {"ML_HISTORY_DB_MAX_BYTES": "3000000001"},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "ML_HISTORY_DB_MAX_BYTES"):
                    importlib.reload(config)
        finally:
            importlib.reload(config)

    def test_database_backup_roots_must_be_distinct(self) -> None:
        try:
            with patch.dict(
                os.environ,
                {
                    "TRADING_BACKUP_DIR": "same-backup-root",
                    "ML_BACKUP_DIR": "same-backup-root",
                    "HISTORY_BACKUP_DIR": "history-backup-root",
                },
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "must be distinct"):
                    importlib.reload(config)
        finally:
            importlib.reload(config)

    def test_observation_risk_environment_values(self) -> None:
        updates = {
            "RISK_MODE": "OBSERVE", "MAX_SINGLE_POSITION_PCT": "31",
            "MAX_TOTAL_POSITION_PCT": "96", "MIN_CASH_RESERVE_PCT": "6",
            "MAX_SECTOR_EXPOSURE_PCT": "61", "MAX_NEW_POSITIONS_PER_DAY": "11",
            "MAX_ORDERS_PER_DAY": "51", "MAX_DAILY_TURNOVER_PCT": "201",
            "DAILY_LOSS_WARN_PCT": "6", "ACCOUNT_DRAWDOWN_WARN_PCT": "16",
            "MAX_CONSECUTIVE_ORDER_FAILURES": "6", "ACCOUNT_SNAPSHOT_MAX_AGE_SEC": "301",
            "SIGNAL_MAX_AGE_SEC": "1201", "RECONCILIATION_POSITION_TOLERANCE": "1.5",
            "JOINQUANT_EXECUTION_INTENT_TTL_SEC": "180",
            "TRADING_DB_FILE": "custom/trading.db",
            "TRADING_BACKUP_DIR": "custom/backups",
            "TRADING_BACKUP_DAILY_KEEP": "8",
            "TRADING_BACKUP_WEEKLY_KEEP": "5",
            "TRADING_BACKUP_MONTHLY_KEEP": "13",
            "ML_DB_FILE": "custom/ml.db",
            "ML_MODEL_DIR": "custom/models",
            "ML_DB_MAX_BYTES": "123456",
            "ML_HISTORY_DB_FILE": "custom/history.db",
            "ML_HISTORY_DB_MAX_BYTES": "234567",
            "ML_TRAINED_SHADOW_ENABLE": "1",
            "ML_PERMISSION_LEVEL_MAX": "2",
            "ML_INFERENCE_TIMEOUT_SEC": "0.75",
            "ML_HISTORY_DATASET_ID": "strict-2025",
            "ML_LABEL_SOURCE": "strict",
            "ML_LABEL_VERSION": "labels-custom",
            "ML_COST_SHA256": "cost-sha",
            "ML_POLICY_SHA256": "policy-sha",
            "ML_TRAINING_START_DATE": "2025-01-01",
            "ML_TRAINING_END_DATE": "2025-12-31",
            "ML_FEATURE_ALLOWLIST": "price,turnover,market_regime",
            "ML_LABEL_LOOKBACK_DAYS": "60",
            "ML_MAINTENANCE_MAX_ROWS": "600000",
            "ML_BACKUP_DIR": "custom/ml-backups",
            "HISTORY_BACKUP_DIR": "custom/history-backups",
            "ML_BACKUP_DAILY_KEEP": "8",
            "ML_BACKUP_WEEKLY_KEEP": "5",
            "ML_BACKUP_MONTHLY_KEEP": "13",
            "HISTORY_BACKUP_DAILY_KEEP": "9",
            "HISTORY_BACKUP_WEEKLY_KEEP": "6",
            "HISTORY_BACKUP_MONTHLY_KEEP": "14",
        }
        old_values = {key: os.environ.get(key) for key in updates}
        try:
            os.environ.update(updates)
            reloaded = importlib.reload(config)
            self.assertEqual(reloaded.RISK_MODE, "observe")
            self.assertEqual(reloaded.MAX_SINGLE_POSITION_PCT, 31)
            self.assertEqual(reloaded.MAX_TOTAL_POSITION_PCT, 96)
            self.assertEqual(reloaded.MIN_CASH_RESERVE_PCT, 6)
            self.assertEqual(reloaded.MAX_SECTOR_EXPOSURE_PCT, 61)
            self.assertEqual(reloaded.MAX_NEW_POSITIONS_PER_DAY, 11)
            self.assertEqual(reloaded.MAX_ORDERS_PER_DAY, 51)
            self.assertEqual(reloaded.MAX_DAILY_TURNOVER_PCT, 201)
            self.assertEqual(reloaded.DAILY_LOSS_WARN_PCT, 6)
            self.assertEqual(reloaded.ACCOUNT_DRAWDOWN_WARN_PCT, 16)
            self.assertEqual(reloaded.MAX_CONSECUTIVE_ORDER_FAILURES, 6)
            self.assertEqual(reloaded.ACCOUNT_SNAPSHOT_MAX_AGE_SEC, 301)
            self.assertEqual(reloaded.SIGNAL_MAX_AGE_SEC, 1201)
            self.assertEqual(reloaded.JOINQUANT_EXECUTION_INTENT_TTL_SEC_DEFAULT, 180)
            self.assertEqual(reloaded.RECONCILIATION_POSITION_TOLERANCE, 1.5)
            self.assertEqual(reloaded.TRADING_DB_FILE, Path("custom/trading.db"))
            self.assertEqual(reloaded.TRADING_BACKUP_DIR, Path("custom/backups"))
            self.assertEqual(reloaded.TRADING_BACKUP_DAILY_KEEP, 8)
            self.assertEqual(reloaded.TRADING_BACKUP_WEEKLY_KEEP, 5)
            self.assertEqual(reloaded.TRADING_BACKUP_MONTHLY_KEEP, 13)
            self.assertEqual(reloaded.ML_DB_FILE, Path("custom/ml.db"))
            self.assertEqual(reloaded.ML_MODEL_DIR, Path("custom/models"))
            self.assertEqual(reloaded.ML_DB_MAX_BYTES, 123456)
            self.assertEqual(reloaded.ML_HISTORY_DB_FILE, Path("custom/history.db"))
            self.assertEqual(reloaded.ML_HISTORY_DB_MAX_BYTES, 234567)
            self.assertTrue(reloaded.ML_TRAINED_SHADOW_ENABLE)
            self.assertEqual(reloaded.ML_PERMISSION_LEVEL_MAX, 2)
            self.assertEqual(reloaded.ML_INFERENCE_TIMEOUT_SEC, 0.75)
            self.assertEqual(reloaded.ML_HISTORY_DATASET_ID, "strict-2025")
            self.assertEqual(reloaded.ML_LABEL_SOURCE, "strict")
            self.assertEqual(reloaded.ML_LABEL_VERSION, "labels-custom")
            self.assertEqual(reloaded.ML_COST_SHA256, "cost-sha")
            self.assertEqual(reloaded.ML_POLICY_SHA256, "policy-sha")
            self.assertEqual(reloaded.ML_TRAINING_START_DATE, "2025-01-01")
            self.assertEqual(reloaded.ML_TRAINING_END_DATE, "2025-12-31")
            self.assertEqual(
                reloaded.ML_FEATURE_ALLOWLIST_TEXT,
                "price,turnover,market_regime",
            )
            self.assertEqual(reloaded.ML_LABEL_LOOKBACK_DAYS, 60)
            self.assertEqual(reloaded.ML_MAINTENANCE_MAX_ROWS, 600000)
            self.assertEqual(reloaded.ML_BACKUP_DIR, Path("custom/ml-backups"))
            self.assertEqual(
                reloaded.HISTORY_BACKUP_DIR, Path("custom/history-backups")
            )
            self.assertEqual(reloaded.ML_BACKUP_DAILY_KEEP, 8)
            self.assertEqual(reloaded.ML_BACKUP_WEEKLY_KEEP, 5)
            self.assertEqual(reloaded.ML_BACKUP_MONTHLY_KEEP, 13)
            self.assertEqual(reloaded.HISTORY_BACKUP_DAILY_KEEP, 9)
            self.assertEqual(reloaded.HISTORY_BACKUP_WEEKLY_KEEP, 6)
            self.assertEqual(reloaded.HISTORY_BACKUP_MONTHLY_KEEP, 14)
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(config)

    def test_linux_env_file_values_override_defaults(self) -> None:
        updates = {
            "WECOM_WEBHOOK_URL": "https://example.invalid/webhook",
            "NOTIFY_ENABLE": "0",
            "NOTIFY_ONLY_SIGNAL": "1",
            "NOTIFY_TOP_N": "3",
            "NOTIFY_COOLDOWN_SEC": "60",
            "NOTIFY_MIN_SCORE": "88.5",
            "NOTIFY_NON_TRADING_DAY": "1",
            "A_SHARE_HOLIDAYS": "2026-10-01,2026-10-02",
            "SCAN_MODE": "auto",
            "SCAN_TOP": "6",
            "SCAN_INTERVAL": "120",
            "SCAN_JITTER_SEC": "9",
            "MIN_PRICE": "2.5",
            "MIN_AMOUNT": "60000000",
            "SKIP_PRESSURE": "1",
            "SKIP_LHB": "1",
            "SKIP_NEWS": "1",
            "STOCK_NEWS_LIMIT": "2",
            "NOTICE_DAYS_BACK": "4",
            "MAX_CANDIDATES_FOR_NEWS": "5",
            "ENABLE_AI": "1",
            "PORTFOLIO_WEB_HOST": "127.0.0.1",
            "PORTFOLIO_WEB_PORT": "8010",
            "JOINQUANT_ENABLE": "1",
            "JOINQUANT_SYNC_TOKEN": "secret",
            "JOINQUANT_DRY_RUN": "0",
            "JOINQUANT_MIN_SCORE": "81.5",
            "JOINQUANT_MAX_SIGNAL_AGE_MIN": "15",
            "JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN": "25",
            "JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN": "12",
            "JOINQUANT_HEALTH_FAILED_ORDER_LIMIT": "2",
            "JOINQUANT_ENFORCE_HEALTH_GATE": "1",
            "JOINQUANT_PORTFOLIO_RISK_ENABLE": "0",
            "JOINQUANT_TRADABILITY_FILTER_ENABLE": "0",
            "JOINQUANT_REGIME_CONFIRM_ENABLE": "0",
            "JOINQUANT_EXIT_COOLDOWN_ENABLE": "0",
            "JOINQUANT_LAYERED_EXIT_ENABLE": "0",
        }
        old_values = {key: os.environ.get(key) for key in updates}
        try:
            os.environ.update(updates)
            reloaded = importlib.reload(config)

            self.assertEqual(reloaded.WECOM_WEBHOOK_URL, updates["WECOM_WEBHOOK_URL"])
            self.assertFalse(reloaded.NOTIFY_ENABLE_DEFAULT)
            self.assertTrue(reloaded.NOTIFY_ONLY_SIGNAL_DEFAULT)
            self.assertEqual(reloaded.NOTIFY_TOP_N_DEFAULT, 3)
            self.assertEqual(reloaded.NOTIFY_COOLDOWN_SEC_DEFAULT, 60)
            self.assertEqual(reloaded.NOTIFY_MIN_SCORE_DEFAULT, 88.5)
            self.assertTrue(reloaded.NOTIFY_NON_TRADING_DAY_DEFAULT)
            self.assertEqual(reloaded.A_SHARE_HOLIDAYS_DEFAULT, {"2026-10-01", "2026-10-02"})
            self.assertEqual(reloaded.SCAN_MODE_DEFAULT, "auto")
            self.assertEqual(reloaded.SCAN_TOP_DEFAULT, 6)
            self.assertEqual(reloaded.SCAN_INTERVAL_DEFAULT, 120)
            self.assertEqual(reloaded.SCAN_JITTER_DEFAULT, 9)
            self.assertEqual(reloaded.MIN_PRICE_DEFAULT, 2.5)
            self.assertEqual(reloaded.MIN_AMOUNT_DEFAULT, 60_000_000)
            self.assertTrue(reloaded.SKIP_PRESSURE_DEFAULT)
            self.assertTrue(reloaded.SKIP_LHB_DEFAULT)
            self.assertTrue(reloaded.SKIP_NEWS_DEFAULT)
            self.assertEqual(reloaded.STOCK_NEWS_LIMIT_DEFAULT, 2)
            self.assertEqual(reloaded.NOTICE_DAYS_BACK_DEFAULT, 4)
            self.assertEqual(reloaded.MAX_CANDIDATES_FOR_NEWS_DEFAULT, 5)
            self.assertTrue(reloaded.ENABLE_AI_DEFAULT)
            self.assertEqual(reloaded.PORTFOLIO_WEB_HOST_DEFAULT, "127.0.0.1")
            self.assertEqual(reloaded.PORTFOLIO_WEB_PORT_DEFAULT, 8010)
            self.assertTrue(reloaded.JOINQUANT_ENABLE_DEFAULT)
            self.assertEqual(reloaded.JOINQUANT_SYNC_TOKEN, "secret")
            self.assertFalse(reloaded.JOINQUANT_DRY_RUN_DEFAULT)
            self.assertEqual(reloaded.JOINQUANT_MIN_SCORE_DEFAULT, 81.5)
            self.assertEqual(reloaded.JOINQUANT_MAX_SIGNAL_AGE_MIN_DEFAULT, 15)
            self.assertEqual(reloaded.JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN_DEFAULT, 25)
            self.assertEqual(reloaded.JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN_DEFAULT, 12)
            self.assertEqual(reloaded.JOINQUANT_HEALTH_FAILED_ORDER_LIMIT_DEFAULT, 2)
            self.assertTrue(reloaded.JOINQUANT_ENFORCE_HEALTH_GATE_DEFAULT)
            self.assertFalse(reloaded.JOINQUANT_PORTFOLIO_RISK_ENABLE_DEFAULT)
            self.assertFalse(reloaded.JOINQUANT_TRADABILITY_FILTER_ENABLE_DEFAULT)
            self.assertFalse(reloaded.JOINQUANT_REGIME_CONFIRM_ENABLE_DEFAULT)
            self.assertFalse(reloaded.JOINQUANT_EXIT_COOLDOWN_ENABLE_DEFAULT)
            self.assertFalse(reloaded.JOINQUANT_LAYERED_EXIT_ENABLE_DEFAULT)
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
