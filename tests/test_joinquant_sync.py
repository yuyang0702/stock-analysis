import json
import tempfile
import unittest
from decimal import Decimal, ROUND_DOWN, getcontext, localcontext
from pathlib import Path

import joinquant_sync
import config as app_config
from joinquant_runtime_isolation import RuntimeIdentityError
from trading_store import TradingStore


class JoinQuantSyncTest(unittest.TestCase):
    def test_stored_snapshot_runtime_validation_fails_closed(self) -> None:
        with self.assertRaises(RuntimeIdentityError):
            joinquant_sync.validate_stored_snapshot_runtime(
                {"schema_version": 1, "runtime_mode": "full_backtest"},
                app_config.JOINQUANT_TEMPLATE_VERSION,
            )

        payload = {
            "schema_version": 1,
            "runtime_mode": "sim_trade",
            "runtime_protocol_version": "1",
            "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
        }
        joinquant_sync.validate_stored_snapshot_runtime(
            payload, app_config.JOINQUANT_TEMPLATE_VERSION,
        )
        self.assertEqual(
            joinquant_sync.sanitize_joinquant_payload(payload)["runtime_mode"],
            "sim_trade",
        )
    def test_cycle_risk_fields_include_active_one_lot_trailing_stop(self) -> None:
        positions = [{
            "code": "600000", "qty": 100, "cost_price": 10,
            "current_price": 12.1,
        }]
        cycles = {"600000": {
            "mode": "short", "initial_qty": 100, "entry_price": 10,
            "initial_stop_price": 9, "highest_price": 13, "atr14": 0.4,
            "take_profit_stage": 0, "manual_stop_price": None,
            "market_state": "NORMAL", "position_cycle_id": "cycle-1",
            "profit_protection_activated_at": "2026-07-28T09:55:00+08:00",
            "trailing_stop_active_from": "2026-07-28T09:55:00.000001+08:00",
        }}

        joinquant_sync.apply_cycle_risk_fields(
            positions, cycles, "2026-07-28T10:00:00+08:00"
        )

        self.assertEqual(positions[0]["trailing_stop_price"], 12.2)
        self.assertEqual(positions[0]["effective_stop_price"], 12.2)

    @staticmethod
    def _ledger_snapshot(generated_at: str = "2026-07-07 10:05:00") -> dict:
        return {
            "schema_version": 1,
            "trade_date": "2026-07-07",
            "generated_at": generated_at,
            "source": "joinquant",
            "template_version": "test-ledger-v6",
            "cash": 89500,
            "available_cash": 89500,
            "total_value": 100000,
            "daily_turnover_pct": 10.5,
            "daily_pnl_pct": 0.5,
            "account_drawdown_pct": -0.2,
            "consecutive_losses": 0,
            "positions": [{
                "code": "600000", "jq_code": "600000.XSHG", "qty": 1000,
                "closeable_amount": 1000, "locked_amount": 0, "today_amount": 0,
                "avg_cost": 10.0, "price": 10.5, "market_value": 10500, "pnl": 500,
            }],
            "orders": [{
                "order_id": "10", "code": "600000", "jq_code": "600000.XSHG",
                "action": "buy", "amount": 1000, "filled": 1000,
                "avg_price": 10.0, "status": "filled", "datetime": "2026-07-07 10:05:00",
            }],
            "trades": [{
                "trade_id": "20", "order_id": "10", "code": "600000",
                "action": "buy", "amount": 1000, "price": 10.0,
                "commission": 5.0, "stamp_tax": 0.0, "other_fee": 0.2,
                "fee_data_status": "reported",
                "datetime": "2026-07-07 10:05:00",
            }],
        }

    def test_ingest_persists_snapshot_order_fill_and_daily_equity_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()

            first = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:02")
            second = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:03")

            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            self.assertEqual([event["event_id"] for event in first["new_executions"]], ["fill:20"])
            self.assertEqual(second["new_executions"], [])
            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM position_snapshots").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM fills").fetchone()[0], 1)
                equity = conn.execute("SELECT * FROM daily_equity WHERE trade_date='2026-07-07'").fetchone()
            self.assertEqual(equity["opening_equity"], 100000)
            self.assertEqual(equity["closing_equity"], 100000)
            self.assertAlmostEqual(equity["fees"], 5.2)
            self.assertEqual(equity["fee_data_status"], "reported")
            self.assertEqual(equity["realized_pnl_status"], "unknown")
            self.assertEqual(equity["unrealized_pnl"], 500)

    def test_snapshot_rejects_future_order_or_fill_evidence_atomically(self) -> None:
        for future_kind in ("order", "fill"):
            with self.subTest(future_kind=future_kind), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                snapshot = self._ledger_snapshot("2026-07-07 10:00:00")
                snapshot["orders"][0]["datetime"] = "2026-07-07 10:00:00"
                snapshot["trades"][0]["datetime"] = "2026-07-07 10:00:00"
                if future_kind == "order":
                    snapshot["orders"][0]["datetime"] = "2026-07-07 10:01:00"
                else:
                    snapshot["trades"][0]["datetime"] = "2026-07-07 10:01:00"

                with self.assertRaisesRegex(ValueError, "follows snapshot generated_at"):
                    joinquant_sync.ingest_snapshot_payload(
                        snapshot, store, "2026-07-07 10:00:02"
                    )

                with store.connect() as conn:
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM account_snapshots"
                    ).fetchone()[0], 0)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM orders"
                    ).fetchone()[0], 0)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM fills"
                    ).fetchone()[0], 0)

    def test_legacy_snapshot_normalizes_adapter_and_daily_risk_evidence(self) -> None:
        payload = self._ledger_snapshot()
        payload["orders"] = []
        payload["trades"] = []
        normalized = joinquant_sync._legacy_broker_snapshot(
            payload, "scope-uuid", "2026-07-07 10:05:02", {},
        )
        with localcontext() as context:
            context.prec = 50
            expected_intraday = (
                Decimal("100000")
                - Decimal("100000") * Decimal("100") / Decimal("100.5")
            )

        self.assertEqual(normalized.adapter, "joinquant")
        self.assertEqual(normalized.daily_risk_evidence_status, "reported")
        self.assertEqual(normalized.intraday_pnl, expected_intraday)
        self.assertEqual(normalized.account_drawdown_pct, Decimal("-0.2"))
        self.assertEqual(normalized.daily_turnover_fraction, Decimal("0.105"))
        self.assertEqual(normalized.consecutive_losses, 0)

        explicit_pnl = dict(payload)
        explicit_pnl.pop("daily_pnl_pct")
        explicit_pnl["intraday_pnl"] = "123.45"
        normalized = joinquant_sync._legacy_broker_snapshot(
            explicit_pnl, "scope-uuid", "2026-07-07 10:05:02", {},
        )
        self.assertEqual(normalized.daily_risk_evidence_status, "reported")
        self.assertEqual(normalized.intraday_pnl, Decimal("123.45"))

        for field in (
            "daily_pnl_pct",
            "account_drawdown_pct",
            "daily_turnover_pct",
            "consecutive_losses",
        ):
            with self.subTest(field=field):
                incomplete = dict(payload)
                incomplete.pop(field, None)
                unknown = joinquant_sync._legacy_broker_snapshot(
                    incomplete, "scope-uuid", "2026-07-07 10:05:02", {},
                )
                self.assertEqual(unknown.daily_risk_evidence_status, "unknown")

    def test_daily_risk_numeric_evidence_fails_closed(self) -> None:
        for field, value in (
            ("daily_turnover_pct", -1),
            ("daily_turnover_pct", "NaN"),
            ("consecutive_losses", -1),
            ("consecutive_losses", "1.5"),
        ):
            with self.subTest(field=field, value=value):
                payload = self._ledger_snapshot()
                payload[field] = value
                payload["orders"] = []
                payload["trades"] = []
                with self.assertRaisesRegex(ValueError, field):
                    joinquant_sync._legacy_broker_snapshot(
                        payload, "scope-uuid", "2026-07-07 10:05:02", {},
                    )

    def test_daily_pnl_normalization_is_independent_of_decimal_context(self) -> None:
        payload = self._ledger_snapshot()
        payload["orders"] = []
        payload["trades"] = []
        context = getcontext()
        original = context.prec, context.rounding
        try:
            hashes = set()
            values = set()
            for precision in (10, 28, 50):
                context.prec = precision
                context.rounding = ROUND_DOWN
                snapshot = joinquant_sync._legacy_broker_snapshot(
                    payload, "scope-uuid", "2026-07-07 10:05:02", {},
                )
                hashes.add(snapshot.snapshot_sha256)
                values.add((snapshot.intraday_pnl, snapshot.daily_turnover_fraction))
            self.assertEqual(len(hashes), 1)
            self.assertEqual(len(values), 1)
        finally:
            context.prec, context.rounding = original

    def test_complete_snapshot_requires_explicit_position_order_and_trade_sets(self) -> None:
        for field in ("positions", "orders", "trades"):
            with self.subTest(field=field):
                payload = self._ledger_snapshot()
                payload.pop(field)
                with self.assertRaisesRegex(ValueError, field):
                    joinquant_sync._legacy_broker_snapshot(
                        payload, "scope-uuid", "2026-07-07 10:05:02", {},
                    )

    def test_ingest_persists_strict_scoped_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            snapshot["token"] = "must-not-enter-current-contract"
            snapshot["nested"] = {
                "access_token": "nested-access-secret",
                "client_secret": "nested-client-secret",
                "private_key": "nested-private-key",
                "session_id": "nested-session-id",
                "refresh_token": "nested-refresh-token",
                "client_id": "nested-client-id",
            }
            snapshot["orders"][0].update(
                status="partial_filled", amount=1000, filled=500,
            )
            snapshot["trades"] = []

            joinquant_sync.ingest_snapshot_payload(
                snapshot, store, "2026-07-07 10:05:02",
            )

            with store.connect() as conn:
                scope = conn.execute(
                    """SELECT account_scope_id FROM account_scopes
                       WHERE adapter='joinquant' AND scope_alias='primary'"""
                ).fetchone()[0]
                payload = conn.execute(
                    """SELECT payload_json FROM broker_snapshot_current
                       WHERE account_scope_id=?""",
                    (scope,),
                ).fetchone()[0]
                history_payload = conn.execute(
                    """SELECT raw_json FROM account_snapshots
                       WHERE raw_json IS NOT NULL"""
                ).fetchone()[0]
                current = store.load_current_broker_snapshot(conn, scope)
            self.assertEqual(current.broker_time, "2026-07-07T02:05:00+00:00")
            self.assertEqual(current.open_orders[0]["status"], "partially_filled")
            self.assertEqual(current.positions[0].sellable_qty, 1000)
            self.assertNotIn("token", payload.lower())
            self.assertNotIn("must-not-enter", history_payload)
            self.assertNotIn("nested-access-secret", history_payload)
            self.assertNotIn("nested-client-secret", history_payload)
            self.assertNotIn("nested-private-key", history_payload)
            self.assertNotIn("nested-session-id", history_payload)
            self.assertNotIn("nested-refresh-token", history_payload)
            self.assertNotIn("nested-client-id", history_payload)

    def test_complete_snapshot_keeps_target_only_open_order_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            snapshot["orders"] = [{
                "order_id": "target-only-open", "action": "buy",
                "code": "600000", "target_qty": 100, "filled": 0,
                "status": "submitted", "datetime": "2026-07-07 10:05:00",
            }]
            snapshot["trades"] = []

            joinquant_sync.ingest_snapshot_payload(
                snapshot, store, "2026-07-07 10:05:02",
            )

            with store.connect() as conn:
                row = conn.execute(
                    """SELECT target_qty, filled_qty, status
                       FROM broker_order_current
                       WHERE broker_order_id='target-only-open'"""
                ).fetchone()
            self.assertEqual(tuple(row), (100, 0, "submitted"))

    def test_complete_snapshot_deduplicates_orders_and_keeps_each_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            snapshot["orders"] = [
                {
                    "order_id": "duplicate-open", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 0,
                    "status": "submitted", "datetime": "2026-07-07 10:03:00",
                },
                {
                    "order_id": "duplicate-open", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 0,
                    "status": "submitted", "datetime": "2026-07-07 10:04:00",
                },
                {
                    "order_id": "other-open", "action": "buy",
                    "code": "000001", "amount": 200, "filled": 0,
                    "status": "submitted", "datetime": "2026-07-07 10:05:00",
                },
            ]
            snapshot["trades"] = []

            joinquant_sync.ingest_snapshot_payload(
                snapshot, store, "2026-07-07 10:05:02",
            )

            with store.connect() as conn:
                rows = conn.execute(
                    """SELECT broker_order_id, updated_at
                       FROM broker_order_current ORDER BY broker_order_id"""
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["updated_at"], rows[1]["updated_at"])
            self.assertTrue(str(rows[0]["updated_at"]).endswith("02:04:00+00:00"))
            self.assertTrue(str(rows[1]["updated_at"]).endswith("02:05:00+00:00"))

    def test_duplicate_submitted_then_filled_order_is_not_left_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            submitted = {
                "order_id": "duplicate-filled", "action": "buy",
                "code": "600000", "amount": 100, "filled": 0,
                "status": "submitted", "datetime": "2026-07-07 10:04:00",
            }
            snapshot["orders"] = [
                submitted,
                {**submitted, "filled": 100, "avg_price": 10,
                 "status": "filled", "datetime": "2026-07-07 10:05:00"},
            ]
            snapshot["trades"] = [{
                "trade_id": "duplicate-fill", "order_id": "duplicate-filled",
                "action": "buy", "code": "600000", "amount": 100,
                "price": 10, "datetime": "2026-07-07 10:05:00",
            }]

            joinquant_sync.ingest_snapshot_payload(
                snapshot, store, "2026-07-07 10:05:02",
            )

            with store.connect() as conn:
                open_count = conn.execute(
                    "SELECT COUNT(*) FROM broker_order_current"
                ).fetchone()[0]
                status = conn.execute(
                    """SELECT status FROM orders
                       WHERE order_id='duplicate-filled'"""
                ).fetchone()[0]
            self.assertEqual(open_count, 0)
            self.assertEqual(status, "filled")

    def test_complete_snapshot_rejects_duplicate_broker_order_with_client_conflict(self) -> None:
        payload = self._ledger_snapshot()
        base = {
            "order_id": "same-broker-order", "action": "buy",
            "code": "600000", "amount": 100, "filled": 0,
            "status": "submitted", "datetime": "2026-07-07 10:04:00",
        }
        payload["orders"] = [
            {**base, "client_order_id": "client-a"},
            {**base, "client_order_id": "client-b"},
        ]
        payload["trades"] = []

        with self.assertRaisesRegex(ValueError, "duplicate snapshot order conflict"):
            joinquant_sync._legacy_broker_snapshot(
                payload, "scope-uuid", "2026-07-07 10:05:02", {},
            )

    def test_complete_snapshot_rejects_conflicting_or_negative_fill_aliases(self) -> None:
        cases = (
            ({"filled": 0, "filled_qty": 50}, "fields conflict"),
            ({"filled_qty": -1}, "non-negative integer"),
        )
        for fill_fields, error in cases:
            with self.subTest(fill_fields=fill_fields), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                snapshot = self._ledger_snapshot()
                snapshot["orders"] = [{
                    "order_id": "invalid-fill-alias", "action": "buy",
                    "code": "600000", "target_qty": 100,
                    "status": "submitted", "datetime": "2026-07-07 10:05:00",
                    **fill_fields,
                }]
                snapshot["trades"] = []

                with self.assertRaisesRegex(ValueError, error):
                    joinquant_sync.ingest_snapshot_payload(
                        snapshot, store, "2026-07-07 10:05:02",
                    )

                with store.connect() as conn:
                    self.assertEqual(conn.execute(
                        "SELECT count(*) FROM broker_snapshot_current",
                    ).fetchone()[0], 0)
                    self.assertEqual(conn.execute(
                        "SELECT count(*) FROM orders",
                    ).fetchone()[0], 0)

    def test_complete_snapshot_normalizes_open_status_from_quantity(self) -> None:
        cases = (
            ("partially_filled", 50, "partial", "partially_filled"),
            ("submitted", 50, "partial", "partially_filled"),
            ("partially_filled", 0, "submitted", "submitted"),
        )
        for source_status, filled_qty, local_status, current_status in cases:
            with self.subTest(
                source_status=source_status, filled_qty=filled_qty,
            ), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                snapshot = self._ledger_snapshot()
                snapshot["orders"] = [{
                    "id": "sig-partial-status", "order_id": "partial-status",
                    "action": "buy", "code": "600000", "amount": 100,
                    "filled": filled_qty, "avg_price": 10,
                    "status": source_status,
                    "datetime": "2026-07-07 10:05:00",
                }]
                snapshot["trades"] = ([{
                    "trade_id": "partial-status-fill",
                    "order_id": "partial-status",
                    "signal_id": "sig-partial-status",
                    "action": "buy", "code": "600000", "amount": filled_qty,
                    "price": 10, "datetime": "2026-07-07 10:05:00",
                }] if filled_qty else [])

                result = joinquant_sync.ingest_snapshot_payload(
                    snapshot, store, "2026-07-07 10:05:02",
                )

                self.assertEqual(result["reconciliation"].result, "matched")
                with store.connect() as conn:
                    local = conn.execute(
                        """SELECT status, filled_qty FROM orders
                           WHERE order_id='partial-status'""",
                    ).fetchone()
                    current = conn.execute(
                        """SELECT status, filled_qty FROM broker_order_current
                           WHERE broker_order_id='partial-status'""",
                    ).fetchone()
                self.assertEqual(tuple(local), (local_status, filled_qty))
                self.assertEqual(tuple(current), (current_status, filled_qty))

    def test_complete_snapshot_rejects_incomplete_filled_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            snapshot["orders"] = [{
                "order_id": "incomplete-filled", "action": "buy",
                "code": "600000", "amount": 100, "filled": 50,
                "avg_price": 10, "status": "filled",
                "datetime": "2026-07-07 10:05:00",
            }]
            snapshot["trades"] = [{
                "trade_id": "incomplete-filled-trade",
                "order_id": "incomplete-filled", "action": "buy",
                "code": "600000", "amount": 50, "price": 10,
                "datetime": "2026-07-07 10:05:00",
            }]

            with self.assertRaisesRegex(ValueError, "filled order quantity is incomplete"):
                joinquant_sync.ingest_snapshot_payload(
                    snapshot, store, "2026-07-07 10:05:02",
                )

            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM broker_snapshot_current",
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM orders",
                ).fetchone()[0], 0)

    def test_trade_quantity_alias_conflicts_roll_back_all_paths(self) -> None:
        for event_only in (False, True):
            for aliases, error in (
                ({"amount": 50, "qty": 60}, "fields conflict"),
                ({"amount": 50, "qty": -60}, "non-negative integer"),
            ):
                with self.subTest(
                    event_only=event_only, aliases=aliases,
                ), tempfile.TemporaryDirectory() as tmp:
                    store = TradingStore(Path(tmp) / "trading.db")
                    order = {
                        "order_id": "trade-alias-order", "action": "buy",
                        "code": "600000", "amount": 100, "filled": 50,
                        "avg_price": 10, "status": "partial",
                        "datetime": "2026-07-07 10:05:00",
                    }
                    trade = {
                        "trade_id": "trade-alias-fill",
                        "order_id": "trade-alias-order", "action": "buy",
                        "code": "600000", "price": 10,
                        "datetime": "2026-07-07 10:05:00", **aliases,
                    }
                    if event_only:
                        snapshot = {
                            "schema_version": 1, "positions": [],
                            "orders": [order], "trades": [trade],
                        }
                    else:
                        snapshot = self._ledger_snapshot()
                        snapshot["orders"] = [order]
                        snapshot["trades"] = [trade]

                    with self.assertRaisesRegex(ValueError, error):
                        joinquant_sync.ingest_snapshot_payload(
                            snapshot, store, "2026-07-07 10:05:02",
                        )

                    with store.connect() as conn:
                        self.assertEqual(conn.execute(
                            "SELECT count(*) FROM orders",
                        ).fetchone()[0], 0)
                        self.assertEqual(conn.execute(
                            "SELECT count(*) FROM fills",
                        ).fetchone()[0], 0)

    def test_missing_fill_order_link_rolls_back_history_and_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            snapshot["trades"][0]["order_id"] = "missing-order"

            with self.assertRaisesRegex(ValueError, "matching order"):
                joinquant_sync.ingest_snapshot_payload(
                    snapshot, store, "2026-07-07 10:05:02",
                )

            with store.connect() as conn:
                for table in (
                    "broker_snapshot_current",
                    "account_snapshots", "orders", "fills",
                ):
                    self.assertEqual(
                        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                        0,
                    )

    def test_stale_current_snapshot_rolls_back_new_history_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            newer = self._ledger_snapshot("2026-07-07 10:06:00")
            newer["trades"] = []
            newer["orders"] = []
            joinquant_sync.ingest_snapshot_payload(
                newer, store, "2026-07-07 10:06:02",
            )
            stale = self._ledger_snapshot("2026-07-07 10:05:00")
            stale["trades"] = []
            stale["orders"] = []

            with self.assertRaisesRegex(ValueError, "stale"):
                joinquant_sync.ingest_snapshot_payload(
                    stale, store, "2026-07-07 10:06:03",
                )

            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0],
                    1,
                )
                scope = conn.execute(
                    "SELECT account_scope_id FROM account_scopes"
                ).fetchone()[0]
                current = store.load_current_broker_snapshot(conn, scope)
            self.assertEqual(current.broker_time, "2026-07-07T02:06:00+00:00")

    def test_strict_mapping_rejects_negative_or_fractional_quantities(self) -> None:
        cases = (
            ("negative order", ("orders", 0, "amount"), -100),
            ("fractional position", ("positions", 0, "qty"), 100.9),
        )
        for label, (collection, index, field), value in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                snapshot = self._ledger_snapshot()
                snapshot[collection][index][field] = value
                with self.assertRaisesRegex(ValueError, "nonnegative|integer"):
                    joinquant_sync.ingest_snapshot_payload(
                        snapshot, store, "2026-07-07 10:05:02",
                    )
                with store.connect() as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM account_snapshots"
                        ).fetchone()[0],
                        0,
                    )

    def test_event_only_legacy_payload_only_persists_execution_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 100, "cost_price": 10,
                    "current_price": 10, "stop_price": 9,
                }], "2026-07-07 09:30:00")
            result = joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1,
                    "trade_date": "2026-07-07",
                    "generated_at": "2026-07-07 10:05:00",
                    "positions": [],
                    "orders": [{
                        "order_id": "event-order-1", "action": "buy",
                        "code": "000001", "amount": 100, "filled": 100,
                        "status": "filled", "datetime": "2026-07-07 10:04:59",
                    }],
                    "trades": [{
                        "trade_id": "event-fill-1", "order_id": "event-order-1",
                        "action": "buy", "code": "000001", "amount": 100,
                        "price": 10, "datetime": "2026-07-07 10:04:59",
                    }],
                },
                store,
                "2026-07-07 10:05:02",
            )
            self.assertTrue(result["event_only"])
            self.assertIsNone(result["snapshot_id"])
            self.assertNotIn("reconciliation", result)
            self.assertNotIn("control_actions", result)
            with store.connect() as conn:
                for table in (
                    "broker_snapshot_current",
                    "account_snapshots", "position_snapshots", "daily_equity",
                    "reconciliation_runs", "control_events",
                ):
                    self.assertEqual(
                        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                        0,
                        table,
                    )
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM account_scopes").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        """SELECT count(*) FROM notification_outbox
                           WHERE event_type IN ('fill','order_terminal')"""
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    [
                        (row["key"], row["value"])
                        for row in conn.execute(
                            "SELECT key, value FROM system_state ORDER BY key"
                        )
                    ],
                    [("sell_enabled", "1")],
                )
                self.assertEqual(conn.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM fills").fetchone()[0], 1)
            self.assertIn("600000", store.get_active_position_cycles())

    def test_event_only_invalid_order_action_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            with self.assertRaisesRegex(ValueError, "order action"):
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [], "trades": [],
                        "orders": [{
                            "order_id": "bad-action", "action": "buv",
                            "code": "600000", "amount": 100, "filled": 0,
                            "status": "submitted",
                            "datetime": "2026-07-07 10:00:00",
                        }],
                    },
                    store,
                    "2026-07-07 10:00:01",
                )
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM orders").fetchone()[0], 0,
                )

    def test_event_only_fill_may_link_persisted_order_but_orphan_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            base = {
                "schema_version": 1, "positions": [], "trades": [],
                "orders": [{
                    "order_id": "persisted-order", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 0,
                    "status": "submitted", "datetime": "2026-07-07 10:00:00",
                }],
            }
            joinquant_sync.ingest_snapshot_payload(
                base, store, "2026-07-07 10:00:01",
            )
            linked = {
                "schema_version": 1, "positions": [], "orders": [],
                "trades": [{
                    "trade_id": "linked-fill", "order_id": "persisted-order",
                    "action": "buy", "code": "600000", "amount": 100,
                    "price": 10, "datetime": "2026-07-07 10:01:00",
                }],
            }
            result = joinquant_sync.ingest_snapshot_payload(
                linked, store, "2026-07-07 10:01:01",
            )
            self.assertEqual(
                [item["event_id"] for item in result["new_executions"]],
                ["fill:linked-fill"],
            )
            orphan = {
                "schema_version": 1, "positions": [],
                "orders": [{
                    "order_id": "must-roll-back", "action": "sell",
                    "code": "000001", "amount": 100, "filled": 0,
                    "status": "submitted", "datetime": "2026-07-07 10:02:00",
                }],
                "trades": [{
                    "trade_id": "orphan", "order_id": "missing",
                    "action": "sell", "code": "000001", "amount": 100,
                    "price": 9, "datetime": "2026-07-07 10:02:00",
                }],
            }
            with self.assertRaisesRegex(ValueError, "matching order"):
                joinquant_sync.ingest_snapshot_payload(
                    orphan, store, "2026-07-07 10:02:01",
                )
            with store.connect() as conn:
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM orders WHERE order_id='must-roll-back'"
                ).fetchone())
                self.assertEqual(conn.execute("SELECT count(*) FROM fills").fetchone()[0], 1)

    def test_event_only_order_progress_requires_positive_finite_average_price(self) -> None:
        invalid_prices = (None, 0, -1, float("inf"), float("nan"))
        for price in invalid_prices:
            with self.subTest(price=price), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                order = {
                    "order_id": "bad-progress", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 100,
                    "status": "filled",
                    "datetime": "2026-07-07 10:00:00",
                }
                if price is not None:
                    order["avg_price"] = price
                with self.assertRaisesRegex(
                    ValueError, "average fill price",
                ):
                    joinquant_sync.ingest_snapshot_payload(
                        {
                            "schema_version": 1, "positions": [],
                            "orders": [order], "trades": [],
                        },
                        store,
                        "2026-07-07 10:00:01",
                    )
                with store.connect() as conn:
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM orders").fetchone()[0],
                        0,
                    )

    def test_unlinked_order_progress_cannot_hide_behind_another_order_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            with self.assertRaisesRegex(ValueError, "average fill price"):
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [],
                        "orders": [
                            {
                                "order_id": "linked-order", "action": "buy",
                                "code": "600000", "amount": 100,
                                "filled": 100, "avg_price": 10,
                                "status": "filled",
                                "datetime": "2026-07-07 10:00:00",
                            },
                            {
                                "order_id": "unlinked-order", "action": "buy",
                                "code": "000001", "amount": 100,
                                "filled": 100, "status": "filled",
                                "datetime": "2026-07-07 10:00:00",
                            },
                        ],
                        "trades": [{
                            "trade_id": "linked-fill",
                            "order_id": "linked-order", "action": "buy",
                            "code": "600000", "amount": 100, "price": 10,
                            "datetime": "2026-07-07 10:00:01",
                        }],
                    },
                    store,
                    "2026-07-07 10:00:02",
                )
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM orders").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM fills").fetchone()[0],
                    0,
                )

    def test_event_only_fill_requires_a_verifiable_timestamp(self) -> None:
        invalid_timestamps = (None, "", "not-a-timestamp", "2026-07-07")
        for timestamp in invalid_timestamps:
            with (
                self.subTest(timestamp=timestamp),
                tempfile.TemporaryDirectory() as tmp,
            ):
                store = TradingStore(Path(tmp) / "trading.db")
                trade = {
                    "trade_id": "bad-time-fill", "order_id": "time-order",
                    "action": "buy", "code": "600000", "amount": 100,
                    "price": 10,
                }
                if timestamp is not None:
                    trade["datetime"] = timestamp
                with self.assertRaisesRegex(ValueError, "filled_at"):
                    joinquant_sync.ingest_snapshot_payload(
                        {
                            "schema_version": 1, "positions": [],
                            "orders": [{
                                "order_id": "time-order", "action": "buy",
                                "code": "600000", "amount": 100, "filled": 0,
                                "status": "submitted",
                                "datetime": "2026-07-07 10:00:00",
                            }],
                            "trades": [trade],
                        },
                        store,
                        "2026-07-07 10:00:01",
                    )
                with store.connect() as conn:
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM orders").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM fills").fetchone()[0],
                        0,
                    )

    def test_event_fill_timestamp_is_normalized_to_shanghai_iso_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            result = joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [],
                    "orders": [{
                        "order_id": "timezone-order", "action": "buy",
                        "code": "600000", "amount": 100, "filled": 0,
                        "status": "submitted",
                        "datetime": "2026-07-07 10:00:00",
                    }],
                    "trades": [{
                        "trade_id": "timezone-fill",
                        "order_id": "timezone-order", "action": "buy",
                        "code": "600000", "amount": 100, "price": 10,
                        "datetime": "2026-07-07T02:01:00Z",
                    }],
                },
                store,
                "2026-07-07 10:01:01",
            )
            self.assertEqual(
                result["new_executions"][0]["filled_at"],
                "2026-07-07T10:01:00+08:00",
            )
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT filled_at FROM fills WHERE fill_id='timezone-fill'"
                    ).fetchone()[0],
                    "2026-07-07T10:01:00+08:00",
                )

    def test_event_order_timestamp_is_strict_and_compared_by_instant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            base_order = {
                "order_id": "timezone-order", "action": "buy",
                "code": "600000", "amount": 100, "filled": 0,
                "status": "submitted",
                "datetime": "2026-07-07T10:00:00+08:00",
            }
            joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "trades": [],
                    "orders": [base_order],
                },
                store,
                "2026-07-07 10:00:01",
            )
            joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "trades": [],
                    "orders": [{
                        **base_order, "status": "cancelled",
                        "datetime": "2026-07-07T02:30:00Z",
                    }],
                },
                store,
                "2026-07-07 10:30:01",
            )
            with self.assertRaisesRegex(ValueError, "order.updated_at"):
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [], "trades": [],
                        "orders": [{
                            **base_order, "datetime": "2026-07-07",
                        }],
                    },
                    store,
                    "2026-07-07 10:31:01",
                )
            with store.connect() as conn:
                row = conn.execute(
                    """SELECT status, updated_at FROM orders
                       WHERE order_id='timezone-order'"""
                ).fetchone()
            self.assertEqual(
                tuple(row), ("cancelled", "2026-07-07T10:30:00+08:00"),
            )

    def test_replayed_legacy_naive_fill_time_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            payload = {
                "schema_version": 1, "positions": [],
                "orders": [{
                    "order_id": "legacy-time-order", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 0,
                    "status": "submitted",
                    "datetime": "2026-07-07 10:00:00",
                }],
                "trades": [{
                    "trade_id": "legacy-time-fill",
                    "order_id": "legacy-time-order", "action": "buy",
                    "code": "600000", "amount": 100, "price": 10,
                    "datetime": "2026-07-07 10:01:00",
                }],
            }
            joinquant_sync.ingest_snapshot_payload(
                payload, store, "2026-07-07 10:01:01",
            )
            with store.transaction() as conn:
                conn.execute(
                    """UPDATE fills SET filled_at='2026-07-07 10:01:00'
                       WHERE fill_id='legacy-time-fill'"""
                )
            replay = joinquant_sync.ingest_snapshot_payload(
                payload, store, "2026-07-07 10:01:02",
            )
            self.assertEqual(replay["new_executions"], [])
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    """SELECT filled_at FROM fills
                       WHERE fill_id='legacy-time-fill'"""
                ).fetchone()[0], "2026-07-07T10:01:00+08:00")

    def test_same_broker_order_reuses_client_identity_without_template_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            first = self._ledger_snapshot("2026-07-07 10:05:00")
            first["orders"][0].update(
                id="signal-identity", status="partial", filled=500,
            )
            first["trades"] = []
            joinquant_sync.ingest_snapshot_payload(
                first, store, "2026-07-07 10:05:02",
            )
            with store.connect() as conn:
                original_client_id = conn.execute(
                    "SELECT client_order_id FROM orders WHERE order_id='10'"
                ).fetchone()[0]

            later = json.loads(json.dumps(first))
            later.pop("template_version")
            later["generated_at"] = "2026-07-07 10:06:00"
            later["orders"][0]["datetime"] = later["generated_at"]
            joinquant_sync.ingest_snapshot_payload(
                later, store, "2026-07-07 10:06:02",
            )

            with store.connect() as conn:
                scope = conn.execute(
                    "SELECT account_scope_id FROM account_scopes"
                ).fetchone()[0]
                current = store.load_current_broker_snapshot(conn, scope)
                rows = conn.execute(
                    "SELECT client_order_id FROM orders WHERE order_id='10'"
                ).fetchall()
            self.assertEqual([row[0] for row in rows], [original_client_id])
            self.assertEqual(
                current.open_orders[0]["client_order_id"], original_client_id,
            )

    def test_explicit_client_order_identity_survives_sanitize_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            first = self._ledger_snapshot("2026-07-07 10:05:00")
            first["orders"][0].update(
                client_order_id="intent-order-1",
                status="partial",
                filled=500,
            )
            first["trades"][0].update(
                client_order_id="intent-order-1",
                amount=500,
            )

            sanitized = joinquant_sync.sanitize_joinquant_payload(first)
            self.assertEqual(
                sanitized["orders"][0]["client_order_id"], "intent-order-1",
            )
            self.assertEqual(
                sanitized["trades"][0]["client_order_id"], "intent-order-1",
            )
            result = joinquant_sync.ingest_snapshot_payload(
                first, store, "2026-07-07 10:05:02",
            )
            self.assertEqual(
                result["new_executions"][0]["client_order_id"],
                "intent-order-1",
            )

            later = json.loads(json.dumps(first))
            later["generated_at"] = "2026-07-07 10:06:00"
            later["orders"][0].pop("client_order_id")
            later["trades"] = []
            later["orders"][0]["datetime"] = later["generated_at"]
            joinquant_sync.ingest_snapshot_payload(
                later, store, "2026-07-07 10:06:02",
            )

            with store.connect() as conn:
                scope = conn.execute(
                    "SELECT account_scope_id FROM account_scopes"
                ).fetchone()[0]
                current = store.load_current_broker_snapshot(conn, scope)
                order_ids = conn.execute(
                    "SELECT client_order_id FROM orders WHERE order_id='10'"
                ).fetchall()
                fill_id = conn.execute(
                    "SELECT client_order_id FROM fills WHERE fill_id='20'"
                ).fetchone()[0]
            self.assertEqual([row[0] for row in order_ids], ["intent-order-1"])
            self.assertEqual(fill_id, "intent-order-1")
            self.assertEqual(
                current.open_orders[0]["client_order_id"], "intent-order-1",
            )

    def test_explicit_client_order_identity_conflict_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            seed = {
                "schema_version": 1,
                "positions": [],
                "trades": [],
                "orders": [{
                    "client_order_id": "intent-order-1",
                    "order_id": "stable-order",
                    "action": "buy",
                    "code": "600000",
                    "amount": 100,
                    "filled": 0,
                    "status": "submitted",
                    "datetime": "2026-07-07 10:00:00",
                }],
            }
            joinquant_sync.ingest_snapshot_payload(
                seed, store, "2026-07-07 10:00:01",
            )
            conflicting = json.loads(json.dumps(seed))
            conflicting["orders"][0]["client_order_id"] = "intent-order-2"
            conflicting["orders"][0]["datetime"] = "2026-07-07 10:01:00"
            with self.assertRaisesRegex(ValueError, "identity"):
                joinquant_sync.ingest_snapshot_payload(
                    conflicting, store, "2026-07-07 10:01:01",
                )
            with store.connect() as conn:
                rows = conn.execute(
                    "SELECT client_order_id FROM orders WHERE order_id='stable-order'"
                ).fetchall()
            self.assertEqual([row[0] for row in rows], ["intent-order-1"])

    def test_invalid_nested_client_order_identity_is_not_sanitized_away(self) -> None:
        payload = {
            "schema_version": 1,
            "positions": [],
            "trades": [],
            "orders": [{
                "client_order_id": ["invalid"],
                "order_id": "bad-order",
                "action": "buy",
                "code": "600000",
                "amount": 100,
                "filled": 0,
                "status": "submitted",
                "datetime": "2026-07-07 10:00:00",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            ValueError, "client_order_id",
        ):
            joinquant_sync.ingest_snapshot_payload(
                payload,
                TradingStore(Path(tmp) / "trading.db"),
                "2026-07-07 10:00:01",
            )

    def test_missing_signal_packet_cannot_erase_broker_order_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            base = {
                "schema_version": 1, "positions": [], "trades": [],
                "orders": [{
                    "order_id": "stable-order", "id": "signal-A",
                    "action": "buy", "code": "600000", "amount": 100,
                    "filled": 0, "status": "submitted",
                    "datetime": "2026-07-07 10:00:00",
                }],
            }
            joinquant_sync.ingest_snapshot_payload(
                base, store, "2026-07-07 10:00:01",
            )
            missing = json.loads(json.dumps(base))
            missing["orders"][0].pop("id")
            missing["orders"][0]["datetime"] = "2026-07-07 10:01:00"
            joinquant_sync.ingest_snapshot_payload(
                missing, store, "2026-07-07 10:01:01",
            )
            drifted = json.loads(json.dumps(missing))
            drifted["orders"][0]["id"] = "signal-B"
            drifted["orders"][0]["datetime"] = "2026-07-07 10:02:00"
            with self.assertRaisesRegex(ValueError, "identity"):
                joinquant_sync.ingest_snapshot_payload(
                    drifted, store, "2026-07-07 10:02:01",
                )

    def test_terminal_order_progress_cannot_grow_without_fill_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            base_order = {
                "order_id": "cancelled-progress", "action": "buy",
                "code": "600000", "amount": 100, "filled": 0,
                "status": "cancelled",
                "datetime": "2026-07-07 10:00:00",
            }
            joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "trades": [],
                    "orders": [base_order],
                },
                store,
                "2026-07-07 10:00:01",
            )
            with self.assertRaisesRegex(ValueError, "terminal order"):
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [], "trades": [],
                        "orders": [{
                            **base_order, "filled": 50, "avg_price": 10,
                            "datetime": "2026-07-07 10:01:00",
                        }],
                    },
                    store,
                    "2026-07-07 10:01:01",
                )

    def test_new_fill_cannot_rewrite_rejected_order_as_filled(self) -> None:
        for status in (
            "rejected", "failed", "skipped", "risk_rejected",
            "not_submitted", "expired",
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [], "trades": [],
                        "orders": [{
                            "order_id": "terminal-order", "action": "buy",
                            "code": "600000", "amount": 100, "filled": 0,
                            "status": status,
                            "datetime": "2026-07-07 10:00:00",
                        }],
                    },
                    store,
                    "2026-07-07 10:00:01",
                )
                with self.assertRaisesRegex(ValueError, "terminal order"):
                    joinquant_sync.ingest_snapshot_payload(
                        {
                            "schema_version": 1, "positions": [], "orders": [],
                            "trades": [{
                                "trade_id": "impossible-fill",
                                "order_id": "terminal-order", "action": "buy",
                                "code": "600000", "amount": 100, "price": 10,
                                "datetime": "2026-07-07 10:01:00",
                            }],
                        },
                        store,
                        "2026-07-07 10:01:01",
                    )
                with store.connect() as conn:
                    order = conn.execute(
                        "SELECT status, filled_qty FROM orders WHERE order_id='terminal-order'"
                    ).fetchone()
                    fill_count = conn.execute("SELECT count(*) FROM fills").fetchone()[0]
                self.assertEqual(tuple(order), (status, 0))
                self.assertEqual(fill_count, 0)

    def test_terminal_orders_accept_only_consistent_historical_fills(self) -> None:
        for status, reported_qty in (("cancelled", 40), ("filled", 100)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [], "trades": [],
                        "orders": [{
                            "order_id": "terminal-history", "action": "buy",
                            "code": "600000", "amount": 100,
                            "filled": reported_qty, "avg_price": 10,
                            "status": status,
                            "datetime": "2026-07-07 10:00:00",
                        }],
                    },
                    store,
                    "2026-07-07 10:00:01",
                )
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [], "orders": [],
                        "trades": [{
                            "trade_id": "historical-fill",
                            "order_id": "terminal-history", "action": "buy",
                            "code": "600000", "amount": reported_qty,
                            "price": 10, "datetime": "2026-07-07 10:01:00",
                        }],
                    },
                    store,
                    "2026-07-07 10:01:01",
                )
                with store.connect() as conn:
                    order = conn.execute(
                        "SELECT status, filled_qty FROM orders WHERE order_id='terminal-history'"
                    ).fetchone()
                self.assertEqual(tuple(order), (status, reported_qty))

    def test_cancelled_order_rejects_fill_above_reported_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "trades": [],
                    "orders": [{
                        "order_id": "cancelled-order", "action": "buy",
                        "code": "600000", "amount": 100, "filled": 40,
                        "avg_price": 10, "status": "cancelled",
                        "datetime": "2026-07-07 10:00:00",
                    }],
                },
                store,
                "2026-07-07 10:00:01",
            )
            with self.assertRaisesRegex(ValueError, "cancelled terminal order"):
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [], "orders": [],
                        "trades": [{
                            "trade_id": "excess-cancelled-fill",
                            "order_id": "cancelled-order", "action": "buy",
                            "code": "600000", "amount": 41, "price": 10,
                            "datetime": "2026-07-07 10:01:00",
                        }],
                    },
                    store,
                    "2026-07-07 10:01:01",
                )
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM fills").fetchone()[0], 0,
                )

    def test_event_order_identity_conflict_rolls_back_the_whole_batch(self) -> None:
        drifts = (
            {
                "order_id": "identity-order", "id": "signal-1",
                "action": "sell", "code": "600000",
            },
            {
                "order_id": "identity-order", "id": "signal-1",
                "action": "buy", "code": "000001",
            },
            {
                "order_id": "different-order", "id": "signal-1",
                "action": "buy", "code": "600000",
            },
            {
                "order_id": "identity-order", "id": "different-signal",
                "action": "buy", "code": "600000",
            },
        )
        for drift in drifts:
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                store = TradingStore(Path(tmp) / "trading.db")
                seed = {
                    "schema_version": 1, "positions": [], "trades": [],
                    "orders": [{
                        "order_id": "identity-order", "id": "signal-1",
                        "action": "buy", "code": "600000", "amount": 100,
                        "filled": 0, "status": "submitted",
                        "datetime": "2026-07-07 10:00:00",
                    }],
                }
                joinquant_sync.ingest_snapshot_payload(
                    seed, store, "2026-07-07 10:00:01",
                )
                valid_new = {
                    "order_id": "must-roll-back", "action": "buy",
                    "code": "000001", "amount": 100, "filled": 0,
                    "status": "submitted",
                    "datetime": "2026-07-07 10:01:00",
                }
                conflicting = {
                    **drift, "amount": 100, "filled": 0,
                    "status": "submitted",
                    "datetime": "2026-07-07 10:01:00",
                }
                with self.assertRaisesRegex(ValueError, "identity"):
                    joinquant_sync.ingest_snapshot_payload(
                        {
                            "schema_version": 1, "positions": [], "trades": [],
                            "orders": [valid_new, conflicting],
                        },
                        store,
                        "2026-07-07 10:01:01",
                    )
                with store.connect() as conn:
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM orders").fetchone()[0],
                        1,
                    )
                    self.assertIsNone(conn.execute(
                        """SELECT 1 FROM orders
                           WHERE order_id='must-roll-back'"""
                    ).fetchone())

    def test_event_fills_cannot_exceed_the_linked_order_quantity(self) -> None:
        cases = (
            (101,),
            (60, 50),
        )
        for quantities in cases:
            with (
                self.subTest(quantities=quantities),
                tempfile.TemporaryDirectory() as tmp,
            ):
                store = TradingStore(Path(tmp) / "trading.db")
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1, "positions": [], "trades": [],
                        "orders": [{
                            "order_id": "bounded-order", "action": "buy",
                            "code": "600000", "amount": 100, "filled": 0,
                            "status": "submitted",
                            "datetime": "2026-07-07 10:00:00",
                        }],
                    },
                    store,
                    "2026-07-07 10:00:01",
                )
                trades = [{
                    "trade_id": f"bounded-fill-{index}",
                    "order_id": "bounded-order", "action": "buy",
                    "code": "600000", "amount": qty, "price": 10 + index,
                    "datetime": f"2026-07-07 10:01:0{index}",
                } for index, qty in enumerate(quantities)]
                with self.assertRaisesRegex(ValueError, "order quantity"):
                    joinquant_sync.ingest_snapshot_payload(
                        {
                            "schema_version": 1, "positions": [],
                            "orders": [], "trades": trades,
                        },
                        store,
                        "2026-07-07 10:01:09",
                    )
                with store.connect() as conn:
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM fills").fetchone()[0],
                        0,
                    )
                    order = conn.execute(
                        """SELECT filled_qty, status FROM orders
                           WHERE order_id='bounded-order'"""
                    ).fetchone()
                self.assertEqual(tuple(order), (0, "submitted"))

    def test_event_order_quantity_cannot_expand_after_identity_is_landed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            seed = {
                "schema_version": 1, "positions": [], "trades": [],
                "orders": [{
                    "order_id": "fixed-quantity", "action": "buy",
                    "code": "600000", "amount": 100, "target_qty": 100,
                    "filled": 0, "status": "submitted",
                    "datetime": "2026-07-07 10:00:00",
                }],
            }
            joinquant_sync.ingest_snapshot_payload(
                seed, store, "2026-07-07 10:00:01",
            )
            expanded = {
                **seed,
                "orders": [{
                    **seed["orders"][0],
                    "amount": 200,
                    "target_qty": 200,
                    "datetime": "2026-07-07 10:01:00",
                }],
            }
            with self.assertRaisesRegex(ValueError, "identity"):
                joinquant_sync.ingest_snapshot_payload(
                    expanded, store, "2026-07-07 10:01:01",
                )
            with store.connect() as conn:
                order = conn.execute(
                    """SELECT requested_qty, target_qty FROM orders
                       WHERE order_id='fixed-quantity'"""
                ).fetchone()
            self.assertEqual(tuple(order), (100, 100))

    def test_rejected_event_order_cannot_report_new_fill_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            seed = {
                "schema_version": 1, "positions": [], "trades": [],
                "orders": [{
                    "order_id": "rejected-progress", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 0,
                    "status": "submitted",
                    "datetime": "2026-07-07 10:00:00",
                }],
            }
            joinquant_sync.ingest_snapshot_payload(
                seed, store, "2026-07-07 10:00:01",
            )
            with self.assertRaisesRegex(ValueError, "terminal order"):
                joinquant_sync.ingest_snapshot_payload(
                    {
                        **seed,
                        "orders": [{
                            **seed["orders"][0],
                            "filled": 50, "avg_price": 10,
                            "status": "rejected",
                            "datetime": "2026-07-07 10:01:00",
                        }],
                    },
                    store,
                    "2026-07-07 10:01:01",
                )
            with store.connect() as conn:
                order = conn.execute(
                    """SELECT filled_qty, status FROM orders
                       WHERE order_id='rejected-progress'"""
                ).fetchone()
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM fills",
                ).fetchone()[0], 0)
            self.assertEqual(tuple(order), (0, "submitted"))

    def test_event_fill_uses_original_target_when_requested_qty_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "trades": [],
                    "orders": [{
                        "order_id": "target-only-order", "action": "buy",
                        "code": "600000", "target_qty": 100, "filled": 0,
                        "status": "submitted",
                        "datetime": "2026-07-07 10:00:00",
                    }],
                },
                store,
                "2026-07-07 10:00:01",
            )
            result = joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "orders": [],
                    "trades": [{
                        "trade_id": "target-only-fill",
                        "order_id": "target-only-order", "action": "buy",
                        "code": "600000", "amount": 100, "price": 10,
                        "datetime": "2026-07-07 10:01:00",
                    }],
                },
                store,
                "2026-07-07 10:01:01",
            )
            self.assertEqual(result["new_executions"][0]["cumulative_qty"], 100)
            with store.connect() as conn:
                order = conn.execute(
                    """SELECT filled_qty, status FROM orders
                       WHERE order_id='target-only-order'"""
                ).fetchone()
            self.assertEqual(tuple(order), (100, "filled"))

    def test_trade_only_fills_advance_order_once_with_weighted_average(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "trades": [],
                    "orders": [{
                        "order_id": "progress-order", "action": "buy",
                        "code": "600000", "amount": 100, "filled": 0,
                        "status": "submitted",
                        "datetime": "2026-07-07 10:00:00",
                    }],
                },
                store,
                "2026-07-07 10:00:01",
            )
            first_payload = {
                "schema_version": 1, "positions": [], "orders": [],
                "trades": [{
                    "trade_id": "progress-fill-1",
                    "order_id": "progress-order", "action": "buy",
                    "code": "600000", "amount": 40, "price": 10,
                    "datetime": "2026-07-07 10:01:00",
                }],
            }
            first = joinquant_sync.ingest_snapshot_payload(
                first_payload, store, "2026-07-07 10:01:01",
            )
            replay = joinquant_sync.ingest_snapshot_payload(
                first_payload, store, "2026-07-07 10:01:02",
            )
            second = joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "orders": [],
                    "trades": [{
                        "trade_id": "progress-fill-2",
                        "order_id": "progress-order", "action": "buy",
                        "code": "600000", "amount": 60, "price": 11,
                        "datetime": "2026-07-07 10:02:00",
                    }],
                },
                store,
                "2026-07-07 10:02:01",
            )
            later_order = joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "trades": [],
                    "orders": [{
                        "order_id": "progress-order", "action": "buy",
                        "code": "600000", "amount": 100, "filled": 100,
                        "avg_price": 10.6, "status": "filled",
                        "datetime": "2026-07-07 10:02:10",
                    }],
                },
                store,
                "2026-07-07 10:02:11",
            )
            self.assertEqual(first["new_executions"][0]["cumulative_qty"], 40)
            self.assertEqual(replay["new_executions"], [])
            self.assertEqual(second["new_executions"][0]["cumulative_qty"], 100)
            self.assertEqual(later_order["new_executions"], [])
            with store.connect() as conn:
                order = conn.execute(
                    """SELECT filled_qty, average_fill_price, status
                       FROM orders WHERE order_id='progress-order'"""
                ).fetchone()
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM fills").fetchone()[0],
                    2,
                )
            self.assertEqual(order["filled_qty"], 100)
            self.assertAlmostEqual(order["average_fill_price"], 10.6)
            self.assertEqual(order["status"], "filled")

    def test_timed_snapshot_requires_complete_account_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            snapshot.pop("cash")
            with self.assertRaisesRegex(ValueError, "missing account fields"):
                joinquant_sync.ingest_snapshot_payload(
                    snapshot, store, "2026-07-07 10:05:02",
                )
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM account_scopes").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM account_snapshots"
                    ).fetchone()[0],
                    0,
                )

    def test_nonempty_positions_never_use_event_only_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            with self.assertRaisesRegex(ValueError, "missing account fields"):
                joinquant_sync.ingest_snapshot_payload(
                    {
                        "schema_version": 1,
                        "positions": [{"code": "600000", "qty": 100}],
                        "orders": [], "trades": [],
                    },
                    store,
                    "2026-07-07 10:05:02",
                )

    def test_missing_fee_fields_are_not_reported_as_known_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            for key in ("commission", "stamp_tax", "other_fee"):
                snapshot["trades"][0].pop(key)
            snapshot["realized_pnl"] = 123.45

            joinquant_sync.ingest_snapshot_payload(
                snapshot, store, "2026-07-07 10:05:02",
            )

            with store.connect() as conn:
                fill = conn.execute("SELECT * FROM fills WHERE fill_id='20'").fetchone()
                equity = conn.execute(
                    "SELECT * FROM daily_equity WHERE trade_date='2026-07-07'"
                ).fetchone()
            self.assertEqual(fill["commission"], 0)
            self.assertEqual(fill["fee_data_status"], "unknown")
            self.assertEqual(equity["fee_data_status"], "unknown")
            self.assertEqual(equity["realized_pnl"], 123.45)
            self.assertEqual(equity["realized_pnl_status"], "reported")

    def test_snapshot_persists_strategy_template_version_for_recovery_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            payload = self._ledger_snapshot()
            payload.pop("template_version", None)
            payload["strategy_template_version"] = "2026-07-15.1-execution-state-recovery"
            result = joinquant_sync.ingest_snapshot_payload(
                payload, store, "2026-07-07 10:05:02"
            )
            with store.connect() as conn:
                version = conn.execute(
                    "SELECT template_version FROM account_snapshots WHERE snapshot_id=?",
                    (result["snapshot_id"],),
                ).fetchone()[0]
            self.assertEqual(version, "2026-07-15.1-execution-state-recovery")

    def test_new_execution_events_include_each_new_partial_fill_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            first = self._ledger_snapshot("2026-07-07 10:05:00")
            first["orders"][0]["filled"] = 50
            first["orders"][0]["amount"] = 100
            first["orders"][0]["status"] = "partial"
            first["trades"][0]["trade_id"] = "fill-50-a"
            first["trades"][0]["amount"] = 50
            second = self._ledger_snapshot("2026-07-07 10:06:00")
            second["orders"][0]["filled"] = 100
            second["orders"][0]["amount"] = 100
            second["trades"] = [
                dict(first["trades"][0]),
                {
                    "trade_id": "fill-50-b",
                    "order_id": "10",
                    "code": "600000",
                    "action": "buy",
                    "amount": 50,
                    "price": 10.1,
                    "commission": 1.0,
                    "stamp_tax": 0.0,
                    "other_fee": 0.0,
                    "datetime": "2026-07-07 10:06:00",
                },
            ]

            first_result = joinquant_sync.ingest_snapshot_payload(first, store, "2026-07-07 10:05:02")
            second_result = joinquant_sync.ingest_snapshot_payload(second, store, "2026-07-07 10:06:02")
            replay_result = joinquant_sync.ingest_snapshot_payload(second, store, "2026-07-07 10:06:03")

            self.assertEqual([row["event_id"] for row in first_result["new_executions"]], ["fill:fill-50-a"])
            self.assertEqual([row["event_id"] for row in second_result["new_executions"]], ["fill:fill-50-b"])
            self.assertEqual(replay_result["new_executions"], [])

    def test_legacy_order_progress_notifies_only_when_filled_quantity_increases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            snapshot["trades"] = []
            snapshot["orders"][0]["amount"] = 100
            snapshot["orders"][0]["filled"] = 50
            snapshot["orders"][0]["status"] = "partial"

            first = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:02")
            replay = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:03")
            snapshot["generated_at"] = "2026-07-07 10:06:00"
            snapshot["orders"][0]["filled"] = 100
            snapshot["orders"][0]["status"] = "filled"
            increased = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:06:02")

            self.assertEqual(first["new_executions"][0]["qty"], 50)
            self.assertEqual(replay["new_executions"], [])
            self.assertEqual(increased["new_executions"][0]["qty"], 50)
            self.assertEqual(increased["new_executions"][0]["cumulative_qty"], 100)

    def test_late_real_trade_does_not_renotify_legacy_covered_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            legacy = self._ledger_snapshot()
            legacy["trades"] = []
            legacy["orders"][0].update({
                "amount": 100, "filled": 50, "avg_price": 10,
                "status": "partial",
            })
            first = joinquant_sync.ingest_snapshot_payload(
                legacy, store, "2026-07-07 10:05:02",
            )

            authoritative = json.loads(json.dumps(legacy))
            authoritative["generated_at"] = "2026-07-07 10:06:00"
            authoritative["orders"][0]["datetime"] = "2026-07-07 10:06:00"
            authoritative["trades"] = [{
                "trade_id": "late-fill-50", "order_id": "10",
                "code": "600000", "action": "buy", "amount": 50,
                "price": 10, "datetime": "2026-07-07 10:05:30",
            }]
            second = joinquant_sync.ingest_snapshot_payload(
                authoritative, store, "2026-07-07 10:06:02",
            )

            with store.connect() as conn:
                fill_count = conn.execute(
                    "SELECT COUNT(*) FROM fills WHERE fill_id='late-fill-50'"
                ).fetchone()[0]
                notice_count = conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE event_type='fill'"
                ).fetchone()[0]
            self.assertEqual(first["new_executions"][0]["qty"], 50)
            self.assertEqual(second["new_executions"], [])
            self.assertEqual(second["inserted_fills"], 1)
            self.assertEqual(fill_count, 1)
            self.assertEqual(notice_count, 1)

    def test_late_broker_order_id_keeps_legacy_fill_notification_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            legacy = self._ledger_snapshot()
            legacy["trades"] = []
            legacy["orders"][0].update({
                "client_order_id": "client-late-broker-id",
                "amount": 100, "filled": 50, "avg_price": 10,
                "status": "partial",
            })
            legacy["orders"][0].pop("order_id")
            first = joinquant_sync.ingest_snapshot_payload(
                legacy, store, "2026-07-07 10:05:02",
            )

            authoritative = json.loads(json.dumps(legacy))
            authoritative["generated_at"] = "2026-07-07 10:06:00"
            authoritative["orders"][0].update({
                "order_id": "broker-arrived-late",
                "datetime": "2026-07-07 10:06:00",
            })
            authoritative["trades"] = [{
                "trade_id": "late-id-fill-50",
                "client_order_id": "client-late-broker-id",
                "order_id": "broker-arrived-late", "code": "600000",
                "action": "buy", "amount": 50, "price": 10,
                "datetime": "2026-07-07 10:05:30",
            }]
            second = joinquant_sync.ingest_snapshot_payload(
                authoritative, store, "2026-07-07 10:06:02",
            )

            with store.connect() as conn:
                notice_count = conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE event_type='fill'"
                ).fetchone()[0]
                broker_id = conn.execute(
                    """SELECT order_id FROM orders
                       WHERE client_order_id='client-late-broker-id'"""
                ).fetchone()[0]
            self.assertEqual(first["new_executions"][0]["qty"], 50)
            self.assertEqual(second["new_executions"], [])
            self.assertEqual(second["inserted_fills"], 1)
            self.assertEqual(notice_count, 1)
            self.assertEqual(broker_id, "broker-arrived-late")

    def test_legacy_progress_fact_direct_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            execution = {
                "event_id": "legacy:client-1:50",
                "source": "legacy_order_progress",
                "client_order_id": "client-1",
                "order_id": "broker-1",
                "signal_id": "signal-1",
                "stock_code": "600000",
                "action": "buy",
                "qty": 50,
                "cumulative_qty": 50,
                "price": 10,
                "status": "partial",
                "filled_at": "2026-07-07T10:05:00+08:00",
            }
            with store.transaction() as conn:
                self.assertTrue(joinquant_sync._insert_legacy_progress_fact(
                    conn, execution,
                ))
                self.assertFalse(joinquant_sync._insert_legacy_progress_fact(
                    conn, execution,
                ))
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM order_events"
                ).fetchone()[0], 1)

    def test_detail_retention_keeps_changes_and_hourly_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            joinquant_sync.ingest_snapshot_payload(
                self._ledger_snapshot("2026-07-07 10:05:00"), store, "2026-07-07 10:05:02"
            )
            joinquant_sync.ingest_snapshot_payload(
                self._ledger_snapshot("2026-07-07 10:06:00"), store, "2026-07-07 10:06:02"
            )
            joinquant_sync.ingest_snapshot_payload(
                self._ledger_snapshot("2026-07-07 11:00:00"), store, "2026-07-07 11:00:02"
            )
            changed = self._ledger_snapshot("2026-07-07 11:01:00")
            changed["positions"][0]["price"] = 10.6
            changed["positions"][0]["market_value"] = 10600
            joinquant_sync.ingest_snapshot_payload(changed, store, "2026-07-07 11:01:02")

            with store.connect() as conn:
                retained = conn.execute(
                    "SELECT generated_at FROM account_snapshots WHERE retained_details=1 ORDER BY generated_at"
                ).fetchall()
                details = conn.execute("SELECT count(*) FROM position_snapshots").fetchone()[0]
                controls = {
                    row["key"]: row["value"] for row in conn.execute(
                        "SELECT key, value FROM system_state WHERE key IN ('buy_enabled','kill_switch')"
                    )
                }
            self.assertEqual([row[0] for row in retained], [
                "2026-07-07 10:05:00", "2026-07-07 11:00:00", "2026-07-07 11:01:00",
            ])
            self.assertEqual(details, 3)
            self.assertEqual(controls.get("buy_enabled", "1"), "1")
            self.assertEqual(controls.get("kill_switch", "0"), "0")

    def test_syncs_snapshot_to_portfolio_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_file = Path(tmp) / "account.json"
            positions_file = Path(tmp) / "positions.json"
            events_file = Path(tmp) / "events.jsonl"
            migration_file = Path(tmp) / "migration.md"
            account_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "trade_date": "2026-07-07",
                        "generated_at": "2026-07-07 15:05:00",
                        "source": "joinquant",
                        "cash": 20000,
                        "available_cash": 18000,
                        "total_value": 100000,
                        "daily_turnover_pct": 12.5,
                        "daily_pnl_pct": -1.2,
                        "account_drawdown_pct": -3.4,
                        "consecutive_losses": 2,
                        "pending_buy_position_pct": 4.5,
                        "pending_buy_risk_pct": 0.4,
                        "positions": [
                            {
                                "code": "600000",
                                "jq_code": "600000.XSHG",
                                "name": "PF Bank",
                                "qty": 1000,
                                "closeable_amount": 600,
                                "locked_amount": 200,
                                "today_amount": 200,
                                "avg_cost": 10.0,
                                "price": 10.5,
                                "market_value": 10500,
                                "pnl": 500,
                                "raw_account_id": "position-account-secret",
                                "authorization": "position-auth-secret",
                            }
                        ],
                        "orders": [],
                        "trades": [],
                    }
                ),
                encoding="utf-8",
            )

            store = TradingStore(Path(tmp) / "trading.db")
            count = joinquant_sync.sync_account_snapshot(
                account_file, positions_file, events_file, store=store, migration_report_file=migration_file
            )

            payload = json.loads(positions_file.read_text(encoding="utf-8"))
            self.assertEqual(count, 1)
            self.assertEqual(payload["positions"][0]["code"], "600000")
            self.assertEqual(payload["positions"][0]["source"], "joinquant")
            self.assertEqual(payload["positions"][0]["current_price"], 10.5)
            self.assertEqual(payload["positions"][0]["closeable_qty"], 600)
            self.assertEqual(payload["positions"][0]["locked_qty"], 200)
            self.assertEqual(payload["account"]["available_cash"], 18000)
            self.assertEqual(payload["account"]["daily_turnover_pct"], 12.5)
            self.assertEqual(payload["account"]["consecutive_losses"], 2)
            self.assertEqual(payload["account"]["pending_buy_position_pct"], 4.5)
            self.assertEqual(payload["account"]["pending_buy_risk_pct"], 0.4)
            self.assertTrue(events_file.exists())
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["current_qty"], 1000)
            self.assertEqual(cycle["initial_stop_price"], 9.4)
            self.assertEqual(payload["positions"][0]["effective_stop_price"], 9.4)
            self.assertIsNone(payload["positions"][0]["manual_stop_price"])
            serialized = positions_file.read_text(encoding="utf-8")
            self.assertNotIn("position-account-secret", serialized)
            self.assertNotIn("position-auth-secret", serialized)

            report = joinquant_sync.build_position_migration_report(payload["positions"], store.get_active_position_cycles())
            self.assertIn("600000", report)
            self.assertIn("ATR", report)
            self.assertIn("enabled_rules", report)
            self.assertEqual(joinquant_sync.unsafe_migration_codes(
                payload["positions"], store.get_active_position_cycles(),
            ), [])
            self.assertIn("固定硬止损", report)
            self.assertTrue(migration_file.exists())

    def test_sync_defaults_missing_consecutive_losses_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_file = root / "account.json"
            positions_file = root / "positions.json"
            account_file.write_text(
                json.dumps({"schema_version": 1, "positions": []}), encoding="utf-8"
            )

            joinquant_sync.sync_account_snapshot(
                account_file,
                positions_file,
                root / "events.jsonl",
                store=TradingStore(root / "trading.db"),
                migration_report_file=root / "migration.md",
            )

            payload = json.loads(positions_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["account"]["consecutive_losses"], 0)


if __name__ == "__main__":
    unittest.main()
