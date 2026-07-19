import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holdings_web
from trading_store import TradingStore


class HoldingsWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.positions = root / "positions.json"
        self.events = root / "events.jsonl"
        self.db = root / "trading.db"
        self.positions.write_text(json.dumps({"positions": [{
            "code": "600000", "name": "浦发银行", "qty": 100, "closeable_qty": 100,
            "cost_price": 10, "current_price": 9.5, "updated_at": "2026-07-16 10:00:00",
        }]}), encoding="utf-8")
        store = TradingStore(self.db)
        store.initialize()
        with store.transaction() as conn:
            store.reconcile_position_cycles(conn, [{
                "code": "600000", "qty": 100, "cost_price": 10,
                "current_price": 9.5, "stop_price": 9.3,
            }], "2026-07-16 10:00:00")
        self.patches = [
            patch.object(holdings_web, "POSITIONS_FILE", self.positions),
            patch.object(holdings_web, "EVENTS_FILE", self.events),
            patch.object(holdings_web, "TRADING_DB_FILE", self.db),
            patch.object(holdings_web.app_config, "PORTFOLIO_WEB_TOKEN", "secret"),
        ]
        for item in self.patches:
            item.start()
        holdings_web.APP.config.update(TESTING=True)
        self.client = holdings_web.APP.test_client()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def login(self):
        return self.client.post("/login", data={"token": "secret"}, follow_redirects=True)

    def test_dashboard_requires_login_and_removed_ocr_routes_are_absent(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.post("/upload").status_code, 404)
        self.assertEqual(self.client.post("/confirm-ocr").status_code, 404)
        response = self.login()
        self.assertIn("交易运行面板", response.get_data(as_text=True))
        self.assertNotIn("上传截图", response.get_data(as_text=True))

    def test_manual_stop_requires_csrf_and_writes_ledger_audit(self) -> None:
        page = self.login().get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        self.assertEqual(self.client.post("/manual-stop/600000", data={
            "manual_stop_price": "9.6", "reason": "收紧", "csrf_token": "bad",
        }).status_code, 403)
        response = self.client.post("/manual-stop/600000", data={
            "manual_stop_price": "9.6", "reason": "收紧", "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 302)
        store = TradingStore(self.db)
        self.assertEqual(store.get_active_position_cycles()["600000"]["manual_stop_price"], 9.6)

    def test_dashboard_shows_independent_freshness_and_template_confirmation(self) -> None:
        with TradingStore(self.db).transaction() as conn:
            conn.execute(
                """INSERT INTO strategy_runs VALUES
                   ('run-web','2026-07-19','2026-07-19 10:00:00','2026-07-19 10:01:00',
                    'abc123','strategy-v1','params-v1','fresh','success','',
                    '2026-07-19 10:00:00','2026-07-19 10:01:00')"""
            )
            conn.execute(
                """INSERT INTO signals(
                   signal_id,run_id,trade_date,stock_code,jq_code,action,generated_at,
                   raw_json,created_at,validated_at,published_at)
                   VALUES('sig-web','run-web','2026-07-19','600000','600000.XSHG','buy',
                   '2026-07-19 10:00:30','{}','2026-07-19 10:00:30',
                   '2026-07-19 10:00:31','2026-07-19 10:00:32')"""
            )
            conn.execute(
                """INSERT INTO account_snapshots(
                   snapshot_id,trade_date,generated_at,received_at,template_version,state_hash)
                   VALUES('snap-web','2026-07-19','2026-07-19 10:02:00',
                   '2026-07-19 10:02:01','template-from-platform','hash-web')"""
            )
        page = self.login().get_data(as_text=True)
        self.assertIn("数据时效与版本", page)
        self.assertIn(holdings_web.app_config.JOINQUANT_TEMPLATE_VERSION, page)
        self.assertIn("template-from-platform", page)
        self.assertIn("最近信号", page)
        self.assertIn("距今", page)

    def test_dashboard_explains_pending_execution_and_issue_impact(self) -> None:
        with TradingStore(self.db).transaction() as conn:
            conn.execute(
                """INSERT INTO orders(
                   client_order_id,stock_code,action,requested_qty,filled_qty,status,
                   reason,first_submitted_at,updated_at,raw_json)
                   VALUES('order-web','600000','sell',100,40,'partial',
                   '跌停等待成交','2026-07-19 10:10:00','2026-07-19 10:15:00','{}')"""
            )
            conn.execute(
                """INSERT INTO exit_intents(
                   signal_id,stock_code,target_qty,reason,status,remaining_qty,
                   created_at,updated_at,validated_at,published_at)
                   VALUES('exit-web','600000',100,'hard_stop','active',60,
                   '2026-07-19 10:09:00','2026-07-19 10:15:00',
                   '2026-07-19 10:09:01','2026-07-19 10:09:02')"""
            )
            conn.execute(
                """INSERT INTO execution_issue_state(
                   issue_key,object_type,object_id,state,severity,first_seen_at,
                   stage_started_at,last_seen_at,last_transition_at,details_json)
                   VALUES('issue-web','order','order-web','LIMIT_DOWN','ERROR',
                   '2026-07-19 10:10:00','2026-07-19 10:10:00',
                   '2026-07-19 10:15:00','2026-07-19 10:10:00','{}')"""
            )
            conn.execute(
                """INSERT INTO system_state(key,value,updated_at,reason)
                   VALUES('buy_enabled','0','2026-07-19 10:15:00','reconciliation')"""
            )
        page = self.login().get_data(as_text=True)
        self.assertIn("待执行与未完成退出", page)
        self.assertIn("部分成交 40/100", page)
        self.assertIn("跌停等待成交", page)
        self.assertIn("停买不影响合法卖出", page)
        self.assertIn("影响当前证券退出执行", page)

    def test_dashboard_traces_position_risk_and_signal_provenance(self) -> None:
        signal_raw = json.dumps({
            "entry_path": "gap_reentry", "execution_plan_version": "plan-v2",
            "reason": "跳空后两次确认",
        }, ensure_ascii=False)
        with TradingStore(self.db).transaction() as conn:
            conn.execute(
                """INSERT INTO strategy_runs VALUES
                   ('run-risk','2026-07-18','2026-07-18 09:30:00','2026-07-18 09:31:00',
                    'abc123','strategy-v1','params-v1','fresh','success','',
                    '2026-07-18 09:30:00','2026-07-18 09:31:00')"""
            )
            conn.execute(
                """INSERT INTO signals(
                   signal_id,run_id,trade_date,stock_code,jq_code,action,signal_price,
                   stop_loss,final_score,generated_at,raw_json,created_at,
                   validated_at,published_at)
                   VALUES('sig-risk','run-risk','2026-07-18','600000','600000.XSHG',
                   'buy',10,9,88,'2026-07-18 09:30:30',?,
                   '2026-07-18 09:30:30','2026-07-18 09:30:31',
                   '2026-07-18 09:30:32')""", (signal_raw,)
            )
            conn.execute(
                """UPDATE position_cycles SET entry_signal_id='sig-risk',
                   opened_at='2026-07-18 09:35:00',initial_r=1,highest_price=11
                   WHERE stock_code='600000' AND status='active'"""
            )
        page = self.login().get_data(as_text=True)
        self.assertIn("持仓天数", page)
        self.assertIn("1R", page)
        self.assertIn("当前R", page)
        self.assertIn("gap_reentry", page)
        self.assertIn("plan-v2", page)
        self.assertIn("跳空后两次确认", page)
        self.assertIn("信号", page)

    def test_dashboard_shows_strict_capability_states_without_controls(self) -> None:
        page = self.login().get_data(as_text=True)
        self.assertIn("研究与验证", page)
        self.assertIn("planned", page)
        self.assertIn("implemented", page)
        self.assertIn("deployed", page)
        self.assertIn("observed", page)
        self.assertIn("validated", page)
        self.assertNotIn("启用模型", page)
        self.assertNotIn("自动解锁", page)
        self.assertNotIn("直接买入", page)

    def test_dashboard_degrades_without_exposing_database_error(self) -> None:
        self.client.post("/login", data={"token": "secret"})
        with patch.object(
            holdings_web, "_dashboard_data",
            side_effect=sqlite3.OperationalError("secret SQL and C:\\private\\trading.db"),
        ):
            response = self.client.get("/")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("只读数据暂不可用", page)
        self.assertNotIn("secret SQL", page)
        self.assertNotIn("private", page)


if __name__ == "__main__":
    unittest.main()
