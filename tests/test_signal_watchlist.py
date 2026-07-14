import tempfile
import unittest
from datetime import datetime, timedelta
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
    def test_review_offsets_use_a_share_trading_days(self) -> None:
        friday = datetime(2026, 7, 10, 10, 0)
        monday = datetime(2026, 7, 13, 15, 30)
        self.assertEqual(strat.trading_day_age(friday, monday), 1)
        self.assertEqual(
            strat.due_review_offset({"kind": "买点", "pushed_at": "2026-07-10 10:00:00"}, monday),
            1,
        )
        self.assertIsNone(
            strat.due_review_offset({"kind": "风险", "pushed_at": "2026-07-10 10:00:00"}, monday)
        )
        now = datetime(2026, 7, 14, 15, 30)
        expected = {
            "2026-07-14 10:00:00": 0,
            "2026-07-13 10:00:00": 1,
            "2026-07-09 10:00:00": 3,
            "2026-07-07 10:00:00": 5,
            "2026-06-30 10:00:00": 10,
        }
        for pushed_at, offset in expected.items():
            with self.subTest(offset=offset):
                self.assertEqual(
                    strat.due_review_offset({"kind": "买点", "pushed_at": pushed_at}, now),
                    offset,
                )

    def test_review_offsets_honor_configured_a_share_holiday(self) -> None:
        with patch.object(strat.app_config, "A_SHARE_HOLIDAYS_DEFAULT", {"2026-07-13"}):
            self.assertEqual(
                strat.trading_day_age(
                    datetime(2026, 7, 10, 10, 0),
                    datetime(2026, 7, 14, 15, 30),
                ),
                1,
            )

    def test_watchlist_retention_is_twenty_days_and_capped_at_five_hundred(self) -> None:
        now = datetime(2026, 7, 31, 15, 30)
        items = [
            {
                "code": f"{index:06d}",
                "pushed_at": (datetime(2026, 7, 15, 10, 0) + timedelta(seconds=index)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
            for index in range(520)
        ]
        kept = strat.prune_signal_watchlist_items(list(reversed(items)), now=now)
        self.assertEqual(len(kept), 500)
        self.assertEqual(kept[0]["code"], "000020")

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

    def test_d1_review_keeps_existing_performance_and_quality_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signal_watchlist.json"
            strat.save_signal_watchlist(
                path,
                {
                    "items": [
                        {
                            "code": "600000",
                            "name": "示例股",
                            "kind": "买点",
                            "mode": "intraday",
                            "pushed_at": "2026-07-13 10:30:00",
                            "entry_price": 10.0,
                            "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 8.0,
                        "final_score": 86,
                        "theme_heat_level": "高",
                            "signal_id": "600000:short:2026-07-13",
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

            messages = strat.build_watchlist_review_messages(
                result,
                path,
                chunk_size=3,
                now=datetime(2026, 7, 14, 15, 30),
            )
            self.assertEqual(len(messages), 1)
            message = messages[0][1]

            self.assertIn("推送跟踪复盘", message)
            self.assertIn("批次样本：1", message)
            self.assertIn("已入场1", message)
            self.assertIn("止盈1", message)
            self.assertIn("最大浮盈+12.00%", message)
            self.assertIn("600000 示例股", message)
            self.assertIn("D+1", message)
            self.assertIn("入10.00 高11.20 低9.90 收10.80", message)
            self.assertIn("触及止盈", message)
            self.assertIn("策略质量", message)
            self.assertIn("intraday", message)

            data = strat.load_signal_watchlist(path)
            item = data["items"][0]
            self.assertEqual(item["review_day"], "D+1")
            self.assertEqual(item["review_history"][0]["return_pct"], 8.0)

    def test_review_messages_cover_complete_cohorts_and_chunk_without_candidate_bias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signal_watchlist.json"
            prior_buys = [
                {
                    "code": f"60000{index}", "name": f"昨日买点{index}", "kind": "买点",
                    "mode": "intraday", "pushed_at": f"2026-07-13 10:0{index}:00",
                    "entry_price": 10.0, "stop_loss": 9.5, "take_profit": 11.0,
                    "signal_id": f"60000{index}:mid:2026-07-13", "active": True,
                }
                for index in range(7)
            ]
            same_day = {
                "code": "000001", "name": "今日买点", "kind": "买点", "mode": "intraday",
                "pushed_at": "2026-07-14 10:00:00", "entry_price": 10.0,
                "stop_loss": 9.5, "take_profit": 11.0,
                "signal_id": "000001:mid:2026-07-14", "active": True,
            }
            risk = {
                "code": "300001", "name": "风险样本", "kind": "风险", "mode": "after",
                "pushed_at": "2026-07-13 15:00:00", "entry_price": 10.0,
                "signal_id": "300001:mid:2026-07-13", "active": True,
            }
            strat.save_signal_watchlist(path, {"items": prior_buys + [same_day, risk]})
            quotes = pd.DataFrame([
                {
                    "code": f"60000{index}", "name": f"昨日买点{index}", "price": 10.5,
                    "high": 10.8, "low": 9.9, "pct_chg": 2.0,
                    "signal_action": "continue", "signal_state": "fresh",
                }
                for index in range(6)
            ] + [{
                "code": "000001", "name": "今日买点", "price": 10.2,
                "high": 10.3, "low": 9.9, "pct_chg": 1.0,
                "signal_action": "continue", "signal_state": "fresh",
            }])

            messages = strat.build_watchlist_review_messages(
                quotes, path, chunk_size=3, now=datetime(2026, 7, 14, 15, 30)
            )
            combined = "\n".join(markdown for _, markdown in messages)
            self.assertEqual(len(messages), 4)
            self.assertIn("D+1", combined)
            self.assertIn("D+0", combined)
            for code in ("600000", "600001", "600002", "600003", "600004", "600005", "600006"):
                self.assertIn(code, combined)
            self.assertIn("行情缺失", combined)
            self.assertNotIn("风险样本", combined)
            d0_markdown = "\n".join(markdown for suffix, markdown in messages if ":d0:" in suffix)
            self.assertIn("待后续复盘", d0_markdown)
            self.assertNotIn("触及止盈", d0_markdown)

    def test_review_keeps_prior_buy_when_full_quote_frame_has_no_matching_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signal_watchlist.json"
            strat.save_signal_watchlist(path, {"items": [{
                "code": "600000", "name": "昨日买点", "kind": "买点",
                "pushed_at": "2026-07-13 10:00:00", "entry_price": 10.0,
                "stop_loss": 9.5, "take_profit": 11.0,
                "signal_id": "600000:mid:2026-07-13", "active": True,
            }]})

            messages = strat.build_watchlist_review_messages(
                pd.DataFrame(columns=["code", "price", "high", "low"]),
                path,
                now=datetime(2026, 7, 14, 15, 30),
            )

            self.assertEqual(len(messages), 1)
            self.assertIn("600000", messages[0][1])
            self.assertIn("行情缺失", messages[0][1])

    def test_review_treats_present_row_without_price_as_missing_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signal_watchlist.json"
            strat.save_signal_watchlist(path, {"items": [{
                "code": "600000", "name": "昨日买点", "kind": "买点",
                "pushed_at": "2026-07-13 10:00:00", "entry_price": 10.0,
                "signal_id": "600000:mid:2026-07-13", "active": True,
            }]})

            messages = strat.build_watchlist_review_messages(
                pd.DataFrame([{"code": "600000", "name": "昨日买点", "price": None}]),
                path,
                now=datetime(2026, 7, 14, 15, 30),
            )

            self.assertEqual(len(messages), 1)
            self.assertIn("行情缺失", messages[0][1])

    def test_dispatch_after_sends_every_review_chunk_from_full_quotes(self) -> None:
        notifier = FakeNotifier()
        result = pd.DataFrame(columns=["code", "final_score"])
        quotes = pd.DataFrame([{"code": "600000", "price": 10.0}])
        chunks = [("d1:1", "第一组"), ("d1:2", "第二组")]

        with patch("a_share_strategy.is_a_share_trading_day", return_value=True):
            with patch("a_share_strategy.select_signal_rows", return_value=(result, result)):
                with patch("a_share_strategy.build_watchlist_review_messages", return_value=chunks) as build:
                    strat.dispatch_notifications(
                        strat.Config(mode="after", notify_top=6),
                        notifier,
                        result,
                        {"state": "震荡"},
                        "中性",
                        review_quotes=quotes,
                    )

        self.assertIs(build.call_args.args[0], quotes)
        review_keys = [key for _, _, key in notifier.sent if key and key.startswith("watch-review:")]
        self.assertEqual(review_keys, ["watch-review:d1:1", "watch-review:d1:2"])

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
