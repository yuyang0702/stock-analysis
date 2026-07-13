import tempfile
import unittest
from pathlib import Path

from joinquant_sync import ingest_snapshot_payload
from reconciliation import ReconciliationDifference, ReconciliationResult
from trading_control import (
    StaleControlStateError, apply_reconciliation_control, change_control,
    unlock_eligibility,
)
from trading_store import TradingStore


class TradingControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TradingStore(Path(self.tmp.name) / "trading.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def result(severity: str) -> ReconciliationResult:
        difference = ReconciliationDifference(
            "account", "account", "ACCOUNT_BALANCE_MISMATCH", "1", "2", 0.01,
            severity, {},
        )
        return ReconciliationResult("r-1", "mismatch", severity, [difference], "", None)

    def test_error_stops_buy_and_critical_adds_kill_switch_without_auto_resume(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            actions = apply_reconciliation_control(self.store, conn, self.result("ERROR"))
        self.assertEqual(actions, ["stop_buy"])
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")

        critical = self.result("CRITICAL")
        critical.reconciliation_id = "r-2"
        with self.store.transaction() as conn:
            actions = apply_reconciliation_control(self.store, conn, critical)
        self.assertEqual(actions, ["kill_switch_on"])
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")
        self.assertEqual(self.store.get_system_state("kill_switch"), "1")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM control_events").fetchone()[0], 2)

    def test_unlock_requires_two_distinct_recent_matched_full_reconciliations(self) -> None:
        payload = {
            "schema_version": 1, "trade_date": "2026-07-14", "generated_at": "2026-07-14 10:00:00",
            "cash": 100000, "available_cash": 100000, "total_value": 100000,
            "positions": [], "orders": [], "trades": [],
        }
        first = ingest_snapshot_payload(payload, self.store, "2026-07-14 10:00:01")["snapshot_id"]
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO reconciliation_runs VALUES
                   ('r-1','full',?,'2026-07-14 10:00:02','2026-07-14 10:00:03','matched','INFO',0,'','{}')""",
                (first,),
            )
        ok, reasons = unlock_eligibility(self.store, now="2026-07-14 10:05:00")
        self.assertFalse(ok)
        self.assertIn("TWO_DISTINCT_FULL_RECONCILIATIONS_REQUIRED", reasons)

        second_payload = dict(payload, generated_at="2026-07-14 10:04:00")
        second = ingest_snapshot_payload(second_payload, self.store, "2026-07-14 10:04:01")["snapshot_id"]
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO reconciliation_runs VALUES
                   ('r-2','full',?,'2026-07-14 10:04:02','2026-07-14 10:04:03','matched','INFO',0,'','{}')""",
                (second,),
            )
        self.assertEqual(unlock_eligibility(self.store, now="2026-07-14 10:05:00"), (True, []))

    def test_manual_changes_require_reason_and_reject_stale_expected_state(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
        with self.assertRaises(ValueError):
            change_control(self.store, "buy_enabled", "0", reason="", operator="tester")
        with self.assertRaises(StaleControlStateError):
            change_control(
                self.store, "buy_enabled", "0", reason="manual stop", operator="tester",
                expected_value="0",
            )
        self.assertEqual(self.store.get_system_state("buy_enabled"), "1")
        self.assertTrue(change_control(
            self.store, "buy_enabled", "0", reason="manual stop", operator="tester",
            expected_value="1",
        ))
        with self.store.connect() as conn:
            event = conn.execute("SELECT * FROM control_events ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertEqual(event["operator"], "tester")
        self.assertEqual(event["reason"], "manual stop")

    def test_kill_switch_off_does_not_resume_buy(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "0", "stopped")
            self.store.set_system_state(conn, "kill_switch", "1", "critical")
        self.assertTrue(change_control(
            self.store, "kill_switch", "0", reason="manual review", operator="tester",
            expected_value="1",
        ))
        self.assertEqual(self.store.get_system_state("kill_switch"), "0")
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")


if __name__ == "__main__":
    unittest.main()
