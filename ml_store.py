"""Independent SQLite ledger for trained-shadow-model data and model state."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from ml_contracts import (
    CandidateSample,
    LabelRecord,
    ModelManifest,
    PredictionRecord,
    canonical_hash,
)


# Python 3.10 does not expose SQLite error codes on exception instances or
# export every symbolic constant from sqlite3.  SQLITE_FULL is stable in the
# SQLite result-code table, so keep capacity handling portable across the
# supported local (3.10) and server (3.12) runtimes.
_SQLITE_FULL = getattr(sqlite3, "SQLITE_FULL", 13)


def _is_sqlite_full(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code == _SQLITE_FULL:
        return True
    message = str(error).lower()
    return "database or disk is full" in message or "database full" in message


SCHEMA_VERSION = 2
BTREE_RESERVE_BYTES = 128 * 1024
RUNTIME_RESERVE_BYTES = 64 * 1024
SCHEMA_BTREE_COUNT = 20
LABEL_MIGRATION_BTREE_COUNT = 8
LABEL_WRITE_BTREE_COUNT = 12
LABEL_MAIN_ROW_RESERVE_BYTES = 1_024
LABEL_CHILD_ROW_RESERVE_BYTES = 512
TRAINING_LABEL_PAGE_LIMIT = 1_000
ML_TABLES = (
    "ml_candidate_samples",
    "ml_label_subjects",
    "ml_labels",
    "ml_label_horizons",
    "ml_label_downside",
    "ml_predictions",
    "ml_models",
    "ml_model_events",
    "ml_runtime_state",
)


class MlDataConflict(RuntimeError):
    """Raised when an immutable ML identity is replayed with different data."""


class MlCapacityError(RuntimeError):
    """Raised when the configured ML database capacity has been reached."""


LABEL_SCHEMA_V1 = """CREATE TABLE IF NOT EXISTS ml_labels(
  sample_id TEXT PRIMARY KEY REFERENCES ml_candidate_samples(sample_id),
  label_version TEXT NOT NULL, label_source TEXT NOT NULL, cost_version TEXT NOT NULL,
  fill_label INTEGER, fill_delay_sec REAL, fill_price REAL,
  ret_3d_net REAL, ret_5d_net REAL, ret_10d_net REAL,
  mfe_10d REAL, mae_10d REAL, hit_stop INTEGER, hit_take INTEGER,
  actual_net_pnl REAL, market_data_sha256 TEXT NOT NULL, matured_at TEXT
);
"""

LABEL_SUBJECT_SCHEMA_V2 = """CREATE TABLE IF NOT EXISTS ml_label_subjects(
  sample_id TEXT PRIMARY KEY,
  candidate_origin TEXT NOT NULL CHECK(candidate_origin IN ('history','ml')),
  source TEXT NOT NULL, dataset_id TEXT NOT NULL,
  trade_date TEXT NOT NULL, decision_at TEXT NOT NULL, code TEXT NOT NULL,
  candidate_content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ml_label_subjects_dataset
ON ml_label_subjects(dataset_id, trade_date, decision_at, sample_id);
"""

LABEL_TABLE_SCHEMA_V2 = """CREATE TABLE IF NOT EXISTS ml_labels(
  label_id TEXT PRIMARY KEY,
  sample_id TEXT NOT NULL REFERENCES ml_label_subjects(sample_id),
  label_version TEXT NOT NULL, label_source TEXT NOT NULL, cost_version TEXT NOT NULL,
  cost_sha256 TEXT NOT NULL DEFAULT '', policy_version TEXT NOT NULL DEFAULT '',
  policy_sha256 TEXT NOT NULL DEFAULT '',
  fill_label INTEGER, fill_delay_sec REAL, fill_price REAL,
  ret_3d_net REAL, ret_5d_net REAL, ret_10d_net REAL,
  mfe_10d REAL, mae_10d REAL, hit_stop INTEGER, hit_take INTEGER,
  actual_net_pnl REAL, market_data_sha256 TEXT NOT NULL, matured_at TEXT,
  candidate_source TEXT NOT NULL DEFAULT '', dataset_id TEXT NOT NULL DEFAULT '',
  trade_date TEXT NOT NULL DEFAULT '', decision_at TEXT, code TEXT NOT NULL DEFAULT '',
  candidate_content_sha256 TEXT NOT NULL DEFAULT '',
  fill_status TEXT NOT NULL DEFAULT 'pending'
    CHECK(fill_status IN ('pending','filled','not_filled','failed','invalid')),
  fill_reason TEXT NOT NULL DEFAULT '',
  fill_evidence_sha256 TEXT NOT NULL DEFAULT '',
  fill_at TEXT, fill_matured_at TEXT,
  reference_qty INTEGER NOT NULL DEFAULT 100 CHECK(reference_qty > 0),
  reference_notional_yuan REAL NOT NULL DEFAULT 10000
    CHECK(reference_notional_yuan > 0),
  reference_trade_notional_yuan REAL
    CHECK(reference_trade_notional_yuan IS NULL OR reference_trade_notional_yuan > 0),
  ret_3d_gross REAL, ret_5d_gross REAL, ret_10d_gross REAL,
  downside_loss REAL CHECK(downside_loss IS NULL OR downside_loss >= 0),
  exit_blocked INTEGER NOT NULL DEFAULT 0 CHECK(exit_blocked IN (0,1)),
  paused_path INTEGER NOT NULL DEFAULT 0 CHECK(paused_path IN (0,1)),
  buy_cost REAL, sell_cost REAL, slippage_cost REAL, commission_cost REAL,
  stamp_tax_cost REAL, transfer_fee_cost REAL, other_fee_cost REAL, net_cost REAL,
  quality_status TEXT NOT NULL DEFAULT 'pending'
    CHECK(quality_status IN ('pending','partial','complete','failed')),
  quality_reasons_json TEXT NOT NULL DEFAULT '[]',
  flags_json TEXT NOT NULL DEFAULT '[]', failure_reasons_json TEXT NOT NULL DEFAULT '[]',
  matured_3d_at TEXT, matured_5d_at TEXT, matured_10d_at TEXT,
  downside_matured_at TEXT, content_sha256 TEXT NOT NULL DEFAULT '',
  updated_at TEXT,
  UNIQUE(sample_id, label_source, label_version, cost_sha256, policy_sha256),
  UNIQUE(label_id, sample_id)
);
"""

LABEL_HORIZON_SCHEMA_V2 = """CREATE INDEX IF NOT EXISTS idx_ml_labels_training
ON ml_labels(label_source, label_version, cost_sha256, policy_sha256, label_id);
CREATE TABLE IF NOT EXISTS ml_label_horizons(
  label_id TEXT NOT NULL,
  sample_id TEXT NOT NULL,
  horizon_days INTEGER NOT NULL CHECK(horizon_days IN (3,5,10)),
  gross_return REAL NOT NULL, net_return REAL NOT NULL,
  exit_price REAL NOT NULL CHECK(exit_price > 0),
  buy_commission_yuan REAL NOT NULL CHECK(buy_commission_yuan >= 0),
  buy_transfer_fee_yuan REAL NOT NULL CHECK(buy_transfer_fee_yuan >= 0),
  buy_other_fee_yuan REAL NOT NULL CHECK(buy_other_fee_yuan >= 0),
  buy_slippage_yuan REAL NOT NULL CHECK(buy_slippage_yuan >= 0),
  sell_commission_yuan REAL NOT NULL CHECK(sell_commission_yuan >= 0),
  sell_stamp_tax_yuan REAL NOT NULL CHECK(sell_stamp_tax_yuan >= 0),
  sell_transfer_fee_yuan REAL NOT NULL CHECK(sell_transfer_fee_yuan >= 0),
  sell_other_fee_yuan REAL NOT NULL CHECK(sell_other_fee_yuan >= 0),
  sell_slippage_yuan REAL NOT NULL CHECK(sell_slippage_yuan >= 0),
  total_cost_yuan REAL NOT NULL CHECK(total_cost_yuan >= 0),
  cost_rate REAL NOT NULL CHECK(cost_rate >= 0), matured_at TEXT NOT NULL,
  market_data_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(label_id, horizon_days),
  FOREIGN KEY(label_id, sample_id)
    REFERENCES ml_labels(label_id, sample_id) ON DELETE CASCADE
);
"""

LABEL_DOWNSIDE_SCHEMA_V2 = """CREATE TABLE IF NOT EXISTS ml_label_downside(
  label_id TEXT PRIMARY KEY,
  sample_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','complete','failed')),
  mfe_10d_net REAL,
  mae_10d_net REAL CHECK(mae_10d_net IS NULL OR mae_10d_net <= 0),
  downside_loss REAL CHECK(downside_loss IS NULL OR downside_loss >= 0),
  paused_path INTEGER NOT NULL CHECK(paused_path IN (0,1)),
  exit_blocked INTEGER NOT NULL CHECK(exit_blocked IN (0,1)),
  hit_stop INTEGER CHECK(hit_stop IS NULL OR hit_stop IN (0,1)),
  hit_take INTEGER CHECK(hit_take IS NULL OR hit_take IN (0,1)),
  failure_reason TEXT NOT NULL DEFAULT '',
  matured_at TEXT, evidence_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK(
    mae_10d_net IS NULL OR downside_loss IS NULL
    OR ABS(mae_10d_net + downside_loss) <= 0.000000000001
  ),
  FOREIGN KEY(label_id, sample_id)
    REFERENCES ml_labels(label_id, sample_id) ON DELETE CASCADE
);
"""

LABEL_SCHEMA_V2 = (
    LABEL_SUBJECT_SCHEMA_V2
    + LABEL_TABLE_SCHEMA_V2
    + LABEL_HORIZON_SCHEMA_V2
    + LABEL_DOWNSIDE_SCHEMA_V2
)

PREDICTION_SCHEMA_V1 = """CREATE TABLE IF NOT EXISTS ml_predictions(
  sample_id TEXT NOT NULL REFERENCES ml_candidate_samples(sample_id),
  model_id TEXT NOT NULL, expected_ret_3d REAL, expected_ret_5d REAL,
  expected_ret_10d REAL, downside_risk REAL, fill_probability REAL,
  ml_score REAL, ml_filter INTEGER, position_multiplier REAL, confidence REAL,
  created_at TEXT NOT NULL, PRIMARY KEY(sample_id, model_id)
);
"""

PREDICTION_SCHEMA_V2 = """CREATE TABLE IF NOT EXISTS ml_predictions(
  sample_id TEXT NOT NULL REFERENCES ml_candidate_samples(sample_id),
  model_id TEXT NOT NULL, expected_ret_3d REAL, expected_ret_5d REAL,
  expected_ret_10d REAL, downside_risk REAL, fill_probability REAL,
  ml_score REAL, ml_filter INTEGER, position_multiplier REAL, confidence REAL,
  feature_coverage REAL CHECK(feature_coverage IS NULL OR
    (feature_coverage >= 0 AND feature_coverage <= 1)),
  max_feature_psi REAL CHECK(max_feature_psi IS NULL OR max_feature_psi >= 0),
  drift_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK(drift_status IN ('unknown','ready','insufficient')),
  reasons_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, PRIMARY KEY(sample_id, model_id)
);
"""

RUNTIME_STATE_SCHEMA_V1 = """CREATE TABLE IF NOT EXISTS ml_runtime_state(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), active_model_id TEXT,
  permission_level INTEGER NOT NULL CHECK(permission_level BETWEEN 0 AND 3),
  updated_at TEXT NOT NULL
);
"""

RUNTIME_STATE_SCHEMA_V2 = """CREATE TABLE IF NOT EXISTS ml_runtime_state(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), active_model_id TEXT,
  permission_level INTEGER NOT NULL CHECK(permission_level BETWEEN 0 AND 3),
  updated_at TEXT NOT NULL,
  health_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK(health_status IN ('unknown','ok','fallback','disabled','no_model')),
  health_reason TEXT NOT NULL DEFAULT '' CHECK(LENGTH(health_reason) <= 512),
  last_attempt_at TEXT, last_success_at TEXT,
  last_prediction_count INTEGER NOT NULL DEFAULT 0
    CHECK(last_prediction_count >= 0),
  last_trading_equivalent INTEGER
    CHECK(last_trading_equivalent IS NULL OR last_trading_equivalent IN (0,1))
);
"""


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ml_candidate_samples(
  sample_id TEXT PRIMARY KEY, source TEXT NOT NULL, dataset_id TEXT NOT NULL,
  trade_date TEXT NOT NULL, decision_at TEXT NOT NULL, code TEXT NOT NULL,
  strategy_version TEXT NOT NULL, parameter_version TEXT NOT NULL,
  feature_schema_version TEXT NOT NULL, features_json TEXT NOT NULL,
  selected INTEGER NOT NULL, rejection_stage TEXT NOT NULL,
  rejection_code TEXT NOT NULL, final_action TEXT NOT NULL,
  universe_hash TEXT NOT NULL, market_data_version TEXT NOT NULL,
  code_hash TEXT NOT NULL, generator_hash TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ml_candidates_date_code
ON ml_candidate_samples(trade_date, code, decision_at);
{LABEL_SCHEMA_V2}
{PREDICTION_SCHEMA_V2}
CREATE TABLE IF NOT EXISTS ml_models(
  model_id TEXT PRIMARY KEY, parent_model_id TEXT, status TEXT NOT NULL,
  artifact_path TEXT NOT NULL, artifact_sha256 TEXT NOT NULL UNIQUE,
  manifest_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ml_model_events(
  event_id TEXT PRIMARY KEY, model_id TEXT NOT NULL REFERENCES ml_models(model_id),
  action TEXT NOT NULL, old_level INTEGER NOT NULL, new_level INTEGER NOT NULL,
  artifact_sha256 TEXT NOT NULL, reason TEXT NOT NULL, operator TEXT NOT NULL,
  created_at TEXT NOT NULL
);
{RUNTIME_STATE_SCHEMA_V2}
"""

