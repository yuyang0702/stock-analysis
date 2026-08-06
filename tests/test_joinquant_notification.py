import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import a_share_strategy
import paper_trading
from trading_store import TradingStore


class JoinQuantNotificationTest(unittest.TestCase):
    def test_joinquant_simulated_order_markdown_is_distinct_from_local_paper_account(self) -> None:
        payload = {
            "run_id": "run-1",
            "trade_date": "2026-07-07",
            "dry_run": False,
            "signals": [
                {
                    "code": "600000",
                    "jq_code": "600000.XSHG",
                    "name": "PF Bank",
                    "action": "buy",
                    "position_pct": 12.5,
                    "price": 10.1,
                    "final_score": 88,
                    "enhanced_score": 91.5,
                    "reason": "signal",
                },
                {
                    "code": "000001",
                    "jq_code": "000001.XSHE",
                    "name": "PA Bank",
                    "action": "sell",
                    "price": 12.3,
                    "reason": "stop_loss",
                },
            ],
        }

        md = a_share_strategy.build_joinquant_dry_run_markdown(payload)

        self.assertIn("JoinQuant 模拟盘", md)
        self.assertIn("JoinQuant 模拟盘执行", md)
        self.assertIn("不是本地模拟盘", md)
        self.assertIn("计划买入", md)
        self.assertIn("计划卖出", md)
        self.assertIn("600000.XSHG", md)
        self.assertIn("分数 88", md)
        self.assertNotIn("影子", md)
        self.assertNotIn("91.5", md)

    def test_local_paper_markdown_has_distinct_marker(self) -> None:
        account = paper_trading.new_account(100_000)

        md = paper_trading.build_paper_trade_markdown(account, [])

        self.assertIn("本地模拟盘", md)
        self.assertIn("不是 JoinQuant", md)

    def test_empty_joinquant_plan_shows_reject_diagnostics(self) -> None:
        payload = {
            "run_id": "run-empty",
            "trade_date": "2026-07-07",
            "dry_run": False,
            "signals": [],
            "diagnostics": {
                "candidate_count": 4,
                "allow_buy": False,
                "min_score": 75.0,
                "reject_reasons": {
                    "buy_disabled": 2,
                    "gap_reentry_per_trade_and_portfolio_risk_exceeded": 1,
                    "sell_without_holding": 1,
                },
            },
        }

        md = a_share_strategy.build_joinquant_dry_run_markdown(payload)

        self.assertIn("候选 4 只", md)
        self.assertIn("非交易时间禁止买入 2", md)
        self.assertIn("最小一手同时超过单笔和组合风险 1", md)
        self.assertIn("未持仓不卖出 1", md)

    def test_pre_close_and_weekly_events_have_stable_keys_and_calendar_ttls(self) -> None:
        shanghai = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TradingStore(Path(tmpdir) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(conn, "joinquant", "primary")

            pre_now = datetime(2026, 7, 31, 9, 16, tzinfo=shanghai)
            pre_key = a_share_strategy.enqueue_scan_notification(
                store, "pre", "【盘前】扫描汇总", "第一版盘前摘要", pre_now,
            )
            repeated_key = a_share_strategy.enqueue_scan_notification(
                store, "pre", "【盘前】扫描汇总", "五分钟后的不同报价", pre_now.replace(minute=21),
            )
            close_key = a_share_strategy.enqueue_scan_notification(
                store, "close", "【盘后】盘后复盘", "盘后摘要", pre_now.replace(hour=15, minute=10),
            )
            weekly_key = a_share_strategy.enqueue_scan_notification(
                store, "weekly", "周度复盘", "本周摘要", pre_now.replace(hour=15, minute=11),
            )

            self.assertEqual(pre_key, repeated_key)
            self.assertEqual(pre_key, f"joinquant:{scope}:pre:2026-07-31")
            self.assertEqual(close_key, f"joinquant:{scope}:close:2026-07-31")
            self.assertEqual(weekly_key, f"joinquant:{scope}:weekly:2026-W31")
            pre = store.get_notification(pre_key)
            close = store.get_notification(close_key)
            weekly = store.get_notification(weekly_key)
            self.assertEqual(pre.body, "第一版盘前摘要")
            self.assertEqual(
                datetime.fromisoformat(pre.expires_at).astimezone(shanghai),
                datetime(2026, 7, 31, 9, 30, tzinfo=shanghai),
            )
            self.assertEqual(
                datetime.fromisoformat(close.expires_at).astimezone(shanghai),
                datetime(2026, 8, 3, 9, 15, tzinfo=shanghai),
            )
            self.assertEqual(close.expires_at, weekly.expires_at)

    def test_scan_notification_is_silent_for_empty_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TradingStore(Path(tmpdir) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.get_or_create_account_scope(conn, "joinquant", "primary")

            for event_type in ("pre", "close", "weekly"):
                with self.subTest(event_type=event_type):
                    key = a_share_strategy.enqueue_scan_notification(
                        store,
                        event_type,
                        "空摘要",
                        "   ",
                        datetime(2026, 7, 31, 9, 16, tzinfo=timezone(timedelta(hours=8))),
                    )
                    self.assertIsNone(key)
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
            self.assertEqual(count, 0)

    def test_close_ttl_skips_configured_holiday(self) -> None:
        shanghai = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TradingStore(Path(tmpdir) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.get_or_create_account_scope(conn, "joinquant", "primary")
            with patch.object(
                a_share_strategy.app_config,
                "A_SHARE_HOLIDAYS_DEFAULT",
                {"2026-08-03"},
            ):
                key = a_share_strategy.enqueue_scan_notification(
                    store,
                    "close",
                    "【盘后】盘后复盘",
                    "摘要",
                    datetime(2026, 7, 31, 15, 10, tzinfo=shanghai),
                )

            event = store.get_notification(key)
            self.assertEqual(
                datetime.fromisoformat(event.expires_at).astimezone(shanghai),
                datetime(2026, 8, 4, 9, 15, tzinfo=shanghai),
            )


if __name__ == "__main__":
    unittest.main()
