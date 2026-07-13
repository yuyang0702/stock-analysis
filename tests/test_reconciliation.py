import tempfile
import unittest
from pathlib import Path

from joinquant_sync import ingest_snapshot_payload
from reconciliation import (
    ReconciliationDifference, ReconciliationResult, build_reconciliation_markdown,
    notify_reconciliation, reconcile_snapshot,
)
from trading_store import TradingStore


def snapshot(generated_at: str = "2026-07-14 10:00:00") -> dict:
    return {
        "schema_version": 1,
        "trade_date": "2026-07-14",
        "generated_at": generated_at,
        "template_version": "ledger-v6",
        "cash": 99000,
        "available_cash": 99000,
        "total_value": 100000,
        "positions": [{
            "code": "600000", "jq_code": "600000.XSHG", "qty": 100,
            "closeable_amount": 80, "locked_amount": 20, "today_amount": 20,
            "avg_cost": 10, "price": 10, "market_value": 1000, "pnl": 0,
        }],
        "orders": [],
        "trades": [],
    }


class ReconciliationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TradingStore(Path(self.tmp.name) / "trading.db")
        self.payload = snapshot()
        result = ingest_snapshot_payload(self.payload, self.store, "2026-07-14 10:00:02")
        self.snapshot_id = result["snapshot_id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reconcile(self, payload: dict, mode: str = "full"):
        with self.store.transaction() as conn:
            return reconcile_snapshot(
                self.store, conn, payload, snapshot_id=self.snapshot_id,
                mode=mode, now="2026-07-14 10:00:03",
            )

    def test_matched_account_and_tolerance_boundary(self) -> None:
        self.assertEqual(self.reconcile(self.payload).result, "matched")
        within = dict(self.payload, total_value=100000.005)
        self.assertEqual(self.reconcile(within).result, "matched")
        outside = dict(self.payload, total_value=100000.02)
        result = self.reconcile(outside)
        self.assertEqual(result.severity, "ERROR")
        self.assertIn("ACCOUNT_BALANCE_MISMATCH", [item.reason_code for item in result.differences])

    def test_position_sellable_and_frozen_differences_have_stable_codes(self) -> None:
        changed = snapshot()
        changed["positions"][0].update(qty=90, closeable_amount=70, locked_amount=20)
        result = self.reconcile(changed)
        codes = {item.reason_code for item in result.differences}
        self.assertIn("POSITION_QTY_MISMATCH", codes)
        self.assertIn("POSITION_SELLABLE_MISMATCH", codes)
        self.assertEqual(result.severity, "ERROR")

    def test_unknown_order_fill_manual_trade_and_fill_quantity_are_detected(self) -> None:
        changed = snapshot()
        changed["orders"] = [{
            "order_id": "o-1", "code": "600000", "action": "buy", "amount": 100,
            "filled": 90, "avg_price": 10, "status": "partial", "datetime": "2026-07-14 10:00:00",
        }]
        changed["trades"] = [{
            "trade_id": "t-1", "order_id": "o-1", "code": "600000", "action": "buy",
            "amount": 80, "price": 10, "datetime": "2026-07-14 10:00:00",
        }]
        result = self.reconcile(changed)
        codes = {item.reason_code for item in result.differences}
        self.assertIn("ORDER_MISSING_LOCAL", codes)
        self.assertIn("FILL_MISSING_LOCAL", codes)
        self.assertIn("ORDER_FILL_QTY_MISMATCH", codes)
        self.assertIn("MANUAL_TRADE", codes)

    def test_exit_intent_and_conflicting_fill_are_detected(self) -> None:
        with self.store.transaction() as conn:
            self.store.upsert_exit_intent(
                conn, "exit-1", "600000", 0, "hard_stop", "2026-07-14 09:59:00"
            )
            conn.execute(
                """INSERT INTO fills(fill_id, order_id, stock_code, action, qty, price, filled_at, raw_json)
                   VALUES ('t-1','o-1','600000','sell',100,10,'2026-07-14 10:00:00','{}')"""
            )
        changed = snapshot()
        changed["trades"] = [{
            "trade_id": "t-1", "order_id": "o-1", "code": "600000", "action": "sell",
            "amount": 100, "price": 10.1, "datetime": "2026-07-14 10:00:00",
        }]
        result = self.reconcile(changed)
        codes = {item.reason_code for item in result.differences}
        self.assertIn("EXIT_INTENT_MISMATCH", codes)
        self.assertIn("IMMUTABLE_FILL_CONFLICT", codes)
        self.assertEqual(result.severity, "CRITICAL")

    def test_compact_notification_excludes_raw_secrets_and_uses_stable_dedupe(self) -> None:
        difference = ReconciliationDifference(
            "fill", "t-1", "IMMUTABLE_FILL_CONFLICT", "old", "new", 0,
            "CRITICAL", {"token": "must-not-leak", "webhook": "must-not-leak"},
        )
        result = ReconciliationResult(
            "r-alert", "mismatch", "CRITICAL", [difference], "kill_switch_on", "s-1"
        )
        controls = {"buy_enabled": "0", "kill_switch": "1"}
        markdown = build_reconciliation_markdown(result, controls)
        self.assertIn("r-alert", markdown)
        self.assertIn("IMMUTABLE_FILL_CONFLICT", markdown)
        self.assertIn("run_ubuntu.sh unlock", markdown)
        self.assertNotIn("must-not-leak", markdown)

        notifier = unittest.mock.Mock()
        notifier.send_markdown.return_value = True
        self.assertTrue(notify_reconciliation(result, controls, notifier=notifier))
        key = notifier.send_markdown.call_args.kwargs["dedupe_key"]
        self.assertEqual(key, "reconciliation:IMMUTABLE_FILL_CONFLICT:t-1:0/1")


if __name__ == "__main__":
    unittest.main()
