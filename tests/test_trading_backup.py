import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

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
            self.assertEqual(saved["table_counts"]["system_state"], 1)

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
            self.assertEqual(daily[0]["table_counts"]["system_state"], 2)
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

    def test_backup_failure_notification_uses_existing_retry_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = base / "notify_failed_queue.jsonl"
            with patch("notifier.requests.post", side_effect=requests.RequestException("offline")):
                sent = notify_failure(
                    {"command": "backup", "status": "failed", "stage": "verify", "error": "broken"},
                    webhook_url="https://example.invalid/webhook",
                    state_file=base / "state.json",
                    queue_file=queue,
                )

            self.assertFalse(sent)
            rows = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["dedupe_key"], "trading-backup:backup:verify")

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
                "drill", "--backup-dir", str(backup_root), "--report-dir", str(output),
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
