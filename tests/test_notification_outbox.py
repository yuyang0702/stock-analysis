from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest
from unittest.mock import patch

from execution_contracts import logical_signal_id as contract_logical_signal_id
from notification_outbox import (
    MAX_BODY_BYTES,
    MAX_ID_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_TITLE_BYTES,
    NotificationConflict,
    NotificationCapacityError,
    NotificationEvent,
    critical_trading_minutes,
    event_payload_sha256,
    logical_signal_id,
    next_critical_reminder_seq,
    notification_event_key,
    plan_version,
)
from trading_store import TradingStore


NOW = "2026-07-28T10:00:00+08:00"


class NotificationOutboxTest(unittest.TestCase):
    @staticmethod
    def _event(
        scope: str,
        *,
        payload: dict[str, object] | None = None,
        title: str = "成交回报",
        body: str = "000001 成交 100 股",
        event_key: str | None = None,
        object_id: str = "fill-1",
        source_fact_id: str = "fill-1",
        metadata: dict[str, object] | None = None,
        occurred_at: str = NOW,
        priority: str = "high",
    ) -> NotificationEvent:
        return NotificationEvent(
            event_key=event_key or notification_event_key(
                "joinquant", scope, "fill", fill_id="fill-1",
            ),
            account_scope_id=scope,
            adapter="joinquant",
            event_type="fill",
            object_type="fill",
            object_id=object_id,
            source_fact_id=source_fact_id,
            priority=priority,
            payload_version=1,
            occurred_at=occurred_at,
            expires_at=None,
            title=title,
            body=body,
            payload=payload or {"code": "000001", "qty": 100},
            metadata=metadata or {"renderer": "v1"},
        )

    @staticmethod
    def _raw_rows(
        conn: object,
        scope: str,
        count: int,
        *,
        priority: str,
        state: str = "pending",
        created_at: str = NOW,
    ) -> None:
        conn.executemany(
            """INSERT INTO notification_outbox(
               event_key, account_scope_id, adapter, event_type, object_type,
               object_id, source_fact_id, priority, payload_version,
               payload_sha256, payload_json, title, body, body_sha256,
               metadata_json, state, attempt_count, occurred_at, created_at,
               terminal_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    f"joinquant:{scope}:control:raw-{priority}-{state}-{index}",
                    scope, "joinquant", "control", "raw", f"raw-{index}",
                    f"raw-{index}", priority, 1, "a" * 64, "{}", "t", "b",
                    "b" * 64, "{}", state, 0, created_at, created_at,
                    created_at if state in {"sent", "dead", "cancelled"} else None,
                )
                for index in range(count)
            ),
        )

    @staticmethod
    def _store(tmp: str) -> tuple[TradingStore, str]:
        store = TradingStore(Path(tmp) / "trading.db")
        store.initialize()
        with store.transaction() as conn:
            scope = store.get_or_create_account_scope(
                conn, "joinquant", "primary",
            )
        return store, scope

    def test_stable_identity_helpers_ignore_runtime_transport_data(self) -> None:
        args = (
            "scope", "2026-07-28", "main", "strategy-v1",
            "000001", "buy", "breakout",
        )
        self.assertIs(logical_signal_id, contract_logical_signal_id)
        self.assertEqual(logical_signal_id(*args), logical_signal_id(*args))
        self.assertEqual(len(logical_signal_id(*args)), 20)
        first = plan_version(
            "000001", "buy", 100, "0.25", 1001, 950,
            "2026-07-28T15:00:00+08:00", "strategy-v1", "params-v1",
        )
        second = plan_version(
            "000001", "buy", 100, "0.25", 1001, 950,
            "2026-07-28T15:00:00+08:00", "strategy-v1", "params-v1",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertEqual(
            notification_event_key(
                "joinquant", "scope", "buy-plan",
                trade_date="2026-07-28", logical_signal_id="logical",
                plan_version=first,
            ),
            f"joinquant:scope:buy-plan:2026-07-28:logical:{first}",
        )
        self.assertEqual(
            event_payload_sha256({"qty": 100, "code": "000001"}),
            event_payload_sha256({"code": "000001", "qty": 100}),
        )

    def test_all_event_key_formats_match_the_frozen_contract(self) -> None:
        cases = (
            ("exit", {"position_cycle_id": "pc", "exit_intent_id": "ei", "stage": 1}, "joinquant:scope:exit:pc:ei:1"),
            ("order-terminal", {"client_order_id": "co", "status": "REJECTED", "reason_code": "LIMIT"}, "joinquant:scope:order-terminal:co:REJECTED:LIMIT"),
            ("issue", {"issue_key": "ik", "incident_id": "inc", "transition_seq": 2, "transition": "OPEN", "severity": "CRITICAL"}, "joinquant:scope:issue:ik:inc:2:OPEN:CRITICAL"),
            ("issue-reminder", {"issue_key": "ik", "incident_id": "inc", "reminder_seq": 3}, "joinquant:scope:issue:ik:inc:reminder:3"),
            ("control", {"control_event_id": "ce"}, "joinquant:scope:control:ce"),
            ("pre", {"trade_date": "2026-07-28"}, "joinquant:scope:pre:2026-07-28"),
            ("close", {"trade_date": "2026-07-28"}, "joinquant:scope:close:2026-07-28"),
            ("weekly", {"iso_week": "2026-W31"}, "joinquant:scope:weekly:2026-W31"),
        )
        for event_type, parts, expected in cases:
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    notification_event_key(
                        "joinquant", "scope", event_type, **parts,
                    ),
                    expected,
                )

    def test_critical_minutes_use_a_share_sessions_and_pause_intervals(self) -> None:
        self.assertEqual(
            critical_trading_minutes(
                "2026-07-27T09:31:00+08:00",
                "2026-07-27T14:01:00+08:00",
                set(),
            ),
            180,
        )
        self.assertEqual(
            critical_trading_minutes(
                "2026-07-27T09:31:00+08:00",
                "2026-07-28T14:01:00+08:00",
                set(),
                paused_intervals=((
                    "2026-07-27T10:00:00+08:00",
                    "2026-07-27T10:30:00+08:00",
                ),),
            ),
            390,
        )

    def test_critical_minutes_exclude_weekends_and_configured_holidays(self) -> None:
        self.assertEqual(
            critical_trading_minutes(
                "2026-07-24T13:30:00+08:00",
                "2026-07-28T10:30:00+08:00",
                {"2026-07-27"},
            ),
            150,
        )
        self.assertEqual(next_critical_reminder_seq(179), 0)
        self.assertEqual(next_critical_reminder_seq(180), 1)
        self.assertEqual(next_critical_reminder_seq(360), 2)

    def test_same_payload_is_idempotent_and_different_payload_conflicts(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                first = store.enqueue_notification(
                    conn, self._event(scope), NOW,
                )
                duplicate = store.enqueue_notification(
                    conn, self._event(scope), NOW,
                )
                with self.assertRaises(NotificationConflict):
                    store.enqueue_notification(
                        conn, self._event(scope, payload={"qty": 200}), NOW,
                    )
            self.assertTrue(first.inserted)
            self.assertFalse(duplicate.inserted)
            self.assertEqual(first.payload_sha256, duplicate.payload_sha256)

    def test_same_key_with_different_rendered_semantics_conflicts_after_compaction(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(
                scope,
                payload={"nested": {"qty": [100]}},
            )
            with self.assertRaises(TypeError):
                event.payload["nested"]["qty"][0] = 200
            with store.transaction() as conn:
                store.enqueue_notification(conn, event, NOW)
                with self.assertRaises(NotificationConflict):
                    store.enqueue_notification(
                        conn, self._event(
                            scope,
                            payload={"nested": {"qty": [100]}},
                            body="000001 成交 200 股",
                        ), NOW,
                    )
                conn.execute(
                    "UPDATE notification_outbox SET state='sent', sent_at=?, terminal_at=? WHERE event_key=?",
                    (NOW, NOW, event.event_key),
                )
            self.assertTrue(store.compact_notification(event.event_key))
            with store.transaction() as conn:
                with self.assertRaises(NotificationConflict):
                    store.enqueue_notification(
                        conn, self._event(
                            scope,
                            payload={"nested": {"qty": [100]}},
                            title="不同标题",
                        ), NOW,
                    )

    def test_unknown_or_mismatched_account_scope_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                with self.assertRaisesRegex(ValueError, "account scope"):
                    store.enqueue_notification(
                        conn, self._event("missing-scope"), NOW,
                    )
                qmt_event = NotificationEvent(
                    **{
                        **self._event(scope).__dict__,
                        "adapter": "qmt",
                        "event_key": notification_event_key(
                            "qmt", scope, "fill", fill_id="fill-1",
                        ),
                    }
                )
                with self.assertRaisesRegex(ValueError, "adapter"):
                    store.enqueue_notification(conn, qmt_event, NOW)

    def test_account_scope_survives_reopen(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            reopened = TradingStore(store.db_path)
            reopened.initialize()
            with reopened.transaction() as conn:
                self.assertEqual(
                    reopened.get_or_create_account_scope(
                        conn, "joinquant", "primary",
                    ),
                    scope,
                )

    def test_title_body_and_payload_limits_count_utf8_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "title"):
            self._event("scope", title="a" * (MAX_TITLE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "body"):
            self._event("scope", body="中" * (MAX_BODY_BYTES // 3 + 1))
        with self.assertRaisesRegex(ValueError, "payload"):
            self._event("scope", payload={"value": "a" * MAX_PAYLOAD_BYTES})

    def test_tombstone_keeps_event_key_reserved(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                store.enqueue_notification(conn, event, NOW)
                conn.execute(
                    "UPDATE notification_outbox SET state='sent', sent_at=?, terminal_at=? WHERE event_key=?",
                    (NOW, NOW, event.event_key),
                )
            self.assertTrue(store.compact_notification(event.event_key))
            with store.transaction() as conn:
                duplicate = store.enqueue_notification(conn, event, NOW)
                with self.assertRaises(NotificationConflict):
                    store.enqueue_notification(
                        conn, self._event(scope, payload={"qty": 200}), NOW,
                    )
            self.assertFalse(duplicate.inserted)
            record = store.get_notification(event.event_key)
            self.assertIsNotNone(record)
            self.assertIsNone(record.payload)
            self.assertEqual(record.state, "sent")

    def test_compacted_dead_row_is_only_a_tombstone_not_dead_capacity(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                store.enqueue_notification(conn, event, NOW)
                conn.execute(
                    """UPDATE notification_outbox
                       SET state='dead', terminal_at=? WHERE event_key=?""",
                    (NOW, event.event_key),
                )
            before = store.notification_capacity()
            self.assertEqual(before.dead_rows, 1)
            self.assertGreater(before.dead_bytes, 0)
            self.assertTrue(store.compact_notification(event.event_key))
            after = store.notification_capacity()
            self.assertEqual(after.dead_rows, 0)
            self.assertEqual(after.dead_bytes, 0)
            self.assertEqual(after.tombstone_rows, 1)

    def test_enqueue_gap_is_idempotent_but_never_overwritten(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                self.assertTrue(store.enqueue_notification_gap(
                    conn, event, "capacity", NOW,
                ))
                self.assertFalse(store.enqueue_notification_gap(
                    conn, event, "capacity", NOW,
                ))
                with self.assertRaises(NotificationConflict):
                    store.enqueue_notification_gap(
                        conn, self._event(scope, payload={"qty": 200}),
                        "capacity", NOW,
                    )
            capacity = store.notification_capacity()
            self.assertEqual(capacity.unresolved_gap_rows, 1)

    def test_claim_complete_cancel_and_capacity_primitives(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            first = self._event(scope)
            second = self._event(
                scope,
                event_key=notification_event_key(
                    "joinquant", scope, "fill", fill_id="fill-2",
                ),
            )
            with store.transaction() as conn:
                store.enqueue_notification(conn, first, NOW)
                store.enqueue_notification(conn, second, NOW)
            claimed = store.claim_notifications(
                worker_id="worker-1", now=NOW, limit=1, lease_seconds=120,
            )
            self.assertEqual(len(claimed), 1)
            self.assertEqual(store.begin_notification_attempt(
                claimed[0].event_key, "worker-1", claimed[0].lease_until, NOW,
            ), 1)
            self.assertIsNone(store.begin_notification_attempt(
                claimed[0].event_key, "worker-1", claimed[0].lease_until, NOW,
            ))
            self.assertFalse(store.complete_notification(
                claimed[0].event_key, "worker-2", NOW,
                expected_lease_until=claimed[0].lease_until,
            ))
            self.assertTrue(store.complete_notification(
                claimed[0].event_key, "worker-1", NOW,
                expected_lease_until=claimed[0].lease_until,
            ))
            self.assertTrue(store.cancel_notification(
                second.event_key, NOW, "replaced",
            ))
            capacity = store.notification_capacity()
            self.assertEqual(capacity.high_active_rows, 0)

    def test_normal_capacity_allows_exact_limit_and_rejects_only_new_row(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                self._raw_rows(conn, scope, 999, priority="normal")
                event = self._event(
                    scope,
                    priority="normal",
                    event_key=notification_event_key(
                        "joinquant", scope, "control", control_event_id="limit",
                    ),
                    object_id="limit",
                    source_fact_id="limit",
                )
                self.assertTrue(store.enqueue_notification(conn, event, NOW).inserted)
            self.assertEqual(store.notification_capacity().normal_active_rows, 1000)

            overflow = self._event(
                scope,
                priority="normal",
                event_key=notification_event_key(
                    "joinquant", scope, "control", control_event_id="overflow",
                ),
                object_id="overflow",
                source_fact_id="overflow",
            )
            with store.transaction() as conn:
                with self.assertRaises(NotificationCapacityError):
                    store.enqueue_notification(conn, overflow, NOW)
                self.assertIsNone(store.enqueue_notification_or_gap(
                    conn, overflow, NOW,
                ))
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE event_key=?",
                    (overflow.event_key,),
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM notification_enqueue_gaps WHERE event_key=?",
                    (overflow.event_key,),
                ).fetchone()[0], 0)

    def test_high_capacity_failure_records_gap_and_stops_only_buy(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "1", "initial")
                store.set_system_state(conn, "kill_switch", "0", "initial")
                self._raw_rows(conn, scope, 5000, priority="high")
                event = self._event(
                    scope,
                    event_key=notification_event_key(
                        "joinquant", scope, "control", control_event_id="high-overflow",
                    ),
                    object_id="high-overflow",
                    source_fact_id="high-overflow",
                )
                self.assertIsNone(store.enqueue_notification_or_gap(conn, event, NOW))
            self.assertEqual(store.get_system_state("buy_enabled"), "0")
            self.assertEqual(store.get_system_state("kill_switch"), "0")
            self.assertTrue(store.get_system_state(
                "notification_capacity_auto_resume_owner",
            ))
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    """SELECT COUNT(*) FROM notification_enqueue_gaps
                       WHERE event_key=? AND priority='high' AND resolved_at IS NULL""",
                    (event.event_key,),
                ).fetchone()[0], 1)

    def test_recoverable_write_failure_is_cleared_by_a_later_writable_enqueue(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            failed = self._event(
                scope,
                priority="normal",
                event_key=notification_event_key(
                    "joinquant", scope, "control", control_event_id="write-failed",
                ),
                object_id="write-failed",
                source_fact_id="write-failed",
            )
            with store.transaction() as conn:
                with patch.object(
                    store,
                    "_insert_notification_row",
                    side_effect=sqlite3.OperationalError("locked"),
                ):
                    self.assertIsNone(
                        store.enqueue_notification_or_gap(
                            conn, failed, NOW, _capacity_reconcile=False,
                        )
                    )
            marker = json.loads(store.get_system_state(
                "notification_outbox_write_failure",
            ))
            self.assertFalse(marker["requires_manual_resolution"])
            self.assertEqual(marker["source_fact_id"], failed.source_fact_id)

            recovered = self._event(
                scope,
                priority="normal",
                event_key=notification_event_key(
                    "joinquant", scope, "control", control_event_id="write-recovered",
                ),
                object_id="write-recovered",
                source_fact_id="write-recovered",
            )
            with store.transaction() as conn:
                store.enqueue_notification_or_gap(
                    conn, recovered, NOW, _capacity_reconcile=False,
                )
            self.assertEqual(
                store.get_system_state("notification_outbox_write_failure"),
                "",
            )

    def test_high_write_failure_is_sticky_and_requires_matching_manual_resolution(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            first = self._event(
                scope,
                event_key=notification_event_key(
                    "joinquant", scope, "fill", fill_id="sticky-1",
                ),
                object_id="sticky-1",
                source_fact_id="sticky-1",
            )
            second = self._event(
                scope,
                event_key=notification_event_key(
                    "joinquant", scope, "fill", fill_id="sticky-2",
                ),
                object_id="sticky-2",
                source_fact_id="sticky-2",
            )
            with store.transaction() as conn:
                with patch.object(
                    store,
                    "_insert_notification_row",
                    side_effect=sqlite3.OperationalError("locked"),
                ), patch.object(
                    store,
                    "enqueue_notification_gap",
                    side_effect=sqlite3.OperationalError("locked"),
                ):
                    self.assertIsNone(
                        store.enqueue_notification_or_gap(
                            conn, first, NOW, _capacity_reconcile=False,
                        )
                    )
                    self.assertIsNone(
                        store.enqueue_notification_or_gap(
                            conn, second, NOW, _capacity_reconcile=False,
                        )
                    )
            marker = json.loads(store.get_system_state(
                "notification_outbox_write_failure",
            ))
            self.assertTrue(marker["requires_manual_resolution"])
            self.assertEqual(marker["event_key"], first.event_key)
            self.assertEqual(marker["payload_sha256"], first.semantic_payload_sha256)

            with store.transaction() as conn:
                with self.assertRaisesRegex(ValueError, "does not match"):
                    store.resolve_notification_write_failure(
                        conn, second.event_key, "wrong event",
                    )
                self.assertTrue(store.resolve_notification_write_failure(
                    conn, first.event_key,
                    "source fact checked; notification is non-replayable",
                ))
            self.assertEqual(
                store.get_system_state("notification_outbox_write_failure"),
                "",
            )

    def test_fail_notification_compacts_dead_detail_before_commit(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                self._raw_rows(conn, scope, 1000, priority="high", state="dead")
            event = self._event(scope)
            with store.transaction() as conn:
                store.enqueue_notification(conn, event, NOW, _capacity_reconcile=False)
            claimed = store.claim_notifications(
                "worker-1", "2026-07-28T02:00:00+00:00", lease_seconds=120,
            )[0]
            self.assertEqual(
                store.begin_notification_attempt(
                    claimed.event_key,
                    "worker-1",
                    claimed.lease_until,
                    "2026-07-28T02:00:01+00:00",
                ),
                1,
            )
            self.assertTrue(store.fail_notification(
                claimed.event_key,
                "worker-1",
                "2026-07-28T02:00:02+00:00",
                "HTTP_500",
                "temporary transport failure",
                expected_lease_until=claimed.lease_until,
                dead=True,
            ))
            capacity = store.notification_capacity()
            self.assertLessEqual(capacity.dead_rows, 1000)
            self.assertLessEqual(capacity.dead_bytes, 4 * 1024 * 1024)
            self.assertEqual(capacity.dead_total_rows, 1001)

    def test_claim_corrupt_content_compacts_dead_detail_before_commit(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                self._raw_rows(conn, scope, 1000, priority="high", state="dead")
                store.enqueue_notification(conn, event, NOW, _capacity_reconcile=False)
                conn.execute(
                    "UPDATE notification_outbox SET payload_json=? WHERE event_key=?",
                    ("{", event.event_key),
                )
            self.assertEqual(
                store.claim_notifications(
                    "worker-1", "2026-07-28T02:00:00+00:00",
                ),
                (),
            )
            capacity = store.notification_capacity()
            self.assertLessEqual(capacity.dead_rows, 1000)
            self.assertLessEqual(capacity.dead_bytes, 4 * 1024 * 1024)
            self.assertEqual(capacity.dead_total_rows, 1001)

    def test_idempotent_enqueue_still_reconciles_existing_high_pressure(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "1", "initial")
                store.enqueue_notification(
                    conn, event, NOW, _capacity_reconcile=False,
                )
                self._raw_rows(conn, scope, 3999, priority="high")
                replay = store.enqueue_notification(conn, event, NOW)
            self.assertFalse(replay.inserted)
            self.assertEqual(store.get_system_state("buy_enabled"), "0")

    def test_cleanup_cancels_pending_but_only_requests_leased_expiry(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            pending = self._event(
                scope, priority="normal",
                event_key=notification_event_key(
                    "joinquant", scope, "control", control_event_id="expired-pending",
                ),
                object_id="expired-pending", source_fact_id="expired-pending",
            )
            leased = self._event(
                scope, priority="normal",
                event_key=notification_event_key(
                    "joinquant", scope, "control", control_event_id="expired-leased",
                ),
                object_id="expired-leased", source_fact_id="expired-leased",
            )
            with store.transaction() as conn:
                store.enqueue_notification(conn, pending, NOW)
                store.enqueue_notification(conn, leased, NOW)
                conn.execute(
                    """UPDATE notification_outbox SET expires_at=?, state='leased',
                       lease_owner='w', lease_until=? WHERE event_key=?""",
                    ("2026-07-28T02:00:01+00:00", "2026-07-28T03:00:00+00:00", leased.event_key),
                )
                conn.execute(
                    "UPDATE notification_outbox SET expires_at=? WHERE event_key=?",
                    ("2026-07-28T02:00:01+00:00", pending.event_key),
                )
                store.cleanup_notifications(conn, "2026-07-28T02:00:02+00:00")
                rows = conn.execute(
                    """SELECT event_key, state, cancel_requested_at
                       FROM notification_outbox WHERE event_key IN (?,?)""",
                    (pending.event_key, leased.event_key),
                ).fetchall()
            values = {str(row["event_key"]): row for row in rows}
            self.assertEqual(values[pending.event_key]["state"], "cancelled")
            self.assertEqual(values[leased.event_key]["state"], "leased")
            self.assertTrue(values[leased.event_key]["cancel_requested_at"])

    def test_cleanup_retention_and_dead_emergency_compaction_are_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            with store.transaction() as conn:
                self._raw_rows(
                    conn, scope, 1, priority="high", state="sent",
                    created_at="2025-07-27T02:00:00+00:00",
                )
                self._raw_rows(
                    conn, scope, 1, priority="normal", state="dead",
                    created_at="2026-07-28T01:58:00+00:00",
                )
                self._raw_rows(
                    conn, scope, 1000, priority="high", state="dead",
                    created_at="2026-07-28T01:59:00+00:00",
                )
                store.cleanup_notifications(conn, NOW)
                sent = conn.execute(
                    "SELECT payload_json, body FROM notification_outbox WHERE state='sent'",
                ).fetchone()
                normal_dead = conn.execute(
                    """SELECT payload_json FROM notification_outbox
                       WHERE state='dead' AND priority='normal'""",
                ).fetchone()
                high_detail = conn.execute(
                    """SELECT COUNT(*) FROM notification_outbox
                       WHERE state='dead' AND priority='high'
                         AND payload_json IS NOT NULL""",
                ).fetchone()[0]
            capacity = store.notification_capacity()
            self.assertIsNone(sent["payload_json"])
            self.assertIsNone(sent["body"])
            self.assertIsNone(normal_dead["payload_json"])
            self.assertEqual(high_detail, 1000)
            self.assertLessEqual(capacity.dead_rows, 1000)
            self.assertLessEqual(capacity.dead_bytes, 4 * 1024 * 1024)
            self.assertEqual(capacity.dead_total_rows, 1001)

    def test_equivalent_offsets_claim_and_lease_generation_is_cas_bound(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                store.enqueue_notification(
                    conn, event, "2026-07-28T10:00:00+08:00",
                )
            first = store.claim_notifications(
                "worker-1", "2026-07-28T02:00:00+00:00",
                lease_seconds=60,
            )[0]
            self.assertEqual(first.attempt_count, 0)
            second = store.claim_notifications(
                "worker-1", "2026-07-28T02:01:00+00:00",
                lease_seconds=60,
            )[0]
            self.assertNotEqual(first.lease_until, second.lease_until)
            self.assertFalse(store.complete_notification(
                event.event_key, "worker-1", "2026-07-28T02:01:01+00:00",
                expected_lease_until=first.lease_until,
            ))
            self.assertEqual(store.begin_notification_attempt(
                event.event_key, "worker-1", second.lease_until,
                "2026-07-28T02:01:01+00:00",
            ), 1)
            self.assertTrue(store.complete_notification(
                event.event_key, "worker-1", "2026-07-28T02:01:02+00:00",
                expected_lease_until=second.lease_until,
            ))

    def test_capacity_counts_identity_bytes_and_identifiers_are_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(
                scope, object_id="o" * MAX_ID_BYTES,
                source_fact_id="s" * MAX_ID_BYTES,
            )
            with store.transaction() as conn:
                store.enqueue_notification(conn, event, NOW)
            capacity = store.notification_capacity()
            self.assertGreater(capacity.high_active_bytes, MAX_ID_BYTES * 2)
            with self.assertRaisesRegex(ValueError, "object_id"):
                self._event(scope, object_id="o" * (MAX_ID_BYTES + 1))

    def test_gap_and_outbox_are_mutually_exclusive_until_atomic_repair(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                self.assertTrue(store.enqueue_notification_gap(
                    conn, event, "capacity", NOW,
                ))
                with self.assertRaises(NotificationConflict):
                    store.enqueue_notification(conn, event, NOW)
                repaired = store.repair_notification_gap(
                    conn, event, NOW, "source fact verified",
                )
                self.assertTrue(repaired.inserted)
            self.assertEqual(store.notification_capacity().unresolved_gap_rows, 0)
            with store.transaction() as conn:
                replay = store.enqueue_notification_or_gap(conn, event, NOW)
                self.assertIsNotNone(replay)
                self.assertFalse(replay.inserted)
                self.assertFalse(store.enqueue_notification_gap(
                    conn, event, "capacity", NOW,
                ))
                with self.assertRaises(NotificationConflict):
                    store.enqueue_notification_or_gap(
                        conn,
                        self._event(scope, payload={"qty": 200}),
                        NOW,
                    )

    def test_non_replayable_gap_resolution_stays_terminal_on_producer_replay(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                store.enqueue_notification_gap(conn, event, "capacity", NOW)
                self.assertTrue(store.resolve_notification_gap(
                    conn, event.event_key, NOW, "source fact cannot be replayed",
                ))
            with store.transaction() as conn:
                self.assertIsNone(
                    store.enqueue_notification_or_gap(conn, event, NOW)
                )
                self.assertEqual(
                    conn.execute(
                        """SELECT COUNT(*) FROM notification_outbox
                           WHERE event_key=?""",
                        (event.event_key,),
                    ).fetchone()[0],
                    0,
                )

    def test_invalid_gap_repair_cannot_leave_both_rows_when_caught(self) -> None:
        with TemporaryDirectory() as tmp:
            store, scope = self._store(tmp)
            event = self._event(scope)
            with store.transaction() as conn:
                store.enqueue_notification_gap(conn, event, "capacity", NOW)
                with self.assertRaisesRegex(ValueError, "resolution"):
                    store.repair_notification_gap(conn, event, NOW, "")
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE event_key=?",
                    (event.event_key,),
                ).fetchone()[0], 0)
                gap = conn.execute(
                    "SELECT resolved_at FROM notification_enqueue_gaps",
                ).fetchone()
            self.assertIsNone(gap[0])

    def test_nested_secret_keys_are_rejected_but_scope_identity_is_allowed(self) -> None:
        safe = self._event(
            "scope", metadata={"account_scope_id": "scope"},
        )
        self.assertEqual(safe.metadata["account_scope_id"], "scope")
        for key in (
            "access_token", "apiToken", "wechat_webhook", "password",
            "client_secret", "api_key", "authorization", "private_key",
            "qmt_access_key", "request_signing_key", "ssh_private_key",
            "x_api_key", "qmt_account", "raw_account_id", "broker_auth",
            "passphrase", "user_id",
            "account_number", "environment_variables",
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "must not contain"):
                    self._event(
                        "scope", metadata={"nested": [{key: "secret"}]},
                    )

    def test_secret_values_are_redacted_from_rendered_and_structured_content(self) -> None:
        event = self._event(
            "scope",
            body="callback failed: https://example.invalid/hook?key=TOPSECRET",
            payload={
                "detail": "token=TOPSECRET",
                "auth_text": "Authorization: Bearer TOPSECRET.value",
                "colon": "token: COLONSECRET",
                "json_text": '{"access_token":"JSONSECRET"}',
                "basic_text": "Authorization: Basic BASICSECRET",
                "case_env": "SYNC_TOKEN=ENVSECRET",
                "case_web": "WECHAT_WEBHOOK=https://example.invalid/SECRETURL",
                "case_account": "account_no=123456",
                "case_broker": "broker_account_id: 654321",
                "case_nested_1": "reason: token=INNERSECRET",
                "case_nested_2": "error: account_no=112233",
                "case_nested_3": "detail: https://x.invalid/hook?key=URLSECRET",
                "case_qmt_key": "QMT_ACCESS_KEY=QMTSECRET",
                "case_signing": "REQUEST_SIGNING_KEY: SIGNINGSECRET",
                "case_ssh": "SSH_PRIVATE_KEY=SSHSECRET",
                "case_api": "X_API_KEY=APISECRET",
                "case_qmt_account": "QMT_ACCOUNT=QMTACCOUNTSECRET",
                "case_raw_account": "RAW_ACCOUNT_ID=RAWACCOUNTSECRET",
                "case_extra_1": "BROKER_AUTH: BROKERAUTHSECRET",
                "case_extra_2": "PASSPHRASE=PHRASESECRET",
                "case_user": "USER_ID=USERSECRET",
            },
        )

        self.assertNotIn("TOPSECRET", event.body)
        payload_text = str(dict(event.payload))
        self.assertNotIn("TOPSECRET", payload_text)
        self.assertNotIn("COLONSECRET", payload_text)
        self.assertNotIn("JSONSECRET", payload_text)
        self.assertNotIn("BASICSECRET", payload_text)
        self.assertNotIn("ENVSECRET", payload_text)
        self.assertNotIn("SECRETURL", payload_text)
        self.assertNotIn("123456", payload_text)
        self.assertNotIn("654321", payload_text)
        self.assertNotIn("INNERSECRET", payload_text)
        self.assertNotIn("112233", payload_text)
        self.assertNotIn("URLSECRET", payload_text)
        for secret in (
            "QMTSECRET", "SIGNINGSECRET", "SSHSECRET", "APISECRET",
            "QMTACCOUNTSECRET", "RAWACCOUNTSECRET", "BROKERAUTHSECRET",
            "PHRASESECRET", "USERSECRET",
        ):
            self.assertNotIn(secret, payload_text)
        self.assertIn("[REDACTED]", event.body)


if __name__ == "__main__":
    unittest.main()
