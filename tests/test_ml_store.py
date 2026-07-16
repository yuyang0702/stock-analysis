import json
import sqlite3
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ml_contracts import (
    CandidateSample,
    LabelRecord,
    ModelManifest,
    PredictionRecord,
    TimedFeature,
)
from ml_store import MlCapacityError, MlDataConflict, MlStore


class MlStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.sample = CandidateSample.from_values(
            source="strict_history",
            dataset_id="dataset-1",
            decision_at="2026-07-15T09:35:00+08:00",
            code="600000",
            strategy_version="strategy-v1",
            parameter_version="params-v1",
            feature_schema_version="features-v1",
            features={
                "price": TimedFeature(10.5, "2026-07-15T09:34:59+08:00"),
                "context": TimedFeature(
                    {"regime": "NORMAL", "levels": [1, 2]},
                    "2026-07-15T09:34:00+08:00",
                ),
            },
            selected=False,
            rejection_stage="score",
            rejection_code="below_min_score",
            final_action="score_rejected",
            universe_hash="universe-sha",
            market_data_version="market-v1",
            code_hash="code-sha",
            generator_hash="generator-sha",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_store(self) -> MlStore:
        store = MlStore(self.root / "cache" / "ml" / "ml.db")
        store.initialize()
        return store

    def manifest(self, model_id: str = "model-1", artifact: str = "artifact-sha") -> ModelManifest:
        return ModelManifest(
            model_id=model_id,
            parent_model_id=None,
            feature_names=("price", "context"),
            train_start="2025-01-01",
            train_end="2025-09-30",
            validation_start="2025-10-11",
            validation_end="2025-11-30",
            holdout_start="2025-12-01",
            holdout_end="2026-01-31",
            dataset_sha256="dataset-sha",
            code_sha256="code-sha",
            config_sha256="config-sha",
            artifact_sha256=artifact,
            parameter_version="params-v1",
            cost_version="cost-v1",
            dependency_versions={"python": "3.12.3"},
            metrics={"validation": {"rank_ic": 0.1}},
            created_at="2026-07-15T16:00:00+08:00",
        )

    def test_initializes_schema_pragmas_and_inactive_runtime(self) -> None:
        store = self.make_store()

        self.assertEqual(store.schema_version(), 1)
        with store.transaction() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertTrue(
            {
                "ml_candidate_samples",
                "ml_labels",
                "ml_predictions",
                "ml_models",
                "ml_model_events",
                "ml_runtime_state",
            }
            <= tables
        )
        self.assertEqual(
            store.runtime_state(),
            {
                "active_model_id": None,
                "permission_level": 0,
                "updated_at": store.runtime_state()["updated_at"],
            },
        )

    def test_candidate_write_is_idempotent_and_isolated_from_trading_db(self) -> None:
        trading = self.root / "cache" / "trading" / "trading.db"
        trading.parent.mkdir(parents=True)
        trading.write_bytes(b"ledger-sentinel")
        store = self.make_store()

        self.assertEqual(store.record_candidates([self.sample]), 1)
        self.assertEqual(store.record_candidates([self.sample]), 0)
        self.assertEqual(trading.read_bytes(), b"ledger-sentinel")
        with store.transaction() as conn:
            features = json.loads(
                conn.execute(
                    "SELECT features_json FROM ml_candidate_samples"
                ).fetchone()[0]
            )
        self.assertEqual(features["context"]["value"]["levels"], [1, 2])

    def test_conflicting_candidate_rolls_back_batch(self) -> None:
        store = self.make_store()
        changed = replace(self.sample, rejection_code="different")

        with self.assertRaises(MlDataConflict):
            store.record_candidates([self.sample, changed])

        self.assertEqual(store.counts()["ml_candidate_samples"], 0)

    def test_label_foreign_key_and_upsert(self) -> None:
        store = self.make_store()
        label = LabelRecord(
            sample_id=self.sample.sample_id,
            label_version="label-v1",
            label_source="historical",
            cost_version="cost-v1",
            fill_label=1,
            fill_price=10.55,
            ret_5d_net=0.03,
            market_data_sha256="market-sha",
            matured_at="2026-07-22T15:00:00+08:00",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            store.upsert_labels([label])
        store.record_candidates([self.sample])
        self.assertEqual(store.upsert_labels([label]), 1)
        self.assertEqual(store.upsert_labels([label]), 0)
        self.assertEqual(
            store.upsert_labels([replace(label, ret_5d_net=0.04)]), 1
        )

    def test_predictions_are_unique_and_immutable_per_sample_and_model(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        prediction = PredictionRecord(
            sample_id=self.sample.sample_id,
            model_id="model-1",
            created_at="2026-07-15T09:35:01+08:00",
            expected_ret_5d=0.02,
            ml_score=73.0,
            ml_filter=False,
        )

        self.assertEqual(store.record_predictions([prediction]), 1)
        self.assertEqual(store.record_predictions([prediction]), 0)
        with self.assertRaises(MlDataConflict):
            store.record_predictions([replace(prediction, ml_score=74.0)])

    def test_model_hash_is_immutable_and_events_require_registered_model(self) -> None:
        store = self.make_store()
        manifest = self.manifest()

        self.assertTrue(
            store.register_model(
                manifest, artifact_path="cache/ml/models/model-1/model.pkl"
            )
        )
        self.assertFalse(
            store.register_model(
                manifest, artifact_path="cache/ml/models/model-1/model.pkl"
            )
        )
        with self.assertRaises(MlDataConflict):
            store.register_model(
                replace(manifest, artifact_sha256="changed-sha"),
                artifact_path="cache/ml/models/model-1/model.pkl",
            )
        with self.assertRaises(MlDataConflict):
            store.record_model_event(
                event_id="event-wrong-hash",
                model_id="model-1",
                action="approve",
                old_level=0,
                new_level=0,
                artifact_sha256="wrong-sha",
                reason="test",
                operator="human",
                created_at="2026-07-15T16:01:00+08:00",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            store.record_model_event(
                event_id="event-missing",
                model_id="missing",
                action="approve",
                old_level=0,
                new_level=0,
                artifact_sha256="missing-sha",
                reason="test",
                operator="human",
                created_at="2026-07-15T16:01:00+08:00",
            )
        self.assertTrue(
            store.record_model_event(
                event_id="event-1",
                model_id="model-1",
                action="approve",
                old_level=0,
                new_level=0,
                artifact_sha256="artifact-sha",
                reason="historical gate passed",
                operator="human",
                created_at="2026-07-15T16:01:00+08:00",
            )
        )

    def test_runtime_compare_and_swap_rejects_stale_expected_state(self) -> None:
        store = self.make_store()
        store.register_model(
            self.manifest(), artifact_path="cache/ml/models/model-1/model.pkl"
        )

        self.assertTrue(
            store.compare_and_swap_runtime(
                expected_model_id=None,
                expected_permission_level=0,
                new_model_id="model-1",
                new_permission_level=0,
                updated_at="2026-07-15T16:02:00+08:00",
            )
        )
        self.assertFalse(
            store.compare_and_swap_runtime(
                expected_model_id=None,
                expected_permission_level=0,
                new_model_id=None,
                new_permission_level=0,
                updated_at="2026-07-15T16:03:00+08:00",
            )
        )
        self.assertEqual(store.runtime_state()["active_model_id"], "model-1")

    def test_transaction_waits_for_configured_busy_timeout(self) -> None:
        store = self.make_store()
        locker = sqlite3.connect(store.path, timeout=0)
        locker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with store.transaction():
                    pass
        finally:
            locker.rollback()
            locker.close()
        self.assertGreaterEqual(time.monotonic() - started, 4.5)

    def test_capacity_limit_refuses_new_detail(self) -> None:
        store = self.make_store()
        limited = MlStore(store.path, max_bytes=1)

        with self.assertRaises(MlCapacityError):
            limited.record_candidates([self.sample])

        self.assertEqual(store.counts()["ml_candidate_samples"], 0)

    def test_capacity_limit_rolls_back_write_that_crosses_limit(self) -> None:
        store = self.make_store()
        limited = MlStore(store.path, max_bytes=store.path.stat().st_size + 1)

        with self.assertRaises(MlCapacityError):
            limited.record_candidates([self.sample])

        self.assertEqual(store.counts()["ml_candidate_samples"], 0)

    def test_online_backup_restores_integrity_and_counts(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        backup = self.root / "backups" / "ml.db"

        store.backup_to(backup)

        restored = MlStore(backup)
        self.assertEqual(restored.integrity_check(), "ok")
        self.assertEqual(restored.schema_version(), 1)
        self.assertEqual(restored.counts(), store.counts())

    def test_all_persisted_timestamps_are_timezone_aware_iso(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        label = LabelRecord(
            sample_id=self.sample.sample_id,
            label_version="label-v1",
            label_source="historical",
            cost_version="cost-v1",
            market_data_sha256="market-sha",
            matured_at="2026-07-22T15:00:00+08:00",
        )
        store.upsert_labels([label])
        store.register_model(
            self.manifest(), artifact_path="cache/ml/models/model-1/model.pkl"
        )
        store.record_model_event(
            event_id="event-1",
            model_id="model-1",
            action="approve",
            old_level=0,
            new_level=0,
            artifact_sha256="artifact-sha",
            reason="test",
            operator="human",
            created_at="2026-07-15T16:01:00+08:00",
        )

        with store.transaction() as conn:
            timestamps = [
                conn.execute(
                    "SELECT created_at FROM ml_candidate_samples"
                ).fetchone()[0],
                conn.execute("SELECT matured_at FROM ml_labels").fetchone()[0],
                conn.execute("SELECT created_at FROM ml_models").fetchone()[0],
                conn.execute("SELECT created_at FROM ml_model_events").fetchone()[0],
                conn.execute(
                    "SELECT updated_at FROM ml_runtime_state"
                ).fetchone()[0],
            ]
        self.assertTrue(
            all(datetime.fromisoformat(value).utcoffset() is not None for value in timestamps)
        )


if __name__ == "__main__":
    unittest.main()
