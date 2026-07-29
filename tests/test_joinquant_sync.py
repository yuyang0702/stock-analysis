import json
import tempfile
import unittest
from pathlib import Path

import joinquant_sync
from trading_store import TradingStore


class JoinQuantSyncTest(unittest.TestCase):
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

    def test_ingest_persists_strict_scoped_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            snapshot = self._ledger_snapshot()
            snapshot["token"] = "must-not-enter-current-contract"
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
                    "account_scopes", "broker_snapshot_current",
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

    def test_event_only_legacy_payload_never_creates_broker_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            result = joinquant_sync.ingest_snapshot_payload(
                {
                    "schema_version": 1, "positions": [], "orders": [],
                    "trades": [],
                },
                store,
                "2026-07-07 10:05:02",
            )
            self.assertTrue(result["snapshot_id"])
            with store.connect() as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM account_scopes").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM broker_snapshot_current"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM account_snapshots"
                    ).fetchone()[0],
                    1,
                )

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

            first = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:02")
            replay = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:03")
            snapshot["generated_at"] = "2026-07-07 10:06:00"
            snapshot["orders"][0]["filled"] = 100
            increased = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:06:02")

            self.assertEqual(first["new_executions"][0]["qty"], 50)
            self.assertEqual(replay["new_executions"], [])
            self.assertEqual(increased["new_executions"][0]["qty"], 50)
            self.assertEqual(increased["new_executions"][0]["cumulative_qty"], 100)

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
                            }
                        ],
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
