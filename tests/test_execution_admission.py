import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import execution_admission
import joinquant_sync
from execution_admission import (
    AdmissionRequest,
    admit_candidate,
    expire_ready_intents,
    synchronize_execution_lifecycle,
)
from pre_trade_check import AdoptedPositionCapacityEvidence
from tests.test_execution_contracts import D, make_candidate
from tests.test_pre_trade_check import (
    CHECKED_AT,
    make_broker,
    make_policy,
    make_quote,
    make_rules,
    position,
)
from trading_store import SignalRecord, StrategyRunRecord, TradingStore


NOW = CHECKED_AT


class ExecutionAdmissionTest(unittest.TestCase):
    def test_capacity_risk_uses_active_one_lot_trailing_stop(self) -> None:
        stop = execution_admission._effective_stop({
            "stock_code": "600000", "mode": "short", "initial_qty": 100,
            "current_qty": 100, "entry_price": 10, "initial_stop_price": 9,
            "highest_price": 13, "atr14": 0.4, "take_profit_stage": 0,
            "manual_stop_price": None, "market_state": "NORMAL",
            "profit_protection_activated_at": "2026-07-28T09:55:00+08:00",
            "trailing_stop_active_from": "2026-07-28T09:55:00.000001+08:00",
        }, NOW)

        self.assertEqual(stop, D("12.2"))

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TradingStore(Path(self.tmp.name) / "trading.db")
        self.store.initialize()

    def seed(self, broker=None) -> None:
        broker = broker or make_broker()
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO account_scopes(
                   account_scope_id, adapter, scope_alias, created_at
                   ) VALUES('scope-uuid', 'joinquant', 'primary', ?)""",
                (NOW,),
            )
            for key, value in {
                "buy_enabled": "1",
                "sell_enabled": "1",
                "kill_switch": "0",
                "market_regime": "NORMAL",
            }.items():
                self.store.set_system_state(conn, key, value, "test")
            self.store.replace_current_broker_snapshot(conn, broker)

    @staticmethod
    def request(code="600000", **policy_changes) -> AdmissionRequest:
        candidate = replace(
            make_candidate(code=code),
            candidate_id=f"candidate-{code}",
            payload_sha256="",
        )
        return AdmissionRequest(
            candidate=candidate,
            quote=make_quote(code=code),
            instrument_rules=make_rules(code=code),
            risk_policy=make_policy(**policy_changes),
        )

    def counts(self) -> dict[str, int]:
        tables = (
            "strategy_order_candidates",
            "pre_trade_results",
            "execution_intents",
            "capacity_reservations",
            "orders",
        )
        with self.store.connect() as conn:
            return {
                table: int(conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0])
                for table in tables
            }

    @staticmethod
    def full_payload(
        *, generated_at: str, positions=(), orders=(), trades=(),
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "trade_date": "2026-07-28",
            "generated_at": generated_at,
            "source": "joinquant",
            "template_version": "test-v1",
            "cash": 100000,
            "available_cash": 100000,
            "total_value": 100000,
            "daily_turnover_pct": 0,
            "daily_pnl_pct": 0,
            "account_drawdown_pct": 0,
            "consecutive_losses": 0,
            "positions": list(positions),
            "orders": list(orders),
            "trades": list(trades),
        }

    @staticmethod
    def insert_matched_reconciliation(
        conn, reconciliation_id: str, broker, finished_at: str,
    ) -> None:
        conn.execute(
            """INSERT INTO reconciliation_runs(
               reconciliation_id, mode, started_at, finished_at, result,
               severity, difference_count, control_action, summary_json,
               account_scope_id, broker_snapshot_id, broker_snapshot_sha256,
               snapshot_broker_time, snapshot_generated_at
               ) VALUES(?, 'full', ?, ?, 'matched', 'INFO', 0, '', '{}',
                        ?, ?, ?, ?, ?)""",
            (
                reconciliation_id, broker.broker_time, finished_at,
                broker.account_scope_id, broker.snapshot_id,
                broker.snapshot_sha256, broker.broker_time,
                broker.generated_at,
            ),
        )

    def test_allowed_buy_persists_one_exact_atomic_chain(self) -> None:
        self.seed()

        result = admit_candidate(self.store, self.request(), NOW)

        self.assertTrue(result.allowed)
        self.assertFalse(result.replayed)
        self.assertIsNotNone(result.execution_intent)
        self.assertEqual(result.reservation_id, f"reservation:{result.client_order_id}")
        self.assertEqual(self.counts(), {
            "strategy_order_candidates": 1,
            "pre_trade_results": 1,
            "execution_intents": 1,
            "capacity_reservations": 1,
            "orders": 1,
        })
        with self.store.connect() as conn:
            order = conn.execute(
                "SELECT * FROM orders WHERE client_order_id=?",
                (result.client_order_id,),
            ).fetchone()
            reservation = conn.execute(
                "SELECT * FROM capacity_reservations WHERE client_order_id=?",
                (result.client_order_id,),
            ).fetchone()
        payload = json.loads(order["raw_json"])
        self.assertEqual(order["status"], "ready")
        self.assertEqual(order["requested_qty"], result.execution_intent.order_qty)
        self.assertEqual(order["target_qty"], result.execution_intent.target_position_qty)
        self.assertEqual(order["submit_count"], 0)
        self.assertIsNone(order["order_id"])
        self.assertIsNone(order["first_submitted_at"])
        self.assertEqual(payload["intent_sha256"], result.execution_intent.intent_sha256)
        self.assertEqual(reservation["target_qty"], result.execution_intent.order_qty)

    def test_rejection_records_candidate_and_result_only(self) -> None:
        self.seed(make_broker(
            broker_time="2026-07-28T09:50:00+08:00",
            generated_at="2026-07-28T09:50:01+08:00",
        ))

        result = admit_candidate(self.store, self.request(), NOW)

        self.assertFalse(result.allowed)
        self.assertIn("ACCOUNT_SNAPSHOT_STALE", result.pre_trade_result.hard_blocks)
        self.assertEqual(self.counts(), {
            "strategy_order_candidates": 1,
            "pre_trade_results": 1,
            "execution_intents": 0,
            "capacity_reservations": 0,
            "orders": 0,
        })

    def test_rejected_candidate_is_terminal_and_replays_original_decision(self) -> None:
        stale = make_broker(
            broker_time="2026-07-28T09:50:00+08:00",
            generated_at="2026-07-28T09:50:01+08:00",
        )
        self.seed(stale)
        request = self.request()
        first = admit_candidate(self.store, request, NOW)
        with self.store.transaction() as conn:
            self.store.replace_current_broker_snapshot(conn, make_broker())

        replay = admit_candidate(self.store, request, NOW)

        self.assertFalse(first.allowed)
        self.assertFalse(replay.allowed)
        self.assertTrue(replay.replayed)
        self.assertEqual(
            replay.pre_trade_result.result_sha256,
            first.pre_trade_result.result_sha256,
        )
        self.assertEqual(self.counts(), {
            "strategy_order_candidates": 1,
            "pre_trade_results": 1,
            "execution_intents": 0,
            "capacity_reservations": 0,
            "orders": 0,
        })

    def test_missing_broker_snapshot_is_a_persisted_risk_rejection(self) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO account_scopes(
                   account_scope_id, adapter, scope_alias, created_at
                   ) VALUES('scope-uuid', 'joinquant', 'primary', ?)""",
                (NOW,),
            )

        result = admit_candidate(self.store, self.request(), NOW)

        self.assertFalse(result.allowed)
        self.assertIn(
            "BROKER_SNAPSHOT_REQUIRED", result.pre_trade_result.hard_blocks,
        )
        self.assertEqual(self.counts(), {
            "strategy_order_candidates": 1,
            "pre_trade_results": 1,
            "execution_intents": 0,
            "capacity_reservations": 0,
            "orders": 0,
        })

    def test_same_candidate_and_hash_replays_the_existing_chain(self) -> None:
        self.seed()
        request = self.request()

        first = admit_candidate(self.store, request, NOW)
        second = admit_candidate(self.store, request, NOW)

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.client_order_id, first.client_order_id)
        self.assertEqual(second.pre_trade_result.result_sha256, first.pre_trade_result.result_sha256)
        self.assertEqual(self.counts(), {
            "strategy_order_candidates": 1,
            "pre_trade_results": 1,
            "execution_intents": 1,
            "capacity_reservations": 1,
            "orders": 1,
        })

    def test_admission_and_replay_ignore_hostile_decimal_context(self) -> None:
        self.seed()
        request = self.request()

        with localcontext() as context:
            context.prec = 3
            first = admit_candidate(self.store, request, NOW)
            replay = admit_candidate(self.store, request, NOW)

        self.assertTrue(first.allowed)
        self.assertTrue(replay.allowed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.client_order_id, replay.client_order_id)

    def test_same_candidate_id_with_different_content_fails_closed(self) -> None:
        self.seed()
        request = self.request()
        admit_candidate(self.store, request, NOW)
        conflict = replace(
            request,
            candidate=replace(
                request.candidate,
                target_price=D("11.50"),
                payload_sha256="",
            ),
        )

        with self.assertRaisesRegex(ValueError, "immutable ID conflicts"):
            admit_candidate(self.store, conflict, NOW)

        self.assertEqual(self.counts()["execution_intents"], 1)

    def test_failure_after_decision_rolls_back_the_whole_chain(self) -> None:
        self.seed()

        with patch.object(
            self.store, "reserve_capacity", side_effect=RuntimeError("boom")
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                admit_candidate(self.store, self.request(), NOW)

        self.assertEqual(self.counts(), {
            "strategy_order_candidates": 0,
            "pre_trade_results": 0,
            "execution_intents": 0,
            "capacity_reservations": 0,
            "orders": 0,
        })

    def test_two_concurrent_buyers_cannot_take_the_same_last_slot(self) -> None:
        self.seed()
        barrier = threading.Barrier(2)

        def run(code: str):
            barrier.wait()
            return admit_candidate(
                self.store, self.request(code, max_positions=1), NOW,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ("600000", "600001")))

        self.assertEqual(sum(result.allowed for result in results), 1)
        self.assertEqual(self.counts()["capacity_reservations"], 1)
        self.assertEqual(self.counts()["orders"], 1)
        rejected = next(result for result in results if not result.allowed)
        self.assertIn(
            "MAX_POSITIONS_EXCEEDED", rejected.pre_trade_result.hard_blocks,
        )

    def test_replay_rejects_tampered_reservation_amounts(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        with self.store.transaction() as conn:
            conn.execute(
                """UPDATE capacity_reservations
                   SET remaining_cash_yuan='0' WHERE reservation_id=?""",
                (admitted.reservation_id,),
            )

        with self.assertRaisesRegex(ValueError, "reservation amounts"):
            admit_candidate(self.store, self.request(), NOW)

    def test_ready_replay_rejects_current_broker_order_evidence(self) -> None:
        self.seed()
        request = self.request()
        admitted = admit_candidate(self.store, request, NOW)
        with self.store.transaction() as conn:
            self.store.replace_current_broker_snapshot(conn, make_broker(
                broker_time="2026-07-28T10:00:02+08:00",
                generated_at="2026-07-28T10:00:03+08:00",
                open_orders=({
                    "client_order_id": admitted.client_order_id,
                    "broker_order_id": "broker-ready-replay",
                    "stock_code": "600000",
                    "side": "buy",
                    "target_qty": admitted.execution_intent.order_qty,
                    "filled_qty": 0,
                    "status": "submitted",
                    "updated_at": "2026-07-28T10:00:02+08:00",
                },),
            ))

        with self.assertRaisesRegex(ValueError, "no longer replayable"):
            admit_candidate(
                self.store,
                replace(
                    request,
                    risk_policy=make_policy(
                        checked_at="2026-07-28T10:00:04+08:00",
                    ),
                ),
                "2026-07-28T10:00:04+08:00",
            )

    def test_ready_expiry_requires_post_expiry_snapshot_and_is_idempotent(self) -> None:
        self.seed()
        request = self.request()
        admitted = admit_candidate(self.store, request, NOW)

        self.assertEqual(
            expire_ready_intents(
                self.store, "2026-07-28T10:00:29+08:00"
            ),
            0,
        )
        with self.store.transaction() as conn:
            self.store.replace_current_broker_snapshot(
                conn,
                make_broker(
                    broker_time="2026-07-28T10:01:00+08:00",
                    generated_at="2026-07-28T10:01:01+08:00",
                ),
            )

        self.assertEqual(
            expire_ready_intents(
                self.store, "2026-07-28T10:01:02+08:00"
            ),
            1,
        )
        self.assertEqual(
            expire_ready_intents(
                self.store, "2026-07-28T10:01:03+08:00"
            ),
            0,
        )
        with self.store.connect() as conn:
            intent = conn.execute(
                "SELECT status FROM execution_intents WHERE client_order_id=?",
                (admitted.client_order_id,),
            ).fetchone()
            order = conn.execute(
                "SELECT status FROM orders WHERE client_order_id=?",
                (admitted.client_order_id,),
            ).fetchone()
            reservation = conn.execute(
                "SELECT status FROM capacity_reservations WHERE client_order_id=?",
                (admitted.client_order_id,),
            ).fetchone()
            notice = conn.execute(
                """SELECT event_type, source_fact_id FROM notification_outbox
                   WHERE object_type='order' AND object_id=?""",
                (admitted.client_order_id,),
            ).fetchone()
        self.assertEqual(intent["status"], "EXPIRED")
        self.assertEqual(order["status"], "expired")
        self.assertEqual(reservation["status"], "released")
        self.assertEqual(
            tuple(notice), ("order_terminal", admitted.client_order_id),
        )
        with self.assertRaisesRegex(ValueError, "no longer replayable"):
            admit_candidate(
                self.store,
                replace(
                    request,
                    risk_policy=make_policy(
                        checked_at="2026-07-28T10:01:03+08:00",
                    ),
                ),
                "2026-07-28T10:01:03+08:00",
            )

    def test_submitting_intent_never_expires_by_ttl(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        with self.store.transaction() as conn:
            self.assertTrue(self.store.compare_and_set_execution_intent_status(
                conn, "scope-uuid", admitted.client_order_id,
                expected_status="READY", new_status="SUBMITTING",
                transitioned_at="2026-07-28T10:00:01+08:00",
            ))
            conn.execute(
                """UPDATE orders SET status='submitting', submit_count=1,
                          first_submitted_at=?, updated_at=?
                   WHERE client_order_id=?""",
                (
                    "2026-07-28T10:00:01+08:00",
                    "2026-07-28T10:00:01+08:00",
                    admitted.client_order_id,
                ),
            )
            self.store.replace_current_broker_snapshot(
                conn,
                make_broker(
                    broker_time="2026-07-28T10:01:00+08:00",
                    generated_at="2026-07-28T10:01:01+08:00",
                ),
            )

        self.assertEqual(
            expire_ready_intents(
                self.store, "2026-07-28T10:01:02+08:00"
            ),
            0,
        )
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM execution_intents WHERE client_order_id=?",
                (admitted.client_order_id,),
            ).fetchone()[0], "SUBMITTING")
            self.assertEqual(conn.execute(
                "SELECT status FROM capacity_reservations WHERE client_order_id=?",
                (admitted.client_order_id,),
            ).fetchone()[0], "active")

    def test_full_snapshot_expires_never_submitted_ready_intent(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)

        result = joinquant_sync.ingest_snapshot_payload(
            self.full_payload(
                generated_at="2026-07-28T10:01:00+08:00",
            ),
            self.store,
            "2026-07-28T10:01:01+08:00",
            mode="full",
        )

        self.assertEqual(result["expired_ready_intents"], 1)
        with self.store.connect() as conn:
            statuses = conn.execute(
                """SELECT i.status, r.status, o.status
                   FROM execution_intents AS i
                   JOIN capacity_reservations AS r
                     ON r.account_scope_id=i.account_scope_id
                    AND r.client_order_id=i.client_order_id
                   JOIN orders AS o ON o.client_order_id=i.client_order_id
                   WHERE i.client_order_id=?""",
                (admitted.client_order_id,),
            ).fetchone()
        self.assertEqual(tuple(statuses), ("EXPIRED", "released", "expired"))

    def test_local_execution_guard_rejects_ready_without_fake_submission(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        intent = admitted.execution_intent

        joinquant_sync.ingest_snapshot_payload(
            self.full_payload(
                generated_at="2026-07-28T10:00:10+08:00",
                orders=({
                    "id": intent.source_signal_id,
                    "client_order_id": intent.client_order_id,
                    "code": intent.code,
                    "action": intent.side,
                    "amount": intent.order_qty,
                    "target_qty": intent.target_position_qty,
                    "filled": 0,
                    "status": "price_moved",
                    "reason": "price_moved",
                    "datetime": "2026-07-28T10:00:10+08:00",
                },),
            ),
            self.store,
            "2026-07-28T10:00:11+08:00",
            mode="full",
        )

        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT i.status, r.status, o.status, o.submit_count,
                          o.first_submitted_at
                   FROM execution_intents AS i
                   JOIN capacity_reservations AS r
                     ON r.account_scope_id=i.account_scope_id
                    AND r.client_order_id=i.client_order_id
                   JOIN orders AS o ON o.client_order_id=i.client_order_id
                   WHERE i.client_order_id=?""",
                (intent.client_order_id,),
            ).fetchone()
        self.assertEqual(tuple(row), ("REJECTED", "released", "not_submitted", 0, None))

    def test_partial_broker_order_advances_intent_and_reservation_once(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        intent = admitted.execution_intent
        filled_qty = 100
        broker_order = {
            "client_order_id": intent.client_order_id,
            "broker_order_id": "broker-partial-1",
            "stock_code": intent.code,
            "side": intent.side,
            "target_qty": intent.order_qty,
            "filled_qty": filled_qty,
            "status": "partially_filled",
            "updated_at": "2026-07-28T10:00:10+08:00",
        }
        order = {
            "client_order_id": intent.client_order_id,
            "signal_id": None,
            "order_id": "broker-partial-1",
            "stock_code": intent.code,
            "action": intent.side,
            "target_qty": intent.target_position_qty,
            "requested_qty": intent.order_qty,
            "filled_qty": filled_qty,
            "average_fill_price": 10,
            "status": "partial",
            "submit_count": 1,
            "reason": "",
            "first_submitted_at": "2026-07-28T10:00:02+08:00",
            "updated_at": "2026-07-28T10:00:10+08:00",
            "completed_at": None,
            "raw_json": "{}",
        }
        with self.store.transaction() as conn:
            self.store.upsert_order(conn, order)
            self.store.replace_current_broker_snapshot(
                conn,
                make_broker(
                    broker_time="2026-07-28T10:00:10+08:00",
                    generated_at="2026-07-28T10:00:11+08:00",
                    open_orders=(broker_order,),
                ),
            )
            first = synchronize_execution_lifecycle(
                self.store, conn, "scope-uuid",
                now="2026-07-28T10:00:12+08:00",
            )
            second = synchronize_execution_lifecycle(
                self.store, conn, "scope-uuid",
                now="2026-07-28T10:00:12+08:00",
            )

        self.assertEqual(first["advanced"], 1)
        self.assertEqual(second["advanced"], 0)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM execution_intents WHERE client_order_id=?",
                (intent.client_order_id,),
            ).fetchone()[0], "PARTIALLY_FILLED")
            reservation = conn.execute(
                """SELECT remaining_target_qty, remaining_cash_yuan
                   FROM capacity_reservations WHERE client_order_id=?""",
                (intent.client_order_id,),
            ).fetchone()
        self.assertEqual(
            reservation["remaining_target_qty"], intent.order_qty - filled_qty,
        )
        self.assertLess(
            D(reservation["remaining_cash_yuan"]),
            intent.pre_trade_result.execution_fee.notional_yuan
            + intent.pre_trade_result.execution_fee.total_yuan,
        )

    def test_full_joinquant_snapshot_drives_partial_lifecycle(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        intent = admitted.execution_intent
        payload = {
            "schema_version": 1,
            "trade_date": "2026-07-28",
            "generated_at": "2026-07-28T10:00:10+08:00",
            "source": "joinquant",
            "template_version": "test-v1",
            "cash": 98995,
            "available_cash": 98995,
            "total_value": 100000,
            "daily_turnover_pct": 1,
            "daily_pnl_pct": 0,
            "account_drawdown_pct": 0,
            "consecutive_losses": 0,
            "positions": [{
                "code": intent.code,
                "qty": 100,
                "closeable_amount": 0,
                "locked_amount": 0,
                "today_amount": 100,
                "avg_cost": 10,
                "price": 10,
                "market_value": 1000,
                "pnl": 0,
            }],
            "orders": [{
                "client_order_id": intent.client_order_id,
                "order_id": "jq-partial-1",
                "code": intent.code,
                "action": "buy",
                "amount": intent.order_qty,
                "target_qty": intent.target_position_qty,
                "filled": 100,
                "avg_price": 10,
                "status": "partial",
                "datetime": "2026-07-28T10:00:09+08:00",
            }],
            "trades": [{
                "client_order_id": intent.client_order_id,
                "trade_id": "jq-fill-1",
                "order_id": "jq-partial-1",
                "code": intent.code,
                "action": "buy",
                "amount": 100,
                "price": 10,
                "commission": 5,
                "stamp_tax": 0,
                "other_fee": 0,
                "fee_data_status": "reported",
                "datetime": "2026-07-28T10:00:09+08:00",
            }],
        }

        result = joinquant_sync.ingest_snapshot_payload(
            payload,
            self.store,
            "2026-07-28T10:00:12+08:00",
        )

        self.assertEqual(result["execution_lifecycle"], {
            "advanced": 1, "adjusted": 1, "released": 0,
        })
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM execution_intents WHERE client_order_id=?",
                (intent.client_order_id,),
            ).fetchone()[0], "PARTIALLY_FILLED")
            self.assertEqual(conn.execute(
                """SELECT remaining_target_qty FROM capacity_reservations
                   WHERE client_order_id=?""",
                (intent.client_order_id,),
            ).fetchone()[0], intent.order_qty - 100)

    def test_terminal_order_waits_for_full_reconciliation_before_release(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        intent = admitted.execution_intent
        terminal_at = "2026-07-28T10:01:10+08:00"
        broker = make_broker(
            broker_time="2026-07-28T10:01:20+08:00",
            generated_at="2026-07-28T10:01:21+08:00",
            positions=(position(
                code=intent.code,
                total_qty=intent.order_qty,
                sellable_qty=0,
                today_buy_qty=intent.order_qty,
            ),),
        )
        with self.store.transaction() as conn:
            self.store.upsert_order(conn, {
                "client_order_id": intent.client_order_id,
                "signal_id": intent.source_signal_id,
                "order_id": "broker-filled-1",
                "stock_code": intent.code,
                "action": intent.side,
                "target_qty": intent.target_position_qty,
                "requested_qty": intent.order_qty,
                "filled_qty": intent.order_qty,
                "average_fill_price": 10,
                "status": "filled",
                "submit_count": 1,
                "reason": "",
                "first_submitted_at": "2026-07-28T10:00:01+08:00",
                "updated_at": terminal_at,
                "completed_at": terminal_at,
                "raw_json": "{}",
            })
            self.store.replace_current_broker_snapshot(conn, broker)
            self.insert_matched_reconciliation(
                conn, "terminal-match", broker,
                "2026-07-28T10:01:22+08:00",
            )
            before = synchronize_execution_lifecycle(
                self.store, conn, "scope-uuid",
                now="2026-07-28T10:01:23+08:00",
            )
            after = synchronize_execution_lifecycle(
                self.store, conn, "scope-uuid",
                now="2026-07-28T10:01:23+08:00",
                reconciliation_id="terminal-match",
            )
            replay = synchronize_execution_lifecycle(
                self.store, conn, "scope-uuid",
                now="2026-07-28T10:01:23+08:00",
                reconciliation_id="terminal-match",
            )

        self.assertEqual(before, {"advanced": 1, "adjusted": 0, "released": 0})
        self.assertEqual(after, {"advanced": 0, "adjusted": 0, "released": 1})
        self.assertEqual(replay, {"advanced": 0, "adjusted": 0, "released": 0})
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM capacity_reservations WHERE client_order_id=?",
                (intent.client_order_id,),
            ).fetchone()[0], "released")

    def test_not_submitted_releases_after_matched_full_reconciliation(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        intent = admitted.execution_intent
        broker = make_broker(
            broker_time="2026-07-28T10:00:20+08:00",
            generated_at="2026-07-28T10:00:21+08:00",
        )
        with self.store.transaction() as conn:
            self.store.upsert_order(conn, {
                "client_order_id": intent.client_order_id,
                "signal_id": intent.source_signal_id,
                "order_id": None,
                "stock_code": intent.code,
                "action": intent.side,
                "target_qty": intent.target_position_qty,
                "requested_qty": intent.order_qty,
                "filled_qty": 0,
                "average_fill_price": None,
                "status": "not_submitted",
                "submit_count": 1,
                "reason": "platform did not accept order",
                "first_submitted_at": "2026-07-28T10:00:01+08:00",
                "updated_at": "2026-07-28T10:00:10+08:00",
                "completed_at": "2026-07-28T10:00:10+08:00",
                "raw_json": "{}",
            })
            self.store.replace_current_broker_snapshot(conn, broker)
            self.insert_matched_reconciliation(
                conn, "not-submitted-match", broker,
                "2026-07-28T10:00:22+08:00",
            )
            with localcontext() as context:
                context.prec = 3
                result = synchronize_execution_lifecycle(
                    self.store, conn, "scope-uuid",
                    now="2026-07-28T10:00:23+08:00",
                    reconciliation_id="not-submitted-match",
                )

        self.assertEqual(result, {"advanced": 1, "adjusted": 0, "released": 1})
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM execution_intents WHERE client_order_id=?",
                (intent.client_order_id,),
            ).fetchone()[0], "NOT_SUBMITTED")
            self.assertEqual(conn.execute(
                "SELECT status FROM capacity_reservations WHERE client_order_id=?",
                (intent.client_order_id,),
            ).fetchone()[0], "released")

        retry_candidate = replace(
            intent.pre_trade_result.candidate,
            candidate_id="candidate-retry-after-not-submitted",
            source_signal_id="signal-run-2",
            source_run_id="run-2",
            payload_sha256="",
        )
        retry = admit_candidate(
            self.store,
            AdmissionRequest(
                retry_candidate,
                make_quote(code=intent.code),
                make_rules(code=intent.code),
                make_policy(checked_at="2026-07-28T10:00:23+08:00"),
            ),
            "2026-07-28T10:00:23+08:00",
        )
        self.assertFalse(retry.allowed)
        self.assertIn("DUPLICATE_ORDER", retry.pre_trade_result.hard_blocks)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM execution_intents",
            ).fetchone()[0], 1)

    def test_idless_submitted_snapshot_stays_unknown_and_blocks_reconciliation(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        intent = admitted.execution_intent
        payload = self.full_payload(
            generated_at="2026-07-28T10:00:10+08:00",
            orders=({
                "client_order_id": intent.client_order_id,
                "code": intent.code,
                "action": intent.side,
                "amount": intent.order_qty,
                "target_qty": intent.target_position_qty,
                "filled": 0,
                "status": "submitted",
                "submit_count": 1,
                "first_submitted_at": "2026-07-28T10:00:01+08:00",
                "datetime": "2026-07-28T10:00:10+08:00",
            },),
        )

        result = joinquant_sync.ingest_snapshot_payload(
            payload, self.store, "2026-07-28T10:00:11+08:00", mode="full",
        )

        self.assertEqual(result["reconciliation"].result, "mismatch")
        self.assertIn(
            "ORDER_SUBMIT_UNKNOWN",
            {difference.reason_code for difference in result["reconciliation"].differences},
        )
        with self.store.connect() as conn:
            statuses = conn.execute(
                """SELECT i.status, r.status, o.status
                   FROM execution_intents AS i
                   JOIN capacity_reservations AS r
                     ON r.account_scope_id=i.account_scope_id
                    AND r.client_order_id=i.client_order_id
                   JOIN orders AS o ON o.client_order_id=i.client_order_id
                   WHERE i.client_order_id=?""",
                (intent.client_order_id,),
            ).fetchone()
        self.assertEqual(
            tuple(statuses), ("SUBMIT_UNKNOWN", "active", "submit_unknown"),
        )

    def test_event_only_unknown_cannot_disappear_into_a_matched_full_snapshot(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        intent = admitted.execution_intent
        event = {
            "schema_version": 1,
            "trade_date": "2026-07-28",
            "generated_at": "2026-07-28T10:00:10+08:00",
            "positions": [],
            "orders": [{
                "client_order_id": intent.client_order_id,
                "code": intent.code,
                "action": intent.side,
                "amount": intent.order_qty,
                "target_qty": intent.target_position_qty,
                "filled": 0,
                "status": "submitted",
                "submit_count": 1,
                "first_submitted_at": "2026-07-28T10:00:01+08:00",
                "datetime": "2026-07-28T10:00:10+08:00",
            }],
            "trades": [],
        }
        joinquant_sync.ingest_snapshot_payload(
            event, self.store, "2026-07-28T10:00:10+08:00",
        )

        full = joinquant_sync.ingest_snapshot_payload(
            self.full_payload(
                generated_at="2026-07-28T10:00:12+08:00",
            ),
            self.store,
            "2026-07-28T10:00:13+08:00",
            mode="full",
        )

        self.assertEqual(full["reconciliation"].result, "mismatch")
        self.assertIn(
            "ORDER_SUBMIT_UNKNOWN",
            {item.reason_code for item in full["reconciliation"].differences},
        )
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM capacity_reservations WHERE client_order_id=?",
                (intent.client_order_id,),
            ).fetchone()[0], "active")

    def test_incremental_nonacceptance_cannot_close_unknown_or_resume_buy(self) -> None:
        self.seed()
        admitted = admit_candidate(self.store, self.request(), NOW)
        intent = admitted.execution_intent
        submitted = {
            "client_order_id": intent.client_order_id,
            "code": intent.code,
            "action": intent.side,
            "amount": intent.order_qty,
            "target_qty": intent.target_position_qty,
            "filled": 0,
            "status": "submitted",
            "submit_count": 1,
            "first_submitted_at": "2026-07-28T10:00:01+08:00",
            "datetime": "2026-07-28T10:00:10+08:00",
        }
        not_submitted = dict(submitted)
        not_submitted.update({
            "status": "not_submitted",
            "reason": "platform did not accept order",
            "datetime": "2026-07-28T10:00:12+08:00",
        })

        with patch.object(
            joinquant_sync.app_config,
            "JOINQUANT_TEMPLATE_VERSION",
            "test-v1",
        ):
            first = joinquant_sync.ingest_snapshot_payload(
                self.full_payload(
                    generated_at="2026-07-28T10:00:10+08:00",
                    orders=(submitted,),
                ),
                self.store,
                "2026-07-28T10:00:11+08:00",
                mode="full",
            )
            second = joinquant_sync.ingest_snapshot_payload(
                self.full_payload(
                    generated_at="2026-07-28T10:00:12+08:00",
                    orders=(not_submitted,),
                ),
                self.store,
                "2026-07-28T10:00:13+08:00",
                mode="incremental",
            )
            third = joinquant_sync.ingest_snapshot_payload(
                self.full_payload(
                    generated_at="2026-07-28T10:00:14+08:00",
                ),
                self.store,
                "2026-07-28T10:00:15+08:00",
                mode="incremental",
            )
            fourth = joinquant_sync.ingest_snapshot_payload(
                self.full_payload(
                    generated_at="2026-07-28T10:00:16+08:00",
                ),
                self.store,
                "2026-07-28T10:00:17+08:00",
                mode="incremental",
            )

        self.assertEqual(first["reconciliation"].result, "mismatch")
        for result in (second, third, fourth):
            self.assertEqual(result["reconciliation"].result, "mismatch")
            self.assertIsNone(result["automatic_recovery"])
        self.assertEqual(self.store.get_system_state("buy_enabled"), "0")
        with self.store.connect() as conn:
            state = conn.execute(
                """SELECT i.status, o.status, r.status
                   FROM execution_intents AS i
                   JOIN orders AS o ON o.client_order_id=i.client_order_id
                   JOIN capacity_reservations AS r
                     ON r.account_scope_id=i.account_scope_id
                    AND r.client_order_id=i.client_order_id
                   WHERE i.client_order_id=?""",
                (intent.client_order_id,),
            ).fetchone()
        self.assertEqual(tuple(state), (
            "SUBMIT_UNKNOWN", "not_submitted", "active",
        ))

        with patch.object(
            joinquant_sync.app_config,
            "JOINQUANT_TEMPLATE_VERSION",
            "test-v1",
        ):
            confirmed = joinquant_sync.ingest_snapshot_payload(
                self.full_payload(
                    generated_at="2026-07-28T10:00:18+08:00",
                ),
                self.store,
                "2026-07-28T10:00:19+08:00",
                mode="full",
            )
            recovered = joinquant_sync.ingest_snapshot_payload(
                self.full_payload(
                    generated_at="2026-07-28T10:00:20+08:00",
                ),
                self.store,
                "2026-07-28T10:00:21+08:00",
                mode="full",
            )

        self.assertEqual(confirmed["reconciliation"].result, "matched")
        self.assertEqual(
            confirmed["execution_lifecycle"],
            {"advanced": 1, "adjusted": 0, "released": 1},
        )
        self.assertIsNone(confirmed["automatic_recovery"])
        self.assertEqual(recovered["reconciliation"].result, "matched")
        self.assertEqual(
            recovered["automatic_recovery"]["action"], "auto_resume_buy",
        )
        self.assertEqual(self.store.get_system_state("buy_enabled"), "1")

    def test_sell_admission_requires_and_reserves_the_active_exit_owner(self) -> None:
        held = position(code="600000", total_qty=500, sellable_qty=500)
        broker = make_broker(positions=(held,))
        self.seed(broker)
        with self.store.transaction() as conn:
            self.store.reconcile_position_cycles(conn, [{
                "code": "600000", "qty": 500, "cost_price": 10,
                "current_price": 10, "stop_price": 9.5,
                "entry_time": "2026-07-25T10:00:00+08:00",
            }], "2026-07-28T09:59:40+08:00")
            cycle = conn.execute(
                """SELECT * FROM position_cycles
                   WHERE stock_code='600000' AND status='active'"""
            ).fetchone()
            self.store.insert_position_capacity_adoption(
                conn,
                AdoptedPositionCapacityEvidence(
                    position_cycle_id=str(cycle["position_cycle_id"]),
                    account_scope_id="scope-uuid", adapter="joinquant",
                    code="600000", industry="technology",
                    theme="artificial-intelligence",
                    effective_stop_price=D("9.50"), gap_price=D("9.00"),
                    initial_qty=500,
                    adopted_at="2026-07-28T09:59:50+08:00",
                    source_sha256=broker.snapshot_sha256,
                ),
            )
            self.store.upsert_exit_intent(
                conn, "exit-owner-1", "600000", 400, "take_profit_1",
                "2026-07-28T09:59:55+08:00",
            )

        def sell_request(
            candidate_id: str, owner: str, target_qty: int,
        ) -> AdmissionRequest:
            candidate = replace(
                make_candidate(
                    code="600000", side="sell", exit_owner_id=owner,
                    requested_target_position_qty=target_qty,
                ),
                candidate_id=candidate_id,
                payload_sha256="",
            )
            return AdmissionRequest(
                candidate, make_quote(), make_rules(), make_policy(),
            )

        wrong_owner = admit_candidate(
            self.store,
            sell_request("candidate-sell-wrong", "other-owner", 400), NOW,
        )
        wrong_target = admit_candidate(
            self.store,
            sell_request("candidate-sell-wrong-target", "exit-owner-1", 0), NOW,
        )
        admitted = admit_candidate(
            self.store,
            sell_request("candidate-sell", "exit-owner-1", 400), NOW,
        )
        duplicate = admit_candidate(
            self.store,
            sell_request("candidate-sell-duplicate", "exit-owner-1", 400), NOW,
        )

        self.assertFalse(wrong_owner.allowed)
        self.assertIn("EXIT_OWNER_MISMATCH", wrong_owner.pre_trade_result.hard_blocks)
        self.assertFalse(wrong_target.allowed)
        self.assertIn(
            "EXIT_TARGET_MISMATCH", wrong_target.pre_trade_result.hard_blocks,
        )
        self.assertTrue(admitted.allowed)
        self.assertEqual(admitted.execution_intent.side, "sell")
        self.assertEqual(admitted.execution_intent.target_position_qty, 400)
        self.assertFalse(duplicate.allowed)
        self.assertIn(
            "DUPLICATE_ORDER", duplicate.pre_trade_result.hard_blocks,
        )
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                """SELECT count(*) FROM capacity_reservations
                   WHERE side='sell' AND status='active'"""
            ).fetchone()[0], 1)

        terminal_at = "2026-07-28T10:00:10+08:00"
        broker = make_broker(
            positions=(held,),
            broker_time=terminal_at,
            generated_at="2026-07-28T10:00:11+08:00",
        )
        intent = admitted.execution_intent
        with self.store.transaction() as conn:
            self.store.upsert_order(conn, {
                "client_order_id": intent.client_order_id,
                "signal_id": intent.source_signal_id,
                "order_id": "broker-cancelled-sell",
                "stock_code": intent.code,
                "action": intent.side,
                "target_qty": intent.target_position_qty,
                "requested_qty": intent.order_qty,
                "filled_qty": 0,
                "average_fill_price": 0,
                "status": "cancelled",
                "submit_count": 1,
                "reason": "broker cancelled",
                "first_submitted_at": "2026-07-28T10:00:01+08:00",
                "updated_at": terminal_at,
                "completed_at": terminal_at,
                "raw_json": "{}",
            })
            self.store.replace_current_broker_snapshot(conn, broker)
            self.insert_matched_reconciliation(
                conn, "cancelled-sell-match", broker,
                "2026-07-28T10:00:12+08:00",
            )
            lifecycle = synchronize_execution_lifecycle(
                self.store, conn, "scope-uuid",
                now="2026-07-28T10:00:13+08:00",
                reconciliation_id="cancelled-sell-match",
            )
        self.assertEqual(
            lifecycle, {"advanced": 1, "adjusted": 0, "released": 1},
        )

        retry_request = sell_request(
            "candidate-sell-retry", "exit-owner-1", 400,
        )
        retry_request = replace(
            retry_request,
            candidate=replace(
                retry_request.candidate,
                source_signal_id="signal-run-2",
                source_run_id="run-2",
                payload_sha256="",
            ),
            quote=make_quote(quote_time="2026-07-28T10:00:13+08:00"),
            risk_policy=make_policy(
                checked_at="2026-07-28T10:00:14+08:00",
            ),
        )
        retry = admit_candidate(
            self.store, retry_request, "2026-07-28T10:00:14+08:00",
        )

        self.assertTrue(retry.allowed)
        self.assertNotEqual(retry.client_order_id, admitted.client_order_id)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute(
                """SELECT count(*) FROM capacity_reservations
                   WHERE side='sell' AND status='active'"""
            ).fetchone()[0], 1)

        retry_intent = retry.execution_intent
        broker = make_broker(
            positions=(held,),
            broker_time="2026-07-28T10:00:20+08:00",
            generated_at="2026-07-28T10:00:21+08:00",
        )
        with self.store.transaction() as conn:
            self.store.upsert_order(conn, {
                "client_order_id": retry_intent.client_order_id,
                "signal_id": retry_intent.source_signal_id,
                "order_id": None,
                "stock_code": retry_intent.code,
                "action": retry_intent.side,
                "target_qty": retry_intent.target_position_qty,
                "requested_qty": retry_intent.order_qty,
                "filled_qty": 0,
                "average_fill_price": 0,
                "status": "not_submitted",
                "submit_count": 1,
                "reason": "platform did not accept order",
                "first_submitted_at": "2026-07-28T10:00:15+08:00",
                "updated_at": "2026-07-28T10:00:20+08:00",
                "completed_at": "2026-07-28T10:00:20+08:00",
                "raw_json": "{}",
            })
            self.store.replace_current_broker_snapshot(conn, broker)
            self.insert_matched_reconciliation(
                conn, "not-submitted-sell-match", broker,
                "2026-07-28T10:00:22+08:00",
            )
            lifecycle = synchronize_execution_lifecycle(
                self.store, conn, "scope-uuid",
                now="2026-07-28T10:00:23+08:00",
                reconciliation_id="not-submitted-sell-match",
            )
        self.assertEqual(
            lifecycle, {"advanced": 1, "adjusted": 0, "released": 1},
        )

        blocked_request = sell_request(
            "candidate-sell-auto-reissue", "exit-owner-1", 400,
        )
        blocked_request = replace(
            blocked_request,
            candidate=replace(
                blocked_request.candidate,
                source_signal_id="signal-run-3",
                source_run_id="run-3",
                payload_sha256="",
            ),
            quote=make_quote(quote_time="2026-07-28T10:00:23+08:00"),
            risk_policy=make_policy(
                checked_at="2026-07-28T10:00:24+08:00",
            ),
        )
        blocked = admit_candidate(
            self.store, blocked_request, "2026-07-28T10:00:24+08:00",
        )
        self.assertFalse(blocked.allowed)
        self.assertIn("DUPLICATE_ORDER", blocked.pre_trade_result.hard_blocks)

    def test_legacy_position_requires_persisted_snapshot_bound_adoption(self) -> None:
        held = position(
            code="600001", total_qty=100, sellable_qty=100,
            last_price=D("10"),
        )
        broker = make_broker(positions=(held,))
        self.seed(broker)
        with self.store.transaction() as conn:
            self.store.reconcile_position_cycles(conn, [{
                "code": "600001", "qty": 100, "cost_price": 10,
                "current_price": 10, "stop_price": 9.5,
                "entry_time": "2026-07-25T10:00:00+08:00",
            }], "2026-07-28T09:59:40+08:00")

        request = self.request("600000")
        rejected = admit_candidate(self.store, request, NOW)
        self.assertFalse(rejected.allowed)
        self.assertIn(
            "RESERVATION_EVIDENCE_INCOMPLETE",
            rejected.pre_trade_result.hard_blocks,
        )

        with self.store.transaction() as conn:
            cycle = conn.execute(
                """SELECT * FROM position_cycles
                   WHERE stock_code='600001' AND status='active'"""
            ).fetchone()
            adoption = AdoptedPositionCapacityEvidence(
                position_cycle_id=str(cycle["position_cycle_id"]),
                account_scope_id="scope-uuid",
                adapter="joinquant",
                code="600001",
                industry="healthcare",
                theme="medical",
                effective_stop_price=D("9.50"),
                gap_price=D("9.00"),
                initial_qty=100,
                adopted_at="2026-07-28T09:59:50+08:00",
                source_sha256=broker.snapshot_sha256,
            )
            self.store.insert_position_capacity_adoption(conn, adoption)

        reduced = position(
            code="600001", total_qty=50, sellable_qty=50,
            last_price=D("10"),
        )
        with self.store.transaction() as conn:
            self.store.replace_current_broker_snapshot(
                conn,
                make_broker(
                    broker_time="2026-07-28T09:59:55+08:00",
                    generated_at="2026-07-28T09:59:56+08:00",
                    positions=(reduced,),
                ),
            )
            self.store.reconcile_position_cycles(conn, [{
                "code": "600001", "qty": 50, "cost_price": 10,
                "current_price": 10, "stop_price": 9.5,
                "entry_time": "2026-07-25T10:00:00+08:00",
            }], "2026-07-28T09:59:55+08:00")

        allowed = admit_candidate(
            self.store,
            replace(
                request,
                candidate=replace(
                    request.candidate,
                    candidate_id="candidate-600000-after-adoption",
                    source_signal_id="signal-run-2",
                    source_run_id="run-2",
                    payload_sha256="",
                ),
            ),
            NOW,
        )
        self.assertTrue(allowed.allowed)
        self.assertFalse(allowed.replayed)

    def test_partial_fill_then_cancelled_is_signed_position_evidence(self) -> None:
        self.seed()
        request = self.request("600000")
        with self.store.transaction() as conn:
            self.store.record_strategy_run(conn, StrategyRunRecord(
                run_id=request.candidate.source_run_id,
                trade_date="2026-07-28",
                started_at="2026-07-28T09:55:00+08:00",
                strategy_version="s1",
                parameter_version="p1",
            ))
            self.store.record_signal(conn, SignalRecord(
                signal_id=request.candidate.source_signal_id,
                run_id=request.candidate.source_run_id,
                trade_date="2026-07-28",
                code=request.candidate.code,
                jq_code="600000.XSHG",
                action="buy",
                position_pct=0.01,
                signal_price=10,
                stop_loss=9.5,
                take_profit=11,
                generated_at="2026-07-28T09:55:00+08:00",
                expires_at="2026-07-28T10:05:00+08:00",
                raw_json=json.dumps({
                    "id": request.candidate.source_signal_id,
                    "action": "buy",
                    "code": request.candidate.code,
                    "stop_loss": 9.5,
                    "industry": "technology",
                    "theme": "artificial-intelligence",
                }, sort_keys=True),
            ))
        admitted = admit_candidate(self.store, request, NOW)
        intent = admitted.execution_intent
        filled_qty = 100
        held = position(
            code=intent.code,
            total_qty=filled_qty,
            sellable_qty=0,
            today_buy_qty=filled_qty,
        )
        broker = make_broker(
            broker_time="2026-07-28T10:00:20+08:00",
            generated_at="2026-07-28T10:00:21+08:00",
            positions=(held,),
        )
        with self.store.transaction() as conn:
            self.store.upsert_order(conn, {
                "client_order_id": intent.client_order_id,
                "signal_id": intent.source_signal_id,
                "order_id": "broker-cancelled-partial-1",
                "stock_code": intent.code,
                "action": intent.side,
                "target_qty": intent.target_position_qty,
                "requested_qty": intent.order_qty,
                "filled_qty": filled_qty,
                "average_fill_price": 10,
                "status": "cancelled",
                "submit_count": 1,
                "reason": "unfilled remainder cancelled",
                "first_submitted_at": "2026-07-28T10:00:01+08:00",
                "updated_at": "2026-07-28T10:00:10+08:00",
                "completed_at": "2026-07-28T10:00:10+08:00",
                "raw_json": "{}",
            })
            self.store.replace_current_broker_snapshot(conn, broker)
            self.store.reconcile_position_cycles(conn, [{
                "code": intent.code,
                "qty": filled_qty,
                "cost_price": 10,
                "current_price": 10,
                "stop_price": 9.5,
                "entry_time": "2026-07-28T10:00:01+08:00",
            }], "2026-07-28T10:00:20+08:00")
            self.insert_matched_reconciliation(
                conn, "cancelled-partial-match", broker,
                "2026-07-28T10:00:22+08:00",
            )
            lifecycle = synchronize_execution_lifecycle(
                self.store, conn, "scope-uuid",
                now="2026-07-28T10:00:23+08:00",
                reconciliation_id="cancelled-partial-match",
            )

        self.assertEqual(lifecycle, {
            "advanced": 1, "adjusted": 0, "released": 1,
        })
        next_result = admit_candidate(
            self.store,
            self.request(
                "600001", checked_at="2026-07-28T10:00:23+08:00",
            ),
            "2026-07-28T10:00:23+08:00",
        )
        self.assertTrue(
            next_result.allowed, next_result.pre_trade_result.hard_blocks,
        )

    def test_snapshot_loss_streak_is_used_without_request_override(self) -> None:
        self.seed(make_broker(consecutive_losses=3))

        result = admit_candidate(
            self.store,
            self.request(max_consecutive_losses=3),
            NOW,
        )

        self.assertFalse(result.allowed)
        self.assertIn(
            "CONSECUTIVE_LOSS_LIMIT_EXCEEDED",
            result.pre_trade_result.hard_blocks,
        )


if __name__ == "__main__":
    unittest.main()
