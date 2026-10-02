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

import joblib

import ml_runtime
import ml_store
from ml_admin import MlAdmin, PermissionEvidence
from ml_train import ModelBundleManifest
from ml_contracts import (
    CandidateSample,
    DownsideLabel,
    HorizonLabel,
    LabelRecord,
    ModelManifest,
    PredictionRecord,
    TimedFeature,
    canonical_hash,
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

    def test_sqlite_full_detection_supports_python310_error_text(self) -> None:
        self.assertTrue(
            ml_store._is_sqlite_full(sqlite3.OperationalError("database or disk is full"))
        )
        self.assertFalse(ml_store._is_sqlite_full(sqlite3.OperationalError("database is locked")))

    def create_v1_store(self, *, with_label: bool = True) -> Path:
        path = self.root / "legacy" / "ml.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in ml_store._schema_statements(ml_store.SCHEMA_V1):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(1,?)",
                ("2026-07-15T16:00:00+08:00",),
            )
            conn.execute(
                """INSERT INTO ml_runtime_state(
                   singleton,active_model_id,permission_level,updated_at)
                   VALUES(1,NULL,0,?)""",
                ("2026-07-15T16:00:00+08:00",),
            )
            conn.execute(
                """INSERT INTO ml_candidate_samples(
                   sample_id,source,dataset_id,trade_date,decision_at,code,
                   strategy_version,parameter_version,feature_schema_version,
                   features_json,selected,rejection_stage,rejection_code,final_action,
                   universe_hash,market_data_version,code_hash,generator_hash,
                   content_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.sample.sample_id,
                    self.sample.source,
                    self.sample.dataset_id,
                    self.sample.trade_date,
                    self.sample.decision_at,
                    self.sample.code,
                    self.sample.strategy_version,
                    self.sample.parameter_version,
                    self.sample.feature_schema_version,
                    ml_store._canonical_json(self.sample.features),
                    int(self.sample.selected),
                    self.sample.rejection_stage,
                    self.sample.rejection_code,
                    self.sample.final_action,
                    self.sample.universe_hash,
                    self.sample.market_data_version,
                    self.sample.code_hash,
                    self.sample.generator_hash,
                    canonical_hash(self.sample),
                    "2026-07-15T16:00:00+08:00",
                ),
            )
            if with_label:
                conn.execute(
                    """INSERT INTO ml_labels(
                       sample_id,label_version,label_source,cost_version,
                       fill_label,fill_delay_sec,fill_price,
                       ret_3d_net,ret_5d_net,ret_10d_net,mfe_10d,mae_10d,
                       hit_stop,hit_take,actual_net_pnl,market_data_sha256,matured_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.sample.sample_id,
                        "label-v1",
                        "historical",
                        "cost-v1",
                        1,
                        60.0,
                        10.0,
                        0.01,
                        0.02,
                        0.03,
                        0.08,
                        -0.04,
                        1,
                        0,
                        20.0,
                        "legacy-market",
                        "2026-07-25T15:00:00+08:00",
                    ),
                )
            conn.commit()
        return path

    def horizon(
        self,
        days: int,
        *,
        gross_return: float | None = None,
        net_return: float | None = None,
        evidence: str | None = None,
    ) -> HorizonLabel:
        gross = gross_return if gross_return is not None else days / 100.0
        net = net_return if net_return is not None else gross - 0.0122
        return HorizonLabel(
            horizon_days=days,
            gross_return=gross,
            net_return=net,
            exit_price=10.0 * (1.0 + gross),
            buy_commission_yuan=5.0,
            buy_transfer_fee_yuan=0.1,
            buy_other_fee_yuan=0.2,
            buy_slippage_yuan=0.3,
            sell_commission_yuan=5.0,
            sell_stamp_tax_yuan=1.0,
            sell_transfer_fee_yuan=0.1,
            sell_other_fee_yuan=0.2,
            sell_slippage_yuan=0.3,
            total_cost_yuan=12.2,
            cost_rate=0.0122,
            matured_at=f"2026-07-{15 + days:02d}T15:00:00+08:00",
            market_data_sha256=evidence or f"market-d{days}",
        )

    def downside(self) -> DownsideLabel:
        return DownsideLabel(
            status="complete",
            mfe_10d_net=0.12,
            mae_10d_net=-0.05,
            downside_loss=0.05,
            paused_path=1,
            exit_blocked=1,
            hit_stop=1,
            hit_take=0,
            failure_reason="",
            matured_at="2026-07-25T15:00:00+08:00",
            evidence_sha256="downside-evidence",
        )

    def v2_label(
        self,
        *,
        sample: CandidateSample | None = None,
        label_source: str = "strict_counterfactual_v2",
        cost_sha256: str = "cost-sha-1",
        policy_sha256: str = "policy-sha-1",
        horizons: tuple[HorizonLabel, ...] = (),
        downside: DownsideLabel | None = None,
        fill_matured_at: str | None = "2026-07-15T10:05:00+08:00",
    ) -> LabelRecord:
        sample = sample or self.sample
        return LabelRecord(
            sample_id=sample.sample_id,
            label_version="label-v2",
            label_source=label_source,
            cost_version="cost-v2",
            cost_sha256=cost_sha256,
            policy_version="policy-v2",
            policy_sha256=policy_sha256,
            candidate_source=sample.source,
            dataset_id=sample.dataset_id,
            trade_date=sample.trade_date,
            decision_at=sample.decision_at,
            code=sample.code,
            candidate_content_sha256=canonical_hash(sample),
            candidate_origin="history" if sample.source.startswith("strict") else "ml",
            fill_label=1 if fill_matured_at is not None else None,
            fill_status="filled" if fill_matured_at is not None else "pending",
            fill_evidence_sha256=(
                "fill-evidence" if fill_matured_at is not None else ""
            ),
            fill_delay_sec=60.0 if fill_matured_at is not None else None,
            fill_price=10.0 if fill_matured_at is not None else None,
            fill_at=(
                "2026-07-15T09:36:00+08:00"
                if fill_matured_at is not None
                else None
            ),
            fill_matured_at=fill_matured_at,
            reference_qty=100,
            reference_notional_yuan=10_000.0,
            reference_trade_notional_yuan=(
                1_000.0 if fill_matured_at is not None else None
            ),
            quality_status="complete" if downside is not None else "partial",
            market_data_sha256="market-all",
            horizons=horizons,
            downside=downside,
        )

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

    def bundle_manifest(self, model_id: str = "model-bundle-1") -> ModelBundleManifest:
        return ModelBundleManifest(
            model_id=model_id,
            parent_model_id=None,
            generated_by="stock-analysis",
            manifest_schema_version="ml-bundle-v1",
            model_strategy_version="five-head-v1",
            status="approvable_l0",
            permission_level=0,
            required_features=("price",),
            numeric_features=("price",),
            categorical_features=(),
            dropped_constant_features=(),
            train_start="2025-01-01",
            train_end="2025-09-30",
            validation_start="2025-10-01",
            validation_end="2025-11-30",
            holdout_start="2025-12-01",
            holdout_end="2026-01-31",
            dataset_sha256="dataset-sha",
            strict_provenance_sha256="strict-sha",
            code_sha256="code-sha",
            feature_sha256="feature-sha",
            label_sha256="label-sha",
            label_policy_sha256="policy-sha",
            cost_sha256="cost-sha",
            split_sha256="split-sha",
            config_sha256="config-sha",
            dependency_sha256="dependency-sha",
            dependency_versions={"python": "3.13"},
            strategy_version="strategy-v1",
            parameter_version="params-v1",
            feature_schema_version="features-v1",
            label_version="label-v2",
            policy_version="policy-v2",
            cost_version="cost-v2",
            selected_parameters={"max_iter": 80},
            search_inputs_hash="search-sha",
            search_indices=(0, 1),
            holdout_indices=(2,),
            holdout_evaluated_after_freeze=True,
            oof_references={"ret_5d": (0.01, 0.02)},
            residual_q80={"ret_5d": 0.03},
            d5_difference_p90=0.04,
            downside_oof_prediction_p80=0.05,
            drift_reference={},
            psi_pseudocount=0.5,
            metrics={"rank_ic": 0.1},
            holdout_metrics={"rank_ic": 0.08},
            failed_gates=(),
            created_at="2026-08-06T10:00:00+08:00",
        )

    def test_initializes_schema_pragmas_and_inactive_runtime(self) -> None:
        store = self.make_store()

        self.assertEqual(store.schema_version(), ml_store.SCHEMA_VERSION)
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
                "ml_label_subjects",
                "ml_labels",
                "ml_label_horizons",
                "ml_label_downside",
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

    def test_public_transaction_always_uses_normal_readonly_uri(self) -> None:
        store = self.make_store()

        with patch.object(ml_store.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            with store.transaction() as conn:
                self.assertEqual(
                    conn.execute("SELECT permission_level FROM ml_runtime_state").fetchone()[0],
                    0,
                )

        uri = str(connect.call_args_list[0].args[0])
        self.assertIn("mode=ro", uri)
        self.assertNotIn("immutable", uri)

    def test_shm_is_excluded_from_quota_and_read_connection_stays_read_only(self) -> None:
        store = self.make_store()
        limited = MlStore(store.path, max_bytes=store.path.stat().st_size + 80_000)
        shm = Path(f"{store.path}-shm")

        with limited.transaction() as conn:
            conn.execute("PRAGMA query_only=OFF")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "UPDATE ml_runtime_state SET permission_level=1 WHERE singleton=1"
                )
            self.assertEqual(shm.stat().st_size, 32_768)

        self.assertTrue(
            limited.compare_and_swap_runtime(
                expected_model_id=None,
                expected_permission_level=0,
                new_model_id=None,
                new_permission_level=1,
                updated_at="2026-07-15T16:02:00+08:00",
            )
        )
        self.assertEqual(limited.runtime_state()["permission_level"], 1)

    def test_repeated_initialize_validates_current_schema_without_full_reserve(self) -> None:
        store = self.make_store()
        limited = MlStore(store.path, max_bytes=store.path.stat().st_size)

        limited.initialize()

        self.assertEqual(limited.schema_version(), ml_store.SCHEMA_VERSION)
        self.assertEqual(limited.counts()["ml_runtime_state"], 1)

    def test_initialize_fails_closed_when_required_index_is_missing(self) -> None:
        store = self.make_store()
        with store._connect_writable() as conn:
            conn.execute("DROP INDEX idx_ml_candidates_date_code")
            conn.commit()

        with self.assertRaisesRegex(RuntimeError, "schema"):
            store.initialize()

        with store.transaction() as conn:
            self.assertIsNone(
                conn.execute(
                    """SELECT sql FROM sqlite_master
                       WHERE type='index' AND name='idx_ml_candidates_date_code'"""
                ).fetchone()
            )

    def test_initialize_fails_closed_for_same_version_fake_core_schema(self) -> None:
        path = self.root / "fake" / "ml.db"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE schema_migrations(
                  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations VALUES(1, '2026-07-15T16:00:00+08:00');
                CREATE TABLE ml_candidate_samples(
                  sample_id TEXT PRIMARY KEY, trade_date TEXT, code TEXT, decision_at TEXT
                );
                CREATE INDEX idx_ml_candidates_date_code
                ON ml_candidate_samples(trade_date, code, decision_at);
                CREATE TABLE ml_labels(sample_id TEXT PRIMARY KEY);
                CREATE TABLE ml_predictions(sample_id TEXT, model_id TEXT);
                CREATE TABLE ml_models(model_id TEXT PRIMARY KEY);
                CREATE TABLE ml_model_events(event_id TEXT PRIMARY KEY);
                CREATE TABLE ml_runtime_state(
                  singleton INTEGER PRIMARY KEY, active_model_id TEXT,
                  permission_level INTEGER, updated_at TEXT
                );
                INSERT INTO ml_runtime_state
                VALUES(1, NULL, 0, '2026-07-15T16:00:00+08:00');
                """
            )

        with self.assertRaisesRegex(RuntimeError, "schema"):
            MlStore(path).initialize()

        with closing(sqlite3.connect(path)) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(ml_candidate_samples)")
            }
        self.assertEqual(columns, {"sample_id", "trade_date", "code", "decision_at"})

    def test_candidate_write_is_idempotent_and_isolated_from_trading_db(self) -> None:
        trading = self.root / "cache" / "trading" / "trading.db"
        trading.parent.mkdir(parents=True)
        trading.write_bytes(b"ledger-sentinel")
        store = self.make_store()

        self.assertEqual(store.record_candidates([self.sample]), 1)
        self.assertEqual(store.record_candidates([self.sample]), 0)
        self.assertEqual(trading.read_bytes(), b"ledger-sentinel")
        with store.transaction() as conn:
            row = conn.execute(
                """SELECT features_json, final_action, universe_hash,
                          market_data_version, code_hash, generator_hash
                   FROM ml_candidate_samples"""
            ).fetchone()
            features = json.loads(row["features_json"])
        self.assertEqual(features["context"]["value"]["levels"], [1, 2])
        self.assertEqual(row["final_action"], self.sample.final_action)
        self.assertEqual(row["universe_hash"], self.sample.universe_hash)
        self.assertEqual(row["market_data_version"], self.sample.market_data_version)
        self.assertEqual(row["code_hash"], self.sample.code_hash)
        self.assertEqual(row["generator_hash"], self.sample.generator_hash)

    def test_candidate_schema_has_recoverable_provenance_columns(self) -> None:
        store = self.make_store()
        with store.transaction() as conn:
            info = {row[1]: row for row in conn.execute("PRAGMA table_info(ml_candidate_samples)")}
        for name in (
            "final_action", "universe_hash", "market_data_version", "code_hash", "generator_hash",
        ):
            self.assertIn(name, info)
            self.assertEqual(info[name][3], 1)

    def test_successful_write_is_immediately_visible_to_public_transaction(self) -> None:
        store = self.make_store()

        self.assertEqual(store.record_candidates([self.sample]), 1)

        with store.transaction() as conn:
            row = conn.execute(
                "SELECT sample_id FROM ml_candidate_samples"
            ).fetchone()
        self.assertEqual(row[0], self.sample.sample_id)

    def test_normal_readonly_connection_can_create_shm_at_data_capacity(self) -> None:
        store = self.make_store()
        max_bytes = store.path.stat().st_size
        limited = MlStore(store.path, max_bytes=max_bytes)
        wal = Path(f"{store.path}-wal")
        shm = Path(f"{store.path}-shm")

        with limited.transaction() as conn:
            conn.execute("PRAGMA query_only=OFF")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "UPDATE ml_runtime_state SET updated_at=? WHERE singleton=1",
                    ("2026-07-15T16:02:00+08:00",),
                )
            self.assertEqual(shm.stat().st_size, 32_768)

        self.assertEqual(wal.stat().st_size if wal.exists() else 0, 0)
        self.assertEqual(store.runtime_state()["permission_level"], 0)

    def test_open_read_transaction_keeps_its_sqlite_snapshot(self) -> None:
        store = self.make_store()
        with store.transaction() as reader:
            self.assertEqual(
                reader.execute(
                    "SELECT permission_level FROM ml_runtime_state"
                ).fetchone()[0],
                0,
            )
            self.assertTrue(
                store.compare_and_swap_runtime(
                    expected_model_id=None,
                    expected_permission_level=0,
                    new_model_id=None,
                    new_permission_level=1,
                    updated_at="2026-07-15T16:02:00+08:00",
                )
            )
            self.assertEqual(
                reader.execute(
                    "SELECT permission_level FROM ml_runtime_state"
                ).fetchone()[0],
                0,
            )

        self.assertEqual(store.runtime_state()["permission_level"], 1)

    def test_conflicting_candidate_rolls_back_batch(self) -> None:
        store = self.make_store()
        changed = replace(self.sample, rejection_code="different")

        with self.assertRaises(MlDataConflict):
            store.record_candidates([self.sample, changed])

        self.assertEqual(store.counts()["ml_candidate_samples"], 0)

    def test_changed_final_action_is_an_immutable_candidate_conflict(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])

        with self.assertRaises(MlDataConflict):
            store.record_candidates([replace(self.sample, final_action="buy_blocked_disabled")])

        with store.transaction() as conn:
            self.assertEqual(
                conn.execute("SELECT final_action FROM ml_candidate_samples").fetchone()[0],
                self.sample.final_action,
            )

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
        before = self.physical_size(store.path)
        sql = []
        original_execute = ml_store._ClosingConnection.execute

        def recording_execute(
            conn: sqlite3.Connection, statement: str, parameters: tuple = ()
        ) -> sqlite3.Cursor:
            sql.append(statement)
            return original_execute(conn, statement, parameters)

        with patch.object(ml_store._ClosingConnection, "execute", recording_execute):
            with self.assertRaises(ValueError):
                LabelRecord(
                    sample_id=self.sample.sample_id,
                    label_version="label-v1",
                    label_source="historical",
                    cost_version="cost-v1",
                    fill_price=payload,  # type: ignore[arg-type]
                    market_data_sha256="market-sha",
                )

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

    def test_v2_contracts_for_same_sample_coexist(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        strict = self.v2_label(horizons=(self.horizon(3),))
        actual = self.v2_label(
            label_source="joinquant_actual_v2",
            horizons=(self.horizon(3),),
        )

        self.assertNotEqual(strict.label_id, actual.label_id)
        self.assertEqual(store.upsert_labels([strict, actual]), 2)
        with store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ml_labels").fetchone()[0], 2)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ml_label_subjects").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ml_label_horizons").fetchone()[0],
                2,
            )

    def test_v2_horizons_mature_independently_and_only_append(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        d3 = self.horizon(3)
        first = self.v2_label(horizons=(d3,))
        second = self.v2_label(horizons=(d3, self.horizon(5)))

        self.assertEqual(store.upsert_labels([first]), 1)
        with store.transaction() as conn:
            d3_hash = conn.execute(
                """SELECT content_sha256 FROM ml_label_horizons
                   WHERE label_id=? AND horizon_days=3""",
                (first.label_id,),
            ).fetchone()[0]
        self.assertEqual(store.upsert_labels([second]), 1)
        self.assertEqual(store.upsert_labels([second]), 0)
        with store.transaction() as conn:
            row = conn.execute(
                """SELECT ret_3d_net,ret_5d_net,matured_3d_at,matured_5d_at,
                          buy_cost,sell_cost,net_cost
                   FROM ml_labels WHERE label_id=?""",
                (first.label_id,),
            ).fetchone()
            child_rows = conn.execute(
                """SELECT horizon_days,content_sha256 FROM ml_label_horizons
                   WHERE label_id=? ORDER BY horizon_days""",
                (first.label_id,),
            ).fetchall()
        self.assertEqual([item[0] for item in child_rows], [3, 5])
        self.assertEqual(child_rows[0][1], d3_hash)
        self.assertAlmostEqual(row[0], d3.net_return)
        self.assertAlmostEqual(row[1], second.horizons[1].net_return)
        self.assertEqual(row[2], d3.matured_at)
        self.assertEqual(row[3], second.horizons[1].matured_at)
        self.assertAlmostEqual(row[4], 5.3)
        self.assertAlmostEqual(row[5], 6.3)
        self.assertAlmostEqual(row[6], 12.2)

    def test_pending_fill_evidence_can_advance_to_terminal_fact(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        pending = replace(
            self.v2_label(fill_matured_at=None),
            fill_evidence_sha256="pending-evidence",
        )
        filled = self.v2_label(horizons=(self.horizon(3),))

        self.assertEqual(store.upsert_labels([pending]), 1)
        self.assertEqual(store.upsert_labels([filled]), 1)

        with store.transaction() as conn:
            row = conn.execute(
                """SELECT fill_status,fill_evidence_sha256,fill_matured_at
                   FROM ml_labels WHERE label_id=?""",
                (filled.label_id,),
            ).fetchone()
        self.assertEqual(tuple(row), (
            "filled", "fill-evidence", filled.fill_matured_at,
        ))

    def test_conflicting_mature_horizon_rolls_back_entire_batch(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        d3 = self.horizon(3)
        original = self.v2_label(horizons=(d3,))
        store.upsert_labels([original])
        conflicting = self.v2_label(
            horizons=(
                replace(d3, net_return=d3.net_return + 0.01),
                self.horizon(5),
            )
        )

        with self.assertRaises(MlDataConflict):
            store.upsert_labels([conflicting])

        with store.transaction() as conn:
            horizons = conn.execute(
                """SELECT horizon_days,net_return FROM ml_label_horizons
                   WHERE label_id=? ORDER BY horizon_days""",
                (original.label_id,),
            ).fetchall()
            main = conn.execute(
                "SELECT ret_3d_net,ret_5d_net FROM ml_labels WHERE label_id=?",
                (original.label_id,),
            ).fetchone()
        self.assertEqual([tuple(row) for row in horizons], [(3, d3.net_return)])
        self.assertEqual(tuple(main), (d3.net_return, None))

    def test_child_sample_id_cannot_disagree_with_main_label(self) -> None:
        store = self.make_store()
        other = replace(self.sample, sample_id="", code="600001")
        store.record_candidates([self.sample, other])
        first = self.v2_label(horizons=(self.horizon(3),))
        second = self.v2_label(sample=other, fill_matured_at=None)
        store.upsert_labels([first, second])

        with store._connect_writable() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """UPDATE ml_label_horizons SET sample_id=?
                       WHERE label_id=? AND horizon_days=3""",
                    (other.sample_id, first.label_id),
                )

    def test_v1_migration_preserves_only_ambiguous_flat_facts(self) -> None:
        path = self.create_v1_store()
        store = MlStore(path)

        store.initialize()

        self.assertEqual(store.schema_version(), ml_store.SCHEMA_VERSION)
        with store.transaction() as conn:
            row = conn.execute(
                """SELECT fill_matured_at,quality_status,quality_reasons_json,
                          failure_reasons_json,ret_5d_net,matured_at,content_sha256
                   FROM ml_labels"""
            ).fetchone()
            subjects = conn.execute(
                "SELECT candidate_origin FROM ml_label_subjects"
            ).fetchall()
            horizons = conn.execute("SELECT COUNT(*) FROM ml_label_horizons").fetchone()[0]
            downside = conn.execute("SELECT COUNT(*) FROM ml_label_downside").fetchone()[0]
        self.assertIsNone(row[0])
        self.assertEqual(row[1], "failed")
        self.assertEqual(json.loads(row[2]), ["LEGACY_AMBIGUOUS_MATURITY"])
        self.assertEqual(json.loads(row[3]), ["LEGACY_AMBIGUOUS_MATURITY"])
        self.assertEqual(row[4], 0.02)
        self.assertEqual(row[5], "2026-07-25T15:00:00+08:00")
        self.assertEqual(len(row[6]), 64)
        self.assertEqual([item[0] for item in subjects], ["history"])
        self.assertEqual(horizons, 0)
        self.assertEqual(downside, 0)

        replay = LabelRecord(
            sample_id=self.sample.sample_id,
            label_version="label-v1",
            label_source="historical",
            cost_version="cost-v1",
            fill_label=1,
            fill_delay_sec=60.0,
            fill_price=10.0,
            ret_3d_net=0.01,
            ret_5d_net=0.02,
            ret_10d_net=0.03,
            mfe_10d=0.08,
            mae_10d=-0.04,
            hit_stop=1,
            hit_take=0,
            actual_net_pnl=20.0,
            market_data_sha256="legacy-market",
            matured_at="2026-07-25T15:00:00+08:00",
        )
        self.assertEqual(store.upsert_labels([replay]), 0)
        self.assertEqual(store.counts()["ml_label_subjects"], 1)
        with store.transaction() as conn:
            reasons = conn.execute(
                "SELECT failure_reasons_json FROM ml_labels"
            ).fetchone()[0]
        self.assertEqual(json.loads(reasons), ["LEGACY_AMBIGUOUS_MATURITY"])

    def test_v1_migration_failure_rolls_back_schema_and_rows(self) -> None:
        path = self.create_v1_store()
        store = MlStore(path)

        with patch.object(
            ml_store,
            "_backfill_label_content_hashes",
            side_effect=sqlite3.OperationalError("injected migration failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                store.initialize()

        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(
                conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                1,
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(ml_labels)")
            }
            subject = conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name='ml_label_subjects'"""
            ).fetchone()
            count = conn.execute("SELECT COUNT(*) FROM ml_labels").fetchone()[0]
        self.assertEqual(columns, {
            "sample_id", "label_version", "label_source", "cost_version",
            "fill_label", "fill_delay_sec", "fill_price", "ret_3d_net",
            "ret_5d_net", "ret_10d_net", "mfe_10d", "mae_10d",
            "hit_stop", "hit_take", "actual_net_pnl", "market_data_sha256",
            "matured_at",
        })
        self.assertIsNone(subject)
        self.assertEqual(count, 1)

    def test_v1_migration_capacity_refusal_leaves_v1_unchanged(self) -> None:
        path = self.create_v1_store()
        before = self.physical_size(path)
        limited = MlStore(path, max_bytes=before + 1)

        with self.assertRaises(MlCapacityError):
            limited.initialize()

        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(
                conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ml_labels").fetchone()[0], 1)
            self.assertIsNone(
                conn.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name='ml_label_subjects'"""
                ).fetchone()
            )

    def test_training_label_rows_are_contract_bound_and_authoritative(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        horizon = self.horizon(5, net_return=0.07)
        strict = self.v2_label(horizons=(horizon,), downside=self.downside())
        actual = self.v2_label(
            label_source="joinquant_actual_v2",
            horizons=(self.horizon(5, net_return=0.02),),
        )
        store.upsert_labels([strict, actual])
        with store._connect_writable() as conn:
            conn.execute(
                "UPDATE ml_labels SET ret_5d_net=999,buy_cost=999 WHERE label_id=?",
                (strict.label_id,),
            )
            conn.commit()

        rows = store.training_label_rows(
            dataset_id=self.sample.dataset_id,
            label_source=strict.label_source,
            label_version=strict.label_version,
            cost_sha256=strict.cost_sha256,
            policy_sha256=strict.policy_sha256,
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["label_id"], strict.label_id)
        self.assertEqual(row["ret_5d_net"], horizon.net_return)
        self.assertAlmostEqual(row["buy_cost"], 5.3)
        self.assertEqual(row["fill_matured_at"], strict.fill_matured_at)
        self.assertEqual(row["ret_5d_matured_at"], horizon.matured_at)
        self.assertEqual(row["downside_matured_at"], strict.downside.matured_at)
        self.assertEqual(row["ret_5d_market_data_sha256"], horizon.market_data_sha256)
        self.assertEqual(len(str(row["content_sha256"])), 64)

    def test_training_label_rows_use_label_id_cursor_and_bounded_limit(self) -> None:
        store = self.make_store()
        samples = [
            self.sample,
            replace(self.sample, sample_id="", code="600001"),
            replace(self.sample, sample_id="", code="600002"),
        ]
        labels = [self.v2_label(sample=sample, horizons=(self.horizon(3),)) for sample in samples]
        store.record_candidates(samples)
        store.upsert_labels(labels)
        contract = labels[0]

        first = store.training_label_rows(
            dataset_id=self.sample.dataset_id,
            label_source=contract.label_source,
            label_version=contract.label_version,
            cost_sha256=contract.cost_sha256,
            policy_sha256=contract.policy_sha256,
            limit=2,
        )
        second = store.training_label_rows(
            dataset_id=self.sample.dataset_id,
            label_source=contract.label_source,
            label_version=contract.label_version,
            cost_sha256=contract.cost_sha256,
            policy_sha256=contract.policy_sha256,
            cursor=str(first[-1]["label_id"]),
            limit=2,
        )
        empty = store.training_label_rows(
            dataset_id=self.sample.dataset_id,
            label_source=contract.label_source,
            label_version=contract.label_version,
            cost_sha256=contract.cost_sha256,
            policy_sha256=contract.policy_sha256,
            cursor=str(second[-1]["label_id"]),
            limit=2,
        )

        combined = [str(row["label_id"]) for row in (*first, *second)]
        self.assertEqual(combined, sorted(label.label_id for label in labels))
        self.assertEqual(empty, [])
        for invalid in (0, ml_store.TRAINING_LABEL_PAGE_LIMIT + 1, True):
            with self.assertRaises(ValueError):
                store.training_label_rows(
                    dataset_id=self.sample.dataset_id,
                    label_source=contract.label_source,
                    label_version=contract.label_version,
                    cost_sha256=contract.cost_sha256,
                    policy_sha256=contract.policy_sha256,
                    limit=invalid,  # type: ignore[arg-type]
                )

    def test_training_label_rows_do_not_treat_legacy_maturity_as_authoritative(self) -> None:
        path = self.create_v1_store()
        store = MlStore(path)
        store.initialize()
        with store.transaction() as conn:
            row = conn.execute(
                """SELECT fill_matured_at,matured_3d_at,matured_5d_at,
                          matured_10d_at,downside_matured_at
                   FROM ml_labels"""
            ).fetchone()
        self.assertEqual(tuple(row), (None, None, None, None, None))

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

    def test_recent_prediction_candidate_rows_are_bounded_and_expand_features(self) -> None:
        store = self.make_store()
        samples = [
            replace(
                self.sample,
                sample_id="",
                code=f"60000{index}",
                decision_at=f"2026-07-15T09:{35 + index * 5:02d}:00+08:00",
            )
            for index in range(3)
        ]
        store.record_candidates(samples)
        store.record_predictions(
            [
                PredictionRecord(
                    sample_id=sample.sample_id,
                    model_id="model-1",
                    created_at=f"2026-07-15T10:0{index}:00+08:00",
                    expected_ret_5d=0.01 * index,
                )
                for index, sample in enumerate(samples)
            ]
        )
        store.record_predictions(
            [
                PredictionRecord(
                    sample_id=samples[0].sample_id,
                    model_id="other-model",
                    created_at="2026-07-15T10:05:00+08:00",
                )
            ]
        )

        recent = store.recent_prediction_candidate_rows(
            "model-1", batch_limit=2, row_limit=10
        )
        newest = store.recent_prediction_candidate_rows(
            "model-1", batch_limit=3, row_limit=1
        )

        self.assertEqual(
            [row["decision_at"] for row in recent],
            [samples[1].decision_at, samples[2].decision_at],
        )
        self.assertEqual([row["sample_id"] for row in newest], [samples[2].sample_id])
        self.assertEqual(recent[0]["price"], 10.5)
        self.assertEqual(recent[0]["context"], {"regime": "NORMAL", "levels": [1, 2]})
        self.assertNotIn("features_json", recent[0])
        for kwargs in (
            {"batch_limit": 0},
            {"batch_limit": 101},
            {"batch_limit": True},
            {"row_limit": 0},
            {"row_limit": 5001},
            {"row_limit": True},
        ):
            with self.assertRaises(ValueError):
                store.recent_prediction_candidate_rows("model-1", **kwargs)

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
                manifest, artifact_path="model-1"
            )
        )
        self.assertFalse(
            store.register_model(
                manifest, artifact_path="model-1"
            )
        )
        with self.assertRaises(MlDataConflict):
            store.register_model(
                replace(manifest, artifact_sha256="changed-sha"),
                artifact_path="model-1",
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
        record = store.model_record("model-1")
        approval = store.approved_model_event("model-1", "artifact-sha")
        self.assertEqual(record["artifact_path"], "model-1")
        self.assertEqual(record["artifact_sha256"], "artifact-sha")
        self.assertEqual(record["manifest"]["model_id"], "model-1")
        self.assertEqual(approval["event_id"], "event-1")
        self.assertIsNone(store.model_record("missing"))
        self.assertIsNone(store.approved_model_event("model-1", "wrong-sha"))

    def test_register_model_rejects_noncanonical_or_escaping_artifact_paths(self) -> None:
        store = self.make_store()
        manifest = self.manifest()

        for artifact_path in (
            "",
            ".",
            "./model-1",
            "../model-1",
            "model-1/../other",
            "model-1//nested",
            "/var/models/model-1",
            "C:/models/model-1",
            "C:\\models\\model-1",
            "model-1\\nested",
        ):
            with self.subTest(artifact_path=artifact_path):
                with self.assertRaises(ValueError):
                    store.register_model(manifest, artifact_path=artifact_path)
        self.assertEqual(store.counts()["ml_models"], 0)

    def test_register_model_accepts_training_bundle_manifest_with_external_hash(self) -> None:
        store = self.make_store()
        manifest = self.bundle_manifest()

        with self.assertRaisesRegex(ValueError, "artifact_sha256"):
            store.register_model(manifest, artifact_path=manifest.model_id)
        self.assertTrue(
            store.register_model(
                manifest,
                artifact_path=manifest.model_id,
                artifact_sha256="artifact-sha",
            )
        )

        record = store.model_record(manifest.model_id)
        self.assertEqual(record["status"], "approvable_l0")
        self.assertEqual(record["artifact_path"], manifest.model_id)
        self.assertEqual(record["artifact_sha256"], "artifact-sha")
        self.assertEqual(record["strategy_version"], "strategy-v1")
        self.assertEqual(record["manifest"]["generated_by"], "stock-analysis")
        self.assertNotIn("artifact_sha256", record["manifest"])

    def test_real_store_register_approve_activate_and_load_bundle(self) -> None:
        store = self.make_store()
        manifest = self.bundle_manifest(model_id="model-live-1")
        model_root = self.root / "models"
        artifact_dir = model_root / manifest.model_id
        artifact_dir.mkdir(parents=True)
        bundle_path = artifact_dir / "bundle.joblib"
        joblib.dump(
            {
                "generated_by": "stock-analysis",
                "model_id": manifest.model_id,
                "bundle_schema_version": manifest.manifest_schema_version,
                "models": {},
            },
            bundle_path,
        )
        manifest_payload = manifest.as_dict()
        manifest_payload["files"] = {
            "bundle.joblib": ml_runtime._file_sha256(bundle_path)
        }
        (artifact_dir / "manifest.json").write_text(
            json.dumps(
                manifest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        artifact_sha256 = ml_runtime._bundle_sha256(artifact_dir)
        store.register_model(
            manifest,
            artifact_path=manifest.model_id,
            artifact_sha256=artifact_sha256,
        )
        evidence = PermissionEvidence(
            model_id=manifest.model_id,
            artifact_sha256=artifact_sha256,
            strategy_version=manifest.strategy_version,
            evidence_start="2026-07-28",
            evidence_end="2026-08-06",
            valid_days=5,
            prediction_count=500,
            mature_d5=0,
            mature_d10=0,
            mature_filters=0,
            closed_cycles=0,
            prediction_availability=1.0,
            runtime_fault_rate=0.0,
            max_psi=0.0,
            behavior_equivalence=1.0,
            mean_improvement_pct_points=0.0,
            improvement_ci_lower=0.0,
            max_drawdown_not_worse=True,
            versions_match=True,
            hashes_match=True,
            permission_violations=0,
            l0_trading_equivalence=1.0,
            l1_set_quantity_equivalence=1.0,
            l2_added_candidates=0,
            l2_increased_quantity=0,
            l3_multiplier_min=1.0,
            l3_multiplier_max=1.0,
            sell_equivalence=1.0,
            hard_rejection_equivalence=1.0,
        )
        admin = MlAdmin(store)
        admin.approve(
            model_id=manifest.model_id,
            level=0,
            evidence=evidence,
            expected_model_id=None,
            expected_level=0,
            reason="integration approval",
            operator="human:test",
            now="2026-08-06T10:01:00+08:00",
        )
        self.assertTrue(
            admin.activate(
                model_id=manifest.model_id,
                level=0,
                expected_model_id=None,
                expected_level=0,
                reason="integration activation",
                operator="human:test",
                now="2026-08-06T10:02:00+08:00",
            )
        )

        loaded = ml_runtime.load_active_bundle(store, model_root, expected_versions={})

        self.assertEqual(loaded.model_id, manifest.model_id)
        self.assertEqual(loaded.artifact_sha256, artifact_sha256)
        self.assertEqual(store.runtime_state()["active_model_id"], manifest.model_id)

    def test_runtime_compare_and_swap_rejects_stale_expected_state(self) -> None:
        store = self.make_store()
        store.register_model(
            self.manifest(), artifact_path="model-1"
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

    def test_runtime_transition_and_audit_event_commit_atomically(self) -> None:
        store = self.make_store()
        store.register_model(self.manifest(), artifact_path="model-1")
        event_id = "event-atomic-transition"
        self.assertTrue(
            store.transition_runtime_with_event(
                expected_model_id=None,
                expected_permission_level=0,
                new_model_id="model-1",
                new_permission_level=0,
                updated_at="2026-07-15T16:02:00+08:00",
                event_id=event_id,
                event_model_id="model-1",
                action="activate",
                event_old_level=0,
                event_new_level=0,
                artifact_sha256="artifact-sha",
                reason="atomic test",
                operator="human:test",
                created_at="2026-07-15T16:02:00+08:00",
            )
        )
        self.assertEqual(store.runtime_state()["active_model_id"], "model-1")
        with store.transaction() as connection:
            event = connection.execute(
                "SELECT action,new_level FROM ml_model_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        self.assertEqual(tuple(event), ("activate", 0))

    def test_checkpoint_busy_commit_is_immediately_visible_to_public_reads(self) -> None:
        store = self.make_store()
        reader = sqlite3.connect(store.path, timeout=0)
        reader.execute("BEGIN")
        self.assertEqual(
            reader.execute(
                "SELECT permission_level FROM ml_runtime_state WHERE singleton=1"
            ).fetchone()[0],
            0,
        )
        try:
            self.assertTrue(
                store.compare_and_swap_runtime(
                    expected_model_id=None,
                    expected_permission_level=0,
                    new_model_id=None,
                    new_permission_level=1,
                    updated_at="2026-07-15T16:02:00+08:00",
                )
            )
            self.assertGreater(Path(f"{store.path}-wal").stat().st_size, 0)
            self.assertEqual(store.runtime_state()["permission_level"], 1)
            with store.transaction() as conn:
                conn.execute("PRAGMA query_only=OFF")
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute(
                        "UPDATE ml_runtime_state SET permission_level=2 WHERE singleton=1"
                    )
        finally:
            reader.rollback()
            reader.close()

        self.assertTrue(
            store.compare_and_swap_runtime(
                expected_model_id=None,
                expected_permission_level=1,
                new_model_id=None,
                new_permission_level=2,
                updated_at="2026-07-15T16:03:00+08:00",
            )
        )
        wal = Path(f"{store.path}-wal")
        self.assertEqual(wal.stat().st_size if wal.exists() else 0, 0)
        self.assertEqual(store.runtime_state()["permission_level"], 2)

    def test_checkpoint_failure_after_commit_does_not_report_write_failure(self) -> None:
        store = self.make_store()

        with patch.object(store, "_wal_has_content", side_effect=OSError("busy")):
            self.assertTrue(
                store.compare_and_swap_runtime(
                    expected_model_id=None,
                    expected_permission_level=0,
                    new_model_id=None,
                    new_permission_level=1,
                    updated_at="2026-07-15T16:02:00+08:00",
                )
            )

        self.assertEqual(store.runtime_state()["permission_level"], 1)

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
                )
                if path.exists()
            ),
            max_bytes,
        )

        with self.assertRaises(MlCapacityError):
            limited.record_candidates([large])

        self.assertEqual(store.counts()["ml_candidate_samples"], 1)
        with store.transaction() as conn:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        self.assertLessEqual(page_size * page_count, max_bytes)
        self.assertLessEqual(
            sum(
                path.stat().st_size
                for path in (
                    store.path,
                    Path(f"{store.path}-wal"),
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
            data_bytes = sum(
                path.stat().st_size
                for path in (
                    store.path,
                    Path(f"{store.path}-wal"),
                )
                if path.exists()
            )
            max_bytes = data_bytes + 100_000
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

    def test_zero_byte_store_is_rejected_without_physical_growth(self) -> None:
        path = self.root / "zero-byte" / "ml.db"
        path.parent.mkdir(parents=True)
        path.touch()
        files = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        before = tuple(item.stat().st_size if item.exists() else None for item in files)

        with patch.object(ml_store.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            with self.assertRaises(MlCapacityError):
                MlStore(path, max_bytes=1).initialize()

        self.assertFalse(connect.called)
        self.assertEqual(
            tuple(item.stat().st_size if item.exists() else None for item in files),
            before,
        )

    def test_existing_wal_at_capacity_is_rejected_before_shm_creation(self) -> None:
        source = self.make_store()
        keeper = source._connect_writable()
        try:
            keeper.execute("BEGIN IMMEDIATE")
            keeper.execute(
                "UPDATE ml_runtime_state SET updated_at=? WHERE singleton=1",
                ("2026-07-15T16:01:00+08:00",),
            )
            keeper.commit()
            source_wal = Path(f"{source.path}-wal")
            self.assertGreater(source_wal.stat().st_size, 32)

            path = self.root / "wal-at-limit" / "ml.db"
            path.parent.mkdir(parents=True)
            path.write_bytes(source.path.read_bytes())
            Path(f"{path}-wal").write_bytes(source_wal.read_bytes())
            files = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
            before = tuple(
                item.stat().st_size if item.exists() else None for item in files
            )
            limit = sum(size or 0 for size in before)

            with self.assertRaises(MlCapacityError):
                MlStore(path, max_bytes=limit).compare_and_swap_runtime(
                    expected_model_id=None,
                    expected_permission_level=0,
                    new_model_id=None,
                    new_permission_level=1,
                    updated_at="2026-07-15T16:02:00+08:00",
                )

            self.assertEqual(
                tuple(item.stat().st_size if item.exists() else None for item in files),
                before,
            )
        finally:
            keeper.close()

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
        self.assertEqual(restored.schema_version(), ml_store.SCHEMA_VERSION)
        self.assertEqual(restored.counts(), store.counts())
        with restored.transaction() as conn:
            row = conn.execute(
                """SELECT final_action, universe_hash, market_data_version,
                          code_hash, generator_hash FROM ml_candidate_samples"""
            ).fetchone()
        self.assertEqual(tuple(row), (
            self.sample.final_action, self.sample.universe_hash,
            self.sample.market_data_version, self.sample.code_hash, self.sample.generator_hash,
        ))

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
            self.manifest(), artifact_path="model-1"
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

    def test_runtime_health_is_bounded_and_prediction_evidence_is_immutable(self) -> None:
        store = self.make_store()
        store.record_candidates([self.sample])
        prediction = PredictionRecord(
            sample_id=self.sample.sample_id,
            model_id="model-health",
            created_at="2026-08-06T09:35:00+08:00",
            expected_ret_5d=0.02,
            fill_probability=0.8,
            ml_score=72.0,
            confidence=0.75,
            feature_coverage=1.0,
            max_feature_psi=0.04,
            drift_status="ready",
            reasons=("TEST_REASON",),
        )

        self.assertEqual(store.record_predictions([prediction]), 1)
        store.record_runtime_health(
            status="ok",
            reason="",
            attempted_at="2026-08-06T09:35:00+08:00",
            prediction_count=1,
            successful=True,
            trading_equivalent=True,
        )
        self.assertEqual(store.record_predictions([prediction]), 0)
        with store.transaction() as conn:
            row = conn.execute(
                "SELECT feature_coverage,max_feature_psi,drift_status,reasons_json "
                "FROM ml_predictions WHERE sample_id=? AND model_id=?",
                (self.sample.sample_id, "model-health"),
            ).fetchone()
        self.assertEqual(tuple(row), (1.0, 0.04, "ready", '["TEST_REASON"]'))
        self.assertEqual(store.runtime_health()["health_status"], "ok")
        self.assertTrue(store.runtime_health()["last_trading_equivalent"])


if __name__ == "__main__":
    unittest.main()
