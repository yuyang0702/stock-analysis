import tempfile
import unittest
from pathlib import Path

from joinquant_sync import ingest_snapshot_payload
from reconciliation import ReconciliationDifference, ReconciliationResult
from trading_control import (
    StaleControlStateError, apply_reconciliation_control, change_control,
    unlock_eligibility, auto_resume_eligibility, apply_automatic_buy_recovery,
    control_status, reconcile_notification_capacity,
)
from trading_store import TradingStore


class TradingControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TradingStore(Path(self.tmp.name) / "trading.db")
        self.store.initialize()
        with self.store.transaction() as conn:
            self.scope = self.store.get_or_create_account_scope(
                conn, "joinquant", "primary",
            )

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

    def test_replayed_reconciliation_preserves_recorded_control_action(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO reconciliation_runs(
                   reconciliation_id, mode, started_at, finished_at, result,
                   severity, difference_count, control_action, summary_json
                   ) VALUES('r-1', 'full', '2026-07-14 10:00:00',
                   '2026-07-14 10:00:01', 'mismatch', 'ERROR', 1, '', '{}')"""
            )
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            first = self.result("ERROR")
            self.assertEqual(
                apply_reconciliation_control(self.store, conn, first),
                ["stop_buy"],
            )
            replay = self.result("ERROR")
            self.assertEqual(
                apply_reconciliation_control(self.store, conn, replay),
                [],
            )
            recorded = conn.execute(
                """SELECT control_action FROM reconciliation_runs
                   WHERE reconciliation_id='r-1'"""
            ).fetchone()[0]

        self.assertEqual(first.control_action, "stop_buy")
        self.assertEqual(replay.control_action, "stop_buy")
        self.assertEqual(recorded, "stop_buy")

    def test_unlock_requires_two_distinct_recent_matched_full_reconciliations(self) -> None:
        payload = {
            "schema_version": 1, "trade_date": "2026-07-14", "generated_at": "2026-07-14 10:00:00",
            "cash": 100000, "available_cash": 100000, "total_value": 100000,
            "positions": [], "orders": [], "trades": [],
        }
        first = ingest_snapshot_payload(payload, self.store, "2026-07-14 10:00:01")["snapshot_id"]
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO reconciliation_runs(
                   reconciliation_id, mode, snapshot_id, started_at,
                   finished_at, result, severity, difference_count,
                   control_action, summary_json
                   ) VALUES('r-1','full',?,'2026-07-14 10:00:02',
                   '2026-07-14 10:00:03','matched','INFO',0,'','{}')""",
                (first,),
            )
        ok, reasons = unlock_eligibility(self.store, now="2026-07-14 10:05:00")
        self.assertFalse(ok)
        self.assertIn("TWO_DISTINCT_FULL_RECONCILIATIONS_REQUIRED", reasons)

        second_payload = dict(payload, generated_at="2026-07-14 10:04:00")
        second = ingest_snapshot_payload(second_payload, self.store, "2026-07-14 10:04:01")["snapshot_id"]
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO reconciliation_runs(
                   reconciliation_id, mode, snapshot_id, started_at,
                   finished_at, result, severity, difference_count,
                   control_action, summary_json
                   ) VALUES('r-2','full',?,'2026-07-14 10:04:02',
                   '2026-07-14 10:04:03','matched','INFO',0,'','{}')""",
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
            scope = self.store.get_or_create_account_scope(
                conn, "joinquant", "primary",
            )
            conn.execute(
                """INSERT INTO reconciliation_runs(
                   reconciliation_id, mode, snapshot_id, started_at,
                   finished_at, result, severity, difference_count,
                   control_action, summary_json, account_scope_id
                   ) VALUES('r-stop','incremental',NULL,
                   '2026-07-15 09:30:00','2026-07-15 09:30:00',
                   'mismatch','ERROR',1,'','{}',?)""",
                (scope,),
            )
            stopped = self.result("ERROR")
            stopped.reconciliation_id = "r-stop"
            apply_reconciliation_control(self.store, conn, stopped)
            other_scope = self.store.get_or_create_account_scope(
                conn, "qmt", "paper",
            )
            for sid, finished in (
                ("qmt-snap-1", "2026-07-15 09:30:10"),
                ("qmt-snap-2", "2026-07-15 09:30:20"),
            ):
                conn.execute(
                    """INSERT INTO account_snapshots(
                       snapshot_id,trade_date,generated_at,received_at,cash,
                       available_cash,total_value,position_market_value,
                       state_hash,template_version)
                       VALUES (?, '2026-07-15', ?, ?, 1,1,1,0,?,?)""",
                    (
                        sid, finished, finished, sid,
                        "2026-07-15.1-execution-state-recovery",
                    ),
                )
                conn.execute(
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, snapshot_id, started_at,
                       finished_at, result, severity, difference_count,
                       control_action, summary_json, account_scope_id
                       ) VALUES(?, 'incremental', ?, ?, ?, 'matched','INFO',
                       0,'','{}',?)""",
                    ("r-" + sid, sid, finished, finished, other_scope),
                )
            eligible, reasons, _ = auto_resume_eligibility(
                self.store, conn, now="2026-07-15 09:30:30",
                required_template="2026-07-15.1-execution-state-recovery",
            )
            self.assertFalse(eligible)
            self.assertIn("TWO_DISTINCT_POST_STOP_MATCHES_REQUIRED", reasons)
            for sid, finished in (("snap-1", "2026-07-15 09:31:00"), ("snap-2", "2026-07-15 09:32:00")):
                conn.execute(
                    """INSERT INTO account_snapshots(
                       snapshot_id,trade_date,generated_at,received_at,cash,available_cash,total_value,
                       position_market_value,state_hash,template_version)
                       VALUES (?, '2026-07-15', ?, ?, 1,1,1,0,?,?)""",
                    (sid, finished, finished, sid, "2026-07-15.1-execution-state-recovery"),
                )
                conn.execute(
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, snapshot_id, started_at,
                       finished_at, result, severity, difference_count,
                       control_action, summary_json, account_scope_id
                       ) VALUES(?, 'incremental', ?, ?, ?, 'matched','INFO',
                       0,'','{}',?)""",
                    ("r-" + sid, sid, finished, finished, scope),
                )
            ok, reasons, _ = auto_resume_eligibility(
                self.store, conn, now="2026-07-15 09:32:30",
                required_template="2026-07-15.1-execution-state-recovery",
            )
            self.assertTrue(ok, reasons)
            other_result = ReconciliationResult(
                "r-qmt-snap-2", "matched", "INFO", [], "", "qmt-snap-2",
            )
            self.assertIsNone(apply_automatic_buy_recovery(
                self.store, conn, other_result,
                now="2026-07-15 09:32:30",
                required_template="2026-07-15.1-execution-state-recovery",
            ))
            matched = ReconciliationResult(
                "r-snap-2", "matched", "INFO", [], "", "snap-2",
            )
            recovered = apply_automatic_buy_recovery(
                self.store, conn, matched, now="2026-07-15 09:32:30",
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

    def test_control_status_exposes_recovery_owner_and_recent_events(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            apply_reconciliation_control(self.store, conn, self.result("ERROR"))

        status = control_status(self.store)

        self.assertEqual(status["controls"]["buy_enabled"]["value"], "0")
        self.assertTrue(status["automatic_recovery_owner"]["value"])
        self.assertEqual(status["recent_control_events"][0]["action"], "stop_buy")
        self.assertIn("notification_health", status)
        self.assertEqual(status["notification_health"]["unresolved_gaps"], 0)

    def test_manual_resume_acknowledges_sticky_critical_issue(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "0", "critical")
            self.store.upsert_execution_issue(conn, {
                "account_scope_id": self.scope,
                "issue_key": f"scope:{self.scope}:fill:t-1",
                "object_type": "fill", "object_id": "t-1",
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
                """SELECT state, recovered_at FROM execution_issue_state
                   WHERE issue_key=?""",
                (f"scope:{self.scope}:fill:t-1",),
            ).fetchone()
        self.assertEqual(row["state"], "RECOVERED")
        self.assertTrue(row["recovered_at"])

    def test_notification_capacity_owner_needs_two_distinct_five_minute_cycles(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            conn.executemany(
                """INSERT INTO notification_outbox(
                   event_key, account_scope_id, adapter, event_type, object_type,
                   object_id, source_fact_id, priority, payload_version,
                   payload_sha256, state, attempt_count, occurred_at, created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        f"joinquant:{self.scope}:control:capacity-{index}",
                        self.scope, "joinquant", "control", "capacity",
                        str(index), str(index), "high", 1, "a" * 64,
                        "pending", 0, "2026-07-28T02:00:00+00:00",
                        "2026-07-28T02:00:00+00:00",
                    )
                    for index in range(4000)
                ),
            )
            first = reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:00:00+00:00",
                cycle_id="pressure",
            )
        self.assertEqual(first["action"], "stop_buy")
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")
        self.assertEqual(self.store.get_system_state("kill_switch", "0"), "0")

        with self.store.transaction() as conn:
            high_rows = self.store.notification_capacity(conn).high_active_rows
            conn.execute(
                """UPDATE notification_outbox SET state='sent', sent_at=?,
                   terminal_at=? WHERE event_key IN (
                     SELECT event_key FROM notification_outbox
                     WHERE priority='high' AND state='pending'
                     ORDER BY event_key LIMIT ?
                   )""",
                (
                    "2026-07-28T02:00:01+00:00",
                    "2026-07-28T02:00:01+00:00",
                    high_rows - 1000,
                ),
            )
            self.assertEqual(
                self.store.notification_capacity(conn).high_active_rows,
                1000,
            )
            self.assertIsNone(reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:00:30+00:00",
                cycle_id="at-20-percent",
            ))
            owner = __import__("json").loads(conn.execute(
                """SELECT value FROM system_state
                   WHERE key='notification_capacity_auto_resume_owner'""",
            ).fetchone()[0])
            self.assertFalse(owner.get("low_cycle_id"))
            conn.execute(
                """UPDATE notification_outbox SET state='sent', sent_at=?,
                   terminal_at=? WHERE priority='high' AND state='pending'""",
                ("2026-07-28T02:00:01+00:00", "2026-07-28T02:00:01+00:00"),
            )
            self.assertIsNone(reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:01:00+00:00",
                cycle_id="low-1",
            ))
            self.assertIsNone(reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:06:00+00:00",
                cycle_id="low-1",
            ))
            recovered = reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:06:00+00:00",
                cycle_id="low-2",
            )
        self.assertEqual(recovered["action"], "auto_resume_buy")
        self.assertEqual(self.store.get_system_state("buy_enabled"), "1")

    def test_manual_same_value_hold_cancels_notification_capacity_owner(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "0", "capacity")
            row = conn.execute(
                "SELECT updated_at FROM system_state WHERE key='buy_enabled'",
            ).fetchone()
            self.store.set_system_state(
                conn,
                "notification_capacity_auto_resume_owner",
                __import__("json").dumps({
                    "owner": "notification_capacity",
                    "expected_value": "0",
                    "expected_updated_at": str(row[0]),
                }),
                "test",
            )
        self.assertTrue(change_control(
            self.store, "buy_enabled", "0", reason="manual hold",
            operator="tester", expected_value="0",
        ))
        self.assertEqual(self.store.get_system_state(
            "notification_capacity_auto_resume_owner",
        ), "")

    def test_manual_resume_under_pressure_keeps_new_capacity_owner(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "1", "initial")
            conn.executemany(
                """INSERT INTO notification_outbox(
                   event_key, account_scope_id, adapter, event_type, object_type,
                   object_id, source_fact_id, priority, payload_version,
                   payload_sha256, state, attempt_count, occurred_at, created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        f"joinquant:{self.scope}:control:manual-pressure-{index}",
                        self.scope, "joinquant", "control", "capacity",
                        str(index), str(index), "high", 1, "a" * 64,
                        "pending", 0, "2026-07-28T02:00:00+00:00",
                        "2026-07-28T02:00:00+00:00",
                    )
                    for index in range(4000)
                ),
            )
            reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:00:00+00:00",
                cycle_id="pressure",
            )

        self.assertTrue(change_control(
            self.store, "buy_enabled", "1", reason="manual resume attempt",
            operator="tester", expected_value="0",
        ))
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")
        owner = self.store.get_system_state(
            "notification_capacity_auto_resume_owner", "",
        )
        self.assertEqual(__import__("json").loads(owner)["owner"], "notification_capacity")

    def test_notification_capacity_owner_is_lost_on_control_generation_change(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "0", "capacity")
            self.store.set_system_state(
                conn,
                "notification_capacity_auto_resume_owner",
                __import__("json").dumps({
                    "owner": "notification_capacity",
                    "expected_value": "0",
                    "expected_updated_at": "older-generation",
                }),
                "test",
            )
            self.assertIsNone(reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:01:00+00:00",
                cycle_id="low-1",
            ))
        self.assertEqual(self.store.get_system_state(
            "notification_capacity_auto_resume_owner",
        ), "")
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")

    def test_high_dead_detail_blocks_recovery_but_tombstone_does_not(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "0", "capacity")
            buy = conn.execute(
                "SELECT updated_at FROM system_state WHERE key='buy_enabled'",
            ).fetchone()
            self.store.set_system_state(
                conn,
                "notification_capacity_auto_resume_owner",
                __import__("json").dumps({
                    "owner": "notification_capacity",
                    "expected_value": "0",
                    "expected_updated_at": str(buy[0]),
                    "last_cycle_id": "",
                    "low_cycle_id": "",
                    "low_checked_at": "",
                }),
                "test",
            )
            conn.execute(
                """INSERT INTO notification_outbox(
                   event_key, account_scope_id, adapter, event_type, object_type,
                   object_id, source_fact_id, priority, payload_version,
                   payload_sha256, payload_json, state, attempt_count,
                   occurred_at, created_at, terminal_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"joinquant:{self.scope}:control:dead", self.scope,
                    "joinquant", "control", "control", "dead", "dead",
                    "high", 1, "a" * 64, "{}", "dead", 0,
                    "2026-07-28T02:00:00+00:00",
                    "2026-07-28T02:00:00+00:00",
                    "2026-07-28T02:00:00+00:00",
                ),
            )
            self.assertIsNone(reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:01:00+00:00",
                cycle_id="dead-detail",
            ))
            conn.execute(
                """UPDATE notification_outbox SET payload_json=NULL,
                   title=NULL, body=NULL, body_sha256=NULL, metadata_json=NULL,
                   last_error_code=NULL, last_error=NULL WHERE state='dead'""",
            )
            self.assertIsNone(reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:02:00+00:00",
                cycle_id="low-1",
            ))
            recovered = reconcile_notification_capacity(
                self.store, conn, now="2026-07-28T02:07:00+00:00",
                cycle_id="low-2",
            )
        self.assertEqual(recovered["action"], "auto_resume_buy")

    def test_reconciliation_hold_cancels_notification_capacity_owner(self) -> None:
        with self.store.transaction() as conn:
            self.store.set_system_state(conn, "buy_enabled", "0", "capacity")
            self.store.set_system_state(
                conn, "notification_capacity_auto_resume_owner",
                '{"owner":"notification_capacity"}', "test",
            )
            apply_reconciliation_control(self.store, conn, self.result("ERROR"))
        self.assertEqual(self.store.get_system_state(
            "notification_capacity_auto_resume_owner",
        ), "")


if __name__ == "__main__":
    unittest.main()
