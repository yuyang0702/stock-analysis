from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
from decimal import Decimal, localcontext
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
from pre_trade_check import AdoptedPositionCapacityEvidence
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
        open_orders: tuple[dict[str, object], ...] | None = None,
    ) -> BrokerSnapshot:
        if open_orders is None:
            open_orders = ({
                "client_order_id": "open-1", "broker_order_id": "jq-1",
                "stock_code": "600001", "side": "buy", "target_qty": 100,
                "filled_qty": 0, "status": "submitted",
                "updated_at": broker_time,
            },)
        return BrokerSnapshot.from_values(
            snapshot_id=snapshot_id,
            account_scope_id=scope,
            trade_date=broker_time[:10],
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
            open_orders=open_orders,
            fills=(),
            adapter_version="legacy-joinquant-v1-adapter",
            node_version="legacy-joinquant-v1-node",
            session_id="legacy-joinquant-v1-session",
            capabilities_version="legacy-joinquant-v1-capabilities",
        )

    @staticmethod
    def _insert_matched_reconciliation(
        conn: sqlite3.Connection,
        reconciliation_id: str,
        account_scope_id: str,
        *,
        broker_time: str,
        generated_at: str,
        finished_at: str,
    ) -> None:
        current = conn.execute(
            """SELECT snapshot_id, snapshot_sha256, broker_time, generated_at
               FROM broker_snapshot_current WHERE account_scope_id=?""",
            (account_scope_id,),
        ).fetchone()
        if current is not None:
            snapshot_id = str(current["snapshot_id"])
            snapshot_sha256 = str(current["snapshot_sha256"])
            broker_time = str(current["broker_time"])
            generated_at = str(current["generated_at"])
        else:
            snapshot_id = f"snapshot-{reconciliation_id}"
            snapshot_sha256 = "a" * 64
        conn.execute(
            """INSERT INTO reconciliation_runs(
               reconciliation_id, mode, started_at, finished_at, result,
               severity, difference_count, control_action, summary_json,
               account_scope_id, broker_snapshot_id, broker_snapshot_sha256,
               snapshot_broker_time, snapshot_generated_at
               ) VALUES(?, 'full', ?, ?, 'matched', 'INFO', 0, '', '{}',
                        ?, ?, ?, ?, ?)""",
            (
                reconciliation_id, broker_time, finished_at,
                account_scope_id, snapshot_id, snapshot_sha256,
                broker_time, generated_at,
            ),
        )

    @staticmethod
    def _reserve_intent(
        store: TradingStore,
        conn: sqlite3.Connection,
        candidate: object,
        result: PreTradeResult,
        intent: ExecutionIntent,
        reservation_id: str,
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO account_scopes(
               account_scope_id, adapter, scope_alias, created_at
               ) VALUES(?, 'joinquant', 'primary', datetime('now'))""",
            (candidate.account_scope_id,),
        )
        store.insert_strategy_order_candidate(conn, candidate)
        store.insert_pre_trade_result(conn, result)
        store.insert_execution_intent(conn, intent)
        store.reserve_capacity(
            conn, account_scope_id=candidate.account_scope_id,
            reservation_id=reservation_id,
            client_order_id=intent.client_order_id,
            stock_code=candidate.code, side=candidate.side,
            target_qty=intent.order_qty,
            cash_yuan="1006.01", position_value_yuan="1000",
            open_risk_yuan="112.37", industry=candidate.industry,
            theme=candidate.theme, uncategorized=candidate.uncategorized,
            created_at="2026-07-28T10:00:00+08:00",
        )

    @staticmethod
    def _insert_local_admission_order(
        conn: sqlite3.Connection,
        candidate: object,
        intent: ExecutionIntent,
        *,
        status: str = "ready",
        submit_count: int = 0,
        first_submitted_at: str | None = None,
    ) -> None:
        payload = {
            "source": "execution_admission",
            "client_order_id": intent.client_order_id,
            "intent_sha256": intent.intent_sha256,
            "pre_trade_result_id": intent.pre_trade_result_id,
            "target_qty": intent.target_position_qty,
            "requested_qty": intent.order_qty,
        }
        updated_at = (
            "2026-07-28T10:00:02+08:00"
            if status == "not_submitted"
            else "2026-07-28T10:00:00+08:00"
        )
        conn.execute(
            """INSERT INTO orders(
               client_order_id, stock_code, action, target_qty,
               requested_qty, filled_qty, average_fill_price, status,
               submit_count, reason, first_submitted_at, updated_at,
               completed_at, raw_json
               ) VALUES(?,?,?,?,?,0,0,?,?,?,?,?,?,?)""",
            (
                intent.client_order_id, candidate.code, candidate.side,
                intent.target_position_qty, intent.order_qty, status,
                submit_count, "", first_submitted_at,
                updated_at,
                updated_at if status in {"expired", "not_submitted"} else None,
                trading_store.canonical_json(payload),
            ),
        )

    def test_schema_v12_is_idempotent_and_has_scoped_execution_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            store.initialize()

            self.assertEqual(store.health().schema_version, SCHEMA_VERSION)
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
                    "position_capacity_adoptions",
                    "notification_outbox", "notification_enqueue_gaps",
                    "logical_signal_plans",
                }.issubset(tables))
                issue_columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(execution_issue_state)"
                    )
                }
                self.assertTrue({
                    "incident_id", "transition_seq",
                    "critical_trading_minutes",
                    "critical_last_counted_minute", "next_reminder_seq",
                }.issubset(issue_columns))
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
                for table in (
                    "broker_position_current", "broker_order_current",
                ):
                    parent_tables = {
                        row[2]
                        for row in conn.execute(
                            f"PRAGMA foreign_key_list({table})"
                        )
                    }
                    self.assertEqual(
                        parent_tables,
                        {"account_scopes", "broker_snapshot_current"},
                    )
                for table, chain_parent in (
                    ("pre_trade_results", "strategy_order_candidates"),
                    ("execution_intents", "pre_trade_results"),
                    ("capacity_reservations", "execution_intents"),
                ):
                    parent_tables = {
                        row[2]
                        for row in conn.execute(
                            f"PRAGMA foreign_key_list({table})"
                        )
                    }
                    self.assertEqual(
                        parent_tables,
                        {"account_scopes", chain_parent},
                    )
                adoption_parents = {
                    row[2]
                    for row in conn.execute(
                        "PRAGMA foreign_key_list(position_capacity_adoptions)"
                    )
                }
                self.assertEqual(
                    adoption_parents, {"account_scopes", "position_cycles"},
                )

    def test_schema_v11_initializes_sell_enabled_without_overwriting_it(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            self.assertEqual(store.get_system_state("sell_enabled"), "1")
            with store.connect() as conn:
                store.set_system_state(conn, "sell_enabled", "0", "manual")
            store.initialize()
            self.assertEqual(store.get_system_state("sell_enabled"), "0")

    def test_position_capacity_adoption_is_scoped_immutable_and_recoverable(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            scope = "scope-adoption"
            with store.transaction() as conn:
                conn.execute(
                    """INSERT INTO account_scopes(
                       account_scope_id, adapter, scope_alias, created_at
                       ) VALUES(?, 'joinquant', 'primary', datetime('now'))""",
                    (scope,),
                )
                conn.execute(
                    """INSERT INTO position_cycles(
                       position_cycle_id, stock_code, opened_at, status, mode,
                       initial_qty, current_qty, entry_price, initial_stop_price,
                       initial_r, atr14, market_state, highest_price,
                       take_profit_stage, last_snapshot_at, created_at, updated_at
                       ) VALUES('cycle-adoption','600000',?,'active','legacy_fixed',
                       100,200,10,9,1,0.5,'normal',10,0,?,?,?)""",
                    (
                        "2026-07-28T09:30:00+08:00",
                        "2026-07-28T10:00:00+08:00",
                        "2026-07-28T10:00:00+08:00",
                        "2026-07-28T10:00:00+08:00",
                    ),
                )
                snapshot = self._broker_snapshot(
                    scope, "adoption-source", qty=200, open_orders=(),
                )
                store.replace_current_broker_snapshot(conn, snapshot)
                evidence = AdoptedPositionCapacityEvidence(
                    position_cycle_id="cycle-adoption",
                    account_scope_id=scope,
                    adapter="joinquant",
                    code="600000",
                    industry="technology",
                    theme="artificial-intelligence",
                    effective_stop_price=Decimal("9"),
                    gap_price=Decimal("8.5"),
                    initial_qty=200,
                    adopted_at="2026-07-28T10:00:00+08:00",
                    source_sha256=snapshot.snapshot_sha256,
                )
                with self.assertRaisesRegex(ValueError, "source"):
                    store.insert_position_capacity_adoption(
                        conn,
                        AdoptedPositionCapacityEvidence.from_dict({
                            **evidence.to_dict(), "source_sha256": "b" * 64,
                        }),
                    )
                with self.assertRaisesRegex(ValueError, "broker snapshot"):
                    store.insert_position_capacity_adoption(
                        conn,
                        AdoptedPositionCapacityEvidence.from_dict({
                            **evidence.to_dict(), "initial_qty": 100,
                        }),
                    )
                self.assertEqual(
                    store.insert_position_capacity_adoption(conn, evidence),
                    "cycle-adoption",
                )
                self.assertEqual(
                    store.insert_position_capacity_adoption(conn, evidence),
                    "cycle-adoption",
                )
                payloads = store.list_position_capacity_adoptions(conn, scope)
                self.assertEqual(
                    AdoptedPositionCapacityEvidence.from_dict(payloads[0]),
                    evidence,
                )
                conflicting = AdoptedPositionCapacityEvidence.from_dict({
                    **evidence.to_dict(),
                    "effective_stop_price": "8.9",
                })
                with self.assertRaisesRegex(ValueError, "immutable ID"):
                    store.insert_position_capacity_adoption(conn, conflicting)

    def test_position_capacity_adoption_rejects_unbound_scope_or_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            evidence = AdoptedPositionCapacityEvidence(
                position_cycle_id="missing-cycle",
                account_scope_id="missing-scope",
                adapter="joinquant",
                code="600000",
                industry="technology",
                theme="artificial-intelligence",
                effective_stop_price=Decimal("9"),
                gap_price=Decimal("8.5"),
                initial_qty=100,
                adopted_at="2026-07-28T10:00:00+08:00",
                source_sha256="a" * 64,
            )
            with store.transaction() as conn, self.assertRaisesRegex(
                ValueError, "account scope",
            ):
                store.insert_position_capacity_adoption(conn, evidence)

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
            self.assertIn("unique", health.error)

    def test_schema_health_rejects_extra_legacy_unique_index(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                conn.execute(
                    """CREATE UNIQUE INDEX forbidden_order_stock
                       ON orders(stock_code)"""
                )
            health = store.health()
            self.assertFalse(health.ok)
            self.assertEqual(health.schema_version, SCHEMA_VERSION)
            self.assertIn("unique", health.error)

    def test_schema_health_rejects_duplicate_unique_signature(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                conn.execute(
                    """CREATE UNIQUE INDEX duplicate_order_id_unique
                       ON orders(order_id)"""
                )
            health = store.health()
            self.assertFalse(health.ok)
            self.assertIn("unique", health.error)

    def test_schema_health_rejects_broadened_partial_unique_predicate(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                conn.execute("DROP INDEX idx_position_cycles_active_code")
                conn.execute(
                    """CREATE UNIQUE INDEX idx_position_cycles_active_code
                       ON position_cycles(stock_code)
                       WHERE status = 'active' OR 1=1"""
                )
            health = store.health()
            self.assertFalse(health.ok)
            self.assertIn("idx_position_cycles_active_code", health.error)

    def test_schema_health_rejects_index_collation_or_direction_change(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                conn.execute("DROP INDEX idx_position_cycles_active_code")
                conn.execute(
                    """CREATE UNIQUE INDEX idx_position_cycles_active_code
                       ON position_cycles(stock_code COLLATE NOCASE DESC)
                       WHERE status='active'"""
                )
            health = store.health()
            self.assertFalse(health.ok)
            self.assertIn("idx_position_cycles_active_code", health.error)

    def test_schema_health_rejects_primary_key_collation_or_direction_change(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                conn.execute("DROP TABLE system_state")
                conn.execute(
                    """CREATE TABLE system_state(
                       key TEXT COLLATE NOCASE,
                       value TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       reason TEXT NOT NULL DEFAULT '',
                       PRIMARY KEY(key DESC)
                       )"""
                )
            health = store.health()
            self.assertFalse(health.ok)
            self.assertIn("primary key index", health.error)

    def test_schema_v11_health_and_initialize_reject_missing_legacy_table(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                conn.execute("DROP TABLE orders")
            self.assertFalse(store.health().ok)
            self.assertIn("orders", store.health().error)
            with self.assertRaisesRegex(RuntimeError, "orders"):
                store.initialize()

    def test_schema_v11_health_rejects_missing_legacy_column(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                conn.execute(
                    "ALTER TABLE daily_equity DROP COLUMN fee_data_status"
                )
            health = store.health()
            self.assertFalse(health.ok)
            self.assertIn("fee_data_status", health.error)

    def test_malformed_schema_v10_is_not_repaired_or_upgraded(self) -> None:
        corruptions = (
            ("missing table", "DROP TABLE orders", "orders"),
            (
                "missing column",
                "ALTER TABLE daily_equity DROP COLUMN fee_data_status",
                "fee_data_status",
            ),
        )
        for label, corruption, expected in corruptions:
            with self.subTest(label=label), TemporaryDirectory() as tmp:
                path = Path(tmp) / "trading.db"
                store = TradingStore(path)
                store.initialize()
                with store.connect() as conn:
                    for table in (
                        "logical_signal_plans", "notification_enqueue_gaps",
                        "notification_outbox",
                        "capacity_reservations", "execution_intents",
                        "pre_trade_results", "strategy_order_candidates",
                        "broker_position_current", "broker_order_current",
                        "broker_snapshot_current", "account_scopes",
                    ):
                        conn.execute(f"DROP TABLE {table}")
                    conn.execute(
                        "DELETE FROM schema_migrations WHERE version>=11"
                    )
                    conn.execute(corruption)
                with self.assertRaisesRegex(RuntimeError, expected):
                    store.initialize()
                conn = sqlite3.connect(path)
                try:
                    self.assertEqual(
                        conn.execute(
                            "SELECT MAX(version) FROM schema_migrations"
                        ).fetchone()[0],
                        10,
                    )
                    self.assertIsNone(conn.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type='table' AND name='account_scopes'"""
                    ).fetchone())
                finally:
                    conn.close()

    def test_schema_v12_refuses_newer_database_without_mutation(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO schema_migrations VALUES(13, '2026-07-29T00:00:00+08:00')"
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

    def test_schema_v11_migrates_to_v12_without_rotating_account_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            with patch.object(
                TradingStore, "_migrate_schema_v12", return_value=None,
            ):
                old_store = TradingStore(path)
                old_store.initialize()
                with old_store.transaction() as conn:
                    scope = old_store.get_or_create_account_scope(
                        conn, "joinquant", "primary",
                    )
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    11,
                )

            store = TradingStore(path)
            store.initialize()
            self.assertEqual(store.health().schema_version, 12)
            with store.transaction() as conn:
                self.assertEqual(
                    store.get_or_create_account_scope(
                        conn, "joinquant", "primary",
                    ),
                    scope,
                )

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

    def test_current_snapshot_requires_caller_owned_transaction(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
                store.replace_current_broker_snapshot(
                    conn, self._broker_snapshot(scope, "first"),
                )
            newer = self._broker_snapshot(
                scope, "newer",
                broker_time="2026-07-29T10:01:00+08:00",
                generated_at="2026-07-29T10:01:01+08:00",
            )
            with store.connect() as conn:
                with self.assertRaisesRegex(ValueError, "BEGIN IMMEDIATE"):
                    store.replace_current_broker_snapshot(conn, newer)
            with store.connect() as conn:
                conn.execute("BEGIN")
                with self.assertRaisesRegex(ValueError, "BEGIN IMMEDIATE"):
                    store.replace_current_broker_snapshot(conn, newer)
                conn.rollback()
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
                    valid_until="2026-07-28T10:01:00+08:00",
                ))
                self.assertEqual(
                    store.insert_pre_trade_result(conn, later_result), "risk-2",
                )
                reservation_id = store.reserve_capacity(
                    conn, account_scope_id=candidate.account_scope_id,
                    reservation_id="reservation-1", client_order_id=intent.client_order_id,
                    stock_code="600000", side="buy", target_qty=100,
                    cash_yuan="1006.01", position_value_yuan="1000",
                    open_risk_yuan="112.37", industry="technology",
                    theme="artificial-intelligence", uncategorized=False,
                    created_at="2026-07-28T10:00:00+08:00",
                )
                self.assertEqual(reservation_id, "reservation-1")
                totals = store.aggregate_active_reservations(
                    conn, candidate.account_scope_id,
                )
                self.assertEqual(totals["cash_yuan"], Decimal("1006.01"))
                self.assertEqual(totals["target_qty"], 100)
                active = store.list_active_reservations(
                    conn, candidate.account_scope_id,
                )
                self.assertEqual(len(active), 1)
                self.assertEqual(
                    active[0]["account_scope_id"],
                    candidate.account_scope_id,
                )
                self.assertEqual(active[0]["reservation_id"], "reservation-1")
                self.assertEqual(active[0]["client_order_id"], intent.client_order_id)
                self.assertEqual(active[0]["intent_status"], "READY")
                self.assertEqual(active[0]["original_target_qty"], 100)
                self.assertEqual(
                    active[0]["remaining_cash_yuan"], Decimal("1006.01"),
                )
                self.assertNotIn("stock_code", active[0])
                self.assertNotIn("status", active[0])
                self.assertTrue(store.compare_and_set_execution_intent_status(
                    conn,
                    candidate.account_scope_id,
                    intent.client_order_id,
                    expected_status="READY",
                    new_status="SUBMITTING",
                    transitioned_at="2026-07-28T10:00:01+08:00",
                ))
                self.assertEqual(
                    store.list_active_reservations(
                        conn, candidate.account_scope_id,
                    )[0]["intent_status"],
                    "SUBMITTING",
                )
                other_scope = store.get_or_create_account_scope(
                    conn, "qmt", "paper",
                )
                self.assertEqual(
                    store.list_active_reservations(conn, other_scope),
                    [],
                )
                later_intent = ExecutionIntent(**intent_values(
                    candidate, later_result,
                ))
                store.insert_execution_intent(conn, later_intent)
                store.reserve_capacity(
                    conn, account_scope_id=candidate.account_scope_id,
                    reservation_id="reservation-0",
                    client_order_id=later_intent.client_order_id,
                    stock_code="600000", side="buy", target_qty=100,
                    cash_yuan="1006.01", position_value_yuan="1000",
                    open_risk_yuan="112.37", industry="technology",
                    theme="artificial-intelligence", uncategorized=False,
                    created_at="2026-07-28T10:00:00+08:00",
                )
                self.assertEqual(
                    [
                        row["reservation_id"]
                        for row in store.list_active_reservations(
                            conn, candidate.account_scope_id,
                        )
                    ],
                    ["reservation-0", "reservation-1"],
                )
                self.assertTrue(store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id,
                    later_intent.client_order_id,
                    expected_status="READY", new_status="EXPIRED",
                    transitioned_at="2026-07-28T10:01:00+08:00",
                ))
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "expired-empty",
                        broker_time="2026-07-28T10:01:01+08:00",
                        generated_at="2026-07-28T10:01:02+08:00",
                        open_orders=(),
                    ),
                )
                self.assertTrue(store.release_capacity_reservation(
                    conn, candidate.account_scope_id, "reservation-0",
                    released_at="2026-07-28T10:01:03+08:00",
                    reason="test cleanup",
                ))
                self.assertTrue(store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="SUBMITTING", new_status="SUBMITTED",
                    transitioned_at="2026-07-28T10:00:02+08:00",
                ))
                self.assertTrue(store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="SUBMITTED", new_status="PARTIALLY_FILLED",
                    transitioned_at="2026-07-28T10:01:00+08:00",
                ))
                conn.execute(
                    """INSERT INTO orders(
                       client_order_id, stock_code, action, target_qty,
                       requested_qty, filled_qty, average_fill_price, status,
                       submit_count, reason, updated_at, raw_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent.client_order_id, candidate.code, "buy", 100,
                        100, 50, 10, "partial", 1, "",
                        "2026-07-28T10:01:00+08:00", "{}",
                    ),
                )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "partial-50",
                        broker_time="2026-07-28T10:01:04+08:00",
                        generated_at="2026-07-28T10:01:05+08:00",
                        open_orders=({
                            "client_order_id": intent.client_order_id,
                            "broker_order_id": "jq-partial-1",
                            "stock_code": candidate.code, "side": "buy",
                            "target_qty": 100, "filled_qty": 50,
                            "status": "partially_filled",
                            "updated_at": "2026-07-28T10:01:00+08:00",
                        },),
                    ),
                )
                with localcontext() as context:
                    context.prec = 2
                    self.assertTrue(store.adjust_capacity_reservation(
                        conn, candidate.account_scope_id, "reservation-1",
                        cumulative_filled_qty=50,
                    ))
                adjusted = store.aggregate_active_reservations(
                    conn, candidate.account_scope_id,
                )
                self.assertEqual(adjusted["cash_yuan"], Decimal("503.01"))
                self.assertEqual(adjusted["target_qty"], 50)
                self.assertEqual(
                    store.reserve_capacity(
                        conn, account_scope_id=candidate.account_scope_id,
                        reservation_id="reservation-1",
                        client_order_id=intent.client_order_id,
                        stock_code="600000", side="buy", target_qty=100,
                        cash_yuan="1006.01", position_value_yuan="1000",
                        open_risk_yuan="112.37", industry="technology",
                        theme="artificial-intelligence",
                        uncategorized=False,
                        created_at="2026-07-28T10:00:00+08:00",
                    ),
                    "reservation-1",
                )
                replayed = conn.execute(
                    """SELECT remaining_target_qty, remaining_cash_yuan, status
                       FROM capacity_reservations
                       WHERE reservation_id='reservation-1'"""
                ).fetchone()
                self.assertEqual(
                    tuple(replayed), (50, "503.01", "active"),
                )
                conn.execute(
                    """UPDATE orders SET filled_qty=25
                       WHERE client_order_id=?""",
                    (intent.client_order_id,),
                )
                with self.assertRaisesRegex(ValueError, "partial-fill evidence"):
                    store.adjust_capacity_reservation(
                        conn, candidate.account_scope_id, "reservation-1",
                        cumulative_filled_qty=25,
                    )
                conn.execute(
                    """UPDATE orders SET filled_qty=100, status='filled'
                       WHERE client_order_id=?""",
                    (intent.client_order_id,),
                )
                self.assertTrue(store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="PARTIALLY_FILLED", new_status="FILLED",
                    transitioned_at="2026-07-28T10:02:00+08:00",
                ))
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "filled-empty",
                        broker_time="2026-07-28T10:02:01+08:00",
                        generated_at="2026-07-28T10:02:02+08:00",
                        open_orders=(),
                    ),
                )
                self._insert_matched_reconciliation(
                    conn, "matched-idempotent", candidate.account_scope_id,
                    broker_time="2026-07-28T10:02:01+08:00",
                    generated_at="2026-07-28T10:02:02+08:00",
                    finished_at="2026-07-28T10:02:03+08:00",
                )
                self.assertTrue(store.release_capacity_reservation(
                    conn, candidate.account_scope_id, "reservation-1",
                    released_at="2026-07-28T10:02:04+08:00", reason="filled",
                    reconciliation_id="matched-idempotent",
                ))
                self.assertFalse(store.release_capacity_reservation(
                    conn, candidate.account_scope_id, "reservation-1",
                    released_at="2026-07-28T10:03:00+08:00", reason="overwrite",
                ))
                self.assertEqual(
                    store.reserve_capacity(
                        conn, account_scope_id=candidate.account_scope_id,
                        reservation_id="reservation-1",
                        client_order_id=intent.client_order_id,
                        stock_code="600000", side="buy", target_qty=100,
                        cash_yuan="1006.01", position_value_yuan="1000",
                        open_risk_yuan="112.37", industry="technology",
                        theme="artificial-intelligence",
                        uncategorized=False,
                        created_at="2026-07-28T10:00:00+08:00",
                    ),
                    "reservation-1",
                )
                row = conn.execute(
                    """SELECT target_qty, remaining_target_qty, release_reason,
                              status
                       FROM capacity_reservations
                       WHERE reservation_id='reservation-1'"""
                ).fetchone()
                self.assertEqual(
                    tuple(row), (100, 50, "filled", "released"),
                )

    def test_execution_intent_and_capacity_inputs_fail_closed(self) -> None:
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
                store.insert_strategy_order_candidate(conn, candidate)
                store.insert_pre_trade_result(conn, result)
                with self.assertRaisesRegex(ValueError, "READY"):
                    store.insert_execution_intent(
                        conn, intent, status="SUBMITTED",
                    )
                store.insert_execution_intent(conn, intent)
                base = {
                    "account_scope_id": candidate.account_scope_id,
                    "reservation_id": "invalid-reservation",
                    "client_order_id": intent.client_order_id,
                    "stock_code": "600000", "side": "buy",
                    "target_qty": 100, "cash_yuan": "1006.01",
                    "position_value_yuan": "1000",
                    "open_risk_yuan": "112.37",
                    "industry": "technology",
                    "theme": "artificial-intelligence",
                    "uncategorized": False,
                    "created_at": "2026-07-28T10:00:00+08:00",
                }
                invalid_cases = (
                    {"target_qty": True},
                    {"target_qty": -1},
                    {"target_qty": 1.5},
                    {"target_qty": float("inf")},
                    {"side": "hold"},
                    {"side": "sell"},
                    {"uncategorized": 1},
                    {"reservation_id": ""},
                    {"stock_code": ""},
                    {"industry": "", "uncategorized": False},
                    {"industry": "__UNCATEGORIZED__", "uncategorized": False},
                    {"uncategorized": True},
                    {"created_at": ""},
                    {"created_at": "2026-07-28T10:00:00"},
                    {"stock_code": "000001"},
                    {"target_qty": 50},
                    {"cash_yuan": "1006"},
                    {"position_value_yuan": "999"},
                    {"open_risk_yuan": "112"},
                )
                for index, changes in enumerate(invalid_cases):
                    conn.execute(f"SAVEPOINT invalid_{index}")
                    try:
                        with self.assertRaises(ValueError):
                            store.reserve_capacity(conn, **{**base, **changes})
                    finally:
                        conn.execute(f"ROLLBACK TO invalid_{index}")
                        conn.execute(f"RELEASE invalid_{index}")
                store.reserve_capacity(conn, **{
                    **base, "reservation_id": "valid-reservation",
                })
                for invalid_qty in (True, -1, 1.5, float("nan")):
                    with self.assertRaises(ValueError):
                        store.adjust_capacity_reservation(
                            conn, candidate.account_scope_id,
                            "valid-reservation",
                            cumulative_filled_qty=invalid_qty,
                        )

    def test_sell_reservation_matches_intent_without_consuming_buy_capacity(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            candidate = make_candidate(side="sell")
            result = PreTradeResult(**pre_trade_values(candidate))
            intent = ExecutionIntent(**intent_values(candidate, result))
            with store.transaction() as conn:
                conn.execute(
                    """INSERT INTO account_scopes(
                       account_scope_id, adapter, scope_alias, created_at
                       ) VALUES(?, 'joinquant', 'primary', datetime('now'))""",
                    (candidate.account_scope_id,),
                )
                store.insert_strategy_order_candidate(conn, candidate)
                store.insert_pre_trade_result(conn, result)
                store.insert_execution_intent(conn, intent)
                reservation = {
                    "account_scope_id": candidate.account_scope_id,
                    "reservation_id": "sell-reservation",
                    "client_order_id": intent.client_order_id,
                    "stock_code": candidate.code, "side": "sell",
                    "target_qty": intent.order_qty,
                    "cash_yuan": "0", "position_value_yuan": "0",
                    "open_risk_yuan": "0",
                    "industry": candidate.industry,
                    "theme": candidate.theme,
                    "uncategorized": candidate.uncategorized,
                    "created_at": "2026-07-28T10:00:00+08:00",
                }
                self.assertEqual(
                    store.reserve_capacity(conn, **reservation),
                    "sell-reservation",
                )
                for field in (
                    "cash_yuan", "position_value_yuan", "open_risk_yuan",
                ):
                    with self.assertRaisesRegex(ValueError, "sell"):
                        store.reserve_capacity(
                            conn,
                            **{
                                **reservation,
                                "reservation_id": f"invalid-{field}",
                                field: "1",
                            },
                        )

    def test_execution_intent_cas_enforces_the_frozen_state_graph(self) -> None:
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
                store.insert_strategy_order_candidate(conn, candidate)
                store.insert_pre_trade_result(conn, result)
                store.insert_execution_intent(conn, intent)
                for expected, new in (
                    ("", "SUBMITTING"),
                    ("READY", ""),
                    ("UNKNOWN", "SUBMITTING"),
                    ("READY", "UNKNOWN"),
                    ("READY", "FILLED"),
                ):
                    with self.assertRaises(ValueError):
                        store.compare_and_set_execution_intent_status(
                            conn, candidate.account_scope_id,
                            intent.client_order_id,
                            expected_status=expected, new_status=new,
                            transitioned_at="2026-07-28T10:00:00+08:00",
                        )
                with self.assertRaisesRegex(ValueError, "expired"):
                    store.compare_and_set_execution_intent_status(
                        conn, candidate.account_scope_id,
                        intent.client_order_id,
                        expected_status="READY", new_status="SUBMITTING",
                        transitioned_at="2026-07-28T10:01:00+08:00",
                    )
                self.assertTrue(store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="READY", new_status="SUBMITTING",
                    transitioned_at="2026-07-28T10:00:00+08:00",
                ))
                self.assertTrue(store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="SUBMITTING", new_status="SUBMIT_UNKNOWN",
                    transitioned_at="2026-07-28T10:00:01+08:00",
                ))
                with self.assertRaises(ValueError):
                    store.compare_and_set_execution_intent_status(
                        conn, candidate.account_scope_id,
                        intent.client_order_id,
                        expected_status="SUBMIT_UNKNOWN", new_status="READY",
                        transitioned_at="2026-07-28T10:00:02+08:00",
                    )
                for recovered_status in (
                    "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED",
                    "REJECTED", "NOT_SUBMITTED",
                ):
                    conn.execute(
                        """UPDATE execution_intents SET status='SUBMIT_UNKNOWN'
                           WHERE account_scope_id=? AND client_order_id=?""",
                        (candidate.account_scope_id, intent.client_order_id),
                    )
                    self.assertTrue(
                        store.compare_and_set_execution_intent_status(
                            conn, candidate.account_scope_id,
                            intent.client_order_id,
                            expected_status="SUBMIT_UNKNOWN",
                            new_status=recovered_status,
                            transitioned_at="2026-07-28T10:00:02+08:00",
                        )
                    )

    def test_reservation_release_requires_aware_time_and_reason(self) -> None:
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
                store.insert_strategy_order_candidate(conn, candidate)
                store.insert_pre_trade_result(conn, result)
                store.insert_execution_intent(conn, intent)
                store.reserve_capacity(
                    conn, account_scope_id=candidate.account_scope_id,
                    reservation_id="release-reservation",
                    client_order_id=intent.client_order_id,
                    stock_code=candidate.code, side="buy",
                    target_qty=intent.order_qty,
                    cash_yuan="1006.01", position_value_yuan="1000",
                    open_risk_yuan="112.37",
                    industry=candidate.industry, theme=candidate.theme,
                    uncategorized=candidate.uncategorized,
                    created_at="2026-07-28T10:00:00+08:00",
                )
                with self.assertRaises(ValueError):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "release-reservation",
                        released_at="2026-07-28T10:01:00",
                        reason="expired",
                    )
                with self.assertRaises(ValueError):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "release-reservation",
                        released_at="2026-07-28T10:01:00+08:00",
                        reason="",
                    )

    def test_reservation_lifecycle_requires_authoritative_execution_evidence(self) -> None:
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
                store.insert_strategy_order_candidate(conn, candidate)
                store.insert_pre_trade_result(conn, result)
                store.insert_execution_intent(conn, intent)
                store.reserve_capacity(
                    conn, account_scope_id=candidate.account_scope_id,
                    reservation_id="evidence-reservation",
                    client_order_id=intent.client_order_id,
                    stock_code=candidate.code, side="buy",
                    target_qty=intent.order_qty,
                    cash_yuan="1006.01", position_value_yuan="1000",
                    open_risk_yuan="112.37",
                    industry=candidate.industry, theme=candidate.theme,
                    uncategorized=candidate.uncategorized,
                    created_at="2026-07-28T10:00:00+08:00",
                )
                with self.assertRaisesRegex(ValueError, "before expiry"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "evidence-reservation",
                        released_at="2026-07-28T10:00:00+08:00",
                        reason="not expired",
                    )
                with self.assertRaisesRegex(ValueError, "EXPIRED"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "evidence-reservation",
                        released_at="2026-07-28T10:01:00+08:00",
                        reason="expired without intent transition",
                    )
                store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="READY", new_status="SUBMITTING",
                    transitioned_at="2026-07-28T10:00:00+08:00",
                )
                with self.assertRaisesRegex(ValueError, "PARTIALLY_FILLED"):
                    store.adjust_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "evidence-reservation", cumulative_filled_qty=50,
                    )
                with self.assertRaisesRegex(ValueError, "cannot be released"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "evidence-reservation",
                        released_at="2026-07-28T10:01:00+08:00",
                        reason="unsafe early release",
                    )
                store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="SUBMITTING", new_status="SUBMITTED",
                    transitioned_at="2026-07-28T10:00:01+08:00",
                )
                store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="SUBMITTED", new_status="PARTIALLY_FILLED",
                    transitioned_at="2026-07-28T10:01:00+08:00",
                )
                conn.execute(
                    """INSERT INTO orders(
                       client_order_id, stock_code, action, target_qty,
                       requested_qty, filled_qty, average_fill_price, status,
                       submit_count, reason, updated_at, raw_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent.client_order_id, candidate.code, "buy", 100,
                        100, 50, 10, "partial", 1, "",
                        "2026-07-28T10:01:00+08:00", "{}",
                    ),
                )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "evidence-partial-50",
                        broker_time="2026-07-28T10:01:01+08:00",
                        generated_at="2026-07-28T10:01:02+08:00",
                        open_orders=({
                            "client_order_id": intent.client_order_id,
                            "broker_order_id": "jq-evidence-partial",
                            "stock_code": candidate.code, "side": "buy",
                            "target_qty": 100, "filled_qty": 50,
                            "status": "partially_filled",
                            "updated_at": "2026-07-28T10:01:00+08:00",
                        },),
                    ),
                )
                with self.assertRaisesRegex(ValueError, "partial-fill evidence"):
                    store.adjust_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "evidence-reservation", cumulative_filled_qty=40,
                    )
                self.assertTrue(store.adjust_capacity_reservation(
                    conn, candidate.account_scope_id,
                    "evidence-reservation", cumulative_filled_qty=50,
                ))
                remaining = conn.execute(
                    """SELECT remaining_target_qty, remaining_cash_yuan,
                              remaining_position_value_yuan,
                              remaining_open_risk_yuan
                       FROM capacity_reservations
                       WHERE reservation_id='evidence-reservation'"""
                ).fetchone()
                self.assertEqual(
                    tuple(remaining), (50, "503.01", "500", "56.19"),
                )
                conn.execute(
                    """UPDATE orders SET filled_qty=100, status='filled',
                       updated_at='2026-07-28T10:02:00+08:00'
                       WHERE client_order_id=?""",
                    (intent.client_order_id,),
                )
                store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="PARTIALLY_FILLED", new_status="FILLED",
                    transitioned_at="2026-07-28T10:02:00+08:00",
                )
                conn.execute(
                    "UPDATE orders SET status='cancelled' WHERE client_order_id=?",
                    (intent.client_order_id,),
                )
                with self.assertRaisesRegex(ValueError, "reconciliation_id"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "evidence-reservation",
                        released_at="2026-07-28T10:02:01+08:00",
                        reason="missing reconciliation",
                    )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "terminal-empty",
                        broker_time="2026-07-28T10:02:01+08:00",
                        generated_at="2026-07-28T10:02:02+08:00",
                        open_orders=(),
                    ),
                )
                self._insert_matched_reconciliation(
                    conn, "matched-wrong-status", candidate.account_scope_id,
                    broker_time="2026-07-28T10:02:01+08:00",
                    generated_at="2026-07-28T10:02:02+08:00",
                    finished_at="2026-07-28T10:02:03+08:00",
                )
                with self.assertRaisesRegex(ValueError, "FILLED.*order"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "evidence-reservation",
                        released_at="2026-07-28T10:02:04+08:00",
                        reason="wrong terminal order",
                        reconciliation_id="matched-wrong-status",
                    )
                conn.execute(
                    """UPDATE orders SET status='filled',
                       updated_at='2026-07-28T10:02:05+08:00'
                       WHERE client_order_id=?""",
                    (intent.client_order_id,),
                )
                self._insert_matched_reconciliation(
                    conn, "matched-stale", candidate.account_scope_id,
                    broker_time="2026-07-28T10:02:03+08:00",
                    generated_at="2026-07-28T10:02:04+08:00",
                    finished_at="2026-07-28T10:02:05+08:00",
                )
                with self.assertRaisesRegex(ValueError, "predates"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "evidence-reservation",
                        released_at="2026-07-28T10:02:06+08:00",
                        reason="stale reconciliation",
                        reconciliation_id="matched-stale",
                    )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "terminal-release",
                        broker_time="2026-07-28T10:02:06+08:00",
                        generated_at="2026-07-28T10:02:07+08:00",
                        open_orders=(),
                    ),
                )
                self._insert_matched_reconciliation(
                    conn, "matched-release", candidate.account_scope_id,
                    broker_time="2026-07-28T10:02:06+08:00",
                    generated_at="2026-07-28T10:02:07+08:00",
                    finished_at="2026-07-28T10:02:08+08:00",
                )
                self.assertTrue(store.release_capacity_reservation(
                    conn, candidate.account_scope_id,
                    "evidence-reservation",
                    released_at="2026-07-28T10:02:09+08:00",
                    reason="terminal order reconciled",
                    reconciliation_id="matched-release",
                ))

    def test_terminal_release_requires_fresh_scoped_snapshot_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            candidate = make_candidate()
            result = PreTradeResult(**pre_trade_values(candidate))
            intent = ExecutionIntent(**intent_values(candidate, result))
            with store.transaction() as conn:
                self._reserve_intent(
                    store, conn, candidate, result, intent,
                    "scoped-release",
                )
                for expected, new, transitioned_at in (
                    ("READY", "SUBMITTING", "2026-07-28T10:00:00+08:00"),
                    ("SUBMITTING", "SUBMITTED", "2026-07-28T10:00:01+08:00"),
                    ("SUBMITTED", "FILLED", "2026-07-28T10:00:02+08:00"),
                ):
                    store.compare_and_set_execution_intent_status(
                        conn, candidate.account_scope_id,
                        intent.client_order_id, expected_status=expected,
                        new_status=new, transitioned_at=transitioned_at,
                    )
                conn.execute(
                    """INSERT INTO orders(
                       client_order_id, stock_code, action, target_qty,
                       requested_qty, filled_qty, average_fill_price, status,
                       submit_count, reason, updated_at, raw_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent.client_order_id, candidate.code, "buy", 500,
                        100, 100, 10, "filled", 1, "",
                        "2026-07-28T10:00:02+08:00", "{}",
                    ),
                )
                conn.execute(
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, started_at, finished_at,
                       result, severity, difference_count, control_action,
                       summary_json
                       ) VALUES('legacy-null','full',?,?,?,?,0,'','{}')""",
                    (
                        "2026-07-28T10:00:03+08:00",
                        "2026-07-28T10:00:04+08:00", "matched", "INFO",
                    ),
                )
                with self.assertRaisesRegex(ValueError, "snapshot evidence"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id, "scoped-release",
                        released_at="2026-07-28T10:00:10+08:00",
                        reason="legacy evidence", reconciliation_id="legacy-null",
                    )

                other_scope = store.get_or_create_account_scope(
                    conn, "qmt", "paper",
                )
                self._insert_matched_reconciliation(
                    conn, "cross-scope", other_scope,
                    broker_time="2026-07-28T10:00:03+08:00",
                    generated_at="2026-07-28T10:00:04+08:00",
                    finished_at="2026-07-28T10:00:05+08:00",
                )
                with self.assertRaisesRegex(ValueError, "account scope"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id, "scoped-release",
                        released_at="2026-07-28T10:00:10+08:00",
                        reason="cross scope", reconciliation_id="cross-scope",
                    )

                self._insert_matched_reconciliation(
                    conn, "prior-day", candidate.account_scope_id,
                    broker_time="2026-07-27T15:00:00+08:00",
                    generated_at="2026-07-27T15:00:01+08:00",
                    finished_at="2026-07-28T10:00:05+08:00",
                )
                with self.assertRaisesRegex(ValueError, "predates"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id, "scoped-release",
                        released_at="2026-07-28T10:00:10+08:00",
                        reason="stale snapshot", reconciliation_id="prior-day",
                    )

                self._insert_matched_reconciliation(
                    conn, "fresh-match", candidate.account_scope_id,
                    broker_time="2026-07-28T10:00:03+08:00",
                    generated_at="2026-07-28T10:00:04+08:00",
                    finished_at="2026-07-28T10:00:05+08:00",
                )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "current-order",
                        broker_time="2026-07-28T10:00:06+08:00",
                        generated_at="2026-07-28T10:00:07+08:00",
                        open_orders=({
                            "client_order_id": intent.client_order_id,
                            "broker_order_id": "jq-still-open",
                            "stock_code": candidate.code, "side": "buy",
                            "target_qty": 100, "filled_qty": 0,
                            "status": "submitted",
                            "updated_at": "2026-07-28T10:00:06+08:00",
                        },),
                    ),
                )
                with self.assertRaisesRegex(ValueError, "current broker order"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id, "scoped-release",
                        released_at="2026-07-28T10:00:08+08:00",
                        reason="broker order still open",
                        reconciliation_id="fresh-match",
                    )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "newer-current",
                        broker_time="2026-07-28T10:00:08+08:00",
                        generated_at="2026-07-28T10:00:09+08:00",
                        open_orders=(),
                    ),
                )
                with self.assertRaisesRegex(
                    ValueError, "does not describe current broker snapshot",
                ):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id, "scoped-release",
                        released_at="2026-07-28T10:00:10+08:00",
                        reason="stale matched snapshot",
                        reconciliation_id="fresh-match",
                    )
                self._insert_matched_reconciliation(
                    conn, "newer-match", candidate.account_scope_id,
                    broker_time="2026-07-28T10:00:08+08:00",
                    generated_at="2026-07-28T10:00:09+08:00",
                    finished_at="2026-07-28T10:00:09+08:00",
                )
                self.assertTrue(store.release_capacity_reservation(
                    conn, candidate.account_scope_id, "scoped-release",
                    released_at="2026-07-28T10:00:10+08:00",
                    reason="fresh scoped evidence",
                    reconciliation_id="newer-match",
                ))

    def test_expired_release_requires_post_expiry_snapshot_without_order(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            candidate = make_candidate()
            result = PreTradeResult(**pre_trade_values(candidate))
            intent = ExecutionIntent(**intent_values(candidate, result))
            with store.transaction() as conn:
                self._reserve_intent(
                    store, conn, candidate, result, intent,
                    "expired-release",
                )
                store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="READY", new_status="EXPIRED",
                    transitioned_at="2026-07-28T10:01:00+08:00",
                )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "expired-has-order",
                        broker_time="2026-07-28T10:01:01+08:00",
                        generated_at="2026-07-28T10:01:02+08:00",
                        open_orders=({
                            "client_order_id": intent.client_order_id,
                            "broker_order_id": "jq-expired",
                            "stock_code": candidate.code, "side": "buy",
                            "target_qty": 100, "filled_qty": 0,
                            "status": "submitted",
                            "updated_at": "2026-07-28T10:01:01+08:00",
                        },),
                    ),
                )
                with self.assertRaisesRegex(ValueError, "current broker order"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id, "expired-release",
                        released_at="2026-07-28T10:01:03+08:00",
                        reason="order still exists",
                    )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "expired-empty-later",
                        broker_time="2026-07-28T10:01:03+08:00",
                        generated_at="2026-07-28T10:01:04+08:00",
                        open_orders=(),
                    ),
                )
                self.assertTrue(store.release_capacity_reservation(
                    conn, candidate.account_scope_id, "expired-release",
                    released_at="2026-07-28T10:01:05+08:00",
                    reason="post-expiry absence confirmed",
                ))

    def test_expired_local_admission_order_releases_only_if_never_submitted(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            candidate = make_candidate()
            result = PreTradeResult(**pre_trade_values(candidate))
            intent = ExecutionIntent(**intent_values(candidate, result))
            with store.transaction() as conn:
                self._reserve_intent(
                    store, conn, candidate, result, intent,
                    "local-expired-release",
                )
                self._insert_local_admission_order(conn, candidate, intent)
                with self.assertRaisesRegex(ValueError, "transition to EXPIRED"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "local-expired-release",
                        released_at="2026-07-28T10:01:01+08:00",
                        reason="still ready",
                    )
                store.compare_and_set_execution_intent_status(
                    conn, candidate.account_scope_id, intent.client_order_id,
                    expected_status="READY", new_status="EXPIRED",
                    transitioned_at="2026-07-28T10:01:00+08:00",
                )
                conn.execute(
                    """UPDATE orders SET status='expired', completed_at=?,
                       updated_at=? WHERE client_order_id=?""",
                    (
                        "2026-07-28T10:01:00+08:00",
                        "2026-07-28T10:01:00+08:00",
                        intent.client_order_id,
                    ),
                )
                invalid_updates = (
                    (
                        "broker id", "order_id='broker-should-not-exist'",
                        "order_id=NULL", "broker order id",
                    ),
                    (
                        "fill", "filled_qty=1", "filled_qty=0", "fill evidence",
                    ),
                    (
                        "submit count", "submit_count=1", "submit_count=0",
                        "submission evidence",
                    ),
                    (
                        "submission time",
                        "first_submitted_at='2026-07-28T10:00:10+08:00'",
                        "first_submitted_at=NULL", "submission evidence",
                    ),
                    (
                        "quantity", "requested_qty=200", "requested_qty=100",
                        "execution intent",
                    ),
                )
                for label, invalid, repair, error in invalid_updates:
                    with self.subTest(label=label):
                        conn.execute(
                            f"UPDATE orders SET {invalid} WHERE client_order_id=?",
                            (intent.client_order_id,),
                        )
                        with self.assertRaisesRegex(ValueError, error):
                            store.release_capacity_reservation(
                                conn, candidate.account_scope_id,
                                "local-expired-release",
                                released_at="2026-07-28T10:01:01+08:00",
                                reason="invalid local expiry",
                            )
                        conn.execute(
                            f"UPDATE orders SET {repair} WHERE client_order_id=?",
                            (intent.client_order_id,),
                        )
                original_json = conn.execute(
                    "SELECT raw_json FROM orders WHERE client_order_id=?",
                    (intent.client_order_id,),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE orders SET raw_json='{}' WHERE client_order_id=?",
                    (intent.client_order_id,),
                )
                with self.assertRaisesRegex(ValueError, "admission content"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "local-expired-release",
                        released_at="2026-07-28T10:01:01+08:00",
                        reason="invalid content",
                    )
                conn.execute(
                    "UPDATE orders SET raw_json=? WHERE client_order_id=?",
                    (original_json, intent.client_order_id),
                )
                with self.assertRaisesRegex(ValueError, "current broker snapshot"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "local-expired-release",
                        released_at="2026-07-28T10:01:01+08:00",
                        reason="snapshot still required",
                    )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "local-expired-empty",
                        broker_time="2026-07-28T10:01:01+08:00",
                        generated_at="2026-07-28T10:01:02+08:00",
                        open_orders=(),
                    ),
                )
                self.assertTrue(store.release_capacity_reservation(
                    conn, candidate.account_scope_id,
                    "local-expired-release",
                    released_at="2026-07-28T10:01:03+08:00",
                    reason="strict local expiry",
                ))

    def test_not_submitted_local_order_still_requires_full_reconciliation(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            candidate = make_candidate()
            result = PreTradeResult(**pre_trade_values(candidate))
            intent = ExecutionIntent(**intent_values(candidate, result))
            with store.transaction() as conn:
                self._reserve_intent(
                    store, conn, candidate, result, intent,
                    "local-not-submitted-release",
                )
                self._insert_local_admission_order(
                    conn, candidate, intent, status="not_submitted",
                    submit_count=1,
                    first_submitted_at="2026-07-28T10:00:01+08:00",
                )
                for expected, new, transitioned_at in (
                    ("READY", "SUBMITTING", "2026-07-28T10:00:00+08:00"),
                    (
                        "SUBMITTING", "NOT_SUBMITTED",
                        "2026-07-28T10:00:02+08:00",
                    ),
                ):
                    store.compare_and_set_execution_intent_status(
                        conn, candidate.account_scope_id,
                        intent.client_order_id, expected_status=expected,
                        new_status=new, transitioned_at=transitioned_at,
                    )
                store.replace_current_broker_snapshot(
                    conn,
                    self._broker_snapshot(
                        candidate.account_scope_id, "not-submitted-empty",
                        broker_time="2026-07-28T10:00:03+08:00",
                        generated_at="2026-07-28T10:00:04+08:00",
                        open_orders=(),
                    ),
                )
                self._insert_matched_reconciliation(
                    conn, "not-submitted-match", candidate.account_scope_id,
                    broker_time="2026-07-28T10:00:03+08:00",
                    generated_at="2026-07-28T10:00:04+08:00",
                    finished_at="2026-07-28T10:00:05+08:00",
                )
                with self.assertRaisesRegex(ValueError, "full reconciliation"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "local-not-submitted-release",
                        released_at="2026-07-28T10:00:06+08:00",
                        reason="missing reconciliation",
                        reconciliation_id="missing-reconciliation",
                    )
                original_json = conn.execute(
                    "SELECT raw_json FROM orders WHERE client_order_id=?",
                    (intent.client_order_id,),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE orders SET raw_json='{}' WHERE client_order_id=?",
                    (intent.client_order_id,),
                )
                with self.assertRaisesRegex(ValueError, "admission content"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "local-not-submitted-release",
                        released_at="2026-07-28T10:00:06+08:00",
                        reason="wrong content",
                        reconciliation_id="not-submitted-match",
                    )
                conn.execute(
                    "UPDATE orders SET raw_json=? WHERE client_order_id=?",
                    (original_json, intent.client_order_id),
                )
                conn.execute(
                    """INSERT INTO fills(
                       fill_id, client_order_id, stock_code, action, qty,
                       price, filled_at, raw_json
                       ) VALUES('unexpected-fill',?,'600000','buy',1,10,?,'{}')""",
                    (intent.client_order_id, "2026-07-28T10:00:01+08:00"),
                )
                with self.assertRaisesRegex(ValueError, "fill evidence"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "local-not-submitted-release",
                        released_at="2026-07-28T10:00:06+08:00",
                        reason="unexpected fill",
                        reconciliation_id="not-submitted-match",
                    )
                conn.execute("DELETE FROM fills WHERE fill_id='unexpected-fill'")
                self.assertTrue(store.release_capacity_reservation(
                    conn, candidate.account_scope_id,
                    "local-not-submitted-release",
                    released_at="2026-07-28T10:00:06+08:00",
                    reason="authoritative absence",
                    reconciliation_id="not-submitted-match",
                ))

    def test_partial_adjustment_binds_broker_order_and_fill_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            candidate = make_candidate()
            result = PreTradeResult(**pre_trade_values(candidate))
            intent = ExecutionIntent(**intent_values(candidate, result))
            with store.transaction() as conn:
                self._reserve_intent(
                    store, conn, candidate, result, intent,
                    "identity-adjust",
                )
                for expected, new, transitioned_at in (
                    ("READY", "SUBMITTING", "2026-07-28T10:00:00+08:00"),
                    ("SUBMITTING", "SUBMITTED", "2026-07-28T10:00:01+08:00"),
                    (
                        "SUBMITTED", "PARTIALLY_FILLED",
                        "2026-07-28T10:00:02+08:00",
                    ),
                ):
                    store.compare_and_set_execution_intent_status(
                        conn, candidate.account_scope_id,
                        intent.client_order_id, expected_status=expected,
                        new_status=new, transitioned_at=transitioned_at,
                    )
                conn.execute(
                    """INSERT INTO orders(
                       client_order_id, order_id, stock_code, action,
                       target_qty, requested_qty, filled_qty,
                       average_fill_price, status, submit_count, reason,
                       updated_at, raw_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent.client_order_id, "jq-partial", candidate.code,
                        "buy", 100, 100, 50, 10, "partial", 1, "",
                        "2026-07-28T10:00:02+08:00", "{}",
                    ),
                )

                def replace_order(snapshot_id: str, **changes: object) -> None:
                    order = {
                        "client_order_id": intent.client_order_id,
                        "broker_order_id": "jq-partial",
                        "stock_code": candidate.code, "side": "buy",
                        "target_qty": 100, "filled_qty": 50,
                        "status": "partially_filled",
                        "updated_at": "2026-07-28T10:00:02+08:00",
                        **changes,
                    }
                    sequence = int(snapshot_id.rsplit("-", 1)[1])
                    store.replace_current_broker_snapshot(
                        conn,
                        self._broker_snapshot(
                            candidate.account_scope_id, snapshot_id,
                            broker_time=(
                                f"2026-07-28T10:00:{sequence:02d}+08:00"
                            ),
                            generated_at=(
                                f"2026-07-28T10:00:{sequence + 1:02d}+08:00"
                            ),
                            open_orders=(order,),
                        ),
                    )

                for snapshot_id, changes in (
                    ("identity-10", {"stock_code": "000001"}),
                    ("identity-12", {"side": "sell"}),
                    ("identity-14", {"target_qty": 200}),
                ):
                    replace_order(snapshot_id, **changes)
                    with self.assertRaisesRegex(ValueError, "broker order"):
                        store.adjust_capacity_reservation(
                            conn, candidate.account_scope_id,
                            "identity-adjust", cumulative_filled_qty=50,
                        )
                replace_order("identity-16")
                conn.execute(
                    """UPDATE orders SET stock_code='000001'
                       WHERE client_order_id=?""",
                    (intent.client_order_id,),
                )
                with self.assertRaisesRegex(ValueError, "legacy order"):
                    store.adjust_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "identity-adjust", cumulative_filled_qty=50,
                    )
                conn.execute(
                    """UPDATE orders SET stock_code=?
                       WHERE client_order_id=?""",
                    (candidate.code, intent.client_order_id),
                )
                conn.execute(
                    """INSERT INTO fills(
                       fill_id, client_order_id, order_id, stock_code, action,
                       qty, price, filled_at, raw_json
                       ) VALUES('wrong-fill', NULL, 'jq-partial', '000001',
                                'buy', 50, 10,
                                '2026-07-28T10:00:02+08:00', '{}')""",
                )
                with self.assertRaisesRegex(ValueError, "fill identity"):
                    store.adjust_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "identity-adjust", cumulative_filled_qty=50,
                    )
                conn.execute("DELETE FROM fills WHERE fill_id='wrong-fill'")
                self.assertTrue(store.adjust_capacity_reservation(
                    conn, candidate.account_scope_id,
                    "identity-adjust", cumulative_filled_qty=50,
                ))

    def test_new_reservation_must_share_the_intent_creation_transaction(self) -> None:
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
                store.insert_strategy_order_candidate(conn, candidate)
                store.insert_pre_trade_result(conn, result)
                store.insert_execution_intent(conn, intent)
            with store.transaction() as conn:
                with self.assertRaisesRegex(ValueError, "same transaction"):
                    store.reserve_capacity(
                        conn, account_scope_id=candidate.account_scope_id,
                        reservation_id="late-reservation",
                        client_order_id=intent.client_order_id,
                        stock_code=candidate.code, side="buy",
                        target_qty=intent.order_qty,
                        cash_yuan="1006.01", position_value_yuan="1000",
                        open_risk_yuan="112.37",
                        industry=candidate.industry, theme=candidate.theme,
                        uncategorized=candidate.uncategorized,
                        created_at="2026-07-28T10:00:00+08:00",
                    )

    def test_manual_transaction_boundary_invalidates_new_intent_marker(self) -> None:
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
                store.insert_strategy_order_candidate(conn, candidate)
                store.insert_pre_trade_result(conn, result)
                store.insert_execution_intent(conn, intent)
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(ValueError, "same transaction"):
                    store.reserve_capacity(
                        conn, account_scope_id=candidate.account_scope_id,
                        reservation_id="committed-reservation",
                        client_order_id=intent.client_order_id,
                        stock_code=candidate.code, side="buy",
                        target_qty=intent.order_qty,
                        cash_yuan="1006.01", position_value_yuan="1000",
                        open_risk_yuan="112.37",
                        industry=candidate.industry, theme=candidate.theme,
                        uncategorized=candidate.uncategorized,
                        created_at="2026-07-28T10:00:00+08:00",
                    )

    def test_terminal_release_binds_transition_and_order_identity(self) -> None:
        for case in ("old_reconciliation", "wrong_order"):
            with self.subTest(case=case), TemporaryDirectory() as tmp:
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
                    store.insert_strategy_order_candidate(conn, candidate)
                    store.insert_pre_trade_result(conn, result)
                    store.insert_execution_intent(conn, intent)
                    store.reserve_capacity(
                        conn, account_scope_id=candidate.account_scope_id,
                        reservation_id="release-binding",
                        client_order_id=intent.client_order_id,
                        stock_code=candidate.code, side="buy",
                        target_qty=intent.order_qty,
                        cash_yuan="1006.01", position_value_yuan="1000",
                        open_risk_yuan="112.37",
                        industry=candidate.industry, theme=candidate.theme,
                        uncategorized=candidate.uncategorized,
                        created_at="2026-07-28T10:00:00+08:00",
                    )
                    store.compare_and_set_execution_intent_status(
                        conn, candidate.account_scope_id,
                        intent.client_order_id,
                        expected_status="READY", new_status="SUBMITTING",
                        transitioned_at="2026-07-28T10:00:00+08:00",
                    )
                    if case == "old_reconciliation":
                        self._insert_matched_reconciliation(
                            conn, "old-match", candidate.account_scope_id,
                            broker_time="2026-07-28T10:00:20+08:00",
                            generated_at="2026-07-28T10:00:25+08:00",
                            finished_at="2026-07-28T10:00:30+08:00",
                        )
                        store.compare_and_set_execution_intent_status(
                            conn, candidate.account_scope_id,
                            intent.client_order_id,
                            expected_status="SUBMITTING",
                            new_status="NOT_SUBMITTED",
                            transitioned_at="2026-07-28T10:01:00+08:00",
                        )
                        error = "predates"
                        reconciliation_id = "old-match"
                    else:
                        store.compare_and_set_execution_intent_status(
                            conn, candidate.account_scope_id,
                            intent.client_order_id,
                            expected_status="SUBMITTING", new_status="SUBMITTED",
                            transitioned_at="2026-07-28T10:01:00+08:00",
                        )
                        store.compare_and_set_execution_intent_status(
                            conn, candidate.account_scope_id,
                            intent.client_order_id,
                            expected_status="SUBMITTED", new_status="FILLED",
                            transitioned_at="2026-07-28T10:01:01+08:00",
                        )
                        conn.execute(
                            """INSERT INTO orders(
                               client_order_id, stock_code, action, target_qty,
                               requested_qty, filled_qty, average_fill_price,
                               status, submit_count, reason, updated_at, raw_json
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                intent.client_order_id, "000001", "sell", 1,
                                1, 1, 10, "filled", 1, "",
                                "2026-07-28T10:01:00+08:00", "{}",
                            ),
                        )
                        self._insert_matched_reconciliation(
                            conn, "wrong-order-match",
                            candidate.account_scope_id,
                            broker_time="2026-07-28T10:01:10+08:00",
                            generated_at="2026-07-28T10:01:15+08:00",
                            finished_at="2026-07-28T10:01:20+08:00",
                        )
                        error = "execution intent"
                        reconciliation_id = "wrong-order-match"
                    with self.assertRaisesRegex(ValueError, error):
                        store.release_capacity_reservation(
                            conn, candidate.account_scope_id,
                            "release-binding",
                            released_at="2026-07-28T10:02:00+08:00",
                            reason="must remain reserved",
                            reconciliation_id=reconciliation_id,
                        )
                    self.assertEqual(conn.execute(
                        """SELECT status FROM capacity_reservations
                           WHERE reservation_id='release-binding'"""
                    ).fetchone()[0], "active")

    def test_order_status_only_advances_and_late_full_fill_wins(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()

            def order(
                client_id: str,
                status: str,
                filled_qty: int,
                updated_at: str,
            ) -> dict:
                return {
                    "client_order_id": client_id,
                    "signal_id": None,
                    "order_id": client_id,
                    "stock_code": "600000",
                    "action": "buy",
                    "target_qty": 100,
                    "requested_qty": 100,
                    "filled_qty": filled_qty,
                    "average_fill_price": 10 if filled_qty else 0,
                    "status": status,
                    "submit_count": 1,
                    "reason": "",
                    "first_submitted_at": updated_at,
                    "updated_at": updated_at,
                    "completed_at": (
                        updated_at
                        if status in {
                            "filled", "cancelled", "failed", "skipped",
                        }
                        else None
                    ),
                    "raw_json": "{}",
                }

            with store.transaction() as conn:
                store.upsert_order(
                    conn, order("monotonic", "submitted", 0, "2026-07-29 10:00:00"),
                )
                store.upsert_order(
                    conn, order("monotonic", "partial", 50, "2026-07-29 10:01:00"),
                )
                store.upsert_order(
                    conn, order("monotonic", "submitted", 0, "2026-07-29 10:00:30"),
                )
                for terminal in ("failed", "skipped"):
                    store.upsert_order(
                        conn, order(terminal, terminal, 0, "2026-07-29 10:00:00"),
                    )
                    store.upsert_order(
                        conn, order(terminal, "submitted", 0, "2026-07-29 10:01:00"),
                    )
                store.upsert_order(
                    conn, order("late-fill", "cancelled", 50, "2026-07-29 10:00:00"),
                )
                store.upsert_order(
                    conn, order("late-fill", "filled", 100, "2026-07-29 10:01:00"),
                )
                store.upsert_order(
                    conn, order("impossible-fill", "rejected", 0, "2026-07-29 10:00:00"),
                )
                with self.assertRaisesRegex(ValueError, "terminal order"):
                    store.upsert_order(
                        conn, order(
                            "impossible-fill", "filled", 100,
                            "2026-07-29 10:01:00",
                        ),
                    )
                for terminal in (
                    "rejected", "risk_rejected", "failed", "skipped",
                ):
                    with self.subTest(terminal=terminal), self.assertRaisesRegex(
                        ValueError, "terminal order",
                    ):
                        store.upsert_order(
                            conn,
                            order(
                                f"incoming-{terminal}", terminal, 50,
                                "2026-07-29 10:01:00",
                            ),
                        )
                store.upsert_order(conn, {
                    **order(
                        "stale-metadata", "filled", 100,
                        "2026-07-29T10:00:00+08:00",
                    ),
                    "average_fill_price": 10,
                    "reason": "confirmed",
                    "raw_json": '{"version":"current"}',
                })
                store.upsert_order(conn, {
                    **order(
                        "stale-metadata", "submitted", 50,
                        "2026-07-29T02:01:00Z",
                    ),
                    "average_fill_price": 9,
                    "reason": "stale",
                    "raw_json": '{"version":"stale"}',
                })

            with store.connect() as conn:
                self.assertEqual(
                    tuple(conn.execute(
                        """SELECT filled_qty, status FROM orders
                           WHERE client_order_id='monotonic'"""
                    ).fetchone()),
                    (50, "partial"),
                )
                for terminal in ("failed", "skipped"):
                    self.assertEqual(conn.execute(
                        "SELECT status FROM orders WHERE client_order_id=?",
                        (terminal,),
                    ).fetchone()[0], terminal)
                self.assertEqual(
                    tuple(conn.execute(
                        """SELECT filled_qty, status FROM orders
                           WHERE client_order_id='late-fill'"""
                    ).fetchone()),
                    (100, "filled"),
                )
                self.assertEqual(
                    tuple(conn.execute(
                        """SELECT filled_qty, average_fill_price, status,
                                  reason, updated_at, raw_json
                           FROM orders WHERE client_order_id='stale-metadata'"""
                    ).fetchone()),
                    (
                        100, 10, "filled", "confirmed",
                        "2026-07-29T10:00:00+08:00",
                        '{"version":"current"}',
                    ),
                )

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

    def test_first_joinquant_scope_adopts_legacy_execution_issues(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "trading.db"
            store = TradingStore(path)
            with patch.object(
                TradingStore, "_migrate_schema_v12", return_value=None,
            ):
                store.initialize()
            with store.transaction() as conn:
                conn.execute(
                    """INSERT INTO execution_issue_state(
                       issue_key, object_type, object_id, state, severity,
                       first_seen_at, stage_started_at, last_seen_at,
                       last_transition_at, details_json
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
                    (
                        "order:legacy-order", "order", "legacy-order",
                        "ORDER_MISSING_PLATFORM", "ERROR",
                        "2026-07-28 10:00:00", "2026-07-28 10:00:00",
                        "2026-07-28 10:00:00", "2026-07-28 10:00:00",
                    ),
                )
            store.initialize()
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
                row = conn.execute(
                    """SELECT issue_key, state, recovered_at
                       FROM execution_issue_state"""
                ).fetchone()

        self.assertEqual(
            row["issue_key"],
            f"scope:{scope}:order:legacy-order",
        )
        self.assertEqual(row["state"], "ORDER_MISSING_PLATFORM")
        self.assertIsNone(row["recovered_at"])

    def test_legacy_issue_collision_merges_without_blocking_scope_lookup(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
                store.upsert_execution_issue(conn, {
                    "issue_key": f"scope:{scope}:ledger:sqlite",
                    "object_type": "ledger",
                    "object_id": "sqlite",
                    "state": "RECOVERABLE_WARNING",
                    "severity": "WARNING",
                    "stage_started_at": "2026-07-28 10:01:00",
                    "seen_at": "2026-07-28 10:01:00",
                    "details": {"source": "scoped"},
                })
                store.recover_execution_issue(
                    conn, f"scope:{scope}:ledger:sqlite",
                    "2026-07-28 10:02:00",
                )
                store.upsert_execution_issue(conn, {
                    "issue_key": "ledger:sqlite",
                    "object_type": "ledger",
                    "object_id": "sqlite",
                    "state": "LEDGER_INTEGRITY_FAILURE",
                    "severity": "CRITICAL",
                    "stage_started_at": "2026-07-28 10:03:00",
                    "seen_at": "2026-07-28 10:03:00",
                    "details": {"source": "legacy"},
                })
                self.assertEqual(
                    store.get_or_create_account_scope(
                        conn, "joinquant", "primary",
                    ),
                    scope,
                )
                rows = conn.execute(
                    """SELECT issue_key, state, severity, recovered_at
                       FROM execution_issue_state"""
                ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["issue_key"], f"scope:{scope}:ledger:sqlite")
        self.assertEqual(rows[0]["state"], "LEDGER_INTEGRITY_FAILURE")
        self.assertEqual(rows[0]["severity"], "CRITICAL")
        self.assertIsNone(rows[0]["recovered_at"])

    def test_schema_v10_marks_historical_fee_and_pnl_evidence_unknown(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "trading.db"
            schemas = (
                SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5,
                SCHEMA_V6, SCHEMA_V7, SCHEMA_V8, SCHEMA_V9,
            )
            with closing(sqlite3.connect(path)) as conn, conn:
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

    def test_v10_reconciliation_is_preserved_but_cannot_release_capacity(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "trading.db"
            store = TradingStore(path)
            with patch.object(
                TradingStore, "_migrate_schema_v11", return_value=None,
            ):
                store.initialize()
            with store.connect() as conn:
                conn.execute(
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, started_at, finished_at,
                       result, severity, difference_count, control_action,
                       summary_json
                       ) VALUES('legacy-v10','full',
                       '2026-07-28T10:00:03+08:00',
                       '2026-07-28T10:00:04+08:00',
                       'matched','INFO',0,'','{}')"""
                )
            store.initialize()

            candidate = make_candidate()
            result = PreTradeResult(**pre_trade_values(candidate))
            intent = ExecutionIntent(**intent_values(candidate, result))
            with store.transaction() as conn:
                legacy = conn.execute(
                    """SELECT account_scope_id, broker_snapshot_id,
                              broker_snapshot_sha256, snapshot_broker_time,
                              snapshot_generated_at
                       FROM reconciliation_runs
                       WHERE reconciliation_id='legacy-v10'"""
                ).fetchone()
                self.assertEqual(tuple(legacy), (None, None, None, None, None))
                self._reserve_intent(
                    store, conn, candidate, result, intent,
                    "legacy-v10-release",
                )
                for expected, new, transitioned_at in (
                    ("READY", "SUBMITTING", "2026-07-28T10:00:00+08:00"),
                    ("SUBMITTING", "SUBMITTED", "2026-07-28T10:00:01+08:00"),
                    ("SUBMITTED", "FILLED", "2026-07-28T10:00:02+08:00"),
                ):
                    store.compare_and_set_execution_intent_status(
                        conn, candidate.account_scope_id,
                        intent.client_order_id, expected_status=expected,
                        new_status=new, transitioned_at=transitioned_at,
                    )
                conn.execute(
                    """INSERT INTO orders(
                       client_order_id, stock_code, action, target_qty,
                       requested_qty, filled_qty, average_fill_price, status,
                       submit_count, reason, updated_at, raw_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent.client_order_id, candidate.code, "buy", 100,
                        100, 100, 10, "filled", 1, "",
                        "2026-07-28T10:00:02+08:00", "{}",
                    ),
                )
                with self.assertRaisesRegex(ValueError, "snapshot evidence"):
                    store.release_capacity_reservation(
                        conn, candidate.account_scope_id,
                        "legacy-v10-release",
                        released_at="2026-07-28T10:00:05+08:00",
                        reason="legacy reconciliation must not release",
                        reconciliation_id="legacy-v10",
                    )

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
                buy_enabled = conn.execute(
                    "SELECT 1 FROM system_state WHERE key='buy_enabled'"
                ).fetchone()
            self.assertEqual(count, 1)
            self.assertIsNone(buy_enabled)

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
            with closing(sqlite3.connect(path)) as conn, conn:
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
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, snapshot_id, started_at,
                       finished_at, result, severity, difference_count,
                       control_action, summary_json
                       ) VALUES('matched-old', 'incremental', 'old',
                       '2025-01-01 15:00:01', '2025-01-01 15:00:02',
                       'matched', 'INFO', 0, '', '{}')"""
                )
                conn.execute(
                    """INSERT INTO reconciliation_runs(
                       reconciliation_id, mode, snapshot_id, started_at,
                       finished_at, result, severity, difference_count,
                       control_action, summary_json
                       ) VALUES('error-old', 'full', 'old',
                       '2025-01-01 15:00:01', '2025-01-01 15:00:02',
                       'mismatch', 'ERROR', 1, 'stop_buy', '{}')"""
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
            self.assertEqual(store.get_active_position_cycles()["600000"]["take_profit_stage"], 0)

    def test_profit_protection_activation_is_atomic_and_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.get_or_create_account_scope(conn, "joinquant", "primary")
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 100, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-08-03 09:55:00")
            cycle = store.get_active_position_cycles()["600000"]

            with store.transaction() as conn:
                activated = store.activate_profit_protection(
                    conn,
                    cycle["position_cycle_id"],
                    expected_current_qty=100,
                    batch_at="2026-08-03T10:00:00+08:00",
                    highest_price=13,
                )
            first = store.get_active_position_cycles()["600000"]
            with store.transaction() as conn:
                replayed = store.activate_profit_protection(
                    conn,
                    cycle["position_cycle_id"],
                    expected_current_qty=100,
                    batch_at="2026-08-03T10:05:00+08:00",
                    highest_price=14,
                )
            second = store.get_active_position_cycles()["600000"]

            self.assertTrue(activated)
            self.assertFalse(replayed)
            self.assertEqual(
                first["profit_protection_activated_at"],
                "2026-08-03T10:00:00+08:00",
            )
            self.assertGreater(
                first["trailing_stop_active_from"],
                first["profit_protection_activated_at"],
            )
            self.assertEqual(
                second["profit_protection_activated_at"],
                first["profit_protection_activated_at"],
            )
            self.assertEqual(second["highest_price"], 13)

    def test_position_cycle_stage_requires_linked_take_profit_fill(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.get_or_create_account_scope(conn, "joinquant", "primary")
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 300, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-08-03 09:55:00")
            cycle = store.get_active_position_cycles()["600000"]
            signal_id = f"{cycle['position_cycle_id']}-take_profit_1-0"
            with store.transaction() as conn:
                store.record_strategy_run(conn, StrategyRunRecord(
                    "tp-run", "2026-08-03", "2026-08-03 10:00:00",
                    "test-v1", "test-params-v1",
                ))
                store.record_signal(conn, SignalRecord(
                    signal_id, "tp-run", "2026-08-03", "600000",
                    "600000.XSHG", "sell", 0,
                    "2026-08-03 10:00:00", "", "{}",
                ))
                store.upsert_exit_intent(
                    conn, signal_id, "600000", 200, "take_profit_1",
                    "2026-08-03 10:00:00",
                )
                conn.execute(
                    """INSERT INTO orders(
                       client_order_id, signal_id, order_id, stock_code, action,
                       target_qty, requested_qty, filled_qty, status, updated_at,
                       raw_json
                       ) VALUES(?, ?, 'tp-order', '600000', 'sell', 200, 100,
                                100, 'filled', '2026-08-03 15:00:00', '{}')""",
                    ("tp-client", signal_id),
                )
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 200, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-08-03 10:01:00")
                self.assertEqual(conn.execute(
                    "SELECT take_profit_stage FROM position_cycles WHERE position_cycle_id=?",
                    (cycle["position_cycle_id"],),
                ).fetchone()[0], 0)
                conn.execute(
                    "UPDATE orders SET updated_at='2026-08-03 09:50:00' WHERE client_order_id='tp-client'"
                )
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 200, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-08-03 10:01:00")
                self.assertEqual(conn.execute(
                    "SELECT take_profit_stage FROM position_cycles WHERE position_cycle_id=?",
                    (cycle["position_cycle_id"],),
                ).fetchone()[0], 0)
                conn.execute(
                    "UPDATE orders SET updated_at='2026-08-03 09:56:00' WHERE client_order_id='tp-client'"
                )
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 200, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-08-03 10:01:00")
                self.assertEqual(conn.execute(
                    "SELECT take_profit_stage FROM position_cycles WHERE position_cycle_id=?",
                    (cycle["position_cycle_id"],),
                ).fetchone()[0], 0)
                conn.execute(
                    "UPDATE orders SET updated_at='2026-08-03 10:01:00' WHERE client_order_id='tp-client'"
                )
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 200, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-08-03 10:01:00")

            self.assertEqual(
                store.get_active_position_cycles()["600000"]["take_profit_stage"],
                1,
            )

    def test_add_position_updates_weighted_cost_without_lowering_frozen_stop(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.get_or_create_account_scope(conn, "joinquant", "primary")
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
                store.get_or_create_account_scope(conn, "joinquant", "primary")
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
            self.assertEqual(store.get_system_state("market_regime"), "RISK_OFF")

    def test_market_regime_persists_directly_when_confirmation_is_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()

            self.assertEqual(
                store.confirm_market_regime("CAUTION", enabled=False), "CAUTION",
            )
            self.assertEqual(store.get_system_state("market_regime"), "CAUTION")

    def test_completed_hard_stop_creates_rebuy_cooldown(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.upsert_exit_intent(conn, "cycle-hard_stop-0", "600000", 0, "hard_stop", "2026-07-13 10:00:00")
                store.reconcile_exit_intents(conn, [], "2026-07-13 10:03:00")
            self.assertTrue(store.is_in_cooldown("600000", "2026-07-14"))
