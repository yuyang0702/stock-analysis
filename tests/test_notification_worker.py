from __future__ import annotations

import hashlib
from contextlib import redirect_stdout
from io import StringIO
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from notification_outbox import NotificationEvent, notification_event_key
from notification_worker import (
    compact_notifications,
    legacy_audit,
    main,
    notification_status,
    run_once,
)
from notifier import DeliveryResult
from trading_store import TradingStore


NOW = "2026-07-28T02:00:00+00:00"
RETRY_AT = "2026-07-28T02:05:00+00:00"
SECOND_RETRY_AT = "2026-07-28T02:20:00+00:00"


class FakeTransport:
    def __init__(self, *results: DeliveryResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, str]] = []

    def deliver_markdown(
        self, title: str, body: str, sent_at: str,
    ) -> DeliveryResult:
        self.calls.append((title, body, sent_at))
        return self.results.pop(0) if self.results else DeliveryResult(True)


class NotificationWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.monotonic_patch = patch(
            "notification_worker.time.monotonic", return_value=100.0,
        )
        self.monotonic_patch.start()

    def tearDown(self) -> None:
        self.monotonic_patch.stop()

    @staticmethod
    def _store(tmp: str) -> tuple[TradingStore, str]:
        store = TradingStore(Path(tmp) / "trading.db")
        store.initialize()
        with store.transaction() as conn:
            scope = store.get_or_create_account_scope(
                conn, "joinquant", "primary",
            )
        return store, scope

    @staticmethod
    def _event(
        scope: str,
        *,
        suffix: str = "1",
        expires_at: str | None = None,
        event_type: str = "fill",
        plan_version: str = "plan-v1",
        priority: str = "high",
    ) -> NotificationEvent:
        if event_type == "buy-plan":
            event_key = notification_event_key(
                "joinquant", scope, "buy-plan",
                trade_date="2026-07-28",
                logical_signal_id="logical-1",
                plan_version=plan_version,
            )
            object_type = "logical_signal_plan"
            object_id = "2026-07-28:logical-1"
        else:
            event_key = notification_event_key(
                "joinquant", scope, "fill", fill_id=f"fill-{suffix}",
            )
            object_type = "fill"
            object_id = f"fill-{suffix}"
        return NotificationEvent(
            event_key=event_key,
            account_scope_id=scope,
            adapter="joinquant",
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            source_fact_id=object_id,
            priority=priority,
            payload_version=1,
            occurred_at="2026-07-28T09:59:00+08:00",
            expires_at=expires_at,
            title="成交回报",
            body="000001 成交 100 股",
            payload={"code": "000001", "qty": 100},
            metadata={"renderer": "v1"},
        )

    @staticmethod
    def _enqueue(
        store: TradingStore,
        event: NotificationEvent,
        now: str = NOW,
    ) -> None:
        with store.transaction() as conn:
            store.enqueue_notification(conn, event, now)

    def test_each_worker_cycle_reconciles_capacity_once(self) -> None:
        with TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            with patch(
                "notification_worker.reconcile_notification_capacity",
                return_value=None,
            ) as reconcile:
                result = run_once(
                    store, FakeTransport(), "worker-1", NOW,
                    cycle_id="cycle-1",
                )
            self.assertEqual(result.claimed, 0)
            self.assertEqual(reconcile.call_count, 1)
            self.assertEqual(reconcile.call_args.kwargs["cycle_id"], "cycle-1")

    @staticmethod
    def _issue(
        scope: str,
        severity: str,
        seen_at: str,
    ) -> dict[str, object]:
        return {
            "account_scope_id": scope,
            "issue_key": f"scope:{scope}:broker:offline",
            "object_type": "broker",
            "object_id": "offline",
            "state": "BROKER_OFFLINE",
            "severity": severity,
            "stage_started_at": "2026-07-27T09:31:00+08:00",
            "seen_at": seen_at,
            "details": {"error_code": "BROKER_OFFLINE"},
        }

    @staticmethod
    def _mark_issue_transitions_sent(store: TradingStore, now: str) -> None:
        with store.transaction() as conn:
            conn.execute(
                """UPDATE notification_outbox SET state='sent', sent_at=?,
                   terminal_at=? WHERE event_type='issue_transition'
                     AND state='pending'""",
                (now, now),
            )

    def test_critical_reminders_use_180_boundaries_and_dead_does_not_reset(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                opened = store.upsert_execution_issue(
                    conn,
                    self._issue(
                        scope, "CRITICAL", "2026-07-27T09:31:00+08:00",
                    ),
                )
            self._mark_issue_transitions_sent(
                store, "2026-07-27T09:31:00+08:00",
            )

            first = run_once(
                store,
                FakeTransport(DeliveryResult(
                    False, error_code="HTTP_400", permanent=True,
                )),
                "worker-1",
                "2026-07-27T14:01:00+08:00",
                calendar=set(),
            )
            second = run_once(
                store,
                FakeTransport(DeliveryResult(True)),
                "worker-2",
                "2026-07-28T13:01:00+08:00",
                calendar=set(),
            )
            replay = run_once(
                store,
                FakeTransport(DeliveryResult(True)),
                "worker-3",
                "2026-07-28T13:01:00+08:00",
                calendar=set(),
            )

            self.assertEqual((first.claimed, first.dead), (1, 1))
            self.assertEqual((second.claimed, second.sent), (1, 1))
            self.assertEqual(replay.claimed, 0)
            with store.connect() as conn:
                reminders = conn.execute(
                    """SELECT event_key, state FROM notification_outbox
                       WHERE event_type='issue_reminder' ORDER BY event_key"""
                ).fetchall()
                issue = conn.execute(
                    """SELECT critical_trading_minutes, next_reminder_seq
                       FROM execution_issue_state WHERE issue_key=?""",
                    (f"scope:{scope}:broker:offline",),
                ).fetchone()
            self.assertEqual(
                [row["event_key"] for row in reminders],
                [
                    notification_event_key(
                        "joinquant", scope, "issue-reminder",
                        issue_key=f"scope:{scope}:broker:offline",
                        incident_id=opened["incident_id"], reminder_seq=1,
                    ),
                    notification_event_key(
                        "joinquant", scope, "issue-reminder",
                        issue_key=f"scope:{scope}:broker:offline",
                        incident_id=opened["incident_id"], reminder_seq=2,
                    ),
                ],
            )
            self.assertEqual([row["state"] for row in reminders], ["dead", "sent"])
            self.assertEqual(tuple(issue), (360, 3))

    def test_two_workers_send_one_due_critical_reminder_once(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                store.upsert_execution_issue(
                    conn,
                    self._issue(
                        scope, "CRITICAL", "2026-07-27T09:31:00+08:00",
                    ),
                )
            self._mark_issue_transitions_sent(
                store, "2026-07-27T09:31:00+08:00",
            )
            transport = FakeTransport(
                DeliveryResult(True), DeliveryResult(True),
            )

            def work(worker: str):
                return run_once(
                    store,
                    transport,
                    worker,
                    "2026-07-27T14:01:00+08:00",
                    calendar=set(),
                    cycle_id=f"cycle-{worker}",
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(work, ("worker-1", "worker-2")))

            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(sum(result.sent for result in results), 1)
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    """SELECT COUNT(*) FROM notification_outbox
                       WHERE event_type='issue_reminder' AND state='sent'""",
                ).fetchone()[0], 1)

    def test_critical_downgrade_pauses_and_reescalation_continues(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                store.upsert_execution_issue(
                    conn,
                    self._issue(
                        scope, "CRITICAL", "2026-07-27T09:30:00+08:00",
                    ),
                )
            with store.transaction() as conn:
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T10:30:00+08:00", set(),
                ), 0)
                store.upsert_execution_issue(
                    conn,
                    self._issue(
                        scope, "ERROR", "2026-07-27T10:30:00+08:00",
                    ),
                )
            with store.transaction() as conn:
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T14:00:00+08:00", set(),
                ), 0)
                store.upsert_execution_issue(
                    conn,
                    self._issue(
                        scope, "CRITICAL", "2026-07-27T14:00:00+08:00",
                    ),
                )
            with store.transaction() as conn:
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-28T10:30:00+08:00", set(),
                ), 1)

            with store.connect() as conn:
                issue = conn.execute(
                    """SELECT critical_trading_minutes,
                              critical_last_counted_minute, next_reminder_seq
                       FROM execution_issue_state WHERE issue_key=?""",
                    (f"scope:{scope}:broker:offline",),
                ).fetchone()
                reminders = conn.execute(
                    """SELECT COUNT(*) FROM notification_outbox
                       WHERE event_type='issue_reminder'"""
                ).fetchone()[0]
            self.assertEqual(issue["critical_trading_minutes"], 180)
            self.assertEqual(issue["next_reminder_seq"], 2)
            self.assertEqual(reminders, 1)

    def test_critical_clock_rollback_never_moves_counted_cursor_backward(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "CRITICAL", "2026-07-27T09:30:00+08:00",
            )
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, issue)
                store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T10:30:00+08:00", set(),
                )
            with store.transaction() as conn:
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T10:00:00+08:00", set(),
                ), 0)
            with store.connect() as conn:
                state = conn.execute(
                    """SELECT critical_trading_minutes,
                              critical_last_counted_minute
                       FROM execution_issue_state WHERE issue_key=?""",
                    (issue["issue_key"],),
                ).fetchone()
            self.assertEqual(state["critical_trading_minutes"], 60)
            self.assertEqual(
                state["critical_last_counted_minute"],
                "2026-07-27T10:30:00+08:00",
            )

    def test_clock_rollback_across_downgrade_and_reescalation_keeps_cursor(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "CRITICAL", "2026-07-27T09:30:00+08:00",
            )
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, issue)
                store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T10:30:00+08:00", set(),
                )
                store.upsert_execution_issue(conn, {
                    **issue,
                    "severity": "ERROR",
                    "seen_at": "2026-07-27T10:00:00+08:00",
                })
                store.upsert_execution_issue(conn, {
                    **issue,
                    "severity": "CRITICAL",
                    "seen_at": "2026-07-27T10:05:00+08:00",
                })
                store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T10:30:00+08:00", set(),
                )
            with store.connect() as conn:
                state = conn.execute(
                    """SELECT critical_trading_minutes,
                              critical_last_counted_minute
                       FROM execution_issue_state WHERE issue_key=?""",
                    (issue["issue_key"],),
                ).fetchone()
            self.assertEqual(state["critical_trading_minutes"], 60)
            self.assertEqual(
                state["critical_last_counted_minute"],
                "2026-07-27T10:30:00+08:00",
            )

    def test_worker_restart_skips_obsolete_reminder_boundaries_without_burst(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "CRITICAL", "2026-07-27T09:30:00+08:00",
            )
            with store.transaction() as conn:
                opened = store.upsert_execution_issue(conn, issue)
                generated = store.enqueue_due_critical_reminders(
                    conn, "2026-07-29T10:30:00+08:00", set(),
                )
            self.assertEqual(generated, 1)
            with store.connect() as conn:
                reminders = conn.execute(
                    """SELECT event_key FROM notification_outbox
                       WHERE event_type='issue_reminder'""",
                ).fetchall()
                next_seq = conn.execute(
                    """SELECT next_reminder_seq FROM execution_issue_state
                       WHERE issue_key=?""",
                    (issue["issue_key"],),
                ).fetchone()[0]
            self.assertEqual(len(reminders), 1)
            self.assertEqual(
                reminders[0]["event_key"],
                notification_event_key(
                    "joinquant", scope, "issue-reminder",
                    issue_key=issue["issue_key"],
                    incident_id=opened["incident_id"], reminder_seq=3,
                ),
            )
            self.assertEqual(next_seq, 4)

    def test_newer_due_reminder_requests_cancel_for_pending_lower_sequence(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "CRITICAL", "2026-07-27T09:30:00+08:00",
            )
            with store.transaction() as conn:
                opened = store.upsert_execution_issue(conn, issue)
                conn.execute(
                    """UPDATE execution_issue_state SET
                       critical_trading_minutes=180,
                       critical_last_counted_minute='2026-07-27T14:00:00+08:00',
                       next_reminder_seq=1 WHERE issue_key=?""",
                    (issue["issue_key"],),
                )
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T14:00:00+08:00", set(),
                ), 1)
                conn.execute(
                    """UPDATE execution_issue_state SET
                       critical_trading_minutes=540,
                       critical_last_counted_minute='2026-07-29T10:30:00+08:00',
                       next_reminder_seq=2 WHERE issue_key=?""",
                    (issue["issue_key"],),
                )
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-29T10:30:00+08:00", set(),
                ), 1)
                rows = conn.execute(
                    """SELECT event_key, cancel_requested_at
                       FROM notification_outbox
                       WHERE event_type='issue_reminder' ORDER BY event_key""",
                ).fetchall()
            seq1 = notification_event_key(
                "joinquant", scope, "issue-reminder",
                issue_key=issue["issue_key"],
                incident_id=opened["incident_id"], reminder_seq=1,
            )
            seq3 = notification_event_key(
                "joinquant", scope, "issue-reminder",
                issue_key=issue["issue_key"],
                incident_id=opened["incident_id"], reminder_seq=3,
            )
            self.assertEqual([row["event_key"] for row in rows], [seq1, seq3])
            self.assertTrue(rows[0]["cancel_requested_at"])
            self.assertIsNone(rows[1]["cancel_requested_at"])

    def test_reminder_crossing_market_close_is_deferred_without_attempt(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "CRITICAL", "2026-07-28T13:00:00+08:00",
            )
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, issue)
                conn.execute(
                    """UPDATE execution_issue_state SET
                       critical_trading_minutes=180,
                       critical_last_counted_minute='2026-07-28T14:59:00+08:00',
                       next_reminder_seq=1 WHERE issue_key=?""",
                    (issue["issue_key"],),
                )
            self._mark_issue_transitions_sent(
                store, "2026-07-28T14:59:00+08:00",
            )
            transport = FakeTransport()
            with patch(
                "notification_worker.time.monotonic",
                side_effect=[100.0, 100.0, 100.0, 100.0, 101.0],
            ):
                result = run_once(
                    store,
                    transport,
                    "worker-close",
                    "2026-07-28T14:59:59.500000+08:00",
                    calendar=set(),
                )
            with store.connect() as conn:
                reminder = conn.execute(
                    """SELECT state, attempt_count FROM notification_outbox
                       WHERE event_type='issue_reminder'""",
                ).fetchone()
            self.assertEqual((result.claimed, result.skipped), (1, 1))
            self.assertEqual(transport.calls, [])
            self.assertEqual(tuple(reminder), ("pending", 0))

    def test_critical_downgrade_excludes_explicit_paused_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "CRITICAL", "2026-07-27T09:30:00+08:00",
            )
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, issue)
                store.upsert_execution_issue(
                    conn,
                    {
                        **issue,
                        "severity": "ERROR",
                        "seen_at": "2026-07-27T10:30:00+08:00",
                    },
                    calendar=set(),
                    paused_intervals=((
                        "2026-07-27T10:00:00+08:00",
                        "2026-07-27T10:30:00+08:00",
                    ),),
                )
            with store.connect() as conn:
                minutes = conn.execute(
                    """SELECT critical_trading_minutes
                       FROM execution_issue_state WHERE issue_key=?""",
                    (issue["issue_key"],),
                ).fetchone()[0]
            self.assertEqual(minutes, 30)

    def test_error_never_repeats_and_recovery_cancels_pending_reminder(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "ERROR", "2026-07-27T09:30:00+08:00",
            )
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, issue)
            self._mark_issue_transitions_sent(
                store, "2026-07-27T09:30:00+08:00",
            )
            result = run_once(
                store, FakeTransport(), "worker-1",
                "2026-07-29T10:30:00+08:00", calendar=set(),
            )
            self.assertEqual(result.claimed, 0)

            with store.transaction() as conn:
                store.upsert_execution_issue(conn, {
                    **issue,
                    "severity": "CRITICAL",
                    "seen_at": "2026-07-29T10:30:00+08:00",
                })
                conn.execute(
                    """UPDATE execution_issue_state SET
                       critical_trading_minutes=180, next_reminder_seq=1
                       WHERE issue_key=?""",
                    (issue["issue_key"],),
                )
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-29T10:30:00+08:00", set(),
                ), 1)
            with store.transaction() as conn:
                recovered = store.recover_execution_issue(
                    conn, str(issue["issue_key"]),
                    "2026-07-29T10:31:00+08:00",
                    account_scope_id=scope,
                )
            with store.transaction() as conn:
                reopened = store.upsert_execution_issue(conn, {
                    **issue,
                    "severity": "CRITICAL",
                    "seen_at": "2026-07-29T10:32:00+08:00",
                })

            self.assertNotEqual(recovered["incident_id"], reopened["incident_id"])
            with store.connect() as conn:
                pending_reminder = conn.execute(
                    """SELECT cancel_requested_at FROM notification_outbox
                       WHERE event_type='issue_reminder'"""
                ).fetchone()
                state = conn.execute(
                    """SELECT critical_trading_minutes, next_reminder_seq
                       FROM execution_issue_state WHERE issue_key=?""",
                    (issue["issue_key"],),
                ).fetchone()
            self.assertIsNotNone(pending_reminder["cancel_requested_at"])
            self.assertEqual(tuple(state), (0, 1))

    def test_pending_critical_reminder_waits_through_lunch_and_overnight(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "CRITICAL", "2026-07-27T09:30:00+08:00",
            )
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, issue)
                conn.execute(
                    """UPDATE execution_issue_state SET
                       critical_trading_minutes=180, next_reminder_seq=1
                       WHERE issue_key=?""",
                    (issue["issue_key"],),
                )
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T10:30:00+08:00", set(),
                ), 1)
                conn.execute(
                    """UPDATE execution_issue_state SET
                       critical_trading_minutes=180,
                       critical_last_counted_minute='2026-07-27T15:00:00+08:00'
                       WHERE issue_key=?""",
                    (issue["issue_key"],),
                )
            self._mark_issue_transitions_sent(
                store, "2026-07-27T10:30:00+08:00",
            )
            transport = FakeTransport(DeliveryResult(True))

            paused = run_once(
                store, transport, "worker-paused",
                "2026-07-27T10:35:00+08:00", calendar=set(),
                paused_intervals=((
                    "2026-07-27T10:00:00+08:00",
                    "2026-07-27T11:00:00+08:00",
                ),),
            )
            lunch = run_once(
                store, transport, "worker-lunch",
                "2026-07-27T12:00:00+08:00", calendar=set(),
            )
            overnight = run_once(
                store, transport, "worker-night",
                "2026-07-27T15:05:00+08:00", calendar=set(),
            )
            reopened = run_once(
                store, transport, "worker-open",
                "2026-07-28T09:30:00+08:00", calendar=set(),
            )

            self.assertEqual(
                (paused.claimed, lunch.claimed, overnight.claimed),
                (0, 0, 0),
            )
            self.assertEqual((reopened.claimed, reopened.sent), (1, 1))
            self.assertEqual(len(transport.calls), 1)

    def test_pending_reminder_is_cancelled_while_incident_is_downgraded(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            issue = self._issue(
                scope, "CRITICAL", "2026-07-27T09:30:00+08:00",
            )
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, issue)
                conn.execute(
                    """UPDATE execution_issue_state SET
                       critical_trading_minutes=180, next_reminder_seq=1
                       WHERE issue_key=?""",
                    (issue["issue_key"],),
                )
                self.assertEqual(store.enqueue_due_critical_reminders(
                    conn, "2026-07-27T10:30:00+08:00", set(),
                ), 1)
                store.upsert_execution_issue(conn, {
                    **issue,
                    "severity": "ERROR",
                    "seen_at": "2026-07-27T10:31:00+08:00",
                })
            self._mark_issue_transitions_sent(
                store, "2026-07-27T10:31:00+08:00",
            )
            transport = FakeTransport(DeliveryResult(True))

            result = run_once(
                store, transport, "worker-1",
                "2026-07-27T10:35:00+08:00", calendar=set(),
            )

            self.assertEqual((result.claimed, result.cancelled), (1, 1))
            self.assertEqual(transport.calls, [])

    def test_success_records_one_attempt_and_explicit_times(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)
            transport = FakeTransport(DeliveryResult(True))

            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual((result.claimed, result.sent), (1, 1))
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "sent")
            self.assertEqual(row.attempt_count, 1)
            self.assertEqual(row.sent_at, NOW)
            self.assertEqual(len(transport.calls), 1)
            _, body, sent_at = transport.calls[0]
            self.assertEqual(body.count("业务时间："), 1)
            self.assertIn("2026-07-28T09:59:00+08:00", body)
            self.assertEqual(sent_at, NOW)

    def test_current_buy_plan_is_sent(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(
                scope, event_type="buy-plan", plan_version="plan-v1",
                expires_at="2026-07-28T07:00:00+00:00",
            )
            with store.transaction() as conn:
                store.upsert_logical_signal_plan(
                    conn, scope, "2026-07-28", "logical-1",
                    "2026-07-28T07:00:00+00:00", "plan-v1", NOW,
                )
                store.enqueue_notification(conn, event, NOW)
            transport = FakeTransport(DeliveryResult(True))

            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual((result.claimed, result.sent), (1, 1))
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(store.get_notification(event.event_key).state, "sent")

    def test_due_high_priority_is_claimed_before_older_normal_event(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            normal = self._event(scope, suffix="aaa", priority="normal")
            high = self._event(scope, suffix="zzz", priority="high")
            self._enqueue(store, normal, "2026-07-28T01:59:00+00:00")
            self._enqueue(store, high)

            claimed = store.claim_notifications(
                "worker-1", NOW, limit=2,
            )

            self.assertEqual(
                [row.event_key for row in claimed],
                [high.event_key, normal.event_key],
            )

    def test_cancelled_expired_and_replaced_rows_never_call_http(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            cancelled = self._event(scope, suffix="cancel")
            expired = self._event(
                scope, suffix="expired",
                expires_at="2026-07-28T01:59:59+00:00",
            )
            replaced = self._event(
                scope, event_type="buy-plan", plan_version="old-plan",
                expires_at="2026-07-28T07:00:00+00:00",
            )
            with store.transaction() as conn:
                store.enqueue_notification(conn, cancelled, NOW)
                store.enqueue_notification(conn, expired, NOW)
                store.enqueue_notification(conn, replaced, NOW)
                store.request_notification_cancel(
                    conn, cancelled.event_key, NOW, "source recovered",
                )
                store.upsert_logical_signal_plan(
                    conn, scope, "2026-07-28", "logical-1",
                    "2026-07-28T07:00:00+00:00", "new-plan", NOW,
                )
            transport = FakeTransport()

            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual(result.cancelled, 3)
            self.assertEqual(transport.calls, [])
            for event in (cancelled, expired, replaced):
                self.assertEqual(
                    store.get_notification(event.event_key).state,
                    "cancelled",
                )

    def test_temporary_failure_retries_and_fifth_failure_is_dead(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            retry = self._event(scope, suffix="retry")
            fifth = self._event(scope, suffix="fifth")
            self._enqueue(store, retry)
            self._enqueue(store, fifth)
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE notification_outbox SET attempt_count=4 WHERE event_key=?",
                    (fifth.event_key,),
                )
            failure = DeliveryResult(
                False, error="service unavailable", error_kind="temporary",
                error_code="HTTP_503",
            )
            transport = FakeTransport(failure, failure)

            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual((result.retried, result.dead), (1, 1))
            retry_row = store.get_notification(retry.event_key)
            self.assertEqual(retry_row.state, "pending")
            self.assertEqual(retry_row.attempt_count, 1)
            self.assertEqual(retry_row.next_attempt_at, RETRY_AT)
            fifth_row = store.get_notification(fifth.event_key)
            self.assertEqual(fifth_row.state, "dead")
            self.assertEqual(fifth_row.attempt_count, 5)

    def test_permanent_error_is_dead_on_first_attempt(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)
            transport = FakeTransport(DeliveryResult(
                False, error="errcode=40058", error_kind="permanent",
                error_code="WECOM_40058", permanent=True,
            ))

            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual(result.dead, 1)
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "dead")
            self.assertEqual(row.attempt_count, 1)
            self.assertEqual(row.last_error_code, "WECOM_40058")

    def test_response_loss_is_persistent_but_never_stores_transport_secret(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)
            lost = DeliveryResult(
                False,
                error="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET",
                error_kind="temporary",
                error_code="RESPONSE_LOST",
                ambiguous=True,
            )

            first = run_once(
                store, FakeTransport(lost), "worker-1", NOW,
            )
            self.assertEqual(first.ambiguous, 1)
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "pending")
            self.assertEqual(row.last_error_code, "AMBIGUOUS_DELIVERY")
            self.assertNotIn("SECRET", row.last_error or "")
            self.assertEqual(row.ambiguous_attempt_mask, 1)
            self.assertEqual(row.last_ambiguous_code, "RESPONSE_LOST")

            second = run_once(
                store,
                FakeTransport(DeliveryResult(
                    False, error="unavailable", error_kind="temporary",
                    error_code="HTTP_503",
                )),
                "worker-2", RETRY_AT,
            )
            self.assertEqual(second.retried, 1)
            row = store.get_notification(event.event_key)
            self.assertEqual(row.last_error_code, "HTTP_503")
            self.assertEqual(row.ambiguous_attempt_mask, 1)
            self.assertEqual(row.last_ambiguous_code, "RESPONSE_LOST")

            third = run_once(
                store, FakeTransport(DeliveryResult(True)),
                "worker-3", SECOND_RETRY_AT,
            )
            self.assertEqual(third.sent, 1)
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "sent")
            self.assertIsNone(row.last_error_code)
            self.assertEqual(row.ambiguous_attempt_mask, 1)
            self.assertEqual(row.last_ambiguous_code, "RESPONSE_LOST")

    def test_http_success_with_lost_completion_cas_records_ambiguity(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)
            with patch.object(store, "complete_notification", return_value=False):
                result = run_once(
                    store, FakeTransport(DeliveryResult(True)),
                    "worker-1", NOW,
                )

            self.assertEqual(result.ambiguous, 1)
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "leased")
            self.assertEqual(row.ambiguous_attempt_mask, 1)
            self.assertEqual(
                row.last_ambiguous_code,
                "SUCCESS_COMPLETION_CAS_LOST",
            )
            self.assertTrue(store.record_notification_ambiguity(
                event.event_key,
                RETRY_AT,
                "DUPLICATE_STALE_CALLBACK",
                1,
            ))
            row = store.get_notification(event.event_key)
            self.assertEqual(row.ambiguous_attempt_mask, 1)
            self.assertEqual(
                row.last_ambiguous_code,
                "SUCCESS_COMPLETION_CAS_LOST",
            )

    def test_corrupted_semantic_content_is_dead_without_http(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE notification_outbox SET body='tampered' WHERE event_key=?",
                    (event.event_key,),
                )
            transport = FakeTransport()

            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual(result.dead, 1)
            self.assertEqual(transport.calls, [])
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "dead")
            self.assertEqual(row.attempt_count, 0)
            self.assertEqual(row.last_error_code, "CONTENT_HASH_MISMATCH")

    def test_attempt_limit_and_corrupt_json_never_block_later_work(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            exhausted = self._event(scope, suffix="exhausted")
            corrupt = self._event(scope, suffix="corrupt")
            healthy = self._event(scope, suffix="healthy")
            with store.transaction() as conn:
                for event in (exhausted, corrupt, healthy):
                    store.enqueue_notification(conn, event, NOW)
                conn.execute(
                    "UPDATE notification_outbox SET attempt_count=5 WHERE event_key=?",
                    (exhausted.event_key,),
                )
                conn.execute(
                    "UPDATE notification_outbox SET payload_json='{' WHERE event_key=?",
                    (corrupt.event_key,),
                )
            transport = FakeTransport(DeliveryResult(True))

            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual(result.sent, 1)
            self.assertEqual(result.dead, 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(store.get_notification(exhausted.event_key).state, "dead")
            with store.connect() as conn:
                corrupt_row = conn.execute(
                    "SELECT state, last_error_code FROM notification_outbox "
                    "WHERE event_key=?",
                    (corrupt.event_key,),
                ).fetchone()
            self.assertEqual(tuple(corrupt_row), ("dead", "CONTENT_DECODE_ERROR"))
            self.assertEqual(store.get_notification(healthy.event_key).state, "sent")

    def test_nan_and_invalid_expiry_are_dead_without_blocking_batch(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            nan_event = self._event(scope, suffix="aaa-nan")
            time_event = self._event(scope, suffix="aab-time")
            healthy = self._event(scope, suffix="zzz-healthy")
            with store.transaction() as conn:
                for event in (nan_event, time_event, healthy):
                    store.enqueue_notification(conn, event, NOW)
                conn.execute(
                    "UPDATE notification_outbox SET payload_json=? WHERE event_key=?",
                    ('{"x":NaN}', nan_event.event_key),
                )
                conn.execute(
                    "UPDATE notification_outbox SET expires_at='invalid' WHERE event_key=?",
                    (time_event.event_key,),
                )

            transport = FakeTransport(DeliveryResult(True))
            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual((result.dead, result.sent), (1, 1))
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(store.get_notification(nan_event.event_key).state, "dead")
            self.assertEqual(store.get_notification(time_event.event_key).state, "dead")
            self.assertEqual(store.get_notification(healthy.event_key).state, "sent")

    def test_deep_json_is_dead_without_blocking_later_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            deep = self._event(scope, suffix="aaa-deep")
            healthy = self._event(scope, suffix="zzz-healthy")
            with store.transaction() as conn:
                store.enqueue_notification(conn, deep, NOW)
                store.enqueue_notification(conn, healthy, NOW)
                nested = '{"x":' + "[" * 1200 + "0" + "]" * 1200 + "}"
                conn.execute(
                    "UPDATE notification_outbox SET payload_json=? WHERE event_key=?",
                    (nested, deep.event_key),
                )

            transport = FakeTransport(DeliveryResult(True))
            result = run_once(store, transport, "worker-1", NOW)

            self.assertEqual(result.sent, 1)
            self.assertEqual(len(transport.calls), 1)
            with store.connect() as conn:
                state = conn.execute(
                    "SELECT state, last_error_code FROM notification_outbox "
                    "WHERE event_key=?",
                    (deep.event_key,),
                ).fetchone()
            self.assertEqual(tuple(state), ("dead", "CONTENT_DECODE_ERROR"))
            self.assertEqual(store.get_notification(healthy.event_key).state, "sent")

    def test_begin_attempt_rechecks_ttl_atomically(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(
                scope, expires_at="2026-07-28T02:30:00+00:00",
            )
            self._enqueue(store, event)
            lease = store.claim_notifications(
                "worker-1", NOW,
            )[0]
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE notification_outbox SET expires_at=? WHERE event_key=?",
                    (NOW, event.event_key),
                )

            self.assertIsNone(store.begin_notification_attempt(
                event.event_key, "worker-1", lease.lease_until, NOW,
            ))
            self.assertEqual(
                store.get_notification(event.event_key).attempt_count,
                0,
            )

    def test_cancel_requested_during_http_is_sent_with_sticky_ambiguity(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)

            class CancellingTransport:
                def deliver_markdown(self, title: str, body: str, sent_at: str) -> DeliveryResult:
                    with store.transaction() as conn:
                        store.request_notification_cancel(
                            conn, event.event_key, NOW, "source recovered",
                        )
                    return DeliveryResult(True)

            result = run_once(store, CancellingTransport(), "worker-1", NOW)

            self.assertEqual((result.sent, result.ambiguous), (1, 1))
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "sent")
            self.assertEqual(row.ambiguous_attempt_mask, 1)
            self.assertEqual(row.last_ambiguous_code, "CANCEL_RACE_AFTER_HTTP")

    def test_stale_success_callback_cannot_complete_a_reclaimed_lease(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)

            class ReclaimingTransport:
                def deliver_markdown(self, title: str, body: str, sent_at: str) -> DeliveryResult:
                    reclaimed = store.claim_notifications(
                        "worker-2",
                        "2026-07-28T02:01:01+00:00",
                        lease_seconds=60,
                    )
                    self.assert_reclaimed = len(reclaimed)
                    return DeliveryResult(True)

            transport = ReclaimingTransport()
            result = run_once(
                store, transport, "worker-1", NOW, lease_seconds=60,
            )

            self.assertEqual(transport.assert_reclaimed, 1)
            self.assertEqual((result.sent, result.ambiguous), (0, 1))
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "leased")
            self.assertEqual(row.lease_owner, "worker-2")
            self.assertEqual(row.ambiguous_attempt_mask, 1)

    def test_http_crossing_ttl_or_lease_uses_refreshed_completion_time(self) -> None:
        class MonotonicClock:
            advanced = False

            def __init__(self, elapsed: float = 2.0) -> None:
                self.elapsed = elapsed

            def __call__(self) -> float:
                return 100.0 + self.elapsed if self.advanced else 100.0

        class AdvancingTransport:
            def __init__(self, clock: MonotonicClock) -> None:
                self.clock = clock

            def deliver_markdown(self, title: str, body: str, sent_at: str) -> DeliveryResult:
                self.clock.advanced = True
                return DeliveryResult(True)

        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(
                scope, expires_at="2026-07-28T02:00:01+00:00",
            )
            self._enqueue(store, event)
            clock = MonotonicClock()
            with patch("notification_worker.time.monotonic", clock):
                result = run_once(
                    store, AdvancingTransport(clock), "worker-1", NOW,
                    lease_seconds=10,
                )
            self.assertEqual((result.sent, result.ambiguous), (1, 1))
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "sent")
            self.assertEqual(row.sent_at, NOW)
            self.assertEqual(row.last_ambiguous_code, "EXPIRY_RACE_AFTER_HTTP")

        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)
            clock = MonotonicClock()
            with patch("notification_worker.time.monotonic", clock):
                result = run_once(
                    store, AdvancingTransport(clock), "worker-1", NOW,
                    lease_seconds=1,
                )
            self.assertEqual((result.sent, result.ambiguous), (0, 1))
            row = store.get_notification(event.event_key)
            self.assertEqual(row.state, "leased")
            self.assertEqual(row.last_ambiguous_code, "LEASE_EXPIRED_AFTER_HTTP")

        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(
                scope, expires_at="2026-07-28T02:00:00.250000+00:00",
            )
            self._enqueue(store, event)
            clock = MonotonicClock(0.6)
            with patch("notification_worker.time.monotonic", clock):
                result = run_once(
                    store, AdvancingTransport(clock), "worker-1", NOW,
                    lease_seconds=10,
                )
            self.assertEqual((result.sent, result.ambiguous), (1, 1))
            row = store.get_notification(event.event_key)
            self.assertEqual(row.last_ambiguous_code, "EXPIRY_RACE_AFTER_HTTP")

    def test_out_of_order_ambiguity_does_not_move_last_evidence_backward(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            self._enqueue(store, event)
            first = store.claim_notifications("worker-1", NOW)[0]
            self.assertEqual(store.begin_notification_attempt(
                event.event_key, "worker-1", first.lease_until, NOW,
            ), 1)
            self.assertTrue(store.fail_notification(
                event.event_key,
                "worker-1",
                NOW,
                "HTTP_503",
                "temporary",
                expected_lease_until=first.lease_until,
                retry_at=RETRY_AT,
            ))
            second = store.claim_notifications("worker-2", RETRY_AT)[0]
            self.assertEqual(store.begin_notification_attempt(
                event.event_key, "worker-2", second.lease_until, RETRY_AT,
            ), 2)
            self.assertTrue(store.record_notification_ambiguity(
                event.event_key, SECOND_RETRY_AT, "ATTEMPT_2", 2,
            ))
            self.assertTrue(store.record_notification_ambiguity(
                event.event_key, RETRY_AT, "ATTEMPT_1_STALE", 1,
            ))

            row = store.get_notification(event.event_key)
            self.assertEqual(row.ambiguous_attempt_mask, 3)
            self.assertEqual(row.last_ambiguous_at, SECOND_RETRY_AT)
            self.assertEqual(row.last_ambiguous_code, "ATTEMPT_2")

    def test_legacy_audit_is_explicit_once_read_only_and_non_sendable(self) -> None:
        with TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            state_file = Path(tmp) / "state.json"
            queue_file = Path(tmp) / "queue.jsonl"
            state_file.write_text(json.dumps({
                "sent": {"old-token-shaped-key": 1_700_000_000},
            }), encoding="utf-8")
            queue_file.write_text("\n".join((
                json.dumps({
                    "title": "contains SECRET", "content": "old body",
                    "error": "errcode=40058", "attempt_count": 1,
                }),
                json.dumps({
                    "title": "unproven", "content": "still private",
                    "attempt_count": 1,
                }),
                "not-json",
            )) + "\n", encoding="utf-8")
            before = (
                hashlib.sha256(state_file.read_bytes()).hexdigest(),
                hashlib.sha256(queue_file.read_bytes()).hexdigest(),
            )

            preview = legacy_audit(
                store, state_file, queue_file, NOW, dry_run=True,
            )
            self.assertEqual(preview.code, "LEGACY_AUDIT_DRY_RUN")
            self.assertFalse(preview.completed)
            self.assertEqual(store.get_system_state(
                "notification_legacy_audit_v1", "",
            ), "")

            first = legacy_audit(store, state_file, queue_file, NOW)
            second = legacy_audit(store, state_file, queue_file, RETRY_AT)
            self.assertTrue(first.completed)
            self.assertEqual(second.code, "LEGACY_AUDIT_ALREADY_COMPLETED")
            self.assertEqual((first.sent, first.dead, first.cancelled), (1, 1, 2))
            self.assertEqual(first.imported, 0)
            self.assertEqual(before, (
                hashlib.sha256(state_file.read_bytes()).hexdigest(),
                hashlib.sha256(queue_file.read_bytes()).hexdigest(),
            ))
            with store.connect() as conn:
                states = dict(conn.execute(
                    "SELECT state, COUNT(*) FROM notification_outbox "
                    "WHERE object_type='legacy_notification_audit' GROUP BY state"
                ).fetchall())
                dump = "\n".join(conn.iterdump())
            self.assertEqual(states, {"cancelled": 2, "dead": 1, "sent": 1})
            self.assertNotIn("SECRET", dump)
            self.assertNotIn("old-token-shaped-key", dump)
            self.assertNotIn("still private", dump)

    def test_legacy_audit_final_dead_update_compacts_detail_before_commit(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            state_file = Path(tmp) / "state.json"
            queue_file = Path(tmp) / "queue.jsonl"
            state_file.write_text("{}", encoding="utf-8")
            queue_file.write_text(json.dumps({
                "state": "dead",
                "attempt_count": 5,
                "error": "errcode=40058",
            }) + "\n", encoding="utf-8")
            with store.transaction() as conn:
                conn.executemany(
                    """INSERT INTO notification_outbox(
                       event_key, account_scope_id, adapter, event_type,
                       object_type, object_id, source_fact_id, priority,
                       payload_version, payload_sha256, payload_json, title,
                       body, body_sha256, metadata_json, state, attempt_count,
                       occurred_at, created_at, terminal_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        (
                            f"joinquant:{scope}:control:legacy-cap-{index}",
                            scope, "joinquant", "control", "raw",
                            f"legacy-cap-{index}", f"legacy-cap-{index}",
                            "high", 1, "a" * 64, "{}", "title", "body",
                            "b" * 64, "{}", "dead", 0, NOW, NOW, NOW,
                        )
                        for index in range(1000)
                    ),
                )
            result = legacy_audit(store, state_file, queue_file, NOW)
            self.assertTrue(result.completed)
            capacity = store.notification_capacity()
            self.assertLessEqual(capacity.dead_rows, 1000)
            self.assertLessEqual(capacity.dead_bytes, 4 * 1024 * 1024)
            self.assertEqual(capacity.dead_total_rows, 1001)

    def test_legacy_audit_concurrency_and_failure_are_atomic(self) -> None:
        with TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            state_file = Path(tmp) / "state.json"
            queue_file = Path(tmp) / "queue.jsonl"
            state_file.write_text('{"sent":{"one":1}}', encoding="utf-8")
            queue_file.write_text("", encoding="utf-8")

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _: legacy_audit(
                        store, state_file, queue_file, NOW,
                    ),
                    range(2),
                ))
            self.assertEqual(sum(item.completed for item in results), 1)
            self.assertEqual(
                sum(item.code == "LEGACY_AUDIT_ALREADY_COMPLETED" for item in results),
                1,
            )

        with TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            state_file = Path(tmp) / "state.json"
            queue_file = Path(tmp) / "queue.jsonl"
            state_file.write_text('{"sent":{"one":1}}', encoding="utf-8")
            queue_file.write_text("", encoding="utf-8")
            with patch.object(
                store, "set_system_state", side_effect=RuntimeError("stop"),
            ):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    legacy_audit(store, state_file, queue_file, NOW)
            with store.connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox "
                    "WHERE object_type='legacy_notification_audit'"
                ).fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(store.get_system_state(
                "notification_legacy_audit_v1", "",
            ), "")

    def test_legacy_audit_bounds_oversized_files_and_detail_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            state_file = Path(tmp) / "state.json"
            queue_file = Path(tmp) / "queue.jsonl"
            state_file.write_text(
                json.dumps({"sent": {f"key-{index}": index for index in range(5)}}),
                encoding="utf-8",
            )
            queue_file.write_bytes(b"private-value-" * 20)

            with patch("notification_worker.MAX_LEGACY_FILE_BYTES", 32), patch(
                "notification_worker.MAX_LEGACY_DETAIL_ROWS", 2,
            ):
                result = legacy_audit(
                    store, state_file, queue_file, NOW,
                )

            self.assertTrue(result.completed)
            self.assertEqual(result.imported, 0)
            with store.connect() as conn:
                rows = conn.execute(
                    "SELECT state, last_error_code FROM notification_outbox "
                    "WHERE object_type='legacy_notification_audit'"
                ).fetchall()
                dump = "\n".join(conn.iterdump())
            self.assertLessEqual(len(rows), 2)
            self.assertTrue(all(row[0] == "cancelled" for row in rows))
            self.assertNotIn("private-value", dump)

        with TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            state_file = Path(tmp) / "state.json"
            queue_file = Path(tmp) / "queue.jsonl"
            state_file.write_text(
                json.dumps({"sent": {f"key-{index}": index for index in range(5)}}),
                encoding="utf-8",
            )
            queue_file.write_text("", encoding="utf-8")
            with patch("notification_worker.MAX_LEGACY_DETAIL_ROWS", 2):
                result = legacy_audit(store, state_file, queue_file, NOW)
            self.assertEqual((result.sent, result.cancelled), (1, 1))
            with store.connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox "
                    "WHERE object_type='legacy_notification_audit'"
                ).fetchone()[0]
            self.assertEqual(count, 2)


class NotificationWorkerCliTest(unittest.TestCase):
    @staticmethod
    def _store(tmp: str) -> tuple[TradingStore, str]:
        store = TradingStore(Path(tmp) / "trading.db")
        store.initialize()
        with store.transaction() as conn:
            scope = store.get_or_create_account_scope(
                conn, "joinquant", "primary",
            )
        return store, scope

    @staticmethod
    def _event(scope: str, suffix: str = "cli") -> NotificationEvent:
        return NotificationEvent(
            event_key=notification_event_key(
                "joinquant", scope, "fill", fill_id=f"fill-{suffix}",
            ),
            account_scope_id=scope,
            adapter="joinquant",
            event_type="fill",
            object_type="fill",
            object_id=f"fill-{suffix}",
            source_fact_id=f"fill-{suffix}",
            priority="high",
            payload_version=1,
            occurred_at=NOW,
            expires_at=None,
            title="fill",
            body="body",
            payload={"fill_id": f"fill-{suffix}"},
            metadata={},
        )

    def test_status_reports_operational_outbox_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                store.enqueue_notification(conn, self._event(scope), NOW)

            result = notification_status(store)

            self.assertEqual(result["pending"], 1)
            self.assertEqual(result["leased"], 0)
            self.assertEqual(result["dead"], 0)
            self.assertEqual(result["unresolved_gaps"], 0)
            self.assertEqual(result["tombstones"], 0)

    def test_compact_dry_run_previews_without_mutating_then_apply_compacts(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope, "old")
            with store.transaction() as conn:
                store.enqueue_notification(conn, event, NOW)
                conn.execute(
                    """UPDATE notification_outbox
                       SET state='cancelled', terminal_at='2025-01-01T00:00:00+00:00'
                       WHERE event_key=?""",
                    (event.event_key,),
                )

            preview = compact_notifications(store, "2026-08-01T00:00:00+00:00")
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["changes"]["compacted"], 1)
            self.assertEqual(notification_status(store)["tombstones"], 0)

            applied = compact_notifications(
                store, "2026-08-01T00:00:00+00:00", apply=True,
            )
            self.assertTrue(applied["applied"])
            self.assertEqual(notification_status(store)["tombstones"], 1)

    def test_legacy_cli_defaults_to_dry_run_and_requires_explicit_apply(self) -> None:
        with TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            state_file = Path(tmp) / "state.json"
            queue_file = Path(tmp) / "queue.jsonl"
            state_file.write_text('{"sent":{"one":1}}', encoding="utf-8")
            queue_file.write_text("", encoding="utf-8")
            args = [
                "--legacy-audit", "--db-file", str(store.db_path),
                "--state-file", str(state_file), "--queue-file", str(queue_file),
            ]

            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(args), 0)
            self.assertEqual(
                json.loads(output.getvalue())["code"], "LEGACY_AUDIT_DRY_RUN",
            )
            self.assertEqual(
                store.get_system_state("notification_legacy_audit_v1", ""), "",
            )

            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main([*args, "--apply"]), 0)
            self.assertEqual(
                json.loads(output.getvalue())["code"], "LEGACY_AUDIT_COMPLETED",
            )
            self.assertTrue(
                store.get_system_state("notification_legacy_audit_v1", ""),
            )

    def test_write_failure_resolution_cli_requires_matching_event_key(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope, "resolve")
            with store.transaction() as conn:
                store.set_system_state(
                    conn,
                    "notification_outbox_write_failure",
                    json.dumps({
                        "event_key": event.event_key,
                        "requires_manual_resolution": True,
                    }),
                    "test sticky marker",
                )

            with redirect_stdout(StringIO()) as output:
                with self.assertRaises(ValueError):
                    main([
                        "--resolve-write-failure",
                        "--db-file", str(store.db_path),
                        "--expected-event-key", "wrong",
                        "--reason", "source fact checked",
                    ])
            self.assertEqual(output.getvalue(), "")

            with redirect_stdout(StringIO()) as output:
                self.assertEqual(
                    main([
                        "--resolve-write-failure",
                        "--db-file", str(store.db_path),
                        "--expected-event-key", event.event_key,
                        "--reason", "source fact checked",
                    ]),
                    0,
                )
            self.assertTrue(json.loads(output.getvalue())["resolved"])
            self.assertEqual(
                store.get_system_state("notification_outbox_write_failure"),
                "",
            )

        with TemporaryDirectory() as tmp:
            missing_db = Path(tmp) / "missing.db"
            with redirect_stdout(StringIO()):
                self.assertEqual(main([
                    "--legacy-audit", "--db-file", str(missing_db),
                    "--state-file", str(Path(tmp) / "missing-state.json"),
                    "--queue-file", str(Path(tmp) / "missing-queue.jsonl"),
                ]), 0)
            self.assertFalse(missing_db.exists())

    def test_once_cli_routes_to_worker_without_printing_webhook(self) -> None:
        with TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            with patch("notification_worker.run_once", return_value={"sent": 0}) as run:
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main([
                        "--once", "--db-file", str(store.db_path),
                    ]), 0)
            self.assertEqual(run.call_count, 1)
            rendered = output.getvalue()
            self.assertNotIn("webhook", rendered.lower())
            self.assertNotIn("token", rendered.lower())

    def test_fixture_notification_conservation_counts_each_event_once(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            states = ("pending", "leased", "sent", "dead", "cancelled")
            with store.transaction() as conn:
                for state in states:
                    event = self._event(scope, state)
                    store.enqueue_notification(conn, event, NOW)
                    if state != "pending":
                        conn.execute(
                            "UPDATE notification_outbox SET state=? WHERE event_key=?",
                            (state, event.event_key),
                        )
                gap = NotificationEvent(
                    **{
                        **self._event(scope, "gap").__dict__,
                        "priority": "normal",
                    }
                )
                store.enqueue_notification_gap(
                    conn, gap, "fixture capacity", NOW,
                )
                unique_sources = int(conn.execute(
                    """SELECT COUNT(DISTINCT source_fact_id) FROM (
                         SELECT source_fact_id FROM notification_outbox
                         UNION ALL
                         SELECT source_fact_id FROM notification_enqueue_gaps
                         WHERE resolved_at IS NULL
                       )"""
                ).fetchone()[0])
                delivered_states = int(conn.execute(
                    """SELECT COUNT(*) FROM notification_outbox
                       WHERE state IN ('sent','pending','leased','dead','cancelled')"""
                ).fetchone()[0])
                gaps = int(conn.execute(
                    """SELECT COUNT(*) FROM notification_enqueue_gaps
                       WHERE resolved_at IS NULL"""
                ).fetchone()[0])

            self.assertEqual(unique_sources, delivered_states + gaps)


if __name__ == "__main__":
    unittest.main()
