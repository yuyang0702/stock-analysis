import json
import re
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


if __name__ == "__main__":
    unittest.main()
