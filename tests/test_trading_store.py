from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal
import sqlite3
import unittest
from unittest.mock import patch

import trading_store
from execution_contracts import (
    BrokerPosition,
    BrokerSnapshot,
    ExecutionIntent,
    PreTradeResult,
)
from tests.test_execution_contracts import (
    intent_values,
    make_candidate,
    pre_trade_values,
)
from trading_store import (
    SCHEMA_VERSION,
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
    SCHEMA_V8,
    SCHEMA_V9,
    SignalConflictError,
    SignalRecord,
    StrategyRunRecord,
    TradingStore,
)


class TradingStoreTest(unittest.TestCase):
    @staticmethod
    def _broker_snapshot(
        scope: str,
        snapshot_id: str,
        *,
        broker_time: str = "2026-07-29T10:00:00+08:00",
        generated_at: str = "2026-07-29T10:00:01+08:00",
        qty: int = 100,
    ) -> BrokerSnapshot:
        return BrokerSnapshot.from_values(
            snapshot_id=snapshot_id,
            account_scope_id=scope,
            trade_date="2026-07-29",
            broker_time=broker_time,
            generated_at=generated_at,
            total_equity=Decimal("10000"),
            cash=Decimal("9000"),
            available_cash=Decimal("9000"),
            frozen_cash=Decimal("0"),
            positions=(
                BrokerPosition.from_values(
                    code="600000", total_qty=qty, sellable_qty=qty,
                    frozen_qty=0, today_buy_qty=0, average_cost="10",
                    last_price="10", market_value=str(qty * 10),
                ),
            ),
            open_orders=({
                "client_order_id": "open-1", "broker_order_id": "jq-1",
                "stock_code": "600001", "side": "buy", "target_qty": 100,
                "filled_qty": 0, "status": "submitted",
                "updated_at": broker_time,
            },),
            fills=(),
            adapter_version="legacy-joinquant-v1-adapter",
            node_version="legacy-joinquant-v1-node",
            session_id="legacy-joinquant-v1-session",
            capabilities_version="legacy-joinquant-v1-capabilities",
        )

    def test_schema_v11_is_idempotent_and_has_scoped_execution_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            store.initialize()

            self.assertEqual(store.health().schema_version, 11)
            with store.connect() as conn:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue({
                    "account_scopes", "broker_snapshot_current",
                    "broker_position_current", "broker_order_current",
                    "strategy_order_candidates", "pre_trade_results",
                    "execution_intents", "capacity_reservations",
                }.issubset(tables))
                cycle_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(position_cycles)")
                }
                self.assertTrue({
                    "profit_protection_activated_at", "trailing_stop_active_from",
                }.issubset(cycle_columns))
                result_fks = conn.execute(
                    "PRAGMA foreign_key_list(pre_trade_results)"
                ).fetchall()
                self.assertEqual(
                    {row[3] for row in result_fks if row[2] == "strategy_order_candidates"},
                    {"account_scope_id", "candidate_id"},
                )
                self.assertEqual(
                    len({
                        row[0] for row in result_fks
                        if row[2] == "strategy_order_candidates"
                    }),
                    1,
                )

    def test_schema_v11_health_rejects_forbidden_global_unique_key(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                conn.execute(
                    """CREATE UNIQUE INDEX forbidden_global_candidate
                       ON strategy_order_candidates(candidate_id)"""
                )
            health = store.health()
            self.assertFalse(health.ok)
            self.assertIn("unique keys", health.error)

    def test_schema_v11_refuses_newer_database_without_mutation(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO schema_migrations VALUES(12, '2026-07-29T00:00:00+08:00')"
                )
                conn.execute("CREATE TABLE sentinel(value TEXT)")
                conn.execute("INSERT INTO sentinel VALUES('keep')")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaisesRegex(RuntimeError, "newer"):
                TradingStore(path).initialize()
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT value FROM sentinel").fetchone()[0], "keep")
                self.assertIsNone(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='account_scopes'"
                ).fetchone())
            finally:
                conn.close()

    def test_schema_v11_migration_rolls_back_all_objects_on_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            statements = (
                trading_store.SCHEMA_V11_STATEMENTS[0],
                "CREATE TABLE broken(",
            )
            with patch.object(
                trading_store, "SCHEMA_V11_STATEMENTS", statements,
            ), self.assertRaises(sqlite3.OperationalError):
                TradingStore(path).initialize()

            conn = sqlite3.connect(path)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    10,
                )
                self.assertIsNone(conn.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name='account_scopes'"""
                ).fetchone())
            finally:
                conn.close()

    def test_current_snapshot_replacement_is_scoped_and_newness_checked(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                first_scope = store.get_or_create_account_scope(conn, "joinquant", "primary")
                second_scope = store.get_or_create_account_scope(conn, "qmt", "paper")
                first = self._broker_snapshot(first_scope, "same-id")
                other = self._broker_snapshot(second_scope, "same-id", qty=200)
                self.assertEqual(store.replace_current_broker_snapshot(conn, first), "same-id")
                self.assertEqual(store.replace_current_broker_snapshot(conn, first), "same-id")
                store.replace_current_broker_snapshot(conn, other)

            with store.connect() as conn:
                self.assertEqual(
                    store.load_current_broker_snapshot(
                        conn, first_scope,
                    ).positions[0].total_qty,
                    100,
                )
                self.assertEqual(
                    store.load_current_broker_snapshot(
                        conn, second_scope,
                    ).positions[0].total_qty,
                    200,
                )
            stale = self._broker_snapshot(
                first_scope, "stale",
                broker_time="2026-07-29T09:59:00+08:00",
                generated_at="2026-07-29T09:59:01+08:00",
            )
            with store.transaction() as conn, self.assertRaisesRegex(ValueError, "stale"):
                store.replace_current_broker_snapshot(conn, stale)

    def test_current_snapshot_conflicts_and_outer_rollback_preserve_children(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
                first = self._broker_snapshot(scope, "first")
                store.replace_current_broker_snapshot(conn, first)
                conn.execute(
                    """CREATE TRIGGER fail_snapshot_insert
                       BEFORE INSERT ON broker_snapshot_current
                       WHEN NEW.snapshot_id='fail'
                       BEGIN SELECT RAISE(ABORT, 'forced snapshot failure'); END"""
                )
            ambiguous = self._broker_snapshot(scope, "ambiguous")
            with store.transaction() as conn, self.assertRaisesRegex(
                ValueError, "ambiguous",
            ):
                store.replace_current_broker_snapshot(conn, ambiguous)
            conflicting = self._broker_snapshot(scope, "first", qty=200)
            with store.transaction() as conn, self.assertRaisesRegex(
                ValueError, "conflicts",
            ):
                store.replace_current_broker_snapshot(conn, conflicting)
            newer = self._broker_snapshot(
                scope, "fail",
                broker_time="2026-07-29T10:01:00+08:00",
                generated_at="2026-07-29T10:01:01+08:00",
                qty=300,
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced"):
                with store.transaction() as conn:
                    store.replace_current_broker_snapshot(conn, newer)
            with store.connect() as conn:
                loaded = store.load_current_broker_snapshot(conn, scope)
                position_row = conn.execute(
                    """SELECT stock_code, total_qty FROM broker_position_current
                       WHERE account_scope_id=?""",
                    (scope,),
                ).fetchone()
                order_row = conn.execute(
                    """SELECT client_order_id, status FROM broker_order_current
                       WHERE account_scope_id=?""",
                    (scope,),
                ).fetchone()
            self.assertEqual(loaded.snapshot_id, "first")
            self.assertEqual(loaded.positions[0].total_qty, 100)
            self.assertEqual(loaded.open_orders[0]["client_order_id"], "open-1")
            self.assertEqual(tuple(position_row), ("600000", 100))
            self.assertEqual(tuple(order_row), ("open-1", "submitted"))

    def test_oversized_current_snapshot_fails_before_mutation(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
                first = self._broker_snapshot(scope, "first")
                store.replace_current_broker_snapshot(conn, first)
            payload = first.to_dict()
            payload["snapshot_id"] = "oversized"
            payload["session_id"] = "x" * (1024 * 1024)
            payload["snapshot_sha256"] = ""
            oversized = BrokerSnapshot(**payload)
            with store.transaction() as conn, self.assertRaisesRegex(
                ValueError, "1 MiB",
            ):
                store.replace_current_broker_snapshot(conn, oversized)
            with store.connect() as conn:
                self.assertEqual(
                    store.load_current_broker_snapshot(
                        conn, scope,
                    ).snapshot_id,
                    "first",
                )

    def test_immutable_chain_and_reservation_are_scoped_and_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            candidate = make_candidate()
            result = PreTradeResult(**pre_trade_values(candidate))
            intent = ExecutionIntent(**intent_values(candidate, result))
            with store.transaction() as conn:
                conn.execute(
                    """INSERT INTO account_scopes(
                       account_scope_id, adapter, scope_alias, created_at
                       ) VALUES(?, 'joinquant', 'primary', datetime('now'))""",
                    (candidate.account_scope_id,),
                )
                self.assertEqual(store.insert_strategy_order_candidate(conn, candidate), candidate.candidate_id)
                self.assertEqual(store.insert_strategy_order_candidate(conn, candidate), candidate.candidate_id)
                self.assertEqual(store.insert_pre_trade_result(conn, result), result.pre_trade_result_id)
                self.assertEqual(store.insert_pre_trade_result(conn, result), result.pre_trade_result_id)
                self.assertEqual(store.insert_execution_intent(conn, intent), intent.client_order_id)
                self.assertEqual(store.insert_execution_intent(conn, intent), intent.client_order_id)
                later_result = PreTradeResult(**pre_trade_values(
                    candidate, pre_trade_result_id="risk-2",
                    checked_at="2026-07-28T09:57:00+08:00",
                    valid_until="2026-07-28T10:02:00+08:00",
                ))
                self.assertEqual(
                    store.insert_pre_trade_result(conn, later_result), "risk-2",
                )
                reservation_id = store.reserve_capacity(
                    conn, account_scope_id=candidate.account_scope_id,
                    reservation_id="reservation-1", client_order_id=intent.client_order_id,
                    stock_code="600000", side="buy", target_qty=100,
                    cash_yuan="1006", position_value_yuan="1000",
                    open_risk_yuan="60", industry="technology",
                    theme="artificial-intelligence", uncategorized=False,
                    created_at="2026-07-29T10:00:00+08:00",
                )
                self.assertEqual(reservation_id, "reservation-1")
                totals = store.aggregate_active_reservations(
                    conn, candidate.account_scope_id,
                )
                self.assertEqual(totals["cash_yuan"], Decimal("1006"))
                self.assertEqual(totals["target_qty"], 100)
                self.assertTrue(store.adjust_capacity_reservation(
                    conn, candidate.account_scope_id, "reservation-1",
                    remaining_target_qty=50, remaining_cash_yuan="503",
                    remaining_position_value_yuan="500",
                    remaining_open_risk_yuan="30",
                ))
                adjusted = store.aggregate_active_reservations(
                    conn, candidate.account_scope_id,
                )
                self.assertEqual(adjusted["cash_yuan"], Decimal("503"))
                self.assertEqual(adjusted["target_qty"], 50)
                with self.assertRaisesRegex(ValueError, "cannot increase"):
                    store.adjust_capacity_reservation(
                        conn, candidate.account_scope_id, "reservation-1",
                        remaining_target_qty=75, remaining_cash_yuan="750",
                        remaining_position_value_yuan="750",
                        remaining_open_risk_yuan="45",
                    )
                self.assertTrue(store.release_capacity_reservation(
                    conn, candidate.account_scope_id, "reservation-1",
                    released_at="2026-07-29T10:01:00+08:00", reason="expired",
                ))
                self.assertFalse(store.release_capacity_reservation(
                    conn, candidate.account_scope_id, "reservation-1",
                    released_at="2026-07-29T10:02:00+08:00", reason="overwrite",
                ))
                row = conn.execute(
                    """SELECT target_qty, remaining_target_qty, release_reason
                       FROM capacity_reservations"""
                ).fetchone()
                self.assertEqual((row[0], row[1], row[2]), (100, 50, "expired"))

    def test_immutable_chain_rejects_changed_embedded_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            original = make_candidate()
            changed = make_candidate(target_price="12")
            with store.transaction() as conn:
                conn.execute(
                    """INSERT INTO account_scopes(
                       account_scope_id, adapter, scope_alias, created_at
                       ) VALUES(?, 'joinquant', 'primary', datetime('now'))""",
                    (original.account_scope_id,),
                )
                store.insert_strategy_order_candidate(conn, original)
                with self.assertRaisesRegex(ValueError, "immutable"):
                    store.insert_strategy_order_candidate(conn, changed)
                changed_result = PreTradeResult(**pre_trade_values(changed))
                with self.assertRaisesRegex(ValueError, "candidate evidence"):
                    store.insert_pre_trade_result(conn, changed_result)

    def test_scope_registry_and_partial_reservation_are_exact(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                first = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
                self.assertEqual(
                    first,
                    store.get_or_create_account_scope(
                        conn, "joinquant", "primary",
                    ),
                )
                self.assertEqual(__import__("uuid").UUID(first).version, 4)
                with self.assertRaisesRegex(ValueError, "adapter"):
                    store.get_or_create_account_scope(conn, "other", "primary")

    def test_schema_v10_marks_historical_fee_and_pnl_evidence_unknown(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "trading.db"
            schemas = (
                SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5,
                SCHEMA_V6, SCHEMA_V7, SCHEMA_V8, SCHEMA_V9,
            )
            with sqlite3.connect(path) as conn:
                for version, schema in enumerate(schemas, 1):
                    conn.executescript(schema)
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                        (version,),
                    )
                conn.execute(
                    """INSERT INTO fills(
                       fill_id, stock_code, action, qty, price, commission, stamp_tax,
                       other_fee, filled_at, raw_json
                       ) VALUES ('legacy-fill','600000','buy',100,10,0,0,0,
                       '2026-07-14 10:00:00','{}')"""
                )
                conn.execute(
                    """INSERT INTO daily_equity(
                       trade_date, opening_equity, closing_equity, cash,
                       position_market_value, realized_pnl, unrealized_pnl, fees,
                       net_deposit, max_drawdown_pct, first_snapshot_at, last_snapshot_at
                       ) VALUES ('2026-07-14',100000,100000,90000,10000,0,0,0,0,0,
                       '2026-07-14 09:30:00','2026-07-14 15:00:00')"""
                )

            store = TradingStore(path)
            store.initialize()

            with store.connect() as conn:
                fill = conn.execute(
                    "SELECT fee_data_status FROM fills WHERE fill_id='legacy-fill'"
                ).fetchone()
                equity = conn.execute(
                    """SELECT fee_data_status, realized_pnl_status
                       FROM daily_equity WHERE trade_date='2026-07-14'"""
                ).fetchone()
            self.assertEqual(store.health().schema_version, SCHEMA_VERSION)
            self.assertEqual(fill["fee_data_status"], "unknown")
            self.assertEqual(equity["fee_data_status"], "unknown")
            self.assertEqual(equity["realized_pnl_status"], "unknown")

    def test_finishes_strategy_run_with_bounded_terminal_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.record_strategy_run(
                    conn,
                    StrategyRunRecord(
                        "run-terminal", "2026-07-26", "2026-07-26 09:30:00", "v1", "p1",
                    ),
                )
                store.finish_strategy_run(
                    conn,
                    "run-terminal",
                    result="failed",
                    data_status="failed",
                    finished_at="2026-07-26 09:30:05",
                    error_message="ValueError: https://example.test/api?token=secret-value " + ("x" * 1000),
                )

            with store.connect() as conn:
                row = conn.execute(
                    "SELECT result, data_status, finished_at, error_message FROM strategy_runs WHERE run_id=?",
                    ("run-terminal",),
                ).fetchone()

            self.assertEqual(row["result"], "failed")
            self.assertEqual(row["data_status"], "failed")
            self.assertEqual(row["finished_at"], "2026-07-26 09:30:05")
            self.assertLessEqual(len(row["error_message"]), 500)
            self.assertNotIn("secret-value", row["error_message"])

    def test_schema_v9_upserts_one_gap_opportunity_per_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            event = {
                "opportunity_id": "gap-20260717-002432",
                "trade_date": "2026-07-17",
                "stock_code": "002432",
                "parent_signal_id": "parent-1",
                "state": "OPEN_OBSERVING",
                "reason": "gap_reentry_open_observing",
                "original_entry_price": 74.72,
                "original_stop_price": 69.49,
                "original_risk_r": 5.23,
                "reentry_cap_price": 77.335,
                "confirmation_count": 1,
                "attempt_count": 1,
            }
            with store.transaction() as conn:
                store.upsert_gap_reentry_opportunity(conn, event)
                store.upsert_gap_reentry_opportunity(
                    conn, {**event, "state": "OPEN_CONFIRMED", "reason": "",
                           "confirmation_count": 2, "planned_qty": 100}
                )
            self.assertEqual(store.health().schema_version, SCHEMA_VERSION)
            row = store.get_gap_reentry_opportunity(event["opportunity_id"])
            self.assertEqual(row["state"], "OPEN_CONFIRMED")
            self.assertEqual(row["planned_qty"], 100)
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM gap_reentry_opportunities"
                ).fetchone()[0], 1)

    def test_schema_v8_tracks_signal_lifecycle_and_execution_issue_transitions(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            self.assertEqual(store.health().schema_version, SCHEMA_VERSION)
            with store.transaction() as conn:
                signal_columns = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
                intent_columns = {row[1] for row in conn.execute("PRAGMA table_info(exit_intents)")}
                self.assertTrue({"validated_at", "published_at"} <= signal_columns)
                self.assertTrue({"validated_at", "published_at"} <= intent_columns)
                issue = {
                    "issue_key": "exit:s-1", "object_type": "exit_intent",
                    "object_id": "s-1", "state": "SIGNAL_DELIVERY_PENDING",
                    "severity": "INFO", "stage_started_at": "2026-07-15 09:30:00",
                    "seen_at": "2026-07-15 09:31:00", "signal_id": "s-1",
                    "order_id": "", "reconciliation_id": "r-1",
                    "details": {"target_qty": 0},
                }
                first = store.upsert_execution_issue(conn, issue)
                replay = store.upsert_execution_issue(
                    conn, {**issue, "seen_at": "2026-07-15 09:31:30"}
                )
                self.assertTrue(first["transitioned"])
                self.assertFalse(replay["transitioned"])
                recovered = store.recover_execution_issue(conn, "exit:s-1", "2026-07-15 09:32:00")
                self.assertEqual(recovered["state"], "RECOVERED")
                self.assertIsNone(store.recover_execution_issue(
                    conn, "exit:s-1", "2026-07-15 09:33:00"
                ))

    def test_signal_insert_is_immutable_and_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord(
                run_id="run-1", trade_date="2026-07-11", started_at="2026-07-11 09:30:00",
                strategy_version="git:abc", parameter_version="risk-observe-v1",
            )
            signal = SignalRecord(
                signal_id="sig-1", run_id="run-1", trade_date="2026-07-11",
                code="600000", jq_code="600000.XSHG", action="buy",
                position_pct=10.0, generated_at="2026-07-11 09:31:00",
                expires_at="2026-07-11 09:51:00", raw_json='{"id":"sig-1"}',
            )
            with store.transaction() as conn:
                self.assertTrue(store.record_strategy_run(conn, run))
                self.assertFalse(store.record_strategy_run(conn, run))
                self.assertTrue(store.record_signal(conn, signal))
                self.assertFalse(store.record_signal(conn, signal))
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM signals WHERE signal_id='sig-1'").fetchone()[0]
            self.assertEqual(count, 1)

    def test_signal_replay_with_changed_payload_raises_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord("run-1", "2026-07-11", "2026-07-11 09:30:00", "v1", "p1")
            original = SignalRecord("sig-1", "run-1", "2026-07-11", "600000", "600000.XSHG", "buy", 10.0, "2026-07-11 09:31:00", "", '{"id":"sig-1","price":10}')
            changed = SignalRecord("sig-1", "run-1", "2026-07-11", "600000", "600000.XSHG", "buy", 10.0, "2026-07-11 09:31:00", "", '{"price":11,"id":"sig-1"}')
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                self.assertTrue(store.record_signal(conn, original))
                with self.assertRaises(SignalConflictError):
                    store.record_signal(conn, changed)

    def test_signal_replay_with_equivalent_canonical_json_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord("run-1", "2026-07-11", "2026-07-11 09:30:00", "v1", "p1")
            first = SignalRecord("sig-1", "run-1", "2026-07-11", "600000", "600000.XSHG", "buy", 10.0, "2026-07-11 09:31:00", "", '{"id":"sig-1","price":10}')
            repeat = SignalRecord("sig-1", "run-1", "2026-07-11", "600000", "600000.XSHG", "buy", 10.0, "2026-07-11 09:31:00", "", '{ "price": 10, "id": "sig-1" }')
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                self.assertTrue(store.record_signal(conn, first))
                self.assertFalse(store.record_signal(conn, repeat))

    def test_system_state_records_latest_value_and_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "0", "ledger unavailable")
            self.assertEqual(store.get_system_state("buy_enabled"), "0")
            with store.connect() as conn:
                reason = conn.execute(
                    "SELECT reason FROM system_state WHERE key = ?", ("buy_enabled",)
                ).fetchone()[0]
            self.assertEqual(reason, "ledger unavailable")

    def test_initialize_creates_current_schema_and_pragmas(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION)
            self.assertTrue({
                "strategy_runs", "signals", "risk_decisions", "system_state",
                "position_cycles", "order_events", "exit_intents", "trade_cooldowns",
                "orders", "fills", "account_snapshots", "position_snapshots",
                "daily_equity", "reconciliation_runs", "reconciliation_items", "control_events",
            }.issubset(tables))
            self.assertEqual(foreign_keys, 1)
            self.assertEqual(busy_timeout, 5000)

    def test_transaction_rolls_back_all_rows_on_error(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with store.transaction() as conn:
                    conn.execute(
                        "INSERT INTO system_state(key, value, updated_at) VALUES (?, ?, datetime('now'))",
                        ("buy_enabled", "1"),
                    )
                    raise RuntimeError("boom")
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM system_state").fetchone()[0]
            self.assertEqual(count, 0)

    def test_initialize_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            store.initialize()
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(count, SCHEMA_VERSION)

    def test_execution_issue_preserves_stage_start_until_state_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            issue = {
                "issue_key": "exit_intent:s-1", "object_type": "exit_intent",
                "object_id": "s-1", "state": "FILL_PENDING", "severity": "WARNING",
                "stage_started_at": "2026-07-15 09:31:00",
                "seen_at": "2026-07-15 09:32:00", "details": {},
            }
            with store.transaction() as conn:
                store.upsert_execution_issue(conn, issue)
                store.upsert_execution_issue(conn, {
                    **issue, "stage_started_at": "2026-07-15 09:32:00",
                    "seen_at": "2026-07-15 09:33:00",
                })
                row = conn.execute(
                    "SELECT stage_started_at FROM execution_issue_state WHERE issue_key=?",
                    (issue["issue_key"],),
                ).fetchone()
            self.assertEqual(row["stage_started_at"], "2026-07-15 09:31:00")

    def test_initialize_migrates_version_five_without_losing_rows(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "trading.db"
            with sqlite3.connect(path) as conn:
                for version, schema in enumerate((SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5), 1):
                    conn.executescript(schema)
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                        (version,),
                    )
                conn.execute(
                    "INSERT INTO system_state(key, value, updated_at, reason) VALUES ('legacy', 'keep', datetime('now'), 'test')"
                )

            store = TradingStore(path)
            store.initialize()

            self.assertEqual(store.health().schema_version, SCHEMA_VERSION)
            self.assertEqual(store.get_system_state("legacy"), "keep")

    def test_prune_execution_history_keeps_mismatch_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                for snapshot_id, trade_date in (("old", "2025-01-01"), ("recent", "2026-07-14")):
                    conn.execute(
                        """INSERT INTO account_snapshots(
                           snapshot_id, trade_date, generated_at, received_at, cash, available_cash,
                           total_value, position_market_value, daily_turnover_pct, daily_pnl_pct,
                           account_drawdown_pct, template_version, state_hash, retained_details, raw_json
                           ) VALUES (?, ?, ?, ?, 1, 1, 1, 0, 0, 0, 0, '', ?, 0, NULL)""",
                        (snapshot_id, trade_date, trade_date + " 15:00:00", trade_date + " 15:00:01", snapshot_id),
                    )
                conn.execute(
                    """INSERT INTO reconciliation_runs VALUES
                       ('matched-old', 'incremental', 'old', '2025-01-01 15:00:01', '2025-01-01 15:00:02', 'matched', 'INFO', 0, '', '{}')"""
                )
                conn.execute(
                    """INSERT INTO reconciliation_runs VALUES
                       ('error-old', 'full', 'old', '2025-01-01 15:00:01', '2025-01-01 15:00:02', 'mismatch', 'ERROR', 1, 'stop_buy', '{}')"""
                )
                conn.execute(
                    """INSERT INTO reconciliation_items(
                       reconciliation_id, category, object_id, reason_code, local_value,
                       platform_value, tolerance, severity, details_json
                       ) VALUES ('error-old', 'position', '600000', 'POSITION_QTY_MISMATCH', '100', '200', 0, 'ERROR', '{}')"""
                )

                deleted = store.prune_execution_history(conn, "2025-07-14", "2026-07-14 16:00:00")

                self.assertEqual(deleted["account_snapshots"], 0)
                self.assertEqual(deleted["reconciliation_runs"], 1)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM reconciliation_runs WHERE result <> 'matched'"
                ).fetchone()[0], 1)

    def test_prune_removes_old_unreferenced_scan_runs_but_keeps_signal_runs(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                for run_id in ("old-empty", "old-signal"):
                    conn.execute(
                        """INSERT INTO strategy_runs(
                           run_id, trade_date, started_at, result, data_status,
                           created_at, updated_at
                           ) VALUES (?, '2025-01-01', '2025-01-01 10:00:00',
                           'success', 'complete', '2025-01-01 10:00:00',
                           '2025-01-01 10:01:00')""",
                        (run_id,),
                    )
                conn.execute(
                    """INSERT INTO signals(
                       signal_id, run_id, trade_date, stock_code, jq_code, action,
                       generated_at, raw_json, created_at
                       ) VALUES ('old-signal-id','old-signal','2025-01-01','600000',
                       '600000.XSHG','buy','2025-01-01 10:00:00','{}',
                       '2025-01-01 10:00:00')"""
                )

                deleted = store.prune_execution_history(
                    conn, "2025-07-14", "2026-07-14 16:00:00",
                )

                self.assertEqual(deleted["strategy_runs"], 1)
                self.assertEqual([
                    row[0] for row in conn.execute(
                        "SELECT run_id FROM strategy_runs ORDER BY run_id"
                    )
                ], ["old-signal"])

    def test_position_cycle_freezes_initial_risk_and_tracks_high_watermark(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10.0,
                    "current_price": 10.5, "stop_price": 9.2, "mode": "short", "atr14": 0.4,
                }], "2026-07-13 09:31:00")
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10.1,
                    "current_price": 12.0, "stop_price": 8.0, "mode": "mid", "atr14": 0.8,
                }], "2026-07-14 09:31:00")
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["initial_stop_price"], 9.3)
            self.assertEqual(cycle["initial_r"], 0.7)
            self.assertEqual(cycle["highest_price"], 12.0)
            self.assertEqual(cycle["mode"], "short")

    def test_position_cycle_closes_and_reopen_gets_new_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            position = {"code": "600000", "qty": 1000, "cost_price": 10, "current_price": 10, "stop_price": 9}
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [position], "2026-07-13 09:31:00")
            first = store.get_active_position_cycles()["600000"]["position_cycle_id"]
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [], "2026-07-14 09:31:00")
                store.reconcile_position_cycles(conn, [position], "2026-07-15 09:31:00")
            second = store.get_active_position_cycles()["600000"]["position_cycle_id"]
            self.assertNotEqual(first, second)

    def test_position_cycle_marks_first_take_profit_after_quantity_reduces(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10,
                    "current_price": 10, "stop_price": 9,
                }], "2026-07-13 09:31:00")
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 500, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-07-14 09:31:00")
            self.assertEqual(store.get_active_position_cycles()["600000"]["take_profit_stage"], 1)

    def test_add_position_updates_weighted_cost_without_lowering_frozen_stop(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10,
                    "current_price": 10, "stop_price": 9.2,
                }], "2026-07-13 09:31:00")
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1500, "cost_price": 10.5,
                    "current_price": 10.8, "stop_price": 8.8,
                }], "2026-07-14 09:31:00")
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["entry_price"], 10.5)
            self.assertEqual(cycle["initial_stop_price"], 9.87)
            self.assertEqual(cycle["initial_r"], 0.63)

    def test_new_position_cycle_uses_latest_buy_signal_risk_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord("run-1", "2026-07-13", "2026-07-13 09:30:00", "v1", "p1")
            signal = SignalRecord(
                "buy-1", "run-1", "2026-07-13", "600000", "600000.XSHG", "buy", 10,
                "2026-07-13 09:31:00", "",
                '{"id":"buy-1","stop_loss":9.1,"signal_type":"short","atr14":0.4,"market_regime":"NORMAL"}',
            )
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                store.record_signal(conn, signal)
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10,
                    "current_price": 10.2, "stop_price": 9.65,
                }], "2026-07-13 09:32:00")
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["entry_signal_id"], "buy-1")
            self.assertEqual(cycle["initial_stop_price"], 9.3)
            self.assertEqual(cycle["mode"], "short")
            self.assertEqual(cycle["atr14"], 0.4)

    def test_manual_stop_is_optional_upward_only_and_audited(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 100, "cost_price": 10,
                    "current_price": 10, "stop_price": 9.3,
                }], "2026-07-16 09:31:00")
                store.set_manual_stop(conn, "600000", 9.6, "收紧风险", now="2026-07-16 09:32:00")
                with self.assertRaisesRegex(ValueError, "只允许上调"):
                    store.set_manual_stop(conn, "600000", 9.5, "尝试放宽")
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["manual_stop_price"], 9.6)
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM control_events WHERE action='set_manual_stop'"
                ).fetchone()[0], 1)

    def test_active_position_classification_is_recovered_from_entry_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord("run-1", "2026-07-13", "2026-07-13 09:30:00", "v1", "p1")
            signal = SignalRecord(
                "buy-1", "run-1", "2026-07-13", "600000", "600000.XSHG", "buy", 10,
                "2026-07-13 09:31:00", "",
                '{"id":"buy-1","industry":"银行","theme_label":"中特估"}',
            )
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                store.record_signal(conn, signal)
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10, "current_price": 10.2,
                }], "2026-07-13 09:32:00")

            self.assertEqual(store.get_active_position_classifications(), {
                "600000": {"industry": "银行", "theme": "中特估"},
            })

    def test_pending_buy_exposure_is_recovered_from_signal_and_order(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord("run-1", "2026-07-13", "2026-07-13 09:30:00", "v1", "p1")
            signal = SignalRecord(
                "buy-1", "run-1", "2026-07-13", "000001", "000001.XSHE", "buy", 8,
                "2026-07-13 09:31:00", "",
                '{"id":"buy-1","industry":"银行","theme":"高股息","position_pct":8}',
            )
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                store.record_signal(conn, signal)
                conn.execute(
                    """INSERT INTO orders(client_order_id,signal_id,stock_code,action,status,updated_at,raw_json)
                       VALUES ('order-1','buy-1','000001','buy','submitted','2026-07-13 09:32:00','{}')"""
                )

            self.assertEqual(store.get_pending_buy_classification_exposures(), [{
                "code": "000001", "industry": "银行", "theme": "高股息", "position_pct": 8.0,
            }])

    def test_online_backup_restores_schema_and_position_cycles(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10,
                    "current_price": 10.2, "stop_price": 9.4,
                }], "2026-07-13 09:31:00")
            backup_path = Path(tmp) / "backups" / "trading.db"

            store.backup_to(backup_path)

            restored = TradingStore(backup_path)
            self.assertTrue(restored.health().ok)
            self.assertEqual(restored.integrity_check(), "ok")
            self.assertIn("600000", restored.get_active_position_cycles())

    def test_online_backup_closes_target_before_returning(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            backup_path = Path(tmp) / "backup.db"

            store.backup_to(backup_path)
            moved_path = Path(tmp) / "moved.db"
            backup_path.replace(moved_path)

            self.assertTrue(moved_path.exists())

    def test_schema_three_reconciles_order_events_idempotently(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            event = {"id": "exit-1", "order_id": "o-1", "code": "600000", "action": "sell",
                     "target_qty": 0, "amount": -1000, "filled": 400, "status": "partial",
                     "reason": "", "datetime": "2026-07-13 10:00:00"}
            with store.transaction() as conn:
                store.reconcile_order_events(conn, [event], "2026-07-13 10:00:01")
                store.reconcile_order_events(conn, [event], "2026-07-13 10:00:02")
            with store.connect() as conn:
                row = conn.execute("SELECT status, filled_qty FROM order_events").fetchone()
                count = conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
            self.assertEqual((row[0], row[1], count), ("partial", 400, 1))

    def test_exit_intent_tracks_partial_fill_and_completes_on_position_target(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.upsert_exit_intent(conn, "exit-1", "600000", 0, "hard_stop", "2026-07-13 10:00:00")
                store.reconcile_order_events(conn, [{"id": "exit-1", "order_id": "o-1", "code": "600000",
                    "action": "sell", "target_qty": 0, "amount": -1000, "filled": 400,
                    "status": "partial", "datetime": "2026-07-13 10:01:00"}], "2026-07-13 10:01:01")
                store.reconcile_exit_intents(conn, [{"code": "600000", "qty": 600}], "2026-07-13 10:02:00")
            self.assertEqual(store.get_open_exit_intents()["600000"]["remaining_qty"], 600)
            with store.transaction() as conn:
                store.reconcile_exit_intents(conn, [], "2026-07-13 10:03:00")
            self.assertEqual(store.get_open_exit_intents(), {})

    def test_higher_priority_exit_supersedes_prior_intent_for_same_stock(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.upsert_exit_intent(conn, "take-1", "600000", 500, "take_profit_1", "2026-07-13 10:00:00")
                store.upsert_exit_intent(conn, "stop-1", "600000", 0, "hard_stop", "2026-07-13 10:01:00")
            self.assertEqual(store.get_open_exit_intents()["600000"]["signal_id"], "stop-1")

    def test_lower_priority_exit_cannot_downgrade_active_hard_stop(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                self.assertTrue(store.upsert_exit_intent(
                    conn, "stop-1", "600000", 0, "hard_stop", "2026-07-13 10:00:00",
                ))
                self.assertFalse(store.upsert_exit_intent(
                    conn, "time-1", "600000", 0, "time_stop", "2026-07-13 10:01:00",
                ))
                self.assertFalse(store.upsert_exit_intent(
                    conn, "take-1", "600000", 500, "take_profit_1", "2026-07-13 10:02:00",
                ))

            active = store.get_open_exit_intents()["600000"]
            self.assertEqual(active["signal_id"], "stop-1")
            self.assertEqual(active["target_qty"], 0)

    def test_market_regime_confirmation_persists_across_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            self.assertEqual(store.confirm_market_regime("RISK_OFF"), "NORMAL")
            self.assertEqual(store.confirm_market_regime("RISK_OFF"), "RISK_OFF")
            self.assertEqual(TradingStore(Path(tmp) / "trading.db").confirm_market_regime("NORMAL"), "RISK_OFF")

    def test_completed_hard_stop_creates_rebuy_cooldown(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.upsert_exit_intent(conn, "cycle-hard_stop-0", "600000", 0, "hard_stop", "2026-07-13 10:00:00")
                store.reconcile_exit_intents(conn, [], "2026-07-13 10:03:00")
            self.assertTrue(store.is_in_cooldown("600000", "2026-07-14"))
