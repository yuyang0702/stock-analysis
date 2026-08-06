import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import trading_backup
from notification_outbox import NotificationEvent, notification_event_key

from trading_backup import (
    create_backup,
    load_latest_status,
    main,
    notify_failure,
    prune_tier,
    run_restore_drill,
    validated_manifests,
    write_latest_report,
)
from trading_store import SCHEMA_VERSION, TradingStore


class TradingBackupTest(unittest.TestCase):
    def test_schema_v12_backup_contract_requires_all_new_tables_and_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            self.make_store(path)
            facts = trading_backup.database_facts(path)
            self.assertEqual(facts["schema_version"], SCHEMA_VERSION)
            self.assertTrue({
                "account_scopes", "broker_snapshot_current",
                "broker_position_current", "broker_order_current",
                "strategy_order_candidates", "pre_trade_results",
                "execution_intents", "capacity_reservations",
                "position_capacity_adoptions",
                "notification_outbox", "notification_enqueue_gaps",
                "logical_signal_plans",
            }.issubset(facts["table_counts"]))
            conn = sqlite3.connect(path)
            try:
                reconciliation_columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(reconciliation_runs)"
                    )
                }
                self.assertTrue({
                    "account_scope_id", "broker_snapshot_id",
                    "broker_snapshot_sha256", "snapshot_broker_time",
                    "snapshot_generated_at",
                }.issubset(reconciliation_columns))
                self.assertIn(
                    "account_scopes",
                    {
                        row[2] for row in conn.execute(
                            "PRAGMA foreign_key_list(reconciliation_runs)"
                        )
                    },
                )
                conn.execute("DROP TABLE capacity_reservations")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaisesRegex(RuntimeError, "capacity_reservations"):
                trading_backup.database_facts(path)

    def test_schema_v11_backup_counts_position_capacity_adoptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            self.make_store(path)
            facts = trading_backup.database_facts(path)
            self.assertEqual(
                facts["table_counts"]["position_capacity_adoptions"], 0,
            )

    def test_schema_v12_backup_and_restore_count_notification_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            store = self.make_store(path)
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
                conn.execute(
                    """INSERT INTO logical_signal_plans(
                       account_scope_id, trade_date, logical_signal_id,
                       frozen_valid_until, current_plan_version, updated_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        scope, "2026-07-28", "logical-1",
                        "2026-07-28T15:00:00+08:00", "plan-1",
                        "2026-07-28T10:00:00+08:00",
                    ),
                )
                for fill_id, as_gap in (("fill-1", False), ("fill-2", True)):
                    event = NotificationEvent(
                        event_key=notification_event_key(
                            "joinquant", scope, "fill", fill_id=fill_id,
                        ),
                        account_scope_id=scope,
                        adapter="joinquant",
                        event_type="fill",
                        object_type="fill",
                        object_id=fill_id,
                        source_fact_id=fill_id,
                        priority="high",
                        payload_version=1,
                        occurred_at="2026-07-28T10:00:00+08:00",
                        expires_at=None,
                        title="成交回报",
                        body=f"{fill_id} 成交",
                        payload={"fill_id": fill_id},
                        metadata={"renderer": "v1"},
                    )
                    if as_gap:
                        store.enqueue_notification_gap(
                            conn, event, "capacity", "2026-07-28T10:00:00+08:00",
                        )
                    else:
                        store.enqueue_notification(
                            conn, event, "2026-07-28T10:00:00+08:00",
                        )
            backup = Path(tmp) / "copy.db"
            store.backup_to(backup)
            live = trading_backup.database_facts(path)
            restored = trading_backup.database_facts(backup)
            self.assertEqual(live["table_counts"], restored["table_counts"])
            self.assertEqual(restored["table_counts"]["logical_signal_plans"], 1)
            # The high-priority gap also persists its capacity control event.
            self.assertEqual(restored["table_counts"]["notification_outbox"], 2)
            self.assertEqual(restored["table_counts"]["notification_enqueue_gaps"], 1)
            self.assertEqual(live["notification_health"], restored["notification_health"])
            self.assertEqual(restored["notification_health"], {
                "pending": 2,
                "leased": 0,
                "sent": 0,
                "dead": 0,
                "cancelled": 0,
                "unresolved_gaps": 1,
                "high_unresolved_gaps": 1,
                "dead_detail_rows": 0,
                "dead_detail_bytes": 0,
                "high_dead_detail_rows": 0,
                "high_dead_total_rows": 0,
                "tombstones": 0,
                "write_failure_marker": False,
                "write_failure_requires_manual_resolution": False,
            })

    def test_schema_v12_backup_rejects_foreign_key_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            self.make_store(path)
            conn = sqlite3.connect(path)
            try:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute(
                    """INSERT INTO logical_signal_plans(
                       account_scope_id, trade_date, logical_signal_id,
                       frozen_valid_until, current_plan_version, updated_at
                       ) VALUES('missing','2026-07-28','logical-1',
                                '2026-07-28T15:00:00+00:00','plan-1',
                                '2026-07-28T02:00:00+00:00')"""
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaisesRegex(ValueError, "foreign_key_check"):
                trading_backup.database_facts(path)

    def test_schema_v10_backup_contract_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            with patch.object(
                TradingStore, "_migrate_schema_v11", return_value=None,
            ):
                TradingStore(path).initialize()
            facts = trading_backup.database_facts(path)
            self.assertEqual(facts["schema_version"], 10)
            self.assertEqual(set(facts["table_counts"]), set(trading_backup.SCHEMA_10_TABLES))

    def test_schema_v11_backup_rejects_missing_profit_protection_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            self.make_store(path)
            with TradingStore(path).connect() as conn:
                conn.execute(
                    "ALTER TABLE position_cycles DROP COLUMN trailing_stop_active_from"
                )
            with self.assertRaisesRegex(RuntimeError, "trailing_stop_active_from"):
                trading_backup.database_facts(path)

    def test_schema_v11_backup_rejects_missing_legacy_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            self.make_store(path)
            with TradingStore(path).connect() as conn:
                conn.execute("DROP TABLE orders")
            with self.assertRaisesRegex(RuntimeError, "orders"):
                trading_backup.database_facts(path)

    def test_schema_v11_backup_rejects_missing_legacy_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            self.make_store(path)
            with TradingStore(path).connect() as conn:
                conn.execute(
                    "ALTER TABLE daily_equity DROP COLUMN fee_data_status"
                )
            with self.assertRaisesRegex(RuntimeError, "fee_data_status"):
                trading_backup.database_facts(path)

    def test_schema_v11_backup_rejects_extra_legacy_unique_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading.db"
            self.make_store(path)
            with TradingStore(path).connect() as conn:
                conn.execute(
                    """CREATE UNIQUE INDEX forbidden_order_stock
                       ON orders(stock_code)"""
                )
            with self.assertRaisesRegex(RuntimeError, "unique"):
                trading_backup.database_facts(path)

    def test_schema_v7_backup_counts_current_execution_issue_state(self) -> None:
        self.assertIn("execution_issue_state", trading_backup.CORE_TABLES)

    def make_store(self, path: Path) -> TradingStore:
        store = TradingStore(path)
        store.initialize()
        with store.transaction() as conn:
            store.set_system_state(conn, "backup_probe", "ok", "test")
        return store

    def test_creates_verified_backup_and_manifest_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            db_file = project / "cache" / "trading" / "trading.db"
            self.make_store(db_file)

            result = create_backup(
                db_file,
                base / "backups",
                now=datetime(2026, 7, 14, 16, 30),
                project_root=project,
                keep_daily=7,
                keep_weekly=4,
                keep_monthly=12,
            )

            backup = Path(str(result["backup_file"]))
            manifest = Path(str(result["manifest_file"]))
            self.assertTrue(backup.exists())
            self.assertTrue(manifest.exists())
            self.assertEqual(TradingStore(backup).integrity_check(), "ok")
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(result["status"], "success")
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(saved["sha256"], result["sha256"])
            self.assertEqual(saved["table_counts"]["system_state"], 2)
            self.assertTrue({
                "orders", "fills", "account_snapshots", "position_snapshots",
                "daily_equity", "reconciliation_runs", "reconciliation_items", "control_events",
                "gap_reentry_opportunities",
            }.issubset(saved["table_counts"]))

    def test_rejects_backup_root_inside_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            db_file = project / "cache" / "trading.db"
            self.make_store(db_file)

            with self.assertRaisesRegex(ValueError, "outside project"):
                create_backup(
                    db_file,
                    project / "backups",
                    now=datetime(2026, 7, 14, 16, 30),
                    project_root=project,
                    keep_daily=7,
                    keep_weekly=4,
                    keep_monthly=12,
                )

    def test_retains_seven_daily_four_weekly_twelve_monthly_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            root = base / "backups"
            db_file = project / "cache" / "trading.db"
            self.make_store(db_file)

            for month in range(1, 13):
                create_backup(
                    db_file,
                    root,
                    now=datetime(2025, month, 15, 16, 30),
                    project_root=project,
                    keep_daily=7,
                    keep_weekly=4,
                    keep_monthly=12,
                )
            create_backup(
                db_file,
                root,
                now=datetime(2026, 1, 15, 16, 30),
                project_root=project,
                keep_daily=7,
                keep_weekly=4,
                keep_monthly=12,
            )

            self.assertEqual(len(validated_manifests(root, "daily")), 7)
            self.assertEqual(len(validated_manifests(root, "weekly")), 4)
            self.assertEqual(len(validated_manifests(root, "monthly")), 12)

    def test_same_day_replaces_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            root = base / "backups"
            db_file = project / "cache" / "trading.db"
            store = self.make_store(db_file)
            first = create_backup(
                db_file,
                root,
                now=datetime(2026, 7, 14, 16, 30),
                project_root=project,
                keep_daily=7,
                keep_weekly=4,
                keep_monthly=12,
            )
            with store.transaction() as conn:
                store.set_system_state(conn, "second_probe", "ok", "test")
            second = create_backup(
                db_file,
                root,
                now=datetime(2026, 7, 14, 17, 0),
                project_root=project,
                keep_daily=7,
                keep_weekly=4,
                keep_monthly=12,
            )

            daily = validated_manifests(root, "daily")
            self.assertEqual(len(daily), 1)
            self.assertEqual(daily[0]["table_counts"]["system_state"], 3)
            self.assertNotEqual(first["sha256"], second["sha256"])
            self.assertFalse(Path(str(first["backup_file"])).exists())

    def test_prune_preserves_invalid_or_unpaired_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            root = base / "backups"
            db_file = project / "cache" / "trading.db"
            self.make_store(db_file)
            create_backup(
                db_file,
                root,
                now=datetime(2026, 7, 14, 16, 30),
                project_root=project,
                keep_daily=7,
                keep_weekly=4,
                keep_monthly=12,
            )
            orphan = root / "daily" / "orphan.db"
            orphan.write_bytes(b"not-a-database")
            bad_db = root / "daily" / "bad.db"
            bad_db.write_bytes(b"bad")
            bad_manifest = root / "manifests" / "daily" / "bad.json"
            bad_manifest.write_text(json.dumps({
                "tier": "daily",
                "date_slot": "2026-07-13",
                "sha256": "0" * 64,
            }), encoding="utf-8")

            result = prune_tier(root, "daily", 1)

            self.assertTrue(orphan.exists())
            self.assertTrue(bad_db.exists())
            self.assertTrue(bad_manifest.exists())
            self.assertIn(orphan, result["invalid"])
            self.assertIn(bad_db, result["invalid"])

    def test_restore_drill_verifies_copy_without_modifying_live_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            root = base / "backups"
            output = project / "output"
            live_db = project / "cache" / "trading.db"
            self.make_store(live_db)
            create_backup(
                live_db,
                root,
                now=datetime(2026, 7, 4, 16, 30),
                project_root=project,
                keep_daily=7,
                keep_weekly=4,
                keep_monthly=12,
            )
            before = hashlib.sha256(live_db.read_bytes()).hexdigest()

            result = run_restore_drill(
                root,
                now=datetime(2026, 7, 5, 3, 30),
                report_dir=output,
            )

            after = hashlib.sha256(live_db.read_bytes()).hexdigest()
            self.assertEqual(result["status"], "success")
            self.assertEqual(before, after)
            self.assertFalse(any((root / "drill").glob("*.db")))
            self.assertTrue((output / "trading_backup_latest.md").exists())
            self.assertTrue((output / "trading_backup_drill_2026-Q3.md").exists())
            self.assertEqual(load_latest_status(root)["drill"]["status"], "success")

    def test_restore_drill_failure_preserves_last_success_and_cleans_temporary_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            root = base / "backups"
            output = project / "output"
            live_db = project / "cache" / "trading.db"
            self.make_store(live_db)
            create_backup(
                live_db,
                root,
                now=datetime(2026, 7, 4, 16, 30),
                project_root=project,
                keep_daily=7,
                keep_weekly=4,
                keep_monthly=12,
            )
            success = run_restore_drill(root, now=datetime(2026, 7, 5, 3, 30), report_dir=output)
            for tier in ("daily", "weekly", "monthly"):
                for backup in (root / tier).glob("*.db"):
                    backup.write_bytes(b"corrupted")

            failure = run_restore_drill(root, now=datetime(2026, 10, 4, 3, 30), report_dir=output)

            self.assertEqual(failure["status"], "failed")
            self.assertEqual(failure["stage"], "select_backup")
            self.assertFalse(any((root / "drill").glob("*.db")))
            status = load_latest_status(root)["drill"]
            self.assertEqual(status["last_success_at"], success["finished_at"])

    def test_latest_report_is_atomically_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "latest.md"
            write_latest_report(report, {
                "command": "backup", "status": "success", "stage": "complete",
                "finished_at": "2026-07-14 16:30:00", "schema_version": 5,
                "sha256": "a" * 64, "table_counts": {"signals": 2},
            })
            write_latest_report(report, {
                "command": "backup", "status": "failed", "stage": "verify",
                "finished_at": "2026-07-15 16:30:00", "error": "broken",
            })
            text = report.read_text(encoding="utf-8")
            self.assertIn("failed", text)
            self.assertIn("broken", text)
            self.assertFalse(report.with_suffix(".md.tmp").exists())

    def test_backup_failure_compatibility_send_never_writes_legacy_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = base / "notify_failed_queue.jsonl"
            with patch("notifier.requests.post") as post:
                sent = notify_failure(
                    {"command": "backup", "status": "failed", "stage": "verify", "error": "broken"},
                    webhook_url="https://example.invalid/webhook",
                    state_file=base / "state.json",
                    queue_file=queue,
                )

            self.assertFalse(sent)
            post.assert_not_called()
            self.assertFalse(queue.exists())
            self.assertFalse((base / "state.json").exists())

    def test_backup_issue_replay_and_recovery_use_transactional_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "trading.db"
            store = self.make_store(db_file)
            with store.transaction() as conn:
                scope = store.get_or_create_account_scope(
                    conn, "joinquant", "primary",
                )
            failed = {
                "command": "backup",
                "status": "failed",
                "stage": "verify",
                "error_code": "IntegrityError",
                "error": "secret path must stay local",
            }
            first_at = datetime(2026, 7, 15, 16, 30)
            self.assertEqual(
                trading_backup.persist_backup_issue_transition(
                    failed, db_file, first_at,
                ),
                "",
            )
            self.assertEqual(
                trading_backup.persist_backup_issue_transition(
                    failed, db_file, datetime(2026, 7, 15, 16, 31),
                ),
                "",
            )
            self.assertEqual(
                trading_backup.persist_backup_issue_transition(
                    {"command": "backup", "status": "success"},
                    db_file,
                    datetime(2026, 7, 15, 16, 32),
                ),
                "",
            )
            with store.connect() as conn:
                issue = conn.execute(
                    """SELECT recovered_at, transition_seq, details_json
                       FROM execution_issue_state
                       WHERE issue_key=?""",
                    (f"scope:{scope}:backup:backup",),
                ).fetchone()
                events = conn.execute(
                    """SELECT state, cancel_requested_at, payload_json
                       FROM notification_outbox
                       WHERE object_type='execution_issue'
                       ORDER BY created_at, event_key"""
                ).fetchall()
            self.assertIsNotNone(issue["recovered_at"])
            self.assertEqual(issue["transition_seq"], 2)
            self.assertEqual(len(events), 2)
            self.assertIsNotNone(events[0]["cancel_requested_at"])
            self.assertIsNone(events[1]["cancel_requested_at"])
            self.assertNotIn("secret path", "".join(str(row["payload_json"]) for row in events))

    def test_backup_issue_does_not_create_missing_database_or_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            missing = base / "missing.db"
            result = {
                "command": "drill", "status": "failed",
                "stage": "select_backup", "error_code": "ValueError",
            }
            self.assertTrue(trading_backup.persist_backup_issue_transition(
                result, missing, datetime(2026, 7, 15, 3, 30),
            ))
            self.assertFalse(missing.exists())

            db_file = base / "trading.db"
            store = self.make_store(db_file)
            self.assertEqual(
                trading_backup.persist_backup_issue_transition(
                    result, db_file, datetime(2026, 7, 15, 3, 30),
                ),
                "account scope is not registered",
            )
            with store.connect() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM account_scopes"
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM notification_outbox"
                ).fetchone()[0], 0)

    def test_cli_runs_backup_drill_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db_file = base / "live" / "trading.db"
            backup_root = base / "backups"
            output = base / "output"
            self.make_store(db_file)

            backup_code = main([
                "backup", "--db", str(db_file), "--backup-dir", str(backup_root),
                "--report-dir", str(output), "--now", "2026-07-14 16:30:00",
            ])
            drill_code = main([
                "drill", "--db", str(db_file),
                "--backup-dir", str(backup_root), "--report-dir", str(output),
                "--now", "2026-07-15 03:30:00",
            ])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status_code = main(["status", "--backup-dir", str(backup_root)])

            self.assertEqual(backup_code, 0)
            self.assertEqual(drill_code, 0)
            self.assertEqual(status_code, 0)
            self.assertIn('"status": "success"', stdout.getvalue())
            status = load_latest_status(backup_root)
            self.assertEqual(status["backup"]["status"], "success")
            self.assertEqual(status["drill"]["status"], "success")


if __name__ == "__main__":
    unittest.main()
