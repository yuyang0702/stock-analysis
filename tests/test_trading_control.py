import tempfile
import unittest
from pathlib import Path

from joinquant_sync import ingest_snapshot_payload
from reconciliation import ReconciliationDifference, ReconciliationResult
from trading_control import (
    StaleControlStateError, apply_reconciliation_control, change_control,
    unlock_eligibility, auto_resume_eligibility, apply_automatic_buy_recovery,
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

    def test_reconciliation_owned_stop_can_auto_resume_after_two_fresh_matches(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            conn.execute(
                """INSERT INTO reconciliation_runs VALUES
                   ('r-stop','incremental',NULL,'2026-07-15 09:30:00','2026-07-15 09:30:00',
                    'mismatch','ERROR',1,'','{}')"""
            )
            stopped = self.result("ERROR")
            stopped.reconciliation_id = "r-stop"
            apply_reconciliation_control(self.store, conn, stopped)
            for sid, finished in (("snap-1", "2026-07-15 09:31:00"), ("snap-2", "2026-07-15 09:32:00")):
                conn.execute(
                    """INSERT INTO account_snapshots(
                       snapshot_id,trade_date,generated_at,received_at,cash,available_cash,total_value,
                       position_market_value,state_hash,template_version)
                       VALUES (?, '2026-07-15', ?, ?, 1,1,1,0,?,?)""",
                    (sid, finished, finished, sid, "2026-07-15.1-execution-state-recovery"),
                )
                conn.execute(
                    """INSERT INTO reconciliation_runs VALUES
                       (?, 'incremental', ?, ?, ?, 'matched','INFO',0,'','{}')""",
                    ("r-" + sid, sid, finished, finished),
                )
            ok, reasons, _ = auto_resume_eligibility(
                self.store, conn, now="2026-07-15 09:32:30",
                required_template="2026-07-15.1-execution-state-recovery",
            )
            self.assertTrue(ok, reasons)
            recovered = apply_automatic_buy_recovery(
                self.store, conn, stopped, now="2026-07-15 09:32:30",
                required_template="2026-07-15.1-execution-state-recovery",
            )
        self.assertEqual(recovered["action"], "auto_resume_buy")
        self.assertEqual(self.store.get_system_state("buy_enabled"), "1")

    def test_manual_stop_while_disabled_cancels_auto_resume_owner(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            apply_reconciliation_control(self.store, conn, self.result("ERROR"))
        self.assertTrue(self.store.get_system_state("reconciliation_auto_resume_owner"))
        change_control(
            self.store, "buy_enabled", "0", reason="manual hold", operator="tester",
            expected_value="0",
        )
        self.assertEqual(self.store.get_system_state("reconciliation_auto_resume_owner"), "")

    def test_critical_never_creates_or_retains_auto_resume_owner(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            critical = self.result("CRITICAL")
            apply_reconciliation_control(self.store, conn, critical)
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")
        self.assertEqual(self.store.get_system_state("kill_switch"), "1")
        self.assertEqual(self.store.get_system_state("reconciliation_auto_resume_owner"), "")

    def test_manual_kill_switch_action_cancels_auto_resume_owner(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            apply_reconciliation_control(self.store, conn, self.result("ERROR"))
        self.assertTrue(self.store.get_system_state("reconciliation_auto_resume_owner"))
        self.assertTrue(change_control(
            self.store, "kill_switch", "1", reason="manual hold", operator="tester",
            expected_value="0",
        ))
        self.assertEqual(self.store.get_system_state("reconciliation_auto_resume_owner"), "")

    def test_error_owner_contains_originating_control_event_id(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            apply_reconciliation_control(self.store, conn, self.result("ERROR"))
            event = conn.execute(
                "SELECT event_id FROM control_events WHERE action='stop_buy' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        owner = __import__("json").loads(
            self.store.get_system_state("reconciliation_auto_resume_owner")
        )
        self.assertEqual(owner["control_event_id"], event["event_id"])

    def test_manual_resume_acknowledges_sticky_critical_issue(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "0", "critical")
            self.store.upsert_execution_issue(conn, {
                "issue_key": "fill:t-1", "object_type": "fill", "object_id": "t-1",
                "state": "IMMUTABLE_FILL_CONFLICT", "severity": "CRITICAL",
                "stage_started_at": "2026-07-15 09:00:00",
                "seen_at": "2026-07-15 09:00:00", "details": {},
            })
        self.assertTrue(change_control(
            self.store, "buy_enabled", "1", reason="manual ledger review complete",
            operator="tester", expected_value="0",
        ))
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT state, recovered_at FROM execution_issue_state WHERE issue_key='fill:t-1'"
            ).fetchone()
        self.assertEqual(row["state"], "RECOVERED")
        self.assertTrue(row["recovered_at"])


if __name__ == "__main__":
    unittest.main()
