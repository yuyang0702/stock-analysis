import importlib
import os
import unittest
from pathlib import Path

import config


class ConfigEnvTest(unittest.TestCase):
    def test_observation_risk_defaults(self) -> None:
        self.assertEqual(config.RISK_MODE, "observe")

    def test_observation_risk_environment_values(self) -> None:
        updates = {
            "RISK_MODE": "OBSERVE", "MAX_SINGLE_POSITION_PCT": "31",
            "MAX_TOTAL_POSITION_PCT": "96", "MIN_CASH_RESERVE_PCT": "6",
            "MAX_SECTOR_EXPOSURE_PCT": "61", "MAX_NEW_POSITIONS_PER_DAY": "11",
            "MAX_ORDERS_PER_DAY": "51", "MAX_DAILY_TURNOVER_PCT": "201",
            "DAILY_LOSS_WARN_PCT": "6", "ACCOUNT_DRAWDOWN_WARN_PCT": "16",
            "MAX_CONSECUTIVE_ORDER_FAILURES": "6", "ACCOUNT_SNAPSHOT_MAX_AGE_SEC": "301",
            "SIGNAL_MAX_AGE_SEC": "1201", "RECONCILIATION_POSITION_TOLERANCE": "1.5",
            "TRADING_DB_FILE": "custom/trading.db",
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
            self.assertEqual(reloaded.RECONCILIATION_POSITION_TOLERANCE, 1.5)
            self.assertEqual(reloaded.TRADING_DB_FILE, Path("custom/trading.db"))
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
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
