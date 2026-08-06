import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import ml_maintenance
from historical_data import HISTORY_SCHEMA_VERSION, HistoricalStore
from ml_store import SCHEMA_VERSION as ML_SCHEMA_VERSION, MlStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


class MlMaintenanceTest(unittest.TestCase):
    def test_label_job_can_scan_more_than_twenty_thousand_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history_db = root / "history.db"
            ml_db = root / "ml.db"
            HistoricalStore(history_db).initialize()
            calls = 0

            def fake_build_labels(*args, **kwargs):
                nonlocal calls
                calls += 1
                return SimpleNamespace(
                    candidate_count=1000,
                    filled_count=1000,
                    no_fill_count=0,
                    pending_count=0,
                    changed_count=1000,
                    next_cursor=(f"cursor-{calls}" if calls < 25 else None),
                )

            with patch.object(
                ml_maintenance, "build_labels", side_effect=fake_build_labels
            ):
                result = ml_maintenance.run_label_job(
                    history_db=history_db,
                    ml_db=ml_db,
                    dataset_id="strict-2025",
                    as_of="2026-08-06T16:10:00+08:00",
                    page_size=1000,
                    max_pages=100,
                )

            self.assertEqual(result["status"], "success")
            self.assertFalse(result["truncated"])
            self.assertEqual(result["candidate_count"], 25_000)

    def test_ml_backup_is_online_verified_and_retained_by_7_4_12_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            db_file = project / "cache" / "ml" / "ml.db"
            backup_root = root / "backups" / "ml"
            store = MlStore(db_file, max_bytes=20_000_000)
            store.initialize()
            first_at = datetime(2026, 8, 5, 16, 40, tzinfo=SHANGHAI)

            first = ml_maintenance.create_database_backup(
                "ml",
                db_file,
                backup_root,
                now=first_at,
                project_root=project,
                expected_schema=ML_SCHEMA_VERSION,
                keep_daily=1,
                keep_weekly=1,
                keep_monthly=1,
            )
            second = ml_maintenance.create_database_backup(
                "ml",
                db_file,
                backup_root,
                now=first_at + timedelta(days=1),
                project_root=project,
                expected_schema=ML_SCHEMA_VERSION,
                keep_daily=1,
                keep_weekly=1,
                keep_monthly=1,
            )

            self.assertEqual(first["integrity_check"], "ok")
            self.assertEqual(second["restore_check"]["status"], "success")
            self.assertEqual(second["sha256"], second["restore_check"]["sha256"])
            self.assertEqual(len(ml_maintenance.validated_manifests(backup_root, tier="daily")), 1)
            self.assertLessEqual(len(ml_maintenance.validated_manifests(backup_root, tier="weekly")), 1)
            self.assertLessEqual(len(ml_maintenance.validated_manifests(backup_root, tier="monthly")), 1)

    def test_history_backup_uses_independent_root_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            db_file = project / "cache" / "backtest" / "history.db"
            backup_root = root / "backups" / "history"
            HistoricalStore(db_file).initialize()

            result = ml_maintenance.create_database_backup(
                "history",
                db_file,
                backup_root,
                now=datetime(2026, 8, 6, 16, 40, tzinfo=SHANGHAI),
                project_root=project,
                expected_schema=HISTORY_SCHEMA_VERSION,
            )

            self.assertEqual(result["schema_version"], HISTORY_SCHEMA_VERSION)
            self.assertEqual(result["restore_check"]["integrity_check"], "ok")
            self.assertTrue(str(result["backup_files"]["daily"]).endswith(".db"))

    def test_backup_manifest_kind_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            db_file = project / "cache" / "ml" / "ml.db"
            backup_root = root / "backups" / "ml"
            MlStore(db_file, max_bytes=20_000_000).initialize()
            ml_maintenance.create_database_backup(
                "ml",
                db_file,
                backup_root,
                now=datetime(2026, 8, 6, 19, 0, tzinfo=SHANGHAI),
                project_root=project,
                expected_schema=ML_SCHEMA_VERSION,
            )
            manifest_files = list(
                (backup_root / "manifests").glob("*/*.json")
            )
            for manifest_file in manifest_files:
                payload = json.loads(manifest_file.read_text(encoding="utf-8"))
                payload["kind"] = "trading"
                manifest_file.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(ml_maintenance.validated_manifests(backup_root), [])

    def test_retention_apply_fails_without_recent_verified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(PermissionError, "restore check"):
                ml_maintenance.apply_retention(
                    Path(tmp),
                    now=datetime(2026, 8, 6, 17, 0, tzinfo=SHANGHAI),
                )

    def test_retention_dry_run_never_deletes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid = root / "daily" / "orphan.db"
            invalid.parent.mkdir(parents=True)
            invalid.write_bytes(b"not sqlite")

            plan = ml_maintenance.retention_plan(root)

            self.assertTrue(invalid.exists())
            self.assertIn(str(invalid), plan["invalid"])

    def test_model_status_is_bounded_and_does_not_change_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MlStore(Path(tmp) / "ml.db", max_bytes=20_000_000)
            store.initialize()
            before = store.runtime_state()

            status = ml_maintenance.model_status(store)

            self.assertEqual(status["schema_version"], ML_SCHEMA_VERSION)
            self.assertEqual(status["integrity_check"], "ok")
            self.assertEqual(status["runtime"], before)
            self.assertEqual(store.runtime_state(), before)
            self.assertIn("ml_candidate_samples", status["counts"])

    def test_automation_module_exposes_no_approval_or_activation_command(self) -> None:
        text = Path(ml_maintenance.__file__).read_text(encoding="utf-8")
        self.assertNotIn('add_parser("approve")', text)
        self.assertNotIn('add_parser("activate")', text)
        self.assertNotIn("compare_and_swap_runtime(", text)
        self.assertIn('"permission_changed": False', text)

    def test_unconfigured_automated_label_and_train_jobs_are_safe_noops(self) -> None:
        self.assertEqual(
            ml_maintenance.main(["labels", "--allow-unconfigured"]),
            0,
        )
        self.assertEqual(
            ml_maintenance.main(["train", "--allow-unconfigured"]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
