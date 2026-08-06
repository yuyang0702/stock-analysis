import copy
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

from joinquant_sync import _legacy_broker_snapshot, ingest_snapshot_payload
from reconciliation import (
    ReconciliationDifference, ReconciliationResult, build_reconciliation_markdown,
    notify_reconciliation, reconcile_snapshot,
    persist_issue_transitions,
)
from trading_store import TradingStore


def snapshot(generated_at: str = "2026-07-14 10:00:00") -> dict:
    return {
        "schema_version": 1,
        "trade_date": generated_at[:10],
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
        self.reconciliation_sequence = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reconcile(self, payload: dict, mode: str = "full"):
        self.reconciliation_sequence += 1
        current_payload = copy.deepcopy(payload)
        trade_date = str(current_payload.get("trade_date") or "2026-07-14")
        broker_time = (
            f"{trade_date} 10:01:{self.reconciliation_sequence:02d}"
        )
        current_payload["generated_at"] = broker_time
        with self.store.transaction() as conn:
            scope = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter='joinquant' AND scope_alias='primary'"""
            ).fetchone()[0]
            broker_snapshot = _legacy_broker_snapshot(
                current_payload, str(scope), broker_time, {},
            )
            self.store.replace_current_broker_snapshot(conn, broker_snapshot)
            return reconcile_snapshot(
                self.store, conn, current_payload,
                snapshot_id=self.snapshot_id,
                broker_snapshot=broker_snapshot, mode=mode,
                now=broker_time,
            )

    def test_matched_account_and_tolerance_boundary(self) -> None:
        self.assertEqual(self.reconcile(self.payload).result, "matched")
        within = dict(self.payload, total_value=100000.005)
        self.assertEqual(self.reconcile(within).result, "matched")
        outside = dict(self.payload, total_value=100000.02)
        result = self.reconcile(outside)
        self.assertEqual(result.severity, "ERROR")
        self.assertIn("ACCOUNT_BALANCE_MISMATCH", [item.reason_code for item in result.differences])

    def test_same_broker_snapshot_replay_is_immutable_and_keeps_finish_time(self) -> None:
        with self.store.transaction() as conn:
            scope = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter='joinquant' AND scope_alias='primary'"""
            ).fetchone()[0]
            broker_snapshot = self.store.load_current_broker_snapshot(
                conn, str(scope),
            )
            first = reconcile_snapshot(
                self.store, conn, self.payload, snapshot_id=self.snapshot_id,
                broker_snapshot=broker_snapshot, mode="full",
                now="2026-07-14 10:00:03",
            )
            finished_at = conn.execute(
                """SELECT finished_at FROM reconciliation_runs
                   WHERE reconciliation_id=?""",
                (first.reconciliation_id,),
            ).fetchone()[0]
        with self.store.transaction() as conn:
            replayed = reconcile_snapshot(
                self.store, conn, self.payload, snapshot_id=self.snapshot_id,
                broker_snapshot=broker_snapshot, mode="full",
                now="2026-07-14 11:00:00",
            )
            self.assertEqual(replayed.reconciliation_id, first.reconciliation_id)
            self.assertEqual(conn.execute(
                """SELECT finished_at FROM reconciliation_runs
                   WHERE reconciliation_id=?""",
                (first.reconciliation_id,),
            ).fetchone()[0], finished_at)
            with self.assertRaisesRegex(ValueError, "not bound to payload"):
                reconcile_snapshot(
                    self.store, conn,
                    dict(self.payload, total_value=100001),
                    snapshot_id=self.snapshot_id,
                    broker_snapshot=broker_snapshot, mode="full",
                    now="2026-07-14 11:00:01",
                )

    def test_replayed_snapshot_freezes_time_dependent_exit_evidence(self) -> None:
        with self.store.transaction() as conn:
            scope = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter='joinquant' AND scope_alias='primary'"""
            ).fetchone()[0]
            self.store.upsert_exit_intent(
                conn, "exit-replay", "600000", 0, "hard_stop",
                "2026-07-14 09:59:00",
            )
            current_payload = copy.deepcopy(self.payload)
            current_payload["generated_at"] = "2026-07-14 10:01:00"
            broker_snapshot = _legacy_broker_snapshot(
                current_payload, str(scope), "2026-07-14 10:01:01", {},
            )
            self.store.replace_current_broker_snapshot(conn, broker_snapshot)
            first = reconcile_snapshot(
                self.store, conn, current_payload,
                snapshot_id=self.snapshot_id,
                broker_snapshot=broker_snapshot, mode="full",
                now="2026-07-14 10:01:01",
            )
            replay = reconcile_snapshot(
                self.store, conn, current_payload,
                snapshot_id=self.snapshot_id,
                broker_snapshot=broker_snapshot, mode="full",
                now="2026-07-14 10:02:01",
            )

        self.assertEqual(first.differences, replay.differences)
        self.assertEqual(first.reconciliation_id, replay.reconciliation_id)

    def test_same_reason_count_with_changed_details_is_an_immutable_conflict(self) -> None:
        with self.store.transaction() as conn:
            scope = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter='joinquant' AND scope_alias='primary'"""
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO orders(
                   client_order_id, order_id, stock_code, action, target_qty,
                   requested_qty, filled_qty, average_fill_price, status,
                   submit_count, reason, updated_at, raw_json
                   ) VALUES('local-a', 'order-a', '600000', 'buy', 100, 100,
                   0, 0, 'submitted', 1, '', '2026-07-14 10:00:00', '{}')"""
            )
            current_payload = copy.deepcopy(self.payload)
            current_payload["generated_at"] = "2026-07-14 10:01:00"
            broker_snapshot = _legacy_broker_snapshot(
                current_payload, str(scope), "2026-07-14 10:01:01", {},
            )
            self.store.replace_current_broker_snapshot(conn, broker_snapshot)
            first = reconcile_snapshot(
                self.store, conn, current_payload,
                snapshot_id=self.snapshot_id,
                broker_snapshot=broker_snapshot, mode="full",
                now="2026-07-14 10:01:01",
            )
            self.assertEqual(
                [item.reason_code for item in first.differences],
                ["ORDER_MISSING_PLATFORM"],
            )
            conn.execute(
                """UPDATE orders SET order_id='order-b'
                   WHERE client_order_id='local-a'"""
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                reconcile_snapshot(
                    self.store, conn, current_payload,
                    snapshot_id=self.snapshot_id,
                    broker_snapshot=broker_snapshot, mode="full",
                    now="2026-07-14 10:01:02",
                )

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

    def test_cancelled_local_order_cannot_match_submitted_broker_order(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO orders(
                   client_order_id, order_id, stock_code, action, target_qty,
                   requested_qty, filled_qty, average_fill_price, status,
                   submit_count, reason, updated_at, raw_json
                   ) VALUES(
                   'local-cancelled', 'o-cancelled', '600000', 'buy', 100,
                   100, 0, 0, 'cancelled', 1, '',
                   '2026-07-14 09:59:00', '{}'
                   )"""
            )
        changed = snapshot()
        changed["orders"] = [{
            "order_id": "o-cancelled", "code": "600000", "action": "buy",
            "amount": 100, "filled": 0, "avg_price": 0,
            "status": "submitted", "datetime": "2026-07-14 10:00:00",
        }]

        result = self.reconcile(changed)

        self.assertEqual(result.result, "mismatch")
        self.assertEqual(result.severity, "ERROR")
        status = [
            item for item in result.differences
            if item.reason_code == "ORDER_STATUS_MISMATCH"
        ]
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0].local_value, "cancelled")
        self.assertEqual(status[0].platform_value, "submitted")

    def test_failed_and_skipped_local_orders_may_be_absent_from_full_snapshot(self) -> None:
        with self.store.transaction() as conn:
            for index, status in enumerate(("failed", "skipped"), start=1):
                conn.execute(
                    """INSERT INTO orders(
                       client_order_id, order_id, stock_code, action, target_qty,
                       requested_qty, filled_qty, average_fill_price, status,
                       submit_count, reason, updated_at, raw_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"local-{status}", f"o-{status}", f"60000{index}", "buy",
                        100, 100, 0, 0, status, 1, "", "2026-07-14 09:59:00", "{}",
                    ),
                )

        result = self.reconcile(self.payload)

        self.assertNotIn(
            "ORDER_MISSING_PLATFORM",
            {item.reason_code for item in result.differences},
        )

    def test_full_reconciliation_ignores_prior_day_local_fills(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO fills(fill_id, order_id, stock_code, action, qty, price, filled_at, raw_json)
                   VALUES ('prior-day-fill','prior-order','000021','sell',100,10,
                           '2026-07-14 09:46:00','{}')"""
            )
        current = snapshot("2026-07-15 10:00:00")
        current_id = ingest_snapshot_payload(
            current, self.store, "2026-07-15 10:00:02"
        )["snapshot_id"]
        with self.store.transaction() as conn:
            scope = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter='joinquant' AND scope_alias='primary'"""
            ).fetchone()[0]
            broker_snapshot = self.store.load_current_broker_snapshot(
                conn, str(scope),
            )
            result = reconcile_snapshot(
                self.store, conn, current, snapshot_id=current_id,
                broker_snapshot=broker_snapshot, mode="full",
                now="2026-07-15 10:00:03",
            )
        self.assertEqual(result.result, "matched")
        self.assertNotIn(
            "FILL_MISSING_PLATFORM",
            {item.reason_code for item in result.differences},
        )

    def test_full_reconciliation_warns_for_missing_same_day_local_fill(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO fills(fill_id, order_id, stock_code, action, qty, price, filled_at, raw_json)
                   VALUES ('same-day-fill','same-order','601600','buy',800,10,
                           '2026-07-14 14:22:00','{}')"""
            )
        result = self.reconcile(self.payload)
        missing = [
            item for item in result.differences
            if item.reason_code == "FILL_MISSING_PLATFORM"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].object_id, "same-day-fill")
        self.assertEqual(missing[0].severity, "WARNING")

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
        changed["orders"] = [{
            "order_id": "o-1", "code": "600000", "action": "sell",
            "amount": 100, "filled": 100, "avg_price": 10.1,
            "status": "filled", "datetime": "2026-07-14 10:00:00",
        }]
        changed["trades"] = [{
            "trade_id": "t-1", "order_id": "o-1", "code": "600000", "action": "sell",
            "amount": 100, "price": 10.1, "datetime": "2026-07-14 10:00:00",
        }]
        result = self.reconcile(changed)
        codes = {item.reason_code for item in result.differences}
        self.assertIn("SIGNAL_DELIVERY_PENDING", codes)
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

        notifier = Mock()
        notifier.send_markdown.return_value = True
        self.assertFalse(notify_reconciliation(result, controls, notifier=notifier))
        notifier.send_markdown.assert_not_called()

    def test_issue_transitions_are_emitted_once_and_recover_once(self) -> None:
        difference = ReconciliationDifference(
            "exit_intent", "s-1", "SIGNAL_STALE", "0", "600", 0,
            "ERROR", {"stage_started_at": "2026-07-15 09:30:00"},
        )
        result = ReconciliationResult("r-1", "mismatch", "ERROR", [difference], "", "snap-1")
        with self.store.transaction() as conn:
            first = persist_issue_transitions(self.store, conn, result, "2026-07-15 09:33:00")
            replay = persist_issue_transitions(self.store, conn, result, "2026-07-15 09:34:00")
            recovered = persist_issue_transitions(
                self.store, conn,
                ReconciliationResult("r-2", "matched", "INFO", [], "", "snap-2"),
                "2026-07-15 09:35:00",
            )
            repeated = persist_issue_transitions(
                self.store, conn,
                ReconciliationResult("r-3", "matched", "INFO", [], "", "snap-3"),
                "2026-07-15 09:36:00",
            )
        self.assertEqual(first[0]["transition"], "OPENED")
        self.assertEqual(replay, [])
        self.assertEqual(recovered[0]["state"], "RECOVERED")
        self.assertEqual(repeated, [])

    def test_pending_info_transition_does_not_send_wecom_message(self) -> None:
        transition = {
            "issue_key": "exit_intent:s-1", "state": "SIGNAL_DELIVERY_PENDING",
            "severity": "INFO", "transition": "OPENED",
        }
        result = ReconciliationResult(
            "r-info", "mismatch", "INFO", [], "", "snap", [transition]
        )
        notifier = Mock()
        self.assertFalse(notify_reconciliation(
            result, {"buy_enabled": "1", "kill_switch": "0"}, notifier=notifier
        ))
        notifier.send_markdown.assert_not_called()

    def test_idless_platform_block_event_is_used_for_exit_classification(self) -> None:
        with self.store.transaction() as conn:
            self.store.upsert_exit_intent(
                conn, "exit-blocked", "600000", 0, "hard_stop", "2026-07-14 09:59:00"
            )
            self.store.reconcile_order_events(conn, [{
                "id": "exit-blocked", "code": "600000", "action": "sell",
                "status": "t_plus_one", "reason": "t_plus_one",
                "datetime": "2026-07-14 10:00:00",
            }], "2026-07-14 10:00:00")
        result = self.reconcile(self.payload)
        states = {item.reason_code for item in result.differences}
        self.assertIn("MARKET_BLOCKED_T1", states)
        self.assertNotIn("SIGNAL_DELIVERY_PENDING", states)

    def test_issue_state_keeps_highest_severity_for_one_object(self) -> None:
        result = ReconciliationResult("r-severity", "mismatch", "ERROR", [
            ReconciliationDifference(
                "position", "600000", "POSITION_QTY_MISMATCH", "100", "90", 0,
                "ERROR", {},
            ),
            ReconciliationDifference(
                "position", "600000", "POSITION_SELLABLE_MISMATCH", "80", "70", 0,
                "WARNING", {},
            ),
        ], "", "snap")
        with self.store.transaction() as conn:
            persist_issue_transitions(self.store, conn, result, "2026-07-15 09:30:00")
            row = conn.execute(
                "SELECT state, severity FROM execution_issue_state WHERE issue_key='position:600000'"
            ).fetchone()
        self.assertEqual((row["state"], row["severity"]), ("POSITION_QTY_MISMATCH", "ERROR"))

    def test_resolved_noncritical_issue_recovers_while_other_mismatch_remains(self) -> None:
        with self.store.transaction() as conn:
            for key, object_id in (("account:cash", "cash"), ("position:600000", "600000")):
                self.store.upsert_execution_issue(conn, {
                    "issue_key": key, "object_type": key.split(":", 1)[0],
                    "object_id": object_id, "state": "OLD_ERROR", "severity": "ERROR",
                    "stage_started_at": "2026-07-15 09:30:00",
                    "seen_at": "2026-07-15 09:30:00", "details": {},
                })
            result = ReconciliationResult("r-partial-recovery", "mismatch", "ERROR", [
                ReconciliationDifference(
                    "position", "600000", "POSITION_QTY_MISMATCH", "100", "90", 0,
                    "ERROR", {},
                )
            ], "", "snap")
            transitions = persist_issue_transitions(
                self.store, conn, result, "2026-07-15 09:31:00"
            )
            cash = conn.execute(
                "SELECT state, recovered_at FROM execution_issue_state WHERE issue_key='account:cash'"
            ).fetchone()
        self.assertEqual(cash["state"], "RECOVERED")
        self.assertTrue(cash["recovered_at"])
        self.assertIn("account:cash", [item["issue_key"] for item in transitions])

    def test_issue_transitions_and_recovery_are_account_scoped(self) -> None:
        difference = ReconciliationDifference(
            "order", "shared-order", "ORDER_MISSING_PLATFORM",
            "present", "missing", 0, "ERROR", {},
        )
        with self.store.transaction() as conn:
            scopes = {
                alias: self.store.get_or_create_account_scope(conn, adapter, alias)
                for alias, adapter in (("primary", "joinquant"), ("paper", "qmt"))
            }
            for reconciliation_id, scope in (
                ("r-joinquant-open", scopes["primary"]),
                ("r-qmt-open", scopes["paper"]),
                ("r-joinquant-clean", scopes["primary"]),
            ):
                conn.execute(
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, started_at, finished_at,
                       result, severity, difference_count, control_action,
                       summary_json, account_scope_id
                       ) VALUES(?, 'full', '2026-07-15 09:30:00',
                       '2026-07-15 09:30:01', 'mismatch', 'ERROR', 1, '',
                       '{}', ?)""",
                    (reconciliation_id, scope),
                )
            persist_issue_transitions(
                self.store, conn,
                ReconciliationResult(
                    "r-joinquant-open", "mismatch", "ERROR",
                    [difference], "", None,
                ),
                "2026-07-15 09:30:02",
            )
            persist_issue_transitions(
                self.store, conn,
                ReconciliationResult(
                    "r-qmt-open", "mismatch", "ERROR",
                    [difference], "", None,
                ),
                "2026-07-15 09:30:03",
            )
            transitions = persist_issue_transitions(
                self.store, conn,
                ReconciliationResult(
                    "r-joinquant-clean", "matched", "INFO", [], "", None,
                ),
                "2026-07-15 09:30:04",
            )
            rows = conn.execute(
                """SELECT issue_key, state, recovered_at
                   FROM execution_issue_state ORDER BY issue_key"""
            ).fetchall()

        self.assertEqual(len(rows), 2)
        by_scope = {
            "joinquant" if scopes["primary"] in row["issue_key"] else "qmt": row
            for row in rows
        }
        self.assertEqual(by_scope["joinquant"]["state"], "RECOVERED")
        self.assertTrue(by_scope["joinquant"]["recovered_at"])
        self.assertEqual(by_scope["qmt"]["state"], "ORDER_MISSING_PLATFORM")
        self.assertIsNone(by_scope["qmt"]["recovered_at"])
        self.assertEqual(len(transitions), 1)
        self.assertIn(scopes["primary"], transitions[0]["issue_key"])

    def test_unscoped_result_cannot_recover_scoped_issue(self) -> None:
        difference = ReconciliationDifference(
            "order", "qmt-order", "ORDER_MISSING_PLATFORM",
            "present", "missing", 0, "ERROR", {},
        )
        with self.store.transaction() as conn:
            scope = self.store.get_or_create_account_scope(conn, "qmt", "paper")
            conn.execute(
                """INSERT INTO reconciliation_runs(
                   reconciliation_id, mode, started_at, finished_at, result,
                   severity, difference_count, control_action, summary_json,
                   account_scope_id
                   ) VALUES('r-qmt-scoped', 'full', '2026-07-15 09:30:00',
                   '2026-07-15 09:30:01', 'mismatch', 'ERROR', 1, '', '{}', ?)""",
                (scope,),
            )
            persist_issue_transitions(
                self.store, conn,
                ReconciliationResult(
                    "r-qmt-scoped", "mismatch", "ERROR",
                    [difference], "", None,
                ),
                "2026-07-15 09:30:02",
            )
            transitions = persist_issue_transitions(
                self.store, conn,
                ReconciliationResult(
                    "unscoped-clean", "matched", "INFO", [], "", None,
                ),
                "2026-07-15 09:30:03",
            )
            row = conn.execute(
                """SELECT state, recovered_at FROM execution_issue_state
                   WHERE issue_key=?""",
                (f"scope:{scope}:order:qmt-order",),
            ).fetchone()

        self.assertEqual(transitions, [])
        self.assertEqual(row["state"], "ORDER_MISSING_PLATFORM")
        self.assertIsNone(row["recovered_at"])

    def test_adopted_legacy_issue_recovers_only_as_primary_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, {
                    "issue_key": "order:legacy-order",
                    "object_type": "order",
                    "object_id": "legacy-order",
                    "state": "ORDER_MISSING_PLATFORM",
                    "severity": "ERROR",
                    "stage_started_at": "2026-07-15 09:30:00",
                    "seen_at": "2026-07-15 09:30:00",
                    "details": {},
                })
                scope = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
                conn.execute(
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, started_at, finished_at,
                       result, severity, difference_count, control_action,
                       summary_json, account_scope_id
                       ) VALUES('r-primary-clean', 'full',
                       '2026-07-15 09:31:00', '2026-07-15 09:31:01',
                       'matched', 'INFO', 0, '', '{}', ?)""",
                    (scope,),
                )
                transitions = persist_issue_transitions(
                    store, conn,
                    ReconciliationResult(
                        "r-primary-clean", "matched", "INFO", [], "", None,
                    ),
                    "2026-07-15 09:31:02",
                )
                row = conn.execute(
                    """SELECT issue_key, state, recovered_at
                       FROM execution_issue_state"""
                ).fetchone()

        self.assertEqual(
            row["issue_key"], f"scope:{scope}:order:legacy-order",
        )
        self.assertEqual(row["state"], "RECOVERED")
        self.assertTrue(row["recovered_at"])
        self.assertEqual(transitions[0]["state"], "RECOVERED")

    def test_matched_snapshot_does_not_auto_recover_sticky_critical(self) -> None:
        with self.store.transaction() as conn:
            self.store.upsert_execution_issue(conn, {
                "issue_key": "ledger:sqlite", "object_type": "ledger", "object_id": "sqlite",
                "state": "LEDGER_INTEGRITY_FAILURE", "severity": "CRITICAL",
                "stage_started_at": "2026-07-15 09:30:00",
                "seen_at": "2026-07-15 09:30:00", "details": {},
            })
            persist_issue_transitions(
                self.store, conn,
                ReconciliationResult("r-clean", "matched", "INFO", [], "", "snap-clean"),
                "2026-07-15 09:31:00",
            )
            row = conn.execute(
                "SELECT recovered_at FROM execution_issue_state WHERE issue_key='ledger:sqlite'"
            ).fetchone()
        self.assertIsNone(row["recovered_at"])

    def test_matched_snapshot_does_not_recover_health_owned_issue(self) -> None:
        with self.store.transaction() as conn:
            scope = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter='joinquant' AND scope_alias='primary'"""
            ).fetchone()[0]
            issue_key = f"scope:{scope}:health:snapshot_stale"
            self.store.upsert_execution_issue(conn, {
                "account_scope_id": scope,
                "issue_key": issue_key,
                "object_type": "health",
                "object_id": "snapshot_stale",
                "state": "SNAPSHOT_STALE",
                "severity": "ERROR",
                "stage_started_at": "2026-07-14 09:30:00",
                "seen_at": "2026-07-14 09:30:00",
                "details": {},
            })

        self.assertEqual(self.reconcile(self.payload).result, "matched")

        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT recovered_at FROM execution_issue_state
                   WHERE issue_key=?""",
                (issue_key,),
            ).fetchone()
        self.assertIsNone(row["recovered_at"])

    def test_sticky_critical_is_not_downgraded_by_later_warning(self) -> None:
        with self.store.transaction() as conn:
            self.store.upsert_execution_issue(conn, {
                "issue_key": "fill:t-1", "object_type": "fill", "object_id": "t-1",
                "state": "IMMUTABLE_FILL_CONFLICT", "severity": "CRITICAL",
                "stage_started_at": "2026-07-15 09:00:00",
                "seen_at": "2026-07-15 09:00:00", "details": {},
            })
            result = ReconciliationResult("r-warning", "mismatch", "WARNING", [
                ReconciliationDifference(
                    "fill", "t-1", "MANUAL_TRADE", "no_signal", "platform_trade", 0,
                    "WARNING", {},
                )
            ], "", "snap")
            persist_issue_transitions(self.store, conn, result, "2026-07-15 09:01:00")
            row = conn.execute(
                "SELECT state, severity FROM execution_issue_state WHERE issue_key='fill:t-1'"
            ).fetchone()
        self.assertEqual((row["state"], row["severity"]), ("IMMUTABLE_FILL_CONFLICT", "CRITICAL"))

    def test_transition_dedupe_key_includes_severity(self) -> None:
        result = ReconciliationResult("r-alert", "mismatch", "ERROR", [], "", "snap", [{
            "issue_key": "exit_intent:s-1", "state": "SIGNAL_STALE",
            "severity": "ERROR", "transition": "CHANGED",
        }])
        notifier = Mock()
        notifier.send_markdown.return_value = True
        self.assertFalse(notify_reconciliation(
            result, {"buy_enabled": "0", "kill_switch": "0"}, notifier=notifier
        ))
        notifier.send_markdown.assert_not_called()

    def test_unchanged_error_does_not_repeat_after_thirty_minutes(self) -> None:
        difference = ReconciliationDifference(
            "exit_intent", "s-1", "SIGNAL_STALE", "0", "600", 0,
            "ERROR", {"stage_started_at": "2026-07-15 09:00:00"},
        )
        with self.store.transaction() as conn:
            opened = ReconciliationResult("r-open", "mismatch", "ERROR", [difference], "", "snap-1")
            persist_issue_transitions(self.store, conn, opened, "2026-07-15 09:00:00")
            self.store.mark_execution_issues_notified(
                conn, ["exit_intent:s-1"], "2026-07-15 09:00:00"
            )
            early = ReconciliationResult("r-early", "mismatch", "ERROR", [difference], "", "snap-2")
            due = ReconciliationResult("r-due", "mismatch", "ERROR", [difference], "", "snap-3")
            self.assertEqual(
                persist_issue_transitions(self.store, conn, early, "2026-07-15 09:29:59"), []
            )
            reminders = persist_issue_transitions(
                self.store, conn, due, "2026-07-15 09:30:00"
            )
        self.assertEqual(reminders, [])

    def test_never_notified_error_still_does_not_repeat(self) -> None:
        difference = ReconciliationDifference(
            "exit_intent", "s-2", "SIGNAL_STALE", "0", "600", 0,
            "ERROR", {"stage_started_at": "2026-07-15 09:00:00"},
        )
        with self.store.transaction() as conn:
            opened = ReconciliationResult("r-open-2", "mismatch", "ERROR", [difference], "", "snap-1")
            persist_issue_transitions(self.store, conn, opened, "2026-07-15 09:00:00")
            early = ReconciliationResult("r-early-2", "mismatch", "ERROR", [difference], "", "snap-2")
            due = ReconciliationResult("r-due-2", "mismatch", "ERROR", [difference], "", "snap-3")
            self.assertEqual(
                persist_issue_transitions(self.store, conn, early, "2026-07-15 09:29:59"), []
            )
            reminders = persist_issue_transitions(
                self.store, conn, due, "2026-07-15 09:30:00"
            )
        self.assertEqual(reminders, [])

    def test_successful_transition_notification_marks_issue_notified(self) -> None:
        difference = ReconciliationDifference(
            "exit_intent", "s-1", "SIGNAL_STALE", "0", "600", 0,
            "ERROR", {"stage_started_at": "2026-07-15 09:00:00"},
        )
        with self.store.transaction() as conn:
            result = ReconciliationResult("r-notify", "mismatch", "ERROR", [difference], "", "snap")
            scope = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter='joinquant' AND scope_alias='primary'"""
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO reconciliation_runs(
                   reconciliation_id, mode, snapshot_id, started_at, finished_at,
                   result, severity, difference_count, control_action,
                   summary_json, account_scope_id
                   ) VALUES ('r-notify', 'full', NULL, ?, ?, 'mismatch',
                   'ERROR', 1, '', '{}', ?)""",
                ("2026-07-15 09:00:00", "2026-07-15 09:00:00", scope),
            )
            persist_issue_transitions(self.store, conn, result, "2026-07-15 09:00:00")
        notifier = Mock()
        notifier.send_markdown.return_value = True
        self.assertFalse(notify_reconciliation(
            result, {"buy_enabled": "0", "kill_switch": "0"}, notifier=notifier,
            store=self.store, now="2026-07-15 09:00:01",
        ))
        notifier.send_markdown.assert_not_called()
        with self.store.connect() as conn:
            notified = conn.execute(
                """SELECT last_notified_at FROM execution_issue_state
                   WHERE issue_key LIKE '%:exit_intent:s-1'"""
            ).fetchone()[0]
            outbox = conn.execute(
                """SELECT event_key FROM notification_outbox
                   WHERE object_type='execution_issue'
                     AND object_id LIKE '%exit_intent:s-1'"""
            ).fetchall()
        self.assertIsNone(notified)
        self.assertEqual(len(outbox), 1)


if __name__ == "__main__":
    unittest.main()
