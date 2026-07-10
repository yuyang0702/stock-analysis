import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import a_share_strategy as strat


class FakeNotifier:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send_markdown(self, title: str, content: str, dedupe_key: str | None = None) -> bool:
        self.sent.append((title, content, dedupe_key))
        return True


class SignalWatchlistTest(unittest.TestCase):
    def test_notification_title_includes_runtime_phase(self) -> None:
        self.assertEqual(strat.notification_title("after", "盘后复盘"), "【盘后】盘后复盘")
        self.assertEqual(strat.notification_title("intraday", "买点提醒 600000"), "【盘中】买点提醒 600000")
        self.assertEqual(strat.notification_title("pre", "扫描汇总"), "【盘前】扫描汇总")

    def test_record_signal_watchlist_persists_mobile_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signal_watchlist.json"
            row = pd.Series(
                {
                    "code": "600000",
                    "name": "示例股",
                    "entry_price": 10.2,
                    "stop_loss": 9.7,
                    "take_profit": 11.4,
                    "position_pct": 8.0,
                    "final_score": 88.0,
                    "market_state": "强势进攻",
                    "risk_reason": "突破型，新闻催化",
                    "theme_label": "AI算力",
                    "buy_state": "已到买点",
                    "signal_anchor_id": "600000:short:2026-07-06",
                }
            )

            strat.record_signal_watchlist(
                path,
                row,
                kind="强势",
                mode="intraday",
                pushed_at=datetime(2026, 7, 6, 10, 30),
            )

            data = strat.load_signal_watchlist(path)
            self.assertEqual(len(data["items"]), 1)
            item = data["items"][0]
            self.assertEqual(item["code"], "600000")
            self.assertEqual(item["kind"], "强势")
            self.assertEqual(item["entry_price"], 10.2)
            self.assertEqual(item["stop_loss"], 9.7)
            self.assertEqual(item["take_profit"], 11.4)
            self.assertEqual(item["pushed_price"], 10.2)
            self.assertEqual(item["final_score"], 88.0)
            self.assertEqual(item["market_state"], "强势进攻")
            self.assertEqual(item["signal_id"], "600000:short:2026-07-06")

    def test_after_review_message_tracks_previous_pushed_stock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signal_watchlist.json"
            strat.save_signal_watchlist(
                path,
                {
                    "items": [
                        {
                            "code": "600000",
                            "name": "示例股",
                            "kind": "强势",
                            "mode": "intraday",
                            "pushed_at": "2026-07-06 10:30:00",
                            "entry_price": 10.0,
                            "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 8.0,
                        "final_score": 86,
                        "theme_heat_level": "高",
                        "signal_id": "600000:short:2026-07-06",
                    }
                ]
                },
            )
            result = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "示例股",
                        "price": 10.8,
                        "high": 11.2,
                        "low": 9.9,
                        "pct_chg": 6.2,
                        "buy_state": "持仓观察",
                        "signal_state": "fresh",
                        "signal_action": "continue",
                        "risk_reason": "继续观察",
                        "theme_label": "AI算力",
                    }
                ]
            )

            message = strat.build_watchlist_review_markdown(result, path, max_rows=3, now=datetime(2026, 7, 8, 15, 30))

            self.assertIn("推送跟踪复盘", message)
            self.assertIn("今日跟踪1只", message)
            self.assertIn("已入场1", message)
            self.assertIn("止盈1", message)
            self.assertIn("最大浮盈+12.00%", message)
            self.assertIn("600000 示例股", message)
            self.assertIn("D+2", message)
            self.assertIn("入10.00 | 高11.20 | 低9.90 | 收10.80", message)
            self.assertIn("触及止盈", message)
            self.assertIn("策略质量", message)
            self.assertIn("强势", message)

            data = strat.load_signal_watchlist(path)
            item = data["items"][0]
            self.assertEqual(item["review_day"], "D+2")
            self.assertEqual(item["review_history"][0]["return_pct"], 8.0)

    def test_dispatch_after_titles_and_records_highlight(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            old_path = strat.SIGNAL_WATCHLIST_FILE
            strat.SIGNAL_WATCHLIST_FILE = Path(tmpdir) / "signal_watchlist.json"
            try:
                result = pd.DataFrame(
                    [
                        {
                            "code": "600000",
                            "name": "示例股",
                            "price": 10.8,
                            "pct_chg": 6.2,
                            "amount": 120_000_000,
                            "news_score": 1,
                            "lhb_tag": "未上榜",
                            "limit_quality": "封板较强",
                            "final_score": 90,
                            "mode": "short",
                            "entry_price": 10.0,
                            "stop_loss": 9.5,
                            "take_profit": 11.0,
                            "position_pct": 8.0,
                            "risk_reason": "突破型",
                            "buy_state": "已到买点",
                            "signal_state": "fresh",
                            "signal_action": "continue",
                            "signal_anchor_id": "600000:short:2026-07-06",
                            "theme_label": "AI算力",
                            "theme_heat_level": "高",
                        }
                    ]
                )
                notifier = FakeNotifier()

                with patch("a_share_strategy.is_a_share_trading_day", return_value=True):
                    strat.dispatch_notifications(
                        strat.Config(mode="after", notify_top=3, notify_min_score=75),
                        notifier,
                        result,
                        {"state": "强势进攻", "sh_pct": 1.2},
                        "题材催化偏强",
                    )

                self.assertTrue(all(title.startswith("【盘后】") for title, _, _ in notifier.sent))
                data = strat.load_signal_watchlist(strat.SIGNAL_WATCHLIST_FILE)
                self.assertEqual(data["items"][0]["code"], "600000")
                self.assertEqual(data["items"][0]["kind"], "强势")
            finally:
                strat.SIGNAL_WATCHLIST_FILE = old_path

    def test_dispatch_notifications_silent_on_non_trading_day_by_default(self) -> None:
        notifier = FakeNotifier()
        result = pd.DataFrame(
            [
                {
                    "code": "600000",
                    "name": "示例股",
                    "price": 10.8,
                    "pct_chg": 6.2,
                    "amount": 120_000_000,
                    "news_score": 1,
                    "lhb_tag": "未上榜",
                    "limit_quality": "封板较强",
                    "final_score": 90,
                    "mode": "short",
                    "entry_price": 10.0,
                    "stop_loss": 9.5,
                    "take_profit": 11.0,
                    "position_pct": 8.0,
                    "risk_reason": "突破型",
                    "buy_state": "已到买点",
                    "signal_state": "fresh",
                    "signal_action": "continue",
                }
            ]
        )

        with patch("a_share_strategy.is_a_share_trading_day", return_value=False):
            strat.dispatch_notifications(
                strat.Config(mode="intraday"),
                notifier,
                result,
                {"state": "强势进攻", "sh_pct": 1.2},
                "题材催化偏强",
                watch_result=result,
            )

        self.assertEqual(notifier.sent, [])

    def test_dispatch_notifications_can_be_enabled_on_non_trading_day_for_debug(self) -> None:
        notifier = FakeNotifier()
        result = pd.DataFrame(
            [
                {
                    "code": "600000",
                    "name": "示例股",
                    "price": 10.8,
                    "pct_chg": 6.2,
                    "amount": 120_000_000,
                    "news_score": 1,
                    "lhb_tag": "未上榜",
                    "limit_quality": "封板较强",
                    "final_score": 90,
                    "mode": "short",
                    "entry_price": 10.0,
                    "stop_loss": 9.5,
                    "take_profit": 11.0,
                    "position_pct": 8.0,
                    "risk_reason": "突破型",
                    "buy_state": "已到买点",
                    "signal_state": "fresh",
                    "signal_action": "continue",
                }
            ]
        )

        with patch("a_share_strategy.is_a_share_trading_day", return_value=False):
            strat.dispatch_notifications(
                strat.Config(mode="intraday", notify_non_trading_day=True),
                notifier,
                result,
                {"state": "强势进攻", "sh_pct": 1.2},
                "题材催化偏强",
                watch_result=result,
            )

        self.assertTrue(notifier.sent)


if __name__ == "__main__":
    unittest.main()
