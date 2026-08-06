import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import joinquant_sync
import joinquant_exporter
import pandas as pd
from notification_outbox import (
    NotificationCapacityError,
    NotificationConflict,
    NotificationEvent,
    notification_event_key,
)
from notification_worker import _business_body
from reconciliation import (
    ReconciliationDifference,
    ReconciliationResult,
    persist_issue_transitions,
)
from trading_control import change_control
from trading_store import TradingStore


NOW = "2026-07-28T10:05:02+08:00"


def ledger_snapshot(*, status: str = "filled", reason: str = "") -> dict:
    filled = 100 if status == "filled" else 0
    return {
        "schema_version": 1,
        "trade_date": "2026-07-28",
        "generated_at": "2026-07-28 10:05:00",
        "source": "joinquant",
        "template_version": "test-ledger-v12",
        "cash": 99000,
        "available_cash": 99000,
        "total_value": 100000,
        "daily_turnover_pct": 1,
        "daily_pnl_pct": 0,
        "account_drawdown_pct": 0,
        "consecutive_losses": 0,
        "positions": [],
        "orders": [{
            "order_id": "order-1",
            "code": "600000",
            "action": "buy",
            "amount": 100,
            "filled": filled,
            "avg_price": 10 if filled else 0,
            "status": status,
            "reason": reason,
            "datetime": "2026-07-28 10:05:00",
        }],
        "trades": ([{
            "trade_id": "fill-1",
            "order_id": "order-1",
            "code": "600000",
            "action": "buy",
            "amount": 100,
            "price": 10,
            "commission": 5,
            "stamp_tax": 0,
            "other_fee": 0,
            "datetime": "2026-07-28 10:05:00",
        }] if filled else []),
    }


class NotificationProducerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TradingStore(Path(self.temp.name) / "trading.db")
        self.store.initialize()

    def scope(self) -> str:
        with self.store.transaction() as conn:
            return self.store.get_or_create_account_scope(
                conn, "joinquant", "primary",
            )

    def test_event_key_escapes_delimiters_without_collision(self) -> None:
        first = notification_event_key(
            "joinquant", "scope", "issue",
            issue_key="order:a%b", incident_id="incident", transition_seq=1,
            transition="OPENED", severity="ERROR",
        )
        second = notification_event_key(
            "joinquant", "scope", "issue",
            issue_key="order%3Aa%25b", incident_id="incident", transition_seq=1,
            transition="OPENED", severity="ERROR",
        )

        self.assertIn("order%3Aa%25b", first)
        self.assertNotEqual(first, second)

    def test_fill_and_terminal_order_each_enqueue_one_event(self) -> None:
        joinquant_sync.ingest_snapshot_payload(
            ledger_snapshot(), self.store, NOW,
        )

        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT event_key, event_type, object_id, source_fact_id
                   FROM notification_outbox
                   WHERE event_type IN ('fill','order_terminal')
                   ORDER BY event_type"""
            ).fetchall()
        self.assertEqual(
            [(row["event_type"], row["object_id"]) for row in rows],
            [("fill", "fill-1"), ("order_terminal", "manual:order-1")],
        )
        self.assertEqual(rows[0]["source_fact_id"], "fill-1")
        self.assertTrue(str(rows[1]["source_fact_id"]).startswith("terminal:"))
        with self.store.connect() as conn:
            terminal_fact = conn.execute(
                "SELECT raw_json FROM order_events WHERE event_key=?",
                (rows[1]["source_fact_id"],),
            ).fetchone()
        self.assertIsNotNone(terminal_fact)
        self.assertEqual(json.loads(terminal_fact["raw_json"])["status"], "FILLED")
        fill_notice = self.store.get_notification(
            next(
                row["event_key"] for row in rows
                if row["event_type"] == "fill"
            )
        )
        rendered = _business_body(fill_notice)
        self.assertEqual(
            rendered.count("业务时间：") + rendered.count("成交时间："), 1,
        )

    def test_replayed_fill_still_emits_later_cancel_terminal(self) -> None:
        first = ledger_snapshot(status="partial")
        first["orders"][0].update({"filled": 50, "avg_price": 10})
        first["trades"] = [{
            "trade_id": "fill-1",
            "order_id": "order-1",
            "code": "600000",
            "action": "buy",
            "amount": 50,
            "price": 10,
            "datetime": "2026-07-28 10:05:00",
        }]
        joinquant_sync.ingest_snapshot_payload(first, self.store, NOW)

        cancelled = json.loads(json.dumps(first))
        cancelled["generated_at"] = "2026-07-28 10:06:00"
        cancelled["orders"][0].update({
            "status": "cancelled",
            "reason": "user_cancel",
            "datetime": "2026-07-28 10:06:00",
        })
        joinquant_sync.ingest_snapshot_payload(
            cancelled, self.store, "2026-07-28T10:06:02+08:00",
        )

        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT payload_json FROM notification_outbox
                   WHERE event_type='order_terminal'"""
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["payload_json"])["status"], "CANCELLED")

    def test_platform_non_submission_statuses_share_one_terminal_contract(self) -> None:
        statuses = (
            "suspended", "limit_up", "limit_down", "t_plus_one",
            "insufficient_cash", "price_moved", "gap_reentry_price_moved",
            "dry_run",
        )
        for status in statuses:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                payload = ledger_snapshot(status=status, reason=status)
                joinquant_sync.ingest_snapshot_payload(payload, store, NOW)
                with store.connect() as conn:
                    order = conn.execute(
                        "SELECT status, submit_count, reason FROM orders"
                    ).fetchone()
                    notice = conn.execute(
                        """SELECT payload_json FROM notification_outbox
                           WHERE event_type='order_terminal'"""
                    ).fetchone()
                self.assertEqual(order["status"], "not_submitted")
                self.assertEqual(order["submit_count"], 0)
                self.assertEqual(order["reason"], status)
                terminal = json.loads(notice["payload_json"])
                self.assertEqual(terminal["status"], "NOT_SUBMITTED")
                self.assertEqual(terminal["reason_code"], status.upper())

    def test_terminal_reason_secret_is_redacted_and_replay_stays_single(self) -> None:
        first = ledger_snapshot(
            status="cancelled",
            reason="https://example.invalid/webhook?key=TOPSECRET",
        )
        joinquant_sync.ingest_snapshot_payload(first, self.store, NOW)
        replay = json.loads(json.dumps(first))
        replay["orders"][0]["reason"] = (
            "https://example.invalid/webhook?key=DIFFERENTSECRET"
        )
        replay["generated_at"] = "2026-07-28 10:06:00"
        replay["orders"][0]["datetime"] = "2026-07-28 10:06:00"
        joinquant_sync.ingest_snapshot_payload(
            replay, self.store, "2026-07-28T10:06:02+08:00",
        )

        with self.store.connect() as conn:
            order = conn.execute(
                "SELECT reason, raw_json FROM orders"
            ).fetchone()
            terminal_rows = conn.execute(
                """SELECT event_key, body, payload_json
                   FROM notification_outbox WHERE event_type='order_terminal'"""
            ).fetchall()
        persisted = " ".join((
            order["reason"], order["raw_json"],
            terminal_rows[0]["event_key"], terminal_rows[0]["body"],
            terminal_rows[0]["payload_json"],
        ))
        self.assertEqual(len(terminal_rows), 1)
        self.assertNotIn("TOPSECRET", persisted)
        self.assertNotIn("DIFFERENTSECRET", persisted)

    def test_terminal_fact_freezes_notice_when_broker_order_id_arrives_late(self) -> None:
        first = ledger_snapshot(status="cancelled", reason="user_cancel")
        first["orders"][0].pop("order_id")
        first["orders"][0]["client_order_id"] = "client-late-order-id"
        joinquant_sync.ingest_snapshot_payload(first, self.store, NOW)

        later = json.loads(json.dumps(first))
        later["orders"][0]["order_id"] = "broker-late-order-id"
        later["generated_at"] = "2026-07-28 10:06:00"
        later["orders"][0]["datetime"] = "2026-07-28 10:06:00"
        joinquant_sync.ingest_snapshot_payload(
            later, self.store, "2026-07-28T10:06:02+08:00",
        )

        with self.store.connect() as conn:
            order_id = conn.execute(
                """SELECT order_id FROM orders
                   WHERE client_order_id='client-late-order-id'"""
            ).fetchone()[0]
            notice_count = conn.execute(
                """SELECT COUNT(*) FROM notification_outbox
                   WHERE event_type='order_terminal'"""
            ).fetchone()[0]
            fact_count = conn.execute(
                """SELECT COUNT(*) FROM order_events
                   WHERE event_key LIKE 'terminal:%'"""
            ).fetchone()[0]
        self.assertEqual(order_id, "broker-late-order-id")
        self.assertEqual(notice_count, 1)
        self.assertEqual(fact_count, 1)

    def test_fill_and_notification_conflict_roll_back_source_fact(self) -> None:
        scope = self.scope()
        conflicting = NotificationEvent(
            event_key=notification_event_key(
                "joinquant", scope, "fill", fill_id="fill-1",
            ),
            account_scope_id=scope,
            adapter="joinquant",
            event_type="fill",
            object_type="fill",
            object_id="fill-1",
            source_fact_id="fill-1",
            priority="high",
            payload_version=1,
            occurred_at=NOW,
            expires_at=None,
            title="conflict",
            body="conflict",
            payload={"different": True},
            metadata={"renderer": "test"},
        )
        with self.store.transaction() as conn:
            self.store.enqueue_notification(conn, conflicting, NOW)

        with self.assertRaises(NotificationConflict):
            joinquant_sync.ingest_snapshot_payload(
                ledger_snapshot(), self.store, NOW,
            )

        with self.store.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0,
            )

    def test_control_event_and_notification_share_transaction(self) -> None:
        self.scope()
        self.assertTrue(change_control(
            self.store,
            "buy_enabled",
            "0",
            reason="operator stop",
            operator="tester",
            expected_value="1",
        ))

        with self.store.connect() as conn:
            event = conn.execute(
                "SELECT * FROM control_events ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            notice = conn.execute(
                """SELECT * FROM notification_outbox
                   WHERE object_type='control_event' AND object_id=?""",
                (event["event_id"],),
            ).fetchone()
        self.assertIsNotNone(notice)
        self.assertEqual(notice["source_fact_id"], event["event_id"])

    def test_control_notification_redacts_secret_reason_without_losing_control(self) -> None:
        self.scope()
        self.assertTrue(change_control(
            self.store,
            "buy_enabled",
            "0",
            reason="https://example.invalid/webhook?key=TOPSECRET",
            operator="tester",
            expected_value="1",
        ))

        with self.store.connect() as conn:
            event = conn.execute(
                "SELECT reason FROM control_events ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            notice = conn.execute(
                """SELECT body, payload_json FROM notification_outbox
                   WHERE event_type='control' ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")
        self.assertNotIn("TOPSECRET", event["reason"])
        self.assertNotIn("TOPSECRET", notice["body"])
        self.assertNotIn("TOPSECRET", notice["payload_json"])

    def test_control_event_uses_registered_qmt_adapter(self) -> None:
        with self.store.transaction() as conn:
            scope = self.store.get_or_create_account_scope(
                conn, "qmt", "paper",
            )
            self.store.insert_control_event(
                conn,
                event_id="qmt-control-1",
                action="stop_buy",
                operator="test",
                old_value="1",
                new_value="0",
                reason="qmt reconciliation",
                created_at=NOW,
                account_scope_id=scope,
            )
        with self.store.connect() as conn:
            notice = conn.execute(
                """SELECT adapter, event_key FROM notification_outbox
                   WHERE object_id='qmt-control-1'"""
            ).fetchone()
        self.assertEqual(notice["adapter"], "qmt")
        self.assertTrue(str(notice["event_key"]).startswith(f"qmt:{scope}:"))

    def test_issue_incident_sequence_and_recovery_cancellation_are_atomic(self) -> None:
        scope = self.scope()
        issue = {
            "account_scope_id": scope,
            "issue_key": f"scope:{scope}:order:order-1",
            "object_type": "order",
            "object_id": "order-1",
            "state": "ORDER_NOT_FILLED",
            "severity": "ERROR",
            "stage_started_at": NOW,
            "seen_at": NOW,
            "details": {"filled_qty": 0},
        }
        with self.store.transaction() as conn:
            opened = self.store.upsert_execution_issue(conn, issue)
            changed = self.store.upsert_execution_issue(conn, {
                **issue,
                "seen_at": "2026-07-28T10:06:00+08:00",
                "details": {"filled_qty": 50},
            })
            recovered = self.store.recover_execution_issue(
                conn,
                str(issue["issue_key"]),
                "2026-07-28T10:07:00+08:00",
                account_scope_id=scope,
            )

        self.assertEqual(opened["transition"], "OPENED")
        self.assertEqual(changed["transition"], "CHANGED")
        self.assertEqual(recovered["transition"], "RECOVERED")
        self.assertEqual(
            [opened["transition_seq"], changed["transition_seq"], recovered["transition_seq"]],
            [1, 2, 3],
        )
        self.assertEqual(opened["incident_id"], changed["incident_id"])
        self.assertEqual(changed["incident_id"], recovered["incident_id"])
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT state, cancel_requested_at FROM notification_outbox
                   WHERE object_type='execution_issue'
                   ORDER BY occurred_at, event_key"""
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["cancel_requested_at"] for row in rows[:2]))
        self.assertIsNone(rows[2]["cancel_requested_at"])

    def test_issue_recovery_resolves_old_capacity_gap(self) -> None:
        scope = self.scope()
        issue_key = f"scope:{scope}:order:capacity-gap"
        issue = {
            "account_scope_id": scope,
            "issue_key": issue_key,
            "object_type": "order",
            "object_id": "capacity-gap",
            "state": "ORDER_NOT_FILLED",
            "severity": "CRITICAL",
            "stage_started_at": NOW,
            "seen_at": NOW,
            "details": {"filled_qty": 0},
        }
        with patch.object(
            self.store, "enqueue_notification",
            side_effect=NotificationCapacityError("high hard limit"),
        ):
            with self.store.transaction() as conn:
                opened = self.store.upsert_execution_issue(conn, issue)
            with self.store.transaction() as conn:
                self.store.recover_execution_issue(
                    conn, issue_key, "2026-07-28T10:07:00+08:00",
                    account_scope_id=scope,
                )

        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT source_fact_id, resolved_at, resolution
                   FROM notification_enqueue_gaps ORDER BY source_fact_id"""
            ).fetchall()
        self.assertEqual(len(rows), 2)
        by_source = {str(row["source_fact_id"]): row for row in rows}
        opened_gap = by_source[f"{issue_key}#{opened['incident_id']}#1"]
        self.assertIsNotNone(opened_gap["resolved_at"])
        self.assertEqual(opened_gap["resolution"], "superseded_by_recovery")
        self.assertNotIn(
            f"{issue_key}#{opened['incident_id']}#2", by_source,
        )
        self.assertEqual(
            sum(row["resolved_at"] is None for row in rows), 1,
        )

    def test_unchanged_error_does_not_create_thirty_minute_reminder(self) -> None:
        scope = self.scope()
        result = ReconciliationResult(
            "recon-1",
            "mismatch",
            "ERROR",
            [ReconciliationDifference(
                "order", "order-1", "ORDER_NOT_FILLED", "open", "missing",
                0, "ERROR", {},
            )],
            "",
            None,
        )
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO reconciliation_runs(
                   reconciliation_id, mode, started_at, finished_at, result,
                   severity, difference_count, control_action, summary_json,
                   account_scope_id
                   ) VALUES(?, 'incremental', ?, ?, 'mismatch', 'ERROR', 1,
                            '', '{}', ?)""",
                ("recon-1", NOW, NOW, scope),
            )
            first = persist_issue_transitions(self.store, conn, result, NOW)
            result.reconciliation_id = "recon-2"
            conn.execute(
                """INSERT INTO reconciliation_runs(
                   reconciliation_id, mode, started_at, finished_at, result,
                   severity, difference_count, control_action, summary_json,
                   account_scope_id
                   ) VALUES(?, 'incremental', ?, ?, 'mismatch', 'ERROR', 1,
                            '', '{}', ?)""",
                (
                    "recon-2",
                    "2026-07-28T10:36:00+08:00",
                    "2026-07-28T10:36:00+08:00",
                    scope,
                ),
            )
            second = persist_issue_transitions(
                self.store,
                conn,
                result,
                "2026-07-28T10:36:00+08:00",
            )

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_reconciliation_value_change_creates_changed_transition(self) -> None:
        scope = self.scope()
        times = (NOW, "2026-07-28T10:06:00+08:00")
        transitions = []
        for index, actual in enumerate(("0", "50"), 1):
            reconciliation_id = f"recon-value-{index}"
            result = ReconciliationResult(
                reconciliation_id, "mismatch", "ERROR",
                [ReconciliationDifference(
                    "position", "600000", "POSITION_QTY_MISMATCH",
                    "100", actual, 0, "ERROR", {},
                )],
                "", None,
            )
            with self.store.transaction() as conn:
                conn.execute(
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, started_at, finished_at,
                       result, severity, difference_count, control_action,
                       summary_json, account_scope_id
                       ) VALUES(?, 'incremental', ?, ?, 'mismatch', 'ERROR',
                                1, '', '{}', ?)""",
                    (reconciliation_id, times[index - 1], times[index - 1], scope),
                )
                transitions.extend(persist_issue_transitions(
                    self.store, conn, result, times[index - 1],
                ))

        self.assertEqual(
            [item["transition"] for item in transitions], ["OPENED", "CHANGED"],
        )

    def test_exit_intent_and_notification_share_transaction(self) -> None:
        output = Path(self.temp.name) / "signals.json"
        joinquant_exporter.export_signals(
            pd.DataFrame([{
                "code": "600000",
                "price": 9,
                "signal_action": "hard_stop",
                "has_holding": True,
                "exit_signal_id": "cycle-1-hard-stop-0",
                "target_qty": 0,
            }]),
            run_id="run-exit",
            trade_date="2026-07-28",
            output_path=output,
            store=self.store,
        )

        with self.store.connect() as conn:
            intent = conn.execute(
                "SELECT * FROM exit_intents WHERE signal_id='cycle-1-hard-stop-0'"
            ).fetchone()
            notice = conn.execute(
                """SELECT * FROM notification_outbox
                   WHERE event_type='exit' AND object_id=?""",
                (intent["signal_id"],),
            ).fetchone()
        self.assertIsNotNone(intent)
        self.assertIsNotNone(notice)
        self.assertEqual(notice["source_fact_id"], intent["signal_id"])
        notice_payload = json.loads(notice["payload_json"])
        self.assertEqual(notice_payload["stage"], "hard_stop")
        self.assertEqual(notice_payload["reason_code"], "hard_stop")

    def test_take_profit_alias_freezes_canonical_exit_stage(self) -> None:
        output = Path(self.temp.name) / "signals-take-profit.json"
        joinquant_exporter.export_signals(
            pd.DataFrame([{
                "code": "600000", "price": 12,
                "signal_action": "take_profit", "has_holding": True,
                "exit_signal_id": "cycle-1-take-profit-0",
                "target_qty": 100,
            }]),
            run_id="run-take-profit",
            trade_date="2026-07-28",
            output_path=output,
            store=self.store,
        )

        with self.store.connect() as conn:
            payload = json.loads(conn.execute(
                """SELECT payload_json FROM notification_outbox
                   WHERE event_type='exit'"""
            ).fetchone()[0])
        self.assertEqual(payload["stage"], "take_profit_1")
        self.assertEqual(payload["reason_code"], "take_profit_1")

    def test_unknown_long_exit_reason_cannot_block_sell_or_leak_into_event_key(self) -> None:
        output = Path(self.temp.name) / "signals-custom-exit.json"
        secret_reason = "自定义" + ("退" * 22000) + " key=TOPSECRET"
        joinquant_exporter.export_signals(
            pd.DataFrame([{
                "code": "600000",
                "price": 9,
                "signal_action": "sell",
                "risk_reason": secret_reason,
                "has_holding": True,
                "exit_signal_id": "custom-exit-1",
                "target_qty": 0,
            }]),
            run_id="run-custom-exit",
            trade_date="2026-07-28",
            output_path=output,
            store=self.store,
        )

        published = json.loads(output.read_text(encoding="utf-8"))["signals"]
        with self.store.connect() as conn:
            notice = conn.execute(
                """SELECT event_key, body, payload_json
                   FROM notification_outbox WHERE event_type='exit'"""
            ).fetchone()
            intent = conn.execute(
                "SELECT status FROM exit_intents WHERE signal_id='custom-exit-1'"
            ).fetchone()
        self.assertEqual(len(published), 1)
        self.assertEqual(intent["status"], "active")
        self.assertTrue(notice["event_key"].endswith(":sell"))
        self.assertLessEqual(len(notice["event_key"].encode("utf-8")), 1024)
        self.assertNotIn("TOPSECRET", notice["body"])
        self.assertNotIn("TOPSECRET", notice["payload_json"])

    def test_exit_capacity_gap_does_not_remove_legal_sell(self) -> None:
        output = Path(self.temp.name) / "signals-gap.json"
        frame = pd.DataFrame([{
            "code": "600000",
            "price": 9,
            "signal_action": "hard_stop",
            "has_holding": True,
            "exit_signal_id": "cycle-2-hard-stop-0",
            "target_qty": 0,
        }])
        with patch.object(
            self.store,
            "enqueue_notification",
            side_effect=NotificationCapacityError("high hard limit"),
        ):
            joinquant_exporter.export_signals(
                frame,
                run_id="run-exit-gap",
                trade_date="2026-07-28",
                output_path=output,
                store=self.store,
            )

        joinquant_exporter.export_signals(
            frame,
            run_id="run-exit-gap",
            trade_date="2026-07-28",
            output_path=output,
            store=self.store,
        )

        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            [signal["id"] for signal in payload["signals"]],
            ["cycle-2-hard-stop-0"],
        )
        with self.store.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM exit_intents WHERE signal_id='cycle-2-hard-stop-0'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM notification_enqueue_gaps"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox"
                ).fetchone()[0],
                0,
            )

    def test_exit_notification_identity_does_not_drift_when_cycle_appears(self) -> None:
        output = Path(self.temp.name) / "signals-exit-replay.json"
        frame = pd.DataFrame([{
            "code": "600000", "price": 9, "signal_action": "hard_stop",
            "has_holding": True, "exit_signal_id": "legacy-exit-1",
            "target_qty": 0,
        }])
        joinquant_exporter.export_signals(
            frame, run_id="run-exit-replay", trade_date="2026-07-28",
            output_path=output, store=self.store,
        )
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO position_cycles(
                   position_cycle_id, stock_code, opened_at, status, mode,
                   initial_qty, current_qty, entry_price, initial_stop_price,
                   initial_r, atr14, market_state, highest_price,
                   take_profit_stage, last_snapshot_at, created_at, updated_at
                   ) VALUES('late-cycle','600000',?,'active','legacy_fixed',
                            100,100,10,9,1,0.5,'normal',10,0,?,?,?)""",
                (NOW, NOW, NOW, NOW),
            )

        joinquant_exporter.export_signals(
            frame, run_id="run-exit-replay", trade_date="2026-07-28",
            output_path=output, store=self.store,
        )

        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT event_key, payload_json FROM notification_outbox
                   WHERE object_type='exit_intent' AND object_id='legacy-exit-1'"""
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            json.loads(rows[0]["payload_json"])["position_cycle_id"],
            "legacy:600000",
        )

    def test_superseded_exit_requests_old_notification_cancellation(self) -> None:
        output = Path(self.temp.name) / "signals-exit-supersede.json"
        common = {"code": "600000", "price": 10, "has_holding": True}
        joinquant_exporter.export_signals(
            pd.DataFrame([{**common, "signal_action": "take_profit_1",
                           "signal_note": "take_profit_1",
                           "exit_signal_id": "exit-tp", "target_qty": 500}]),
            run_id="run-exit-tp", trade_date="2026-07-28",
            output_path=output, store=self.store,
        )
        joinquant_exporter.export_signals(
            pd.DataFrame([{**common, "signal_action": "hard_stop",
                           "signal_note": "hard_stop",
                           "exit_signal_id": "exit-stop", "target_qty": 0}]),
            run_id="run-exit-stop", trade_date="2026-07-28",
            output_path=output, store=self.store,
        )

        with self.store.connect() as conn:
            old = conn.execute(
                """SELECT cancel_requested_at FROM notification_outbox
                   WHERE object_type='exit_intent' AND object_id='exit-tp'"""
            ).fetchone()
            new = conn.execute(
                """SELECT state, cancel_requested_at FROM notification_outbox
                   WHERE object_type='exit_intent' AND object_id='exit-stop'"""
            ).fetchone()
        self.assertIsNotNone(old["cancel_requested_at"])
        self.assertEqual((new["state"], new["cancel_requested_at"]), ("pending", None))


if __name__ == "__main__":
    unittest.main()
