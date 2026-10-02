import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import a_share_strategy as strat
from notification_outbox import NotificationEvent, notification_event_key
from trading_store import TradingStore


class FakeNotifier:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send_markdown(self, title: str, content: str, dedupe_key: str | None = None) -> bool:
        self.sent.append((title, content, dedupe_key))
        return True


class SignalWatchlistTest(unittest.TestCase):
    @staticmethod
    def _store_with_sent_buy_plans(tmpdir: str, path: Path) -> TradingStore:
        store = TradingStore(Path(tmpdir) / "trading.db")
        store.initialize()
        payload = strat.load_signal_watchlist(path)
        with store.transaction() as conn:
            scope = store.get_or_create_account_scope(conn, "joinquant", "primary")
            for index, item in enumerate(payload["items"]):
                if item.get("kind") != "买点":
                    continue
                trade_date = str(item.get("pushed_at") or "")[:10]
                logical_id = f"logical-{index}"
                version = f"version-{index}"
                event_key = notification_event_key(
                    "joinquant",
                    scope,
                    "buy-plan",
                    trade_date=trade_date,
                    logical_signal_id=logical_id,
                    plan_version=version,
                )
                occurred_at = (
                    datetime.fromisoformat(str(item["pushed_at"]))
                    .replace(tzinfo=timezone(timedelta(hours=8)))
                    .isoformat()
                )
                store.enqueue_notification(
                    conn,
                    NotificationEvent(
                        event_key=event_key,
                        account_scope_id=scope,
                        adapter="joinquant",
                        event_type="buy-plan",
                        object_type="logical_signal_plan",
                        object_id=logical_id,
                        source_fact_id=f"{logical_id}:{version}",
                        priority="normal",
                        payload_version=1,
                        occurred_at=occurred_at,
                        expires_at=None,
                        title="买入计划",
                        body=f"计划 {index}",
                        payload={
                            "trade_date": trade_date,
                            "logical_signal_id": logical_id,
                            "plan_version": version,
                            "code": str(item["code"]),
                            "side": "buy",
                        },
                        metadata={"renderer": "buy-plan-v1"},
                    ),
                    occurred_at,
                )
                conn.execute(
                    """UPDATE notification_outbox
                       SET state='sent', sent_at=?, terminal_at=?
                       WHERE event_key=?""",
                    (occurred_at, occurred_at, event_key),
                )
                item.update({
                    "buy_plan_event_key": event_key,
                    "account_scope_id": scope,
                    "trade_date": trade_date,
                    "logical_signal_id": logical_id,
                    "plan_version": version,
                })
        strat.save_signal_watchlist(path, payload)
        return store

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
                pushed_at=datetime.now().replace(microsecond=0),
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
            store = self._store_with_sent_buy_plans(tmpdir, path)
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
                store=store,
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
            store = self._store_with_sent_buy_plans(tmpdir, path)
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
                quotes, path, chunk_size=3, now=datetime(2026, 7, 14, 15, 30),
                store=store,
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
            store = self._store_with_sent_buy_plans(tmpdir, path)

            messages = strat.build_watchlist_review_messages(
                pd.DataFrame(columns=["code", "price", "high", "low"]),
                path,
                now=datetime(2026, 7, 14, 15, 30),
                store=store,
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
            store = self._store_with_sent_buy_plans(tmpdir, path)

            messages = strat.build_watchlist_review_messages(
                pd.DataFrame([{"code": "600000", "name": "昨日买点", "price": None}]),
                path,
                now=datetime(2026, 7, 14, 15, 30),
                store=store,
            )

            self.assertEqual(len(messages), 1)
            self.assertIn("行情缺失", messages[0][1])

    def test_dispatch_after_enqueues_one_close_event_with_review_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            notifier = FakeNotifier()
            store = TradingStore(Path(tmpdir) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(conn, "joinquant", "primary")
            result = pd.DataFrame([{"code": "600000", "final_score": 80}])
            quotes = pd.DataFrame([{"code": "600000", "price": 10.0}])
            chunks = [("d1:1", "第一组"), ("d1:2", "第二组")]

            with patch("a_share_strategy.is_a_share_trading_day", return_value=True):
                with patch("a_share_strategy.build_summary_markdown", return_value="盘后摘要"):
                    with patch("a_share_strategy.build_watchlist_review_messages", return_value=chunks) as build:
                        strat.dispatch_notifications(
                            strat.Config(mode="after", notify_top=6),
                            notifier,
                            result,
                            {"state": "震荡"},
                            "中性",
                            review_quotes=quotes,
                            store=store,
                            now=datetime(2026, 7, 30, 15, 10),
                        )

            self.assertIs(build.call_args.args[0], quotes)
            self.assertEqual(notifier.sent, [])
            close = store.get_notification(f"joinquant:{scope}:close:2026-07-30")
            self.assertIsNotNone(close)
            self.assertIn("第一组", close.body)
            self.assertIn("第二组", close.body)

    def test_dispatch_after_does_not_record_unsent_highlight(self) -> None:
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
                store = TradingStore(Path(tmpdir) / "trading.db")
                store.initialize()
                with store.transaction() as conn:
                    scope = store.get_or_create_account_scope(conn, "joinquant", "primary")

                with patch("a_share_strategy.is_a_share_trading_day", return_value=True):
                    strat.dispatch_notifications(
                        strat.Config(mode="after", notify_top=3, notify_min_score=75),
                        notifier,
                        result,
                        {"state": "强势进攻", "sh_pct": 1.2},
                        "题材催化偏强",
                        store=store,
                        now=datetime(2026, 7, 30, 15, 10),
                    )

                self.assertEqual(notifier.sent, [])
                data = strat.load_signal_watchlist(strat.SIGNAL_WATCHLIST_FILE)
                self.assertEqual(data["items"], [])
                close = store.get_notification(f"joinquant:{scope}:close:2026-07-30")
                self.assertTrue(close.title.startswith("【盘后】"))
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

    def test_dispatch_notifications_stays_silent_on_non_trading_day_debug_flag(self) -> None:
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

        self.assertEqual(notifier.sent, [])

    def test_review_eligibility_requires_successful_buy_plan_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signal_watchlist.json"
            store = TradingStore(Path(tmpdir) / "trading.db")
            store.initialize()
            sent_at = "2026-07-13T10:05:00+08:00"
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(conn, "joinquant", "primary")
                event_key = notification_event_key(
                    "joinquant", scope, "buy-plan",
                    trade_date="2026-07-11",
                    logical_signal_id="logical-retry",
                    plan_version="version-1",
                )
                store.enqueue_notification(
                    conn,
                    NotificationEvent(
                        event_key=event_key,
                        account_scope_id=scope,
                        adapter="joinquant",
                        event_type="buy-plan",
                        object_type="logical_signal_plan",
                        object_id="logical-retry",
                        source_fact_id="logical-retry:version-1",
                        priority="normal",
                        payload_version=1,
                        occurred_at="2026-07-11T10:00:00+08:00",
                        expires_at=None,
                        title="买入计划",
                        body="等待重试",
                        payload={
                            "trade_date": "2026-07-11",
                            "logical_signal_id": "logical-retry",
                            "plan_version": "version-1",
                            "code": "600000",
                            "side": "buy",
                        },
                        metadata={"renderer": "buy-plan-v1"},
                    ),
                    "2026-07-11T10:00:00+08:00",
                )
            strat.save_signal_watchlist(path, {"items": [{
                "code": "600000", "name": "重试样本", "kind": "买点",
                "buy_plan_event_key": event_key, "entry_price": 10.0,
                "stop_loss": 9.5, "take_profit": 11.0, "active": True,
                "account_scope_id": scope, "trade_date": "2026-07-11",
                "logical_signal_id": "logical-retry", "plan_version": "version-1",
            }]})
            quotes = pd.DataFrame([{
                "code": "600000", "price": 10.5, "high": 10.6, "low": 9.9,
            }])

            for state in ("pending", "leased", "dead", "cancelled"):
                with store.transaction() as conn:
                    conn.execute(
                        "UPDATE notification_outbox SET state=?, sent_at=? WHERE event_key=?",
                        (state, sent_at, event_key),
                    )
                self.assertEqual(
                    strat.build_watchlist_review_messages(
                        quotes, path, now=datetime(2026, 7, 14, 15, 30), store=store,
                    ),
                    [],
                )

            with store.transaction() as conn:
                conn.execute(
                    """UPDATE notification_outbox
                       SET state='sent', sent_at=?, terminal_at=?, attempt_count=2
                       WHERE event_key=?""",
                    (sent_at, sent_at, event_key),
                )
            messages = strat.build_watchlist_review_messages(
                quotes, path, now=datetime(2026, 7, 14, 15, 30), store=store,
            )

            self.assertEqual(len(messages), 1)
            self.assertIn(":d1:", messages[0][0])
            item = strat.load_signal_watchlist(path)["items"][0]
            self.assertEqual(item["pushed_at"], "2026-07-13 10:05:00")
            self.assertEqual(item["buy_plan_sent_at"], "2026-07-13 10:05:00")

    def test_review_delivery_binding_fails_closed_for_forged_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "watchlist.json"
            strat.save_signal_watchlist(path, {"items": [{
                "code": "600000", "name": "绑定样本", "kind": "买点",
                "pushed_at": "2026-07-13 10:05:00", "active": True,
            }]})
            store = self._store_with_sent_buy_plans(tmpdir, path)
            item = strat.load_signal_watchlist(path)["items"][0]

            self.assertIsNotNone(strat._sent_buy_plan_watchlist_item(store, item))
            for field, forged in (
                ("account_scope_id", "other-scope"),
                ("logical_signal_id", "other-logical"),
                ("plan_version", "other-version"),
                ("code", "000001"),
            ):
                with self.subTest(field=field):
                    self.assertIsNone(strat._sent_buy_plan_watchlist_item(
                        store, {**item, field: forged},
                    ))

            with store.transaction() as conn:
                conn.execute(
                    "UPDATE notification_outbox SET object_type='other' WHERE event_key=?",
                    (item["buy_plan_event_key"],),
                )
            self.assertIsNone(strat._sent_buy_plan_watchlist_item(store, item))
            with store.transaction() as conn:
                conn.execute(
                    """UPDATE notification_outbox
                       SET object_type='logical_signal_plan', sent_at='2026-07-13T10:05:00'
                       WHERE event_key=?""",
                    (item["buy_plan_event_key"],),
                )
            self.assertIsNone(strat._sent_buy_plan_watchlist_item(store, item))

    def test_review_item_rejects_file_only_delivery_claim(self) -> None:
        with self.assertRaisesRegex(ValueError, "notification_outbox.sent_at"):
            strat.review_watchlist_item(
                {
                    "code": "600000", "kind": "买点",
                    "pushed_at": "2026-07-13 10:00:00",
                },
                None,
                datetime(2026, 7, 14, 15, 30),
                1,
            )

    def test_sync_buy_plan_watchlist_requires_stable_export_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = Path(tmpdir) / "signals.json"
            watchlist_path = Path(tmpdir) / "watchlist.json"
            signal_path.write_text(
                """{"signals":[
                  {"action":"buy","code":"600000","name":"稳定计划",
                   "entry_price":10.1,"stop_loss":9.7,"take_profit":11.2,
                   "buy_plan_event_key":"joinquant:scope:buy-plan:2026-08-04:logical:v1",
                   "account_scope_id":"scope","trade_date":"2026-08-04",
                   "logical_signal_id":"logical","plan_version":"v1"},
                  {"action":"buy","code":"000001","name":"缺身份"},
                  {"action":"sell","code":"300001","name":"卖出"}
                ]}""",
                encoding="utf-8",
            )

            fixed_now = datetime(2026, 8, 4, 15, 0, 0)
            strat.sync_buy_plan_watchlist(
                signal_path,
                watchlist_path,
                mode="intraday",
                now=fixed_now,
            )
            strat.sync_buy_plan_watchlist(
                signal_path,
                watchlist_path,
                mode="intraday",
                now=fixed_now,
            )

            items = strat.load_signal_watchlist(watchlist_path)["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["code"], "600000")
            self.assertEqual(items[0]["kind"], "买点")
            self.assertEqual(items[0]["buy_plan_event_key"], "joinquant:scope:buy-plan:2026-08-04:logical:v1")
            self.assertEqual(items[0]["logical_signal_id"], "logical")
            self.assertEqual(items[0]["plan_version"], "v1")
            self.assertEqual(items[0]["pushed_at"], "")


if __name__ == "__main__":
    unittest.main()
