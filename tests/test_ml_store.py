import json
import sqlite3
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ml_store
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

    @staticmethod
    def physical_size(path: Path) -> int:
        return sum(
            candidate.stat().st_size
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
            if candidate.exists()
        )

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
            self.assertEqual(conn.execute("PRAGMA cache_spill").fetchone()[0], 0)
        with store._connect_writable() as conn:
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

    def test_successful_write_is_immediately_visible_to_public_connect(self) -> None:
        store = self.make_store()

        self.assertEqual(store.record_candidates([self.sample]), 1)

        with store.connect() as conn:
            row = conn.execute(
                "SELECT sample_id FROM ml_candidate_samples"
            ).fetchone()
        self.assertEqual(row[0], self.sample.sample_id)

    def test_public_connect_is_immutable_and_never_creates_sidecars(self) -> None:
        store = self.make_store()
        before = store.runtime_state()
        max_bytes = store.path.stat().st_size
        limited = MlStore(store.path, max_bytes=max_bytes)
        sidecars = (Path(f"{store.path}-wal"), Path(f"{store.path}-shm"))
        self.assertFalse(any(path.exists() for path in sidecars))

        with limited.connect() as conn:
            conn.execute("PRAGMA query_only=OFF")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "UPDATE ml_runtime_state SET updated_at=? WHERE singleton=1",
                    ("2026-07-15T16:02:00+08:00",),
                )
            self.assertFalse(any(path.exists() for path in sidecars))

        self.assertFalse(any(path.exists() for path in sidecars))
        self.assertEqual(store.runtime_state(), before)

    def test_public_transaction_never_creates_sidecars_at_capacity(self) -> None:
        store = self.make_store()
        max_bytes = store.path.stat().st_size
        limited = MlStore(store.path, max_bytes=max_bytes)
        sidecars = (Path(f"{store.path}-wal"), Path(f"{store.path}-shm"))
        self.assertFalse(any(path.exists() for path in sidecars))

        with limited.transaction() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ml_runtime_state").fetchone()[0],
                1,
            )
            self.assertFalse(any(path.exists() for path in sidecars))

        self.assertFalse(any(path.exists() for path in sidecars))

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

    def test_label_rejects_prepare_protocol_before_sql_or_growth(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])

        class LargeBlob:
            calls = 0

            def __conform__(self, protocol: object) -> bytes | None:
                if protocol is sqlite3.PrepareProtocol:
                    self.calls += 1
                    return b"x" * 10_000_000
                return None

        payload = LargeBlob()
        label = LabelRecord(
            sample_id=self.sample.sample_id,
            label_version="label-v1",
            label_source="historical",
            cost_version="cost-v1",
            fill_price=payload,  # type: ignore[arg-type]
            market_data_sha256="market-sha",
        )
        before = self.physical_size(store.path)
        sql = []
        original_execute = ml_store._ClosingConnection.execute

        def recording_execute(
            conn: sqlite3.Connection, statement: str, parameters: tuple = ()
        ) -> sqlite3.Cursor:
            sql.append(statement)
            return original_execute(conn, statement, parameters)

        with patch.object(ml_store._ClosingConnection, "execute", recording_execute):
            with self.assertRaises(TypeError):
                store.upsert_labels([label])

        self.assertEqual(payload.calls, 0)
        self.assertFalse(sql)
        self.assertEqual(self.physical_size(store.path), before)
        self.assertEqual(store.counts()["ml_labels"], 0)

    def test_pending_label_round_trips_none_matured_at(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        label = LabelRecord(
            sample_id=self.sample.sample_id,
            label_version="label-v1",
            label_source="historical",
            cost_version="cost-v1",
            market_data_sha256="market-sha",
            matured_at=None,
        )

        self.assertEqual(store.upsert_labels([label]), 1)
        self.assertEqual(store.upsert_labels([label]), 0)
        with store.transaction() as conn:
            row = conn.execute(
                "SELECT matured_at FROM ml_labels WHERE sample_id=?",
                (self.sample.sample_id,),
            ).fetchone()
        self.assertIsNone(row[0])

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

    def test_prediction_rejects_prepare_protocol_before_sql_or_growth(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])

        class LargeBlob:
            calls = 0

            def __conform__(self, protocol: object) -> bytes | None:
                if protocol is sqlite3.PrepareProtocol:
                    self.calls += 1
                    return b"x" * 10_000_000
                return None

        payload = LargeBlob()
        prediction = PredictionRecord(
            sample_id=self.sample.sample_id,
            model_id="model-1",
            created_at="2026-07-15T09:35:01+08:00",
            expected_ret_5d=payload,  # type: ignore[arg-type]
        )
        before = self.physical_size(store.path)
        sql = []
        original_execute = ml_store._ClosingConnection.execute

        def recording_execute(
            conn: sqlite3.Connection, statement: str, parameters: tuple = ()
        ) -> sqlite3.Cursor:
            sql.append(statement)
            return original_execute(conn, statement, parameters)

        with patch.object(ml_store._ClosingConnection, "execute", recording_execute):
            with self.assertRaises(TypeError):
                store.record_predictions([prediction])

        self.assertEqual(payload.calls, 0)
        self.assertFalse(sql)
        self.assertEqual(self.physical_size(store.path), before)
        self.assertEqual(store.counts()["ml_predictions"], 0)

    def test_write_rejects_non_finite_float_before_sql(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        prediction = PredictionRecord(
            sample_id=self.sample.sample_id,
            model_id="model-1",
            created_at="2026-07-15T09:35:01+08:00",
            expected_ret_5d=float("inf"),
        )
        sql = []
        original_execute = ml_store._ClosingConnection.execute

        def recording_execute(
            conn: sqlite3.Connection, statement: str, parameters: tuple = ()
        ) -> sqlite3.Cursor:
            sql.append(statement)
            return original_execute(conn, statement, parameters)

        with patch.object(ml_store._ClosingConnection, "execute", recording_execute):
            with self.assertRaises(ValueError):
                store.record_predictions([prediction])

        self.assertFalse(sql)
        self.assertEqual(store.counts()["ml_predictions"], 0)

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
                store.compare_and_swap_runtime(
                    expected_model_id=None,
                    expected_permission_level=0,
                    new_model_id=None,
                    new_permission_level=1,
                    updated_at="2026-07-15T16:02:00+08:00",
                )
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

    def test_capacity_limit_rolls_back_large_commit_growth(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        max_bytes = store.path.stat().st_size + 150_000
        limited = MlStore(store.path, max_bytes=max_bytes)
        large = replace(
            self.sample,
            sample_id="",
            code="600001",
            features={
                "payload": TimedFeature(
                    "x" * 120_000, "2026-07-15T09:34:59+08:00"
                )
            },
        )
        self.assertLess(
            sum(
                path.stat().st_size
                for path in (
                    store.path,
                    Path(f"{store.path}-wal"),
                    Path(f"{store.path}-shm"),
                )
                if path.exists()
            ),
            max_bytes,
        )

        with self.assertRaises(MlCapacityError):
            limited.record_candidates([large])

        self.assertEqual(store.counts()["ml_candidate_samples"], 1)
        with store.connect() as conn:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        self.assertLessEqual(page_size * page_count, max_bytes)
        self.assertLessEqual(
            sum(
                path.stat().st_size
                for path in (
                    store.path,
                    Path(f"{store.path}-wal"),
                    Path(f"{store.path}-shm"),
                )
                if path.exists()
            ),
            max_bytes,
        )

    def test_capacity_limit_never_spills_large_payload_past_limit(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        max_bytes = store.path.stat().st_size + 3_000_000

        class TinyCacheMlStore(MlStore):
            def _connect_writable(self) -> sqlite3.Connection:
                conn = super()._connect_writable()
                conn.execute("PRAGMA cache_size=1")
                if conn.execute("PRAGMA cache_spill").fetchone()[0] != 0:
                    conn.execute("PRAGMA cache_spill=1")
                return conn

        observed_sizes = []
        candidate_inserts = []
        original_execute = ml_store._ClosingConnection.execute

        def observed_execute(
            conn: sqlite3.Connection, sql: str, parameters: tuple = ()
        ) -> sqlite3.Cursor:
            cursor = original_execute(conn, sql, parameters)
            if "INSERT" in sql and "ml_candidate_samples" in sql:
                candidate_inserts.append(sql)
            observed_sizes.append(
                sum(
                    path.stat().st_size
                    for path in (
                        store.path,
                        Path(f"{store.path}-wal"),
                        Path(f"{store.path}-shm"),
                    )
                    if path.exists()
                )
            )
            return cursor

        large = replace(
            self.sample,
            sample_id="",
            code="600001",
            features={
                "payload": TimedFeature(
                    "x" * 120_000, "2026-07-15T09:34:59+08:00"
                )
            },
        )
        changed = replace(large, rejection_code="different")
        limited = TinyCacheMlStore(store.path, max_bytes=max_bytes)
        with patch.object(ml_store._ClosingConnection, "execute", observed_execute):
            with self.assertRaises(MlDataConflict):
                limited.record_candidates([large, changed])

        self.assertTrue(observed_sizes)
        self.assertTrue(candidate_inserts)
        self.assertLessEqual(max(observed_sizes), max_bytes)
        self.assertEqual(store.counts()["ml_candidate_samples"], 1)

    def test_record_candidates_rejects_large_payload_before_dml(self) -> None:
        store = self.make_store()
        max_bytes = store.path.stat().st_size + 100_000
        large = replace(
            self.sample,
            features={
                "payload": TimedFeature(
                    "x" * 300_000, "2026-07-15T09:34:59+08:00"
                )
            },
        )
        candidate_inserts = []
        original_execute = ml_store._ClosingConnection.execute

        def recording_execute(
            conn: sqlite3.Connection, sql: str, parameters: tuple = ()
        ) -> sqlite3.Cursor:
            if "INSERT OR IGNORE INTO ml_candidate_samples" in sql:
                candidate_inserts.append(sql)
            return original_execute(conn, sql, parameters)

        limited = MlStore(store.path, max_bytes=max_bytes)
        with patch.object(ml_store._ClosingConnection, "execute", recording_execute):
            with self.assertRaises(MlCapacityError):
                limited.record_candidates([large])

        self.assertFalse(candidate_inserts)
        self.assertEqual(store.counts()["ml_candidate_samples"], 0)

    def test_large_database_runtime_cas_succeeds_with_100kb_headroom(self) -> None:
        store = self.make_store()
        large = replace(
            self.sample,
            features={
                "payload": TimedFeature(
                    "x" * 300_000, "2026-07-15T09:34:59+08:00"
                )
            },
        )
        store.record_candidates([large])
        keeper = store._connect_writable()
        try:
            keeper.execute("BEGIN IMMEDIATE")
            keeper.execute(
                "UPDATE ml_runtime_state SET updated_at=? WHERE singleton=1",
                ("2026-07-15T16:01:00+08:00",),
            )
            keeper.commit()
            physical_bytes = sum(
                path.stat().st_size
                for path in (
                    store.path,
                    Path(f"{store.path}-wal"),
                    Path(f"{store.path}-shm"),
                )
                if path.exists()
            )
            max_bytes = physical_bytes + 100_000
            self.assertGreater(Path(f"{store.path}-wal").stat().st_size, 32)
            self.assertGreater(store.path.stat().st_size, max_bytes // 2)

            limited = MlStore(store.path, max_bytes=max_bytes)
            self.assertTrue(
                limited.compare_and_swap_runtime(
                    expected_model_id=None,
                    expected_permission_level=0,
                    new_model_id=None,
                    new_permission_level=1,
                    updated_at="2026-07-15T16:02:00+08:00",
                )
            )
        finally:
            keeper.close()

    def test_capacity_reserves_main_growth_while_checkpoint_keeps_wal(self) -> None:
        store = self.make_store()
        keeper = store._connect_writable()
        candidate_inserts = []
        original_execute = ml_store._ClosingConnection.execute

        def recording_execute(
            conn: sqlite3.Connection, sql: str, parameters: tuple = ()
        ) -> sqlite3.Cursor:
            if "INSERT OR IGNORE INTO ml_candidate_samples" in sql:
                candidate_inserts.append(sql)
            return original_execute(conn, sql, parameters)

        try:
            keeper.execute("BEGIN IMMEDIATE")
            keeper.execute(
                "UPDATE ml_runtime_state SET updated_at=? WHERE singleton=1",
                ("2026-07-15T16:01:00+08:00",),
            )
            keeper.commit()
            self.assertGreater(Path(f"{store.path}-wal").stat().st_size, 32)

            limited = MlStore(store.path, max_bytes=600_000)
            with patch.object(ml_store._ClosingConnection, "execute", recording_execute):
                with self.assertRaises(MlCapacityError):
                    limited.record_candidates([self.sample])
        finally:
            keeper.close()

        self.assertFalse(candidate_inserts)
        self.assertEqual(store.counts()["ml_candidate_samples"], 0)

    def test_transaction_is_read_only_even_if_query_only_is_disabled(self) -> None:
        store = self.make_store()
        before = store.runtime_state()

        with self.assertRaises(sqlite3.OperationalError):
            with store.transaction() as conn:
                conn.execute("PRAGMA query_only=OFF")
                conn.execute(
                    """UPDATE ml_runtime_state
                       SET updated_at=zeroblob(80000) WHERE singleton=1"""
                )
        with self.assertRaises(TypeError):
            store.transaction(reserve_bytes=1)

        self.assertEqual(store.runtime_state(), before)

    def test_capacity_limit_rejects_runtime_cas_without_changing_state(self) -> None:
        store = self.make_store()
        limited = MlStore(store.path, max_bytes=1)
        before = store.runtime_state()

        with self.assertRaises(MlCapacityError):
            limited.compare_and_swap_runtime(
                expected_model_id=None,
                expected_permission_level=0,
                new_model_id=None,
                new_permission_level=1,
                updated_at="2026-07-15T16:02:00+08:00",
            )

        self.assertEqual(store.runtime_state(), before)

    def test_capacity_limit_rejects_initialize_without_partial_schema(self) -> None:
        path = self.root / "limited" / "ml.db"
        store = MlStore(path, max_bytes=1)

        with self.assertRaises(MlCapacityError):
            store.initialize()

        self.assertFalse(path.exists())
        self.assertFalse(Path(f"{path}-wal").exists())
        self.assertFalse(Path(f"{path}-shm").exists())

    def test_initialize_rolls_back_schema_version_and_runtime_on_ddl_error(self) -> None:
        path = self.root / "broken" / "ml.db"
        store = MlStore(path)
        broken_schema = """
        CREATE TABLE schema_migrations(
          version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE partial_schema(value TEXT);
        CREATE TABLE broken_schema(;
        """

        with patch.object(ml_store, "SCHEMA", broken_schema):
            with self.assertRaises(sqlite3.OperationalError):
                store.initialize()

        with closing(sqlite3.connect(path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("schema_migrations", tables)
        self.assertNotIn("partial_schema", tables)

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