SCHEMA_V1 = (
    SCHEMA.replace(LABEL_SCHEMA_V2, LABEL_SCHEMA_V1)
    .replace(PREDICTION_SCHEMA_V2, PREDICTION_SCHEMA_V1)
    .replace(RUNTIME_STATE_SCHEMA_V2, RUNTIME_STATE_SCHEMA_V1)
)

LABEL_MAIN_COLUMNS = (
    "sample_id",
    "label_version",
    "label_source",
    "cost_version",
    "cost_sha256",
    "policy_version",
    "policy_sha256",
    "candidate_source",
    "dataset_id",
    "trade_date",
    "decision_at",
    "code",
    "candidate_content_sha256",
    "fill_label",
    "fill_status",
    "fill_reason",
    "fill_evidence_sha256",
    "fill_delay_sec",
    "fill_price",
    "fill_at",
    "fill_matured_at",
    "reference_qty",
    "reference_notional_yuan",
    "reference_trade_notional_yuan",
    "ret_3d_gross",
    "ret_3d_net",
    "ret_5d_gross",
    "ret_5d_net",
    "ret_10d_gross",
    "ret_10d_net",
    "mfe_10d",
    "mae_10d",
    "downside_loss",
    "hit_stop",
    "hit_take",
    "exit_blocked",
    "paused_path",
    "buy_cost",
    "sell_cost",
    "slippage_cost",
    "commission_cost",
    "stamp_tax_cost",
    "transfer_fee_cost",
    "other_fee_cost",
    "net_cost",
    "actual_net_pnl",
    "quality_status",
    "quality_reasons_json",
    "flags_json",
    "failure_reasons_json",
    "market_data_sha256",
    "matured_3d_at",
    "matured_5d_at",
    "matured_10d_at",
    "downside_matured_at",
    "matured_at",
    "content_sha256",
    "updated_at",
)


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class MlStore:
    def __init__(self, path: Path, max_bytes: int = 2_000_000_000) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_bytes)

    def _connect_readonly(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise RuntimeError("ML store is not initialized")
        conn = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
            factory=_ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA cache_spill=OFF")
        return conn

    def _is_current_schema(self) -> bool:
        current_version = False
        try:
            with self._connect_readonly() as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
                if version is None or int(version[0] or 0) != SCHEMA_VERSION:
                    return False
                current_version = True
                required = _required_schema_fingerprint()
                actual = _schema_fingerprint(conn)
                mismatched = [
                    key[1]
                    for key, sql in required.items()
                    if key != ("table", "ml_labels") and actual.get(key) != sql
                ]
                if mismatched:
                    raise RuntimeError(
                        "ML schema structure mismatch for version "
                        f"{SCHEMA_VERSION}: {', '.join(sorted(mismatched))}"
                    )
                _validate_label_schema_v2(conn)
                runtime = conn.execute(
                    """SELECT permission_level,updated_at,health_status,
                              health_reason,last_attempt_at,last_success_at,
                              last_prediction_count,last_trading_equivalent
                       FROM ml_runtime_state
                       WHERE singleton=1"""
                ).fetchone()
        except RuntimeError:
            raise
        except sqlite3.Error as exc:
            if current_version:
                raise RuntimeError(
                    f"ML schema validation failed for version {SCHEMA_VERSION}"
                ) from exc
            return False
        try:
            if runtime is None or not 0 <= int(runtime[0]) <= 3:
                raise ValueError("invalid runtime row")
            _aware_iso(str(runtime[1]), "updated_at")
            if str(runtime[2]) not in {
                "unknown", "ok", "fallback", "disabled", "no_model"
            }:
                raise ValueError("invalid runtime health status")
            if len(str(runtime[3] or "")) > 512:
                raise ValueError("invalid runtime health reason")
            for index, name in ((4, "last_attempt_at"), (5, "last_success_at")):
                if runtime[index] is not None:
                    _aware_iso(str(runtime[index]), name)
            if int(runtime[6]) < 0:
                raise ValueError("invalid runtime prediction count")
            if runtime[7] is not None and int(runtime[7]) not in {0, 1}:
                raise ValueError("invalid runtime equivalence evidence")
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"ML runtime state is invalid for schema version {SCHEMA_VERSION}"
            ) from exc
        return True

    def _connect_writable(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path, timeout=5.0, factory=_ClosingConnection
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("PRAGMA cache_spill=OFF")
        return conn

    def initialize(self) -> None:
        if self._file_size(self.path) > 0:
            version = self._existing_schema_version()
            if version == SCHEMA_VERSION:
                if self._is_current_schema():
                    return
            elif version == 1:
                self._migrate_v1_to_v2()
                if not self._is_current_schema():
                    raise RuntimeError("ML schema migration to version 2 was incomplete")
                return
            elif version > 0:
                raise RuntimeError(f"unsupported ML schema version: {version}")
        statements = tuple(_schema_statements(SCHEMA))
        now = _now()
        reserved_bytes = (
            2 * len(SCHEMA.encode("utf-8"))
            + SCHEMA_BTREE_COUNT * BTREE_RESERVE_BYTES
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
            initialize=True,
        ) as conn:
            for statement in statements:
                conn.execute(statement)
            conn.executemany(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                ((1, now), (SCHEMA_VERSION, now)),
            )
            conn.execute(
                """INSERT OR IGNORE INTO ml_runtime_state(
                   singleton, active_model_id, permission_level, updated_at
                   ) VALUES (1, NULL, 0, ?)""",
                (now,),
            )

    def _existing_schema_version(self) -> int:
        try:
            with self._connect_readonly() as conn:
                row = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0] or 0) if row is not None else 0

    def _migrate_v1_to_v2(self) -> None:
        with self._connect_readonly() as conn:
            required = _required_schema_fingerprint(SCHEMA_V1)
            actual = _schema_fingerprint(conn)
            mismatched = [
                key[1] for key, sql in required.items() if actual.get(key) != sql
            ]
            if mismatched:
                raise RuntimeError(
                    "ML schema structure mismatch for version 1: "
                    + ", ".join(sorted(mismatched))
                )
            migration_row = conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(
                     LENGTH(l.sample_id) + LENGTH(l.label_version)
                     + LENGTH(l.label_source) + LENGTH(l.cost_version)
                     + LENGTH(l.market_data_sha256)
                     + LENGTH(c.source) + LENGTH(c.dataset_id)
                     + LENGTH(c.trade_date) + LENGTH(c.decision_at)
                     + LENGTH(c.code) + LENGTH(c.content_sha256) + 512
                   ), 0)
                   FROM ml_labels l
                   JOIN ml_candidate_samples c ON c.sample_id=l.sample_id"""
            ).fetchone()
        legacy_count = int(migration_row[0] or 0) if migration_row else 0
        legacy_payload_bytes = int(migration_row[1] or 0) if migration_row else 0
        migration_data_reserve = (
            4 * legacy_payload_bytes + legacy_count * 512
        )
        reserved_bytes = (
            2 * len(LABEL_SCHEMA_V2.encode("utf-8"))
            + LABEL_MIGRATION_BTREE_COUNT * BTREE_RESERVE_BYTES
            + migration_data_reserve
        )
        now = _now()
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
            for statement in _schema_statements(LABEL_SUBJECT_SCHEMA_V2):
                conn.execute(statement)
            conn.execute(
                """INSERT INTO ml_label_subjects(
                   sample_id,candidate_origin,source,dataset_id,trade_date,
                   decision_at,code,candidate_content_sha256,created_at)
                   SELECT l.sample_id,
                          CASE WHEN LOWER(c.source) LIKE 'strict%'
                               THEN 'history' ELSE 'ml' END,
                          c.source,c.dataset_id,c.trade_date,
                          c.decision_at,c.code,c.content_sha256,?
                   FROM ml_labels l
                   JOIN ml_candidate_samples c ON c.sample_id=l.sample_id""",
                (now,),
            )
            conn.execute(
                LABEL_TABLE_SCHEMA_V2.replace(
                    "CREATE TABLE IF NOT EXISTS ml_labels(",
                    "CREATE TABLE ml_labels_v2(",
                    1,
                )
            )
            conn.execute(
                """INSERT INTO ml_labels_v2(
                   label_id,sample_id,label_version,label_source,cost_version,
                   cost_sha256,policy_version,policy_sha256,
                   fill_label,fill_delay_sec,fill_price,
                   ret_3d_net,ret_5d_net,ret_10d_net,mfe_10d,mae_10d,
                   hit_stop,hit_take,actual_net_pnl,market_data_sha256,matured_at,
                   candidate_source,dataset_id,trade_date,decision_at,code,
                   candidate_content_sha256,fill_status,fill_evidence_sha256,reference_qty,
                   reference_notional_yuan,reference_trade_notional_yuan,fill_matured_at,
                   quality_status,quality_reasons_json,flags_json,
                   failure_reasons_json,content_sha256,updated_at)
                   SELECT l.sample_id,l.sample_id,l.label_version,l.label_source,l.cost_version,
                          '', 'legacy_v1', '',
                          l.fill_label,l.fill_delay_sec,l.fill_price,
                          l.ret_3d_net,l.ret_5d_net,l.ret_10d_net,l.mfe_10d,l.mae_10d,
                          l.hit_stop,l.hit_take,l.actual_net_pnl,
                          l.market_data_sha256,l.matured_at,
                          COALESCE(c.source,''),COALESCE(c.dataset_id,''),
                          COALESCE(c.trade_date,''),c.decision_at,COALESCE(c.code,''),
                          COALESCE(c.content_sha256,''),
                          CASE WHEN l.fill_label=1 THEN 'filled'
                               WHEN l.fill_label=0 THEN 'not_filled'
                               ELSE 'pending' END,
                          '',100,10000,NULL,NULL,
                          CASE WHEN l.fill_label IS NOT NULL
                                  OR l.fill_delay_sec IS NOT NULL
                                  OR l.fill_price IS NOT NULL
                                  OR l.ret_3d_net IS NOT NULL
                                  OR l.ret_5d_net IS NOT NULL
                                  OR l.ret_10d_net IS NOT NULL
                                  OR l.mfe_10d IS NOT NULL
                                  OR l.mae_10d IS NOT NULL
                                  OR l.hit_stop IS NOT NULL
                                  OR l.hit_take IS NOT NULL
                                  OR l.actual_net_pnl IS NOT NULL
                                  OR l.matured_at IS NOT NULL
                               THEN 'failed'
                               ELSE 'pending' END,
                          CASE WHEN l.fill_label IS NOT NULL
                                  OR l.fill_delay_sec IS NOT NULL
                                  OR l.fill_price IS NOT NULL
                                  OR l.ret_3d_net IS NOT NULL
                                  OR l.ret_5d_net IS NOT NULL
                                  OR l.ret_10d_net IS NOT NULL
                                  OR l.mfe_10d IS NOT NULL
                                  OR l.mae_10d IS NOT NULL
                                  OR l.hit_stop IS NOT NULL
                                  OR l.hit_take IS NOT NULL
                                  OR l.actual_net_pnl IS NOT NULL
                                  OR l.matured_at IS NOT NULL
                               THEN '["LEGACY_AMBIGUOUS_MATURITY"]'
                               ELSE '[]' END,
                          '[]',
                          CASE WHEN l.fill_label IS NOT NULL
                                  OR l.fill_delay_sec IS NOT NULL
                                  OR l.fill_price IS NOT NULL
                                  OR l.ret_3d_net IS NOT NULL
                                  OR l.ret_5d_net IS NOT NULL
                                  OR l.ret_10d_net IS NOT NULL
                                  OR l.mfe_10d IS NOT NULL
                                  OR l.mae_10d IS NOT NULL
                                  OR l.hit_stop IS NOT NULL
                                  OR l.hit_take IS NOT NULL
                                  OR l.actual_net_pnl IS NOT NULL
                                  OR l.matured_at IS NOT NULL
                               THEN '["LEGACY_AMBIGUOUS_MATURITY"]'
                               ELSE '[]' END,
                          '',?
                   FROM ml_labels l
                   LEFT JOIN ml_candidate_samples c ON c.sample_id=l.sample_id""",
                (now,),
            )
            _backfill_label_content_hashes(conn, "ml_labels_v2")
            conn.execute("DROP TABLE ml_labels")
            conn.execute("ALTER TABLE ml_labels_v2 RENAME TO ml_labels")
            for statement in _schema_statements(LABEL_HORIZON_SCHEMA_V2):
                conn.execute(statement)
            conn.execute(LABEL_DOWNSIDE_SCHEMA_V2)
            conn.execute("ALTER TABLE ml_predictions RENAME TO ml_predictions_v1")
            conn.execute(PREDICTION_SCHEMA_V2)
            conn.execute(
                """INSERT INTO ml_predictions(
                   sample_id,model_id,expected_ret_3d,expected_ret_5d,
                   expected_ret_10d,downside_risk,fill_probability,ml_score,
                   ml_filter,position_multiplier,confidence,feature_coverage,
                   max_feature_psi,drift_status,reasons_json,created_at)
                   SELECT sample_id,model_id,expected_ret_3d,expected_ret_5d,
                          expected_ret_10d,downside_risk,fill_probability,ml_score,
                          ml_filter,position_multiplier,confidence,NULL,NULL,
                          'unknown','[]',created_at
                   FROM ml_predictions_v1"""
            )
            conn.execute("DROP TABLE ml_predictions_v1")
            conn.execute("ALTER TABLE ml_runtime_state RENAME TO ml_runtime_state_v1")
            conn.execute(RUNTIME_STATE_SCHEMA_V2)
            conn.execute(
                """INSERT INTO ml_runtime_state(
                   singleton,active_model_id,permission_level,updated_at,
                   health_status,health_reason,last_attempt_at,last_success_at,
                   last_prediction_count,last_trading_equivalent)
                   SELECT singleton,active_model_id,permission_level,updated_at,
                          'unknown','',NULL,NULL,0,NULL
                   FROM ml_runtime_state_v1"""
            )
            conn.execute("DROP TABLE ml_runtime_state_v1")
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, now),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a SQLite-enforced read-only snapshot transaction."""
        with self._connect_readonly() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def schema_version(self) -> int:
        with self._connect_readonly() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0) if row is not None else 0

    def counts(self) -> dict[str, int]:
        with self._connect_readonly() as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ML_TABLES
            }

    def record_candidates(self, samples: list[CandidateSample]) -> int:
        changed = 0
        rows = []
        for sample in samples:
            features_json = _canonical_json(sample.features)
            content_sha256 = canonical_hash(sample)
            rows.append(
                (
                    sample,
                    (
                        sample.sample_id,
                        sample.source,
                        sample.dataset_id,
                        sample.trade_date,
                        sample.decision_at,
                        sample.code,
                        sample.strategy_version,
                        sample.parameter_version,
                        sample.feature_schema_version,
                        features_json,
                        int(sample.selected),
                        sample.rejection_stage,
                        sample.rejection_code,
                        sample.final_action,
                        sample.universe_hash,
                        sample.market_data_version,
                        sample.code_hash,
                        sample.generator_hash,
                        content_sha256,
                        _now(),
                    ),
                )
            )
        if not rows:
            return 0
        reserved_bytes = _write_reserve_bytes(
            (values for _, values in rows), len(rows), btrees_per_write=3
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
            for sample, values in rows:
                content_sha256 = str(values[-2])
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO ml_candidate_samples(
                       sample_id, source, dataset_id, trade_date, decision_at, code,
                       strategy_version, parameter_version, feature_schema_version,
                       features_json, selected, rejection_stage, rejection_code,
                       final_action, universe_hash, market_data_version, code_hash,
                       generator_hash, content_sha256, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                if cursor.rowcount == 1:
                    changed += 1
                    continue
                row = conn.execute(
                    "SELECT content_sha256 FROM ml_candidate_samples WHERE sample_id=?",
                    (sample.sample_id,),
                ).fetchone()
                if row is None or str(row[0]) != content_sha256:
                    raise MlDataConflict(
                        f"immutable candidate conflict: {sample.sample_id}"
                    )
        return changed

    def upsert_labels(self, labels: list[LabelRecord]) -> int:
        changed = 0
        columns = LABEL_MAIN_COLUMNS
        rows = []
        for label in labels:
            incoming: dict[str, object] = {}
            for column in columns:
                if column == "quality_reasons_json":
                    value = _canonical_json(label.quality_reasons)
                elif column == "flags_json":
                    value = _canonical_json(label.flags)
                elif column == "failure_reasons_json":
                    value = _canonical_json(label.failure_reasons)
                elif column == "content_sha256":
                    value = ""
                elif column == "updated_at":
                    value = _now()
                else:
                    value = (
                        label.sample_id
                        if column == "sample_id"
                        else getattr(label, column, None)
                    )
                incoming[column] = value
            _project_authoritative_label_facts(label, incoming)
            rows.append((label, tuple(incoming[column] for column in columns)))
        if not rows:
            return 0
        reserved_bytes = _label_write_reserve_bytes(rows)
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
            for label, values in rows:
                incoming = dict(zip(columns, values))
                _bind_label_identity(conn, label, incoming)
                incoming["content_sha256"] = _label_row_hash(incoming)
                row = conn.execute(
                    f"SELECT {','.join(columns)} FROM ml_labels WHERE label_id=?",
                    (label.label_id,),
                ).fetchone()
                main_changed = row is None
                if row is not None:
                    existing = dict(zip(columns, tuple(row)))
                    if existing["content_sha256"] == incoming["content_sha256"]:
                        main_changed = False
                    elif _is_v2_label(label):
                        incoming = _merge_label_v2(existing, incoming)
                        incoming["content_sha256"] = _label_row_hash(incoming)
                        if all(
                            existing[column] == incoming[column]
                            for column in columns
                            if column != "updated_at"
                        ):
                            main_changed = False
                        else:
                            main_changed = True
                    elif existing["policy_version"] == "legacy_v1":
                        _validate_migrated_v1_replay(existing, incoming)
                        main_changed = False
                    else:
                        main_changed = True
                if main_changed:
                    values = tuple(incoming[column] for column in columns)
                    conn.execute(
                        f"""INSERT INTO ml_labels(label_id,{','.join(columns)})
                            VALUES (?,{','.join('?' for _ in columns)})
                            ON CONFLICT(label_id) DO UPDATE SET
                            {','.join(f'{column}=excluded.{column}' for column in columns)}""",
                        (label.label_id, *values),
                    )
                horizon_changed = _upsert_label_horizons(conn, label)
                downside_changed = _upsert_label_downside(conn, label)
                if main_changed or horizon_changed or downside_changed:
                    changed += 1
        return changed

    def training_label_rows(
        self,
        *,
        dataset_id: str,
        label_source: str,
        label_version: str,
        cost_sha256: str,
        policy_sha256: str,
        cursor: str | None = None,
        limit: int = TRAINING_LABEL_PAGE_LIMIT,
    ) -> list[dict[str, object]]:
        """Read one contract-bound page of authoritative label facts."""

        filters = {
            "dataset_id": dataset_id,
            "label_source": label_source,
            "label_version": label_version,
            "cost_sha256": cost_sha256,
            "policy_sha256": policy_sha256,
        }
        normalized_filters = {
            name: str(value).strip() for name, value in filters.items()
        }
        missing = [name for name, value in normalized_filters.items() if not value]
        if missing:
            raise ValueError(
                "ML_TRAINING_LABEL_CONTRACT_REQUIRED: " + ",".join(sorted(missing))
            )
        if type(limit) is not int or not 1 <= limit <= TRAINING_LABEL_PAGE_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {TRAINING_LABEL_PAGE_LIMIT}"
            )
        normalized_cursor = "" if cursor is None else str(cursor).strip()
        with self.transaction() as conn:
            main_rows = conn.execute(
                """SELECT l.*
                   FROM ml_labels l
                   JOIN ml_label_subjects s ON s.sample_id=l.sample_id
                   WHERE l.dataset_id=? AND s.dataset_id=?
                     AND l.label_source=? AND l.label_version=?
                     AND l.cost_sha256=? AND l.policy_sha256=?
                     AND l.label_id>?
                   ORDER BY l.label_id
                   LIMIT ?""",
                (
                    normalized_filters["dataset_id"],
                    normalized_filters["dataset_id"],
                    normalized_filters["label_source"],
                    normalized_filters["label_version"],
                    normalized_filters["cost_sha256"],
                    normalized_filters["policy_sha256"],
                    normalized_cursor,
                    limit,
                ),
            ).fetchall()
            if not main_rows:
                return []
            label_ids = [str(row["label_id"]) for row in main_rows]
            horizons = _read_label_children(
                conn,
                table="ml_label_horizons",
                label_ids=label_ids,
                order_by="label_id,horizon_days",
            )
            downside = _read_label_children(
                conn,
                table="ml_label_downside",
                label_ids=label_ids,
                order_by="label_id",
            )
        horizons_by_label: dict[str, dict[int, sqlite3.Row]] = {}
        for row in horizons:
            horizons_by_label.setdefault(str(row["label_id"]), {})[
                int(row["horizon_days"])
            ] = row
        downside_by_label = {str(row["label_id"]): row for row in downside}
        return [
            _training_label_projection(
                row,
                horizons_by_label.get(str(row["label_id"]), {}),
                downside_by_label.get(str(row["label_id"])),
            )
            for row in main_rows
        ]

    def record_predictions(self, predictions: list[PredictionRecord]) -> int:
        changed = 0
        columns = (
            "expected_ret_3d",
            "expected_ret_5d",
            "expected_ret_10d",
            "downside_risk",
            "fill_probability",
            "ml_score",
            "ml_filter",
            "position_multiplier",
            "confidence",
            "feature_coverage",
            "max_feature_psi",
            "drift_status",
            "reasons_json",
            "created_at",
        )
        rows = [
            (
                prediction,
                tuple(
                    int(value)
                    if column == "ml_filter" and value is not None
                    else _canonical_json(prediction.reasons)
                    if column == "reasons_json"
                    else value
                    for column, value in (
                        (
                            column,
                            getattr(
                                prediction,
                                "reasons" if column == "reasons_json" else column,
                            ),
                        )
                        for column in columns
                    )
                ),
            )
            for prediction in predictions
        ]
        if not rows:
            return 0
        reserved_bytes = _write_reserve_bytes(
            (
                (prediction.sample_id, prediction.model_id, *values)
                for prediction, values in rows
            ),
            len(rows),
            btrees_per_write=2,
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
            for prediction, values in rows:
                cursor = conn.execute(
                    f"""INSERT OR IGNORE INTO ml_predictions(
                       sample_id,model_id,{','.join(columns)})
                       VALUES (?,?,{','.join('?' for _ in columns)})""",
                    (prediction.sample_id, prediction.model_id, *values),
                )
                if cursor.rowcount == 1:
                    changed += 1
                    continue
                row = conn.execute(
                    f"SELECT {','.join(columns)} FROM ml_predictions WHERE sample_id=? AND model_id=?",
                    (prediction.sample_id, prediction.model_id),
                ).fetchone()
                if row is None or tuple(row) != values:
                    raise MlDataConflict(
                        "immutable prediction conflict: "
                        f"{prediction.sample_id}/{prediction.model_id}"
                    )
        return changed

    def recent_prediction_candidate_rows(
        self,
        model_id: str,
        *,
        batch_limit: int = 20,
        row_limit: int = 1_000,
    ) -> list[dict[str, object]]:
        """Return bounded recent candidate features that have model predictions."""

        normalized_model_id = str(model_id).strip()
        if not normalized_model_id:
            raise ValueError("model_id is required")
        if type(batch_limit) is not int or not 1 <= batch_limit <= 100:
            raise ValueError("batch_limit must be between 1 and 100")
        if type(row_limit) is not int or not 1 <= row_limit <= 5_000:
            raise ValueError("row_limit must be between 1 and 5000")
        with self.transaction() as conn:
            rows = conn.execute(
                """WITH recent_batches AS (
                     SELECT c.decision_at
                     FROM ml_predictions p
                     JOIN ml_candidate_samples c ON c.sample_id=p.sample_id
                     WHERE p.model_id=?
                     GROUP BY c.decision_at
                     ORDER BY c.decision_at DESC
                     LIMIT ?
                   ), recent_rows AS (
                     SELECT c.sample_id,c.decision_at,c.code,c.features_json
                     FROM ml_predictions p
                     JOIN ml_candidate_samples c ON c.sample_id=p.sample_id
                     JOIN recent_batches b ON b.decision_at=c.decision_at
                     WHERE p.model_id=?
                     ORDER BY c.decision_at DESC,c.sample_id DESC
                     LIMIT ?
                   )
                   SELECT sample_id,decision_at,code,features_json
                   FROM recent_rows
                   ORDER BY decision_at,sample_id""",
                (normalized_model_id, batch_limit, normalized_model_id, row_limit),
            ).fetchall()
        return [_expand_candidate_feature_row(row) for row in rows]

    def register_model(
        self,
        manifest: ModelManifest | object,
        *,
        artifact_path: str,
        status: str | None = None,
        artifact_sha256: str | None = None,
    ) -> bool:
        manifest_payload = _model_manifest_payload(manifest)
        model_id = str(manifest_payload.get("model_id") or "").strip()
        if not model_id:
            raise ValueError("model manifest model_id is required")
        parent_model_id_value = manifest_payload.get("parent_model_id")
        parent_model_id = (
            None
            if parent_model_id_value in (None, "")
            else str(parent_model_id_value).strip()
        )
        created_at = _aware_iso(
            str(manifest_payload.get("created_at") or ""), "created_at"
        )
        manifest_artifact = str(
            getattr(manifest, "artifact_sha256", "") or ""
        ).strip()
        explicit_artifact = str(artifact_sha256 or "").strip()
        if manifest_artifact and explicit_artifact and manifest_artifact != explicit_artifact:
            raise ValueError("MODEL_ARTIFACT_HASH_ARGUMENT_MISMATCH")
        resolved_artifact = explicit_artifact or manifest_artifact
        if not resolved_artifact:
            raise ValueError("artifact_sha256 is required for this model manifest")
        resolved_status = str(
            status
            if status is not None
            else manifest_payload.get("status") or "challenger"
        ).strip()
        if not resolved_status:
            raise ValueError("model status is required")
        _parameter_bytes(resolved_status)
        normalized_artifact_path = _normalized_model_artifact_path(artifact_path)
        manifest_json = _canonical_json(manifest_payload)
        values = (
            parent_model_id,
            resolved_status,
            normalized_artifact_path,
            resolved_artifact,
            manifest_json,
            created_at,
        )
        reserved_bytes = _write_reserve_bytes(
            ((model_id, *values),), 1, btrees_per_write=3
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
            row = conn.execute(
                """SELECT parent_model_id,status,artifact_path,artifact_sha256,
                          manifest_json,created_at FROM ml_models WHERE model_id=?""",
                (model_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) != values:
                    raise MlDataConflict(
                        f"immutable model conflict: {model_id}"
                    )
                return False
            try:
                conn.execute(
                    """INSERT INTO ml_models(
                       model_id,parent_model_id,status,artifact_path,artifact_sha256,
                       manifest_json,created_at) VALUES (?,?,?,?,?,?,?)""",
                    (model_id, *values),
                )
            except sqlite3.IntegrityError as exc:
                raise MlDataConflict(
                    f"artifact already registered: {resolved_artifact}"
                ) from exc
        return True

    def model_record(self, model_id: str) -> dict[str, object] | None:
        normalized_model_id = str(model_id).strip()
        if not normalized_model_id:
            raise ValueError("model_id is required")
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT model_id,parent_model_id,status,artifact_path,
                          artifact_sha256,manifest_json,created_at
                   FROM ml_models WHERE model_id=?""",
                (normalized_model_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            manifest = json.loads(str(row["manifest_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid persisted model manifest JSON") from exc
        if not isinstance(manifest, dict):
            raise ValueError("invalid persisted model manifest JSON")
        result: dict[str, object] = dict(manifest)
        result.update(
            {
                "model_id": row["model_id"],
                "parent_model_id": row["parent_model_id"],
                "status": row["status"],
                "artifact_path": row["artifact_path"],
                "artifact_sha256": row["artifact_sha256"],
                "created_at": row["created_at"],
                "manifest": manifest,
            }
        )
        return result

    def record_model_event(
        self,
        *,
        event_id: str,
        model_id: str,
        action: str,
        old_level: int,
        new_level: int,
        artifact_sha256: str,
        reason: str,
        operator: str,
        created_at: str,
    ) -> bool:
        for value in (
            event_id,
            model_id,
            action,
            old_level,
            new_level,
            artifact_sha256,
            reason,
            operator,
            created_at,
        ):
            _parameter_bytes(value)
        created_at = _aware_iso(created_at, "created_at")
        values = (
            str(model_id),
            str(action),
            int(old_level),
            int(new_level),
            str(artifact_sha256),
            str(reason),
            str(operator),
            created_at,
        )
        reserved_bytes = _write_reserve_bytes(
            ((event_id, *values),), 1, btrees_per_write=2
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
            model = conn.execute(
                "SELECT artifact_sha256 FROM ml_models WHERE model_id=?", (values[0],)
            ).fetchone()
            if model is not None and str(model[0]) != str(artifact_sha256):
                raise MlDataConflict(f"model event artifact mismatch: {model_id}")
            row = conn.execute(
                """SELECT model_id,action,old_level,new_level,artifact_sha256,
                          reason,operator,created_at FROM ml_model_events WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) != values:
                    raise MlDataConflict(f"immutable model event conflict: {event_id}")
                return False
            conn.execute(
                """INSERT INTO ml_model_events(
                   event_id,model_id,action,old_level,new_level,artifact_sha256,
                   reason,operator,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (event_id, *values),
            )
        return True

    def approved_model_event(
        self, model_id: str, artifact_sha256: str
    ) -> dict[str, object] | None:
        normalized_model_id = str(model_id).strip()
        normalized_artifact = str(artifact_sha256).strip()
        if not normalized_model_id or not normalized_artifact:
            raise ValueError("model_id and artifact_sha256 are required")
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT event_id,model_id,action,old_level,new_level,
                          artifact_sha256,reason,operator,created_at
                   FROM ml_model_events
                   WHERE model_id=? AND artifact_sha256=? AND action='approve'
                   ORDER BY created_at DESC,event_id DESC
                   LIMIT 1""",
                (normalized_model_id, normalized_artifact),
            ).fetchone()
        return None if row is None else dict(row)

    def runtime_state(self) -> dict[str, object]:
        with self._connect_readonly() as conn:
            row = conn.execute(
                """SELECT active_model_id,permission_level,updated_at,
                          health_status,health_reason,last_attempt_at,
                          last_success_at,last_prediction_count,
                          last_trading_equivalent
                   FROM ml_runtime_state WHERE singleton=1"""
            ).fetchone()
        if row is None:
            raise RuntimeError("ML runtime state is not initialized")
        return {
            "active_model_id": row[0],
            "permission_level": int(row[1]),
            "updated_at": str(row[2]),
        }

    def runtime_health(self) -> dict[str, object]:
        with self._connect_readonly() as conn:
            row = conn.execute(
                """SELECT health_status,health_reason,last_attempt_at,
                          last_success_at,last_prediction_count,
                          last_trading_equivalent
                   FROM ml_runtime_state WHERE singleton=1"""
            ).fetchone()
        if row is None:
            raise RuntimeError("ML runtime state is not initialized")
        return {
            "health_status": str(row[0]),
            "health_reason": str(row[1] or ""),
            "last_attempt_at": row[2],
            "last_success_at": row[3],
            "last_prediction_count": int(row[4]),
            "last_trading_equivalent": (
                None if row[5] is None else bool(row[5])
            ),
        }

    def record_runtime_health(
        self,
        *,
        status: str,
        reason: str,
        attempted_at: str,
        prediction_count: int,
        successful: bool,
        trading_equivalent: bool | None,
    ) -> None:
        normalized_status = str(status).strip().lower()
        if normalized_status not in {
            "unknown", "ok", "fallback", "disabled", "no_model"
        }:
            raise ValueError("runtime health status is invalid")
        normalized_reason = " ".join(str(reason or "").split())
        if len(normalized_reason) > 512:
            raise ValueError("runtime health reason exceeds 512 characters")
        attempted = _aware_iso(attempted_at, "attempted_at")
        if isinstance(prediction_count, bool) or int(prediction_count) < 0:
            raise ValueError("prediction_count must be non-negative")
        if trading_equivalent is not None and not isinstance(
            trading_equivalent, bool
        ):
            raise ValueError("trading_equivalent must be boolean or None")
        values = (
            normalized_status,
            normalized_reason,
            attempted,
            attempted if successful else None,
            int(prediction_count),
            None if trading_equivalent is None else int(trading_equivalent),
        )
        reserved_bytes = (
            sum(_parameter_bytes(value) for value in values)
            + RUNTIME_RESERVE_BYTES
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=0,
        ) as conn:
            cursor = conn.execute(
                """UPDATE ml_runtime_state
                   SET health_status=?,health_reason=?,last_attempt_at=?,
                       last_success_at=CASE WHEN ? IS NULL
                         THEN last_success_at ELSE ? END,
                       last_prediction_count=?,last_trading_equivalent=?
                   WHERE singleton=1""",
                (
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[3],
                    values[4],
                    values[5],
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("ML runtime state is not initialized")

    def compare_and_swap_runtime(
        self,
        *,
        expected_model_id: str | None,
        expected_permission_level: int,
        new_model_id: str | None,
        new_permission_level: int,
        updated_at: str,
    ) -> bool:
        for value in (
            expected_model_id,
            expected_permission_level,
            new_model_id,
            new_permission_level,
            updated_at,
        ):
            _parameter_bytes(value)
        updated_at = _aware_iso(updated_at, "updated_at")
        if not 0 <= int(new_permission_level) <= 3:
            raise ValueError("permission level must be between 0 and 3")
        parameters = (
            new_model_id,
            int(new_permission_level),
            updated_at,
            expected_model_id,
            int(expected_permission_level),
        )
        reserved_bytes = (
            2 * sum(_parameter_bytes(value) for value in parameters)
            + RUNTIME_RESERVE_BYTES
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=0,
        ) as conn:
            cursor = conn.execute(
                """UPDATE ml_runtime_state
                   SET active_model_id=?, permission_level=?, updated_at=?
                   WHERE singleton=1 AND active_model_id IS ? AND permission_level=?""",
                parameters,
            )
        return cursor.rowcount == 1

    def transition_runtime_with_event(
        self,
        *,
        expected_model_id: str | None,
        expected_permission_level: int,
        new_model_id: str | None,
        new_permission_level: int,
        updated_at: str,
        event_id: str,
        event_model_id: str,
        action: str,
        event_old_level: int,
        event_new_level: int,
        artifact_sha256: str,
        reason: str,
        operator: str,
        created_at: str,
    ) -> bool:
        """CAS-update runtime state and append its audit event atomically."""

        values = (
            expected_model_id,
            expected_permission_level,
            new_model_id,
            new_permission_level,
            updated_at,
            event_id,
            event_model_id,
            action,
            event_old_level,
            event_new_level,
            artifact_sha256,
            reason,
            operator,
            created_at,
        )
        for value in values:
            _parameter_bytes(value)
        updated_at = _aware_iso(updated_at, "updated_at")
        created_at = _aware_iso(created_at, "created_at")
        if not 0 <= int(new_permission_level) <= 3:
            raise ValueError("permission level must be between 0 and 3")
        event_values = (
            str(event_model_id), str(action), int(event_old_level),
            int(event_new_level), str(artifact_sha256), str(reason),
            str(operator), created_at,
        )
        runtime_values = (
            new_model_id, int(new_permission_level), updated_at,
            expected_model_id, int(expected_permission_level),
        )
        reserved_bytes = (
            _write_reserve_bytes(((event_id, *event_values),), 1, btrees_per_write=2)
            + RUNTIME_RESERVE_BYTES
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
            cursor = conn.execute(
                """UPDATE ml_runtime_state
                   SET active_model_id=?, permission_level=?, updated_at=?
                   WHERE singleton=1 AND active_model_id IS ? AND permission_level=?""",
                runtime_values,
            )
            if cursor.rowcount != 1:
                return False
            model = conn.execute(
                "SELECT artifact_sha256 FROM ml_models WHERE model_id=?",
                (event_values[0],),
            ).fetchone()
            if model is not None and str(model[0]) != str(artifact_sha256):
                raise MlDataConflict(
                    f"model event artifact mismatch: {event_model_id}"
                )
            existing = conn.execute(
                """SELECT model_id,action,old_level,new_level,artifact_sha256,
                          reason,operator,created_at
                   FROM ml_model_events WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != event_values:
                    raise MlDataConflict(
                        f"immutable model event conflict: {event_id}"
                    )
                return True
            conn.execute(
                """INSERT INTO ml_model_events(
                   event_id,model_id,action,old_level,new_level,artifact_sha256,
                   reason,operator,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (event_id, *event_values),
            )
        return True

    def backup_to(self, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connect_readonly() as source, closing(
            sqlite3.connect(destination)
        ) as target:
            source.backup(target)

    def integrity_check(self) -> str:
        with self._connect_readonly() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row is not None else "missing"

    @contextmanager
    def _audited_write(
        self,
        *,
        reserved_bytes: int,
        growth_bytes: int,
        initialize: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        self._raw_write_preflight(reserved_bytes)
        with self._connect_writable() as conn:
            if initialize:
                conn.execute("PRAGMA journal_mode=WAL")
            with self._write_transaction(
                conn,
                reserved_bytes=reserved_bytes,
                growth_bytes=growth_bytes,
            ):
                yield conn

    def _raw_write_preflight(self, reserved_bytes: int) -> None:
        if reserved_bytes <= 0:
            raise ValueError("invalid write capacity reservation")
        main_bytes = self._file_size(self.path)
        wal_bytes = self._file_size(Path(f"{self.path}-wal"))
        shm_bytes = self._file_size(Path(f"{self.path}-shm"))
        data_bytes = main_bytes + wal_bytes
        if main_bytes == 0:
            required_bytes = data_bytes + _new_store_minimum_bytes(reserved_bytes)
        else:
            required_bytes = (
                data_bytes
                + max(0, 32 - wal_bytes)
                + reserved_bytes
            )
        if data_bytes > self.max_bytes or required_bytes > self.max_bytes:
            raise MlCapacityError(
                "ML database capacity reached before SQLite open: "
                f"data={data_bytes}, shm={shm_bytes}, required={required_bytes}, "
                f"limit={self.max_bytes}"
            )

    @contextmanager
    def _write_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        reserved_bytes: int,
        growth_bytes: int,
    ) -> Iterator[None]:
        if reserved_bytes <= 0 or growth_bytes < 0:
            raise ValueError("invalid write capacity reservation")
        self._ensure_capacity(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._set_max_page_count(conn)
            self._ensure_capacity(
                conn,
                reserve_bytes=reserved_bytes,
                growth_bytes=growth_bytes,
            )
            yield
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if _is_sqlite_full(exc):
                raise MlCapacityError(
                    f"ML database capacity reached: {self.max_bytes} bytes"
                ) from exc
            raise
        except Exception:
            conn.rollback()
            raise
        self._checkpoint(conn)

    def _set_max_page_count(self, conn: sqlite3.Connection) -> None:
        page_size = self._page_size(conn)
        max_pages = max(1, self.max_bytes // page_size)
        actual = int(
            conn.execute(f"PRAGMA max_page_count={max_pages}").fetchone()[0]
        )
        if actual * page_size > self.max_bytes:
            raise MlCapacityError(
                "ML logical database exceeds capacity: "
                f"{actual * page_size} > {self.max_bytes}"
            )

    def _ensure_capacity(
        self,
        conn: sqlite3.Connection,
        *,
        reserve_bytes: int = 0,
        growth_bytes: int = 0,
    ) -> None:
        page_size = self._page_size(conn)
        page_count = self._page_count(conn)
        logical_bytes = page_count * page_size
        main_bytes = self._file_size(self.path)
        wal_bytes = self._file_size(Path(f"{self.path}-wal"))
        shm_bytes = self._file_size(Path(f"{self.path}-shm"))
        data_bytes = main_bytes + wal_bytes
        if logical_bytes > self.max_bytes or data_bytes > self.max_bytes:
            raise MlCapacityError(
                "ML database capacity reached: "
                f"logical={logical_bytes}, data={data_bytes}, shm={shm_bytes}, "
                f"limit={self.max_bytes}"
            )
        if reserve_bytes <= 0:
            return

        reserved_pages = (reserve_bytes + page_size - 1) // page_size
        growth_pages = (growth_bytes + page_size - 1) // page_size
        frame_bytes = page_size + 24
        commit_wal_bytes = (32 if wal_bytes == 0 else 0) + reserved_pages * frame_bytes
        commit_data_bytes = (
            main_bytes
            + wal_bytes
            + commit_wal_bytes
        )
        future_main_bytes = max(
            main_bytes, logical_bytes + growth_pages * page_size
        )
        checkpoint_data_bytes = (
            future_main_bytes
            + wal_bytes
            + commit_wal_bytes
        )
        conservative_data_bytes = max(commit_data_bytes, checkpoint_data_bytes)
        if conservative_data_bytes > self.max_bytes:
            raise MlCapacityError(
                "ML transaction would exceed capacity: "
                f"logical={logical_bytes}, "
                f"reserved={reserve_bytes}, "
                f"shm={shm_bytes}, "
                f"conservative_data={conservative_data_bytes}, "
                f"limit={self.max_bytes}"
            )

    def _checkpoint(self, conn: sqlite3.Connection) -> bool:
        try:
            if not self._wal_has_content():
                return True
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return (
                row is not None
                and int(row[0]) == 0
                and not self._wal_has_content()
            )
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return False

    def _wal_has_content(self) -> bool:
        return self._file_size(Path(f"{self.path}-wal")) > 0

    @staticmethod
    def _page_size(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA page_size").fetchone()[0])

    @staticmethod
    def _page_count(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA page_count").fetchone()[0])

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0


def _label_write_reserve_bytes(
    rows: list[tuple[LabelRecord, tuple[object, ...]]],
) -> int:
    parameter_bytes = 0
    horizon_count = 0
    downside_count = 0
    for label, values in rows:
        parameter_bytes += sum(
            _parameter_bytes(value) for value in (label.label_id, *values)
        )
        for horizon in label.horizons:
            horizon_count += 1
            parameter_bytes += _parameter_bytes(label.label_id)
            parameter_bytes += _parameter_bytes(label.sample_id)
            parameter_bytes += _parameter_bytes(_canonical_json(horizon))
        if label.downside is not None:
            downside_count += 1
            parameter_bytes += _parameter_bytes(label.label_id)
            parameter_bytes += _parameter_bytes(label.sample_id)
            parameter_bytes += _parameter_bytes(_canonical_json(label.downside))
    return (
        2 * parameter_bytes
        + LABEL_WRITE_BTREE_COUNT * BTREE_RESERVE_BYTES
        + len(rows) * LABEL_MAIN_ROW_RESERVE_BYTES
        + (horizon_count + downside_count) * LABEL_CHILD_ROW_RESERVE_BYTES
    )


def _normalized_model_artifact_path(value: str) -> str:
    raw = str(value)
    if not raw or raw != raw.strip() or "\\" in raw:
        raise ValueError("MODEL_ARTIFACT_PATH_MUST_BE_CANONICAL_RELATIVE_DIRECTORY")
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw)
    if windows.drive or windows.is_absolute() or posix.is_absolute():
        raise ValueError("MODEL_ARTIFACT_PATH_MUST_BE_CANONICAL_RELATIVE_DIRECTORY")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError("MODEL_ARTIFACT_PATH_MUST_BE_CANONICAL_RELATIVE_DIRECTORY")
    normalized = posix.as_posix()
    if normalized != raw or normalized in {"", "."}:
        raise ValueError("MODEL_ARTIFACT_PATH_MUST_BE_CANONICAL_RELATIVE_DIRECTORY")
    _parameter_bytes(normalized)
    return normalized


def _model_manifest_payload(manifest: object) -> dict[str, object]:
    as_dict = getattr(manifest, "as_dict", None)
    raw = as_dict() if callable(as_dict) else _json_value(manifest)
    if not isinstance(raw, Mapping):
        raise TypeError("model manifest must be a mapping or dataclass")
    normalized = _json_value(raw)
    if not isinstance(normalized, dict):
        raise TypeError("model manifest must serialize to an object")
    return normalized


def _project_authoritative_label_facts(
    label: LabelRecord, incoming: dict[str, object]
) -> None:
    if not _is_v2_label(label):
        return
    if incoming["fill_matured_at"] is not None:
        incoming["fill_matured_at"] = _aware_iso(
            str(incoming["fill_matured_at"]), "fill_matured_at"
        )

    horizons = {horizon.horizon_days: horizon for horizon in label.horizons}
    authoritative_maturities: list[str] = []
    if incoming["fill_matured_at"] is not None:
        authoritative_maturities.append(str(incoming["fill_matured_at"]))
    for days in (3, 5, 10):
        horizon = horizons.get(days)
        fields = {
            f"ret_{days}d_gross": None if horizon is None else horizon.gross_return,
            f"ret_{days}d_net": None if horizon is None else horizon.net_return,
            f"matured_{days}d_at": None if horizon is None else horizon.matured_at,
        }
        for field, value in fields.items():
            _project_authoritative_fact(incoming, field, value)
        if horizon is not None:
            authoritative_maturities.append(horizon.matured_at)

    primary = horizons.get(5)
    primary_costs: dict[str, float | None]
    if primary is None:
        primary_costs = {
            "buy_cost": None,
            "sell_cost": None,
            "slippage_cost": None,
            "commission_cost": None,
            "stamp_tax_cost": None,
            "transfer_fee_cost": None,
            "other_fee_cost": None,
            "net_cost": None,
        }
    else:
        primary_costs = {
            "buy_cost": (
                primary.buy_commission_yuan
                + primary.buy_transfer_fee_yuan
                + primary.buy_other_fee_yuan
            ),
            "sell_cost": (
                primary.sell_commission_yuan
                + primary.sell_stamp_tax_yuan
                + primary.sell_transfer_fee_yuan
                + primary.sell_other_fee_yuan
            ),
            "slippage_cost": (
                primary.buy_slippage_yuan + primary.sell_slippage_yuan
            ),
            "commission_cost": (
                primary.buy_commission_yuan + primary.sell_commission_yuan
            ),
            "stamp_tax_cost": primary.sell_stamp_tax_yuan,
            "transfer_fee_cost": (
                primary.buy_transfer_fee_yuan + primary.sell_transfer_fee_yuan
            ),
            "other_fee_cost": primary.buy_other_fee_yuan + primary.sell_other_fee_yuan,
            "net_cost": primary.total_cost_yuan,
        }
    for field, value in primary_costs.items():
        _project_authoritative_fact(incoming, field, value)

    downside = label.downside
    if downside is None:
        downside_values: dict[str, object] = {
            "mfe_10d": None,
            "mae_10d": None,
            "downside_loss": None,
            "hit_stop": None,
            "hit_take": None,
            "downside_matured_at": None,
        }
        for field, value in downside_values.items():
            _project_authoritative_fact(incoming, field, value)
        for field in ("paused_path", "exit_blocked"):
            if int(incoming[field] or 0) != 0:
                raise ValueError(f"ML_LABEL_DOWNSIDE_FACT_REQUIRES_CHILD: {field}")
            incoming[field] = 0
    else:
        downside_values = {
            "mfe_10d": downside.mfe_10d_net,
            "mae_10d": downside.mae_10d_net,
            "downside_loss": downside.downside_loss,
            "hit_stop": downside.hit_stop,
            "hit_take": downside.hit_take,
            "downside_matured_at": downside.matured_at,
        }
        for field, value in downside_values.items():
            _project_authoritative_fact(incoming, field, value)
        for field in ("paused_path", "exit_blocked"):
            supplied = int(incoming[field] or 0)
            authoritative = int(getattr(downside, field))
            if supplied not in {0, authoritative}:
                raise ValueError(f"ML_LABEL_DOWNSIDE_FACT_CONFLICT: {field}")
            incoming[field] = authoritative
        if downside.matured_at is not None:
            authoritative_maturities.append(downside.matured_at)

    authoritative_matured_at = _latest_aware_iso(authoritative_maturities)
    _project_authoritative_fact(
        incoming, "matured_at", authoritative_matured_at, timestamp=True
    )


def _project_authoritative_fact(
    incoming: dict[str, object],
    field: str,
    authoritative: object,
    *,
    timestamp: bool = False,
) -> None:
    supplied = incoming[field]
    if timestamp:
        supplied = (
            None if supplied is None else _aware_iso(str(supplied), field)
        )
        authoritative = (
            None
            if authoritative is None
            else _aware_iso(str(authoritative), field)
        )
    if supplied is not None and authoritative is None:
        raise ValueError(f"ML_LABEL_FACT_REQUIRES_CHILD: {field}")
    if supplied is not None and not _facts_equal(supplied, authoritative):
        raise ValueError(f"ML_LABEL_FACT_CONFLICT: {field}")
    incoming[field] = authoritative


def _facts_equal(left: object, right: object) -> bool:
    if type(left) in {int, float} and type(right) in {int, float}:
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def _latest_aware_iso(values: Iterable[str]) -> str | None:
    normalized = [_aware_iso(str(value), "matured_at") for value in values]
    if not normalized:
        return None
    return max(normalized, key=datetime.fromisoformat)


def _is_v2_label(label: LabelRecord) -> bool:
    version = label.label_version.casefold()
    return version not in {"l1", "label-v1"} and not version.endswith("-v1")


def _bind_label_identity(
    conn: sqlite3.Connection,
    label: LabelRecord,
    incoming: dict[str, object],
) -> None:
    candidate = conn.execute(
        """SELECT source,dataset_id,trade_date,decision_at,code,content_sha256
           FROM ml_candidate_samples WHERE sample_id=?""",
        (label.sample_id,),
    ).fetchone()
    if _is_v2_label(label):
        required = (
            "candidate_source",
            "dataset_id",
            "trade_date",
            "decision_at",
            "code",
            "candidate_content_sha256",
        )
        if any(incoming.get(name) in (None, "") for name in required):
            raise ValueError("ML_SCHEMA_V2_LABEL_IDENTITY_REQUIRED")
        if not incoming.get("cost_sha256") or not incoming.get("policy_sha256"):
            raise ValueError("ML_SCHEMA_V2_LABEL_CONTRACT_HASH_REQUIRED")
        if label.candidate_origin not in {"history", "ml"}:
            raise ValueError("ML_LABEL_CANDIDATE_ORIGIN_REQUIRED")
        decision_at = _aware_iso(str(incoming["decision_at"]), "decision_at")
        if decision_at[:10] != str(incoming["trade_date"]):
            raise ValueError("ML_LABEL_TRADE_DATE_MISMATCH")
        digest = str(incoming["candidate_content_sha256"])
        if len(digest) != 64:
            raise ValueError("ML_LABEL_CANDIDATE_HASH_REQUIRED")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("ML_LABEL_CANDIDATE_HASH_REQUIRED") from exc
        if candidate is not None and tuple(candidate) != (
            incoming["candidate_source"],
            incoming["dataset_id"],
            incoming["trade_date"],
            decision_at,
            incoming["code"],
            digest,
        ):
            raise MlDataConflict(
                f"label candidate identity conflict: {label.sample_id}"
            )
        incoming["decision_at"] = decision_at
        subject_values = (
            label.candidate_origin,
            incoming["candidate_source"],
            incoming["dataset_id"],
            incoming["trade_date"],
            decision_at,
            incoming["code"],
            digest,
        )
    else:
        if candidate is None:
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        for name, value in zip(
            (
                "candidate_source",
                "dataset_id",
                "trade_date",
                "decision_at",
                "code",
                "candidate_content_sha256",
            ),
            tuple(candidate),
        ):
            incoming[name] = value
        origin = "history" if str(candidate[0]).casefold().startswith("strict") else "ml"
        subject_values = (origin, *tuple(candidate))
    subject = conn.execute(
        """SELECT candidate_origin,source,dataset_id,trade_date,decision_at,code,
                  candidate_content_sha256
           FROM ml_label_subjects WHERE sample_id=?""",
        (label.sample_id,),
    ).fetchone()
    if subject is None:
        conn.execute(
            """INSERT INTO ml_label_subjects(
               sample_id,candidate_origin,source,dataset_id,trade_date,decision_at,
               code,candidate_content_sha256,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (label.sample_id, *subject_values, _now()),
        )
    elif tuple(subject) != subject_values:
        raise MlDataConflict(f"immutable label subject conflict: {label.sample_id}")


def _label_row_hash(values: Mapping[str, object]) -> str:
    return canonical_hash(
        {
            key: value
            for key, value in values.items()
            if key not in {"content_sha256", "updated_at"}
        }
    )


def _merge_label_v2(
    existing: dict[str, object], incoming: dict[str, object]
) -> dict[str, object]:
    result = dict(incoming)
    identity_fields = (
        "sample_id",
        "label_version",
        "label_source",
        "cost_version",
        "cost_sha256",
        "policy_version",
        "policy_sha256",
        "candidate_source",
        "dataset_id",
        "trade_date",
        "decision_at",
        "code",
        "candidate_content_sha256",
        "reference_notional_yuan",
    )
    for field in identity_fields:
        if existing[field] != incoming[field]:
            raise MlDataConflict(f"immutable label identity conflict: {field}")

    immutable_when_present = (
        "fill_label",
        "fill_delay_sec",
        "fill_price",
        "fill_at",
        "fill_matured_at",
        "reference_qty",
        "reference_trade_notional_yuan",
        "ret_3d_gross",
        "ret_3d_net",
        "ret_5d_gross",
        "ret_5d_net",
        "ret_10d_gross",
        "ret_10d_net",
        "mfe_10d",
        "mae_10d",
        "downside_loss",
        "hit_stop",
        "hit_take",
        "buy_cost",
        "sell_cost",
        "slippage_cost",
        "commission_cost",
        "stamp_tax_cost",
        "transfer_fee_cost",
        "other_fee_cost",
        "net_cost",
        "actual_net_pnl",
        "matured_3d_at",
        "matured_5d_at",
        "matured_10d_at",
        "downside_matured_at",
    )
    for field in immutable_when_present:
        old = existing[field]
        new = incoming[field]
        if old is None:
            continue
        if new is None:
            result[field] = old
        elif old != new:
            raise MlDataConflict(f"immutable mature label conflict: {field}")

    old_fill_status = str(existing["fill_status"])
    new_fill_status = str(incoming["fill_status"])
    if old_fill_status != "pending" and old_fill_status != new_fill_status:
        raise MlDataConflict("immutable fill status conflict")
    old_fill_evidence = str(existing["fill_evidence_sha256"] or "")
    new_fill_evidence = str(incoming["fill_evidence_sha256"] or "")
    if old_fill_status == "pending":
        if not new_fill_evidence and old_fill_evidence:
            result["fill_evidence_sha256"] = old_fill_evidence
    elif not new_fill_evidence:
        result["fill_evidence_sha256"] = old_fill_evidence
    elif old_fill_evidence != new_fill_evidence:
        raise MlDataConflict("immutable fill evidence conflict")
    if existing["fill_reason"] and not incoming["fill_reason"]:
        result["fill_reason"] = existing["fill_reason"]
    elif (
        existing["fill_reason"]
        and incoming["fill_reason"]
        and existing["fill_reason"] != incoming["fill_reason"]
    ):
        raise MlDataConflict("immutable fill reason conflict")

    result["exit_blocked"] = max(
        int(existing["exit_blocked"] or 0), int(incoming["exit_blocked"] or 0)
    )
    result["paused_path"] = max(
        int(existing["paused_path"] or 0), int(incoming["paused_path"] or 0)
    )
    old_reasons = set(json.loads(str(existing["quality_reasons_json"])))
    new_reasons = set(json.loads(str(incoming["quality_reasons_json"])))
    result["quality_reasons_json"] = _canonical_json(old_reasons | new_reasons)
    old_flags = set(json.loads(str(existing["flags_json"])))
    new_flags = set(json.loads(str(incoming["flags_json"])))
    result["flags_json"] = _canonical_json(old_flags | new_flags)
    old_failures = set(json.loads(str(existing["failure_reasons_json"])))
    new_failures = set(json.loads(str(incoming["failure_reasons_json"])))
    result["failure_reasons_json"] = _canonical_json(
        old_failures | new_failures
    )
    rank = {"pending": 0, "partial": 1, "complete": 2, "failed": 3}
    old_quality = str(existing["quality_status"])
    new_quality = str(incoming["quality_status"])
    if old_quality == "failed" and new_quality != "failed":
        raise MlDataConflict("failed label cannot become usable")
    result["quality_status"] = (
        old_quality if rank[old_quality] > rank[new_quality] else new_quality
    )
    old_matured = existing["matured_at"]
    new_matured = incoming["matured_at"]
    if old_matured is not None and (
        new_matured is None
        or _aware_iso(str(old_matured), "matured_at")
        > _aware_iso(str(new_matured), "matured_at")
    ):
        result["matured_at"] = old_matured
    if incoming["market_data_sha256"] in (None, ""):
        result["market_data_sha256"] = existing["market_data_sha256"]
    return result


def _validate_migrated_v1_replay(
    existing: Mapping[str, object], incoming: Mapping[str, object]
) -> None:
    legacy_fields = (
        "sample_id",
        "label_version",
        "label_source",
        "cost_version",
        "fill_label",
        "fill_delay_sec",
        "fill_price",
        "ret_3d_net",
        "ret_5d_net",
        "ret_10d_net",
        "mfe_10d",
        "mae_10d",
        "hit_stop",
        "hit_take",
        "actual_net_pnl",
        "market_data_sha256",
        "matured_at",
    )
    conflicts = [
        field
        for field in legacy_fields
        if not _facts_equal(existing[field], incoming[field])
    ]
    if conflicts:
        raise MlDataConflict(
            "immutable migrated v1 label conflict: " + ",".join(conflicts)
        )


def _upsert_label_horizons(
    conn: sqlite3.Connection, label: LabelRecord
) -> bool:
    columns = (
        "gross_return",
        "net_return",
        "exit_price",
        "buy_commission_yuan",
        "buy_transfer_fee_yuan",
        "buy_other_fee_yuan",
        "buy_slippage_yuan",
        "sell_commission_yuan",
        "sell_stamp_tax_yuan",
        "sell_transfer_fee_yuan",
        "sell_other_fee_yuan",
        "sell_slippage_yuan",
        "total_cost_yuan",
        "cost_rate",
        "matured_at",
        "market_data_sha256",
        "content_sha256",
        "created_at",
    )
    changed = False
    for horizon in label.horizons:
        content_sha256 = canonical_hash(horizon)
        values = tuple(
            content_sha256
            if column == "content_sha256"
            else _now()
            if column == "created_at"
            else getattr(horizon, column)
            for column in columns
        )
        existing = conn.execute(
            """SELECT content_sha256 FROM ml_label_horizons
               WHERE label_id=? AND horizon_days=?""",
            (label.label_id, horizon.horizon_days),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != content_sha256:
                raise MlDataConflict(
                    "immutable horizon label conflict: "
                    f"{label.label_id}/D{horizon.horizon_days}"
                )
            continue
        conn.execute(
            f"""INSERT INTO ml_label_horizons(
               label_id,sample_id,horizon_days,{','.join(columns)})
               VALUES (?,?,?,{','.join('?' for _ in columns)})""",
            (label.label_id, label.sample_id, horizon.horizon_days, *values),
        )
        changed = True
    return changed


def _upsert_label_downside(
    conn: sqlite3.Connection, label: LabelRecord
) -> bool:
    downside = label.downside
    if downside is None:
        return False
    content_sha256 = canonical_hash(downside)
    existing = conn.execute(
        "SELECT content_sha256 FROM ml_label_downside WHERE label_id=?",
        (label.label_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != content_sha256:
            raise MlDataConflict(
                f"immutable downside label conflict: {label.label_id}"
            )
        return False
    conn.execute(
        """INSERT INTO ml_label_downside(
           label_id,sample_id,status,mfe_10d_net,mae_10d_net,downside_loss,
           paused_path,exit_blocked,hit_stop,hit_take,failure_reason,matured_at,
           evidence_sha256,content_sha256,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            label.label_id,
            label.sample_id,
            downside.status,
            downside.mfe_10d_net,
            downside.mae_10d_net,
            downside.downside_loss,
            downside.paused_path,
            downside.exit_blocked,
            downside.hit_stop,
            downside.hit_take,
            downside.failure_reason,
            downside.matured_at,
            downside.evidence_sha256,
            content_sha256,
            _now(),
        ),
    )
    return True


def _backfill_label_content_hashes(
    conn: sqlite3.Connection, table: str, *, batch_size: int = 500
) -> None:
    if table != "ml_labels_v2":
        raise ValueError("unsupported label hash backfill table")
    cursor = 0
    while True:
        rows = conn.execute(
            f"""SELECT rowid AS migration_rowid,{','.join(LABEL_MAIN_COLUMNS)}
                FROM {table}
                WHERE rowid>?
                ORDER BY rowid
                LIMIT ?""",
            (cursor, batch_size),
        ).fetchall()
        if not rows:
            return
        updates = []
        for row in rows:
            values = {column: row[column] for column in LABEL_MAIN_COLUMNS}
            updates.append((_label_row_hash(values), int(row["migration_rowid"])))
        conn.executemany(
            f"UPDATE {table} SET content_sha256=? WHERE rowid=?", updates
        )
        cursor = int(rows[-1]["migration_rowid"])


def _read_label_children(
    conn: sqlite3.Connection,
    *,
    table: str,
    label_ids: list[str],
    order_by: str,
) -> list[sqlite3.Row]:
    allowed = {
        "ml_label_horizons": "label_id,horizon_days",
        "ml_label_downside": "label_id",
    }
    if allowed.get(table) != order_by:
        raise ValueError("unsupported label child query")
    result: list[sqlite3.Row] = []
    for start in range(0, len(label_ids), 400):
        chunk = label_ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        result.extend(
            conn.execute(
                f"""SELECT * FROM {table}
                    WHERE label_id IN ({placeholders})
                    ORDER BY {order_by}""",
                tuple(chunk),
            ).fetchall()
        )
    return result


def _training_label_projection(
    main: sqlite3.Row,
    horizons: Mapping[int, sqlite3.Row],
    downside: sqlite3.Row | None,
) -> dict[str, object]:
    result = {
        field: main[field]
        for field in (
            "label_id",
            "sample_id",
            "label_version",
            "label_source",
            "cost_version",
            "cost_sha256",
            "policy_version",
            "policy_sha256",
            "candidate_source",
            "dataset_id",
            "trade_date",
            "decision_at",
            "code",
            "candidate_content_sha256",
            "fill_label",
            "fill_status",
            "fill_reason",
            "fill_evidence_sha256",
            "fill_delay_sec",
            "fill_price",
            "fill_at",
            "fill_matured_at",
            "reference_qty",
            "reference_notional_yuan",
            "reference_trade_notional_yuan",
            "actual_net_pnl",
            "quality_status",
            "quality_reasons_json",
            "flags_json",
            "failure_reasons_json",
            "market_data_sha256",
        )
    }
    result["entry_ref"] = main["fill_price"]
    result["label_main_content_sha256"] = main["content_sha256"]
    result["quality_reason"] = _joined_json_reasons(main["quality_reasons_json"])
    result["quality_failure_reason"] = _joined_json_reasons(
        main["failure_reasons_json"]
    )
    maturities: list[str] = []
    if main["fill_matured_at"] is not None:
        maturities.append(str(main["fill_matured_at"]))

    for days in (3, 5, 10):
        prefix = f"ret_{days}d"
        horizon = horizons.get(days)
        fields = (
            "gross_return",
            "net_return",
            "exit_price",
            "buy_commission_yuan",
            "buy_transfer_fee_yuan",
            "buy_other_fee_yuan",
            "buy_slippage_yuan",
            "sell_commission_yuan",
            "sell_stamp_tax_yuan",
            "sell_transfer_fee_yuan",
            "sell_other_fee_yuan",
            "sell_slippage_yuan",
            "total_cost_yuan",
            "cost_rate",
            "matured_at",
            "market_data_sha256",
            "content_sha256",
        )
        for field in fields:
            output = {
                "gross_return": f"{prefix}_gross",
                "net_return": f"{prefix}_net",
                "matured_at": f"{prefix}_matured_at",
            }.get(field, f"{prefix}_{field}")
            result[output] = None if horizon is None else horizon[field]
        result[f"matured_{days}d_at"] = (
            None if horizon is None else horizon["matured_at"]
        )
        if horizon is not None:
            maturities.append(str(horizon["matured_at"]))

    primary = horizons.get(5)
    if primary is None:
        primary_costs = {
            "buy_cost": None,
            "sell_cost": None,
            "slippage_cost": None,
            "commission_cost": None,
            "stamp_tax_cost": None,
            "transfer_fee_cost": None,
            "other_fee_cost": None,
            "net_cost": None,
        }
    else:
        primary_costs = {
            "buy_cost": (
                primary["buy_commission_yuan"]
                + primary["buy_transfer_fee_yuan"]
                + primary["buy_other_fee_yuan"]
            ),
            "sell_cost": (
                primary["sell_commission_yuan"]
                + primary["sell_stamp_tax_yuan"]
                + primary["sell_transfer_fee_yuan"]
                + primary["sell_other_fee_yuan"]
            ),
            "slippage_cost": (
                primary["buy_slippage_yuan"] + primary["sell_slippage_yuan"]
            ),
            "commission_cost": (
                primary["buy_commission_yuan"] + primary["sell_commission_yuan"]
            ),
            "stamp_tax_cost": primary["sell_stamp_tax_yuan"],
            "transfer_fee_cost": (
                primary["buy_transfer_fee_yuan"]
                + primary["sell_transfer_fee_yuan"]
            ),
            "other_fee_cost": (
                primary["buy_other_fee_yuan"] + primary["sell_other_fee_yuan"]
            ),
            "net_cost": primary["total_cost_yuan"],
        }
    result.update(primary_costs)
    result["other_cost"] = result["other_fee_cost"]

    downside_fields = {
        "downside_status": "status",
        "mfe_10d": "mfe_10d_net",
        "mae_10d": "mae_10d_net",
        "downside_loss": "downside_loss",
        "paused_path": "paused_path",
        "exit_blocked": "exit_blocked",
        "hit_stop": "hit_stop",
        "hit_take": "hit_take",
        "downside_failure_reason": "failure_reason",
        "downside_matured_at": "matured_at",
        "downside_evidence_sha256": "evidence_sha256",
        "downside_content_sha256": "content_sha256",
    }
    for output, field in downside_fields.items():
        result[output] = None if downside is None else downside[field]
    if downside is not None and downside["matured_at"] is not None:
        maturities.append(str(downside["matured_at"]))
    result["matured_at"] = _latest_aware_iso(maturities)
    result["content_sha256"] = canonical_hash(result)
    return result


def _expand_candidate_feature_row(row: sqlite3.Row) -> dict[str, object]:
    try:
        features = json.loads(str(row["features_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid persisted candidate feature JSON") from exc
    if not isinstance(features, dict):
        raise ValueError("invalid persisted candidate feature JSON")
    result: dict[str, object] = {
        "sample_id": row["sample_id"],
        "decision_at": row["decision_at"],
        "code": row["code"],
    }
    for name, payload in features.items():
        if not isinstance(name, str) or name in result:
            raise ValueError("invalid persisted candidate feature name")
        if not isinstance(payload, dict) or "value" not in payload:
            raise ValueError("invalid persisted candidate feature JSON")
        result[name] = payload["value"]
    return result


def _joined_json_reasons(value: object) -> str:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid persisted label reason JSON") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError("invalid persisted label reason JSON")
    return "|".join(parsed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema_statements(script: str) -> Iterator[str]:
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending.strip()
            pending = ""
    if pending.strip():
        yield pending.strip()


def _schema_fingerprint(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    return {
        (str(row[0]), str(row[1])): " ".join(str(row[2]).split()).casefold()
        for row in conn.execute(
            """SELECT type, name, sql FROM sqlite_master
               WHERE type IN ('table', 'index')
                 AND sql IS NOT NULL
                 AND name NOT LIKE 'sqlite_%'"""
        )
    }


def _required_schema_fingerprint(
    script: str = SCHEMA,
) -> dict[tuple[str, str], str]:
    with closing(sqlite3.connect(":memory:")) as conn:
        for statement in _schema_statements(script):
            conn.execute(statement)
        return _schema_fingerprint(conn)


def _validate_label_schema_v2(conn: sqlite3.Connection) -> None:
    expected = {
        "label_id",
        "sample_id",
        "label_version",
        "label_source",
        "cost_version",
        "cost_sha256",
        "policy_version",
        "policy_sha256",
        "fill_label",
        "fill_delay_sec",
        "fill_price",
        "ret_3d_net",
        "ret_5d_net",
        "ret_10d_net",
        "mfe_10d",
        "mae_10d",
        "hit_stop",
        "hit_take",
        "actual_net_pnl",
        "market_data_sha256",
        "matured_at",
        "candidate_source",
        "dataset_id",
        "trade_date",
        "decision_at",
        "code",
        "candidate_content_sha256",
        "fill_status",
        "fill_reason",
        "fill_evidence_sha256",
        "fill_at",
        "fill_matured_at",
        "reference_qty",
        "reference_notional_yuan",
        "reference_trade_notional_yuan",
        "ret_3d_gross",
        "ret_5d_gross",
        "ret_10d_gross",
        "downside_loss",
        "exit_blocked",
        "paused_path",
        "buy_cost",
        "sell_cost",
        "slippage_cost",
        "commission_cost",
        "stamp_tax_cost",
        "transfer_fee_cost",
        "other_fee_cost",
        "net_cost",
        "quality_status",
        "quality_reasons_json",
        "flags_json",
        "failure_reasons_json",
        "matured_3d_at",
        "matured_5d_at",
        "matured_10d_at",
        "downside_matured_at",
        "content_sha256",
        "updated_at",
    }
    rows = conn.execute("PRAGMA table_info(ml_labels)").fetchall()
    actual = {str(row[1]) for row in rows}
    if actual != expected:
        raise RuntimeError("ML label schema structure mismatch for version 2")
    primary = [str(row[1]) for row in rows if int(row[5]) == 1]
    if primary != ["label_id"]:
        raise RuntimeError("ML label primary key mismatch for version 2")
    label_indexes = _index_columns(conn, "ml_labels")
    if (True, ("label_id", "sample_id")) not in label_indexes:
        raise RuntimeError("ML label identity unique mismatch for version 2")
    if (
        True,
        (
            "sample_id",
            "label_source",
            "label_version",
            "cost_sha256",
            "policy_sha256",
        ),
    ) not in label_indexes:
        raise RuntimeError("ML label contract unique mismatch for version 2")
    if (
        False,
        (
            "label_source",
            "label_version",
            "cost_sha256",
            "policy_sha256",
            "label_id",
        ),
    ) not in label_indexes:
        raise RuntimeError("ML label training index mismatch for version 2")
    foreign = conn.execute("PRAGMA foreign_key_list(ml_labels)").fetchall()
    if not any(
        str(row[2]) == "ml_label_subjects"
        and str(row[3]) == "sample_id"
        and str(row[4]) == "sample_id"
        for row in foreign
    ):
        raise RuntimeError("ML label subject foreign key mismatch for version 2")
    subject_rows = conn.execute("PRAGMA table_info(ml_label_subjects)").fetchall()
    if {str(row[1]) for row in subject_rows} != {
        "sample_id",
        "candidate_origin",
        "source",
        "dataset_id",
        "trade_date",
        "decision_at",
        "code",
        "candidate_content_sha256",
        "created_at",
    }:
        raise RuntimeError("ML label subject schema structure mismatch for version 2")
    subject_primary = [str(row[1]) for row in subject_rows if int(row[5]) == 1]
    if subject_primary != ["sample_id"]:
        raise RuntimeError("ML label subject primary key mismatch for version 2")
    if (
        False,
        ("dataset_id", "trade_date", "decision_at", "sample_id"),
    ) not in _index_columns(conn, "ml_label_subjects"):
        raise RuntimeError("ML label subject dataset index mismatch for version 2")
    horizon_rows = conn.execute("PRAGMA table_info(ml_label_horizons)").fetchall()
    horizon_expected = {
        "sample_id",
        "label_id",
        "horizon_days",
        "gross_return",
        "net_return",
        "exit_price",
        "buy_commission_yuan",
        "buy_transfer_fee_yuan",
        "buy_other_fee_yuan",
        "buy_slippage_yuan",
        "sell_commission_yuan",
        "sell_stamp_tax_yuan",
        "sell_transfer_fee_yuan",
        "sell_other_fee_yuan",
        "sell_slippage_yuan",
        "total_cost_yuan",
        "cost_rate",
        "matured_at",
        "market_data_sha256",
        "content_sha256",
        "created_at",
    }
    if {str(row[1]) for row in horizon_rows} != horizon_expected:
        raise RuntimeError("ML horizon label schema structure mismatch for version 2")
    horizon_primary = [
        str(row[1]) for row in horizon_rows if int(row[5]) > 0
    ]
    if horizon_primary != ["label_id", "horizon_days"]:
        raise RuntimeError("ML horizon label primary key mismatch for version 2")
    if (
        "ml_labels",
        (("label_id", "label_id"), ("sample_id", "sample_id")),
        "CASCADE",
    ) not in _foreign_key_groups(conn, "ml_label_horizons"):
        raise RuntimeError("ML horizon label foreign key mismatch for version 2")
    downside_rows = conn.execute("PRAGMA table_info(ml_label_downside)").fetchall()
    if {str(row[1]) for row in downside_rows} != {
        "label_id",
        "sample_id",
        "status",
        "mfe_10d_net",
        "mae_10d_net",
        "downside_loss",
        "paused_path",
        "exit_blocked",
        "hit_stop",
        "hit_take",
        "failure_reason",
        "matured_at",
        "evidence_sha256",
        "content_sha256",
        "created_at",
    }:
        raise RuntimeError("ML downside label schema structure mismatch for version 2")
    downside_primary = [str(row[1]) for row in downside_rows if int(row[5]) == 1]
    if downside_primary != ["label_id"]:
        raise RuntimeError("ML downside label primary key mismatch for version 2")
    if (
        "ml_labels",
        (("label_id", "label_id"), ("sample_id", "sample_id")),
        "CASCADE",
    ) not in _foreign_key_groups(conn, "ml_label_downside"):
        raise RuntimeError("ML downside label foreign key mismatch for version 2")


def _index_columns(
    conn: sqlite3.Connection, table: str
) -> set[tuple[bool, tuple[str, ...]]]:
    result: set[tuple[bool, tuple[str, ...]]] = set()
    for row in conn.execute(f"PRAGMA index_list({table})"):
        name = str(row[1])
        columns = tuple(
            str(info[2])
            for info in sorted(
                conn.execute(f"PRAGMA index_info('{name}')").fetchall(),
                key=lambda item: int(item[0]),
            )
        )
        result.add((bool(row[2]), columns))
    return result


def _foreign_key_groups(
    conn: sqlite3.Connection, table: str
) -> set[tuple[str, tuple[tuple[str, str], ...], str]]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
        grouped.setdefault(int(row[0]), []).append(row)
    return {
        (
            str(rows[0][2]),
            tuple(
                (str(row[3]), str(row[4]))
                for row in sorted(rows, key=lambda item: int(item[1]))
            ),
            str(rows[0][6]),
        )
        for rows in grouped.values()
    }


def _aware_iso(value: str, field: str) -> str:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field}")
    return parsed.isoformat()


def _new_store_minimum_bytes(reserved_bytes: int) -> int:
    page_size = 4_096
    reserved_pages = (reserved_bytes + page_size - 1) // page_size
    wal_bytes = 32 + reserved_pages * (page_size + 24)
    main_bytes = (reserved_pages + 1) * page_size
    return main_bytes + wal_bytes


def _write_reserve_bytes(
    rows: Iterable[tuple[object, ...]],
    writes: int,
    *,
    btrees_per_write: int,
) -> int:
    parameter_bytes = sum(
        _parameter_bytes(value) for row in rows for value in row
    )
    return (
        2 * parameter_bytes
        + writes * btrees_per_write * BTREE_RESERVE_BYTES
    )


def _parameter_bytes(value: object) -> int:
    value_type = type(value)
    if value_type is type(None):
        return 1
    if value_type is str:
        return len(value.encode("utf-8"))
    if value_type is bytes:
        return len(value)
    if value_type is float and not math.isfinite(value):
        raise ValueError("SQLite float parameters must be finite")
    if value_type in (bool, int, float):
        return 8
    raise TypeError(
        "SQLite parameters must be None, bool, int, float, str, or bytes"
    )


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be str")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=repr)
    value_type = type(value)
    if value_type is float and not math.isfinite(value):
        raise ValueError("JSON float values must be finite")
    if value_type in (type(None), bool, int, float, str):
        return value
    raise TypeError("JSON values must contain only standard scalar types")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
