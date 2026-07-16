"""Independent SQLite ledger for trained-shadow-model data and model state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from ml_contracts import (
    CandidateSample,
    LabelRecord,
    ModelManifest,
    PredictionRecord,
    canonical_hash,
)


SCHEMA_VERSION = 1
ML_TABLES = (
    "ml_candidate_samples",
    "ml_labels",
    "ml_predictions",
    "ml_models",
    "ml_model_events",
    "ml_runtime_state",
)


class MlDataConflict(RuntimeError):
    """Raised when an immutable ML identity is replayed with different data."""


class MlCapacityError(RuntimeError):
    """Raised when the configured ML database capacity has been reached."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ml_candidate_samples(
  sample_id TEXT PRIMARY KEY, source TEXT NOT NULL, dataset_id TEXT NOT NULL,
  trade_date TEXT NOT NULL, decision_at TEXT NOT NULL, code TEXT NOT NULL,
  strategy_version TEXT NOT NULL, parameter_version TEXT NOT NULL,
  feature_schema_version TEXT NOT NULL, features_json TEXT NOT NULL,
  selected INTEGER NOT NULL, rejection_stage TEXT NOT NULL,
  rejection_code TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ml_candidates_date_code
ON ml_candidate_samples(trade_date, code, decision_at);
CREATE TABLE IF NOT EXISTS ml_labels(
  sample_id TEXT PRIMARY KEY REFERENCES ml_candidate_samples(sample_id),
  label_version TEXT NOT NULL, label_source TEXT NOT NULL, cost_version TEXT NOT NULL,
  fill_label INTEGER, fill_delay_sec REAL, fill_price REAL,
  ret_3d_net REAL, ret_5d_net REAL, ret_10d_net REAL,
  mfe_10d REAL, mae_10d REAL, hit_stop INTEGER, hit_take INTEGER,
  actual_net_pnl REAL, market_data_sha256 TEXT NOT NULL, matured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ml_predictions(
  sample_id TEXT NOT NULL REFERENCES ml_candidate_samples(sample_id),
  model_id TEXT NOT NULL, expected_ret_3d REAL, expected_ret_5d REAL,
  expected_ret_10d REAL, downside_risk REAL, fill_probability REAL,
  ml_score REAL, ml_filter INTEGER, position_multiplier REAL, confidence REAL,
  created_at TEXT NOT NULL, PRIMARY KEY(sample_id, model_id)
);
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
CREATE TABLE IF NOT EXISTS ml_runtime_state(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), active_model_id TEXT,
  permission_level INTEGER NOT NULL CHECK(permission_level BETWEEN 0 AND 3),
  updated_at TEXT NOT NULL
);
"""


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

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path, timeout=5.0, factory=_ClosingConnection
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            now = _now()
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO ml_runtime_state(
                   singleton, active_model_id, permission_level, updated_at
                   ) VALUES (1, NULL, 0, ?)""",
                (now,),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0) if row is not None else 0

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ML_TABLES
            }

    def record_candidates(self, samples: list[CandidateSample]) -> int:
        self._ensure_capacity()
        changed = 0
        with self.transaction() as conn:
            for sample in samples:
                features_json = _canonical_json(sample.features)
                content_sha256 = canonical_hash(sample)
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO ml_candidate_samples(
                       sample_id, source, dataset_id, trade_date, decision_at, code,
                       strategy_version, parameter_version, feature_schema_version,
                       features_json, selected, rejection_stage, rejection_code,
                       content_sha256, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        content_sha256,
                        _now(),
                    ),
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
            self._ensure_capacity()
        return changed

    def upsert_labels(self, labels: list[LabelRecord]) -> int:
        self._ensure_capacity()
        changed = 0
        columns = (
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
        with self.transaction() as conn:
            for label in labels:
                if label.matured_at is None:
                    raise ValueError("matured_at is required")
                values = tuple(getattr(label, column) for column in columns)
                row = conn.execute(
                    f"SELECT {','.join(columns)} FROM ml_labels WHERE sample_id=?",
                    (label.sample_id,),
                ).fetchone()
                if row is not None and tuple(row) == values:
                    continue
                conn.execute(
                    f"""INSERT INTO ml_labels(sample_id,{','.join(columns)})
                        VALUES (?,{','.join('?' for _ in columns)})
                        ON CONFLICT(sample_id) DO UPDATE SET
                        {','.join(f'{column}=excluded.{column}' for column in columns)}""",
                    (label.sample_id, *values),
                )
                changed += 1
            self._ensure_capacity()
        return changed

    def record_predictions(self, predictions: list[PredictionRecord]) -> int:
        self._ensure_capacity()
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
            "created_at",
        )
        with self.transaction() as conn:
            for prediction in predictions:
                values = tuple(
                    int(value) if column == "ml_filter" and value is not None else value
                    for column, value in (
                        (column, getattr(prediction, column)) for column in columns
                    )
                )
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
            self._ensure_capacity()
        return changed

    def register_model(
        self,
        manifest: ModelManifest,
        *,
        artifact_path: str,
        status: str = "challenger",
    ) -> bool:
        self._ensure_capacity()
        manifest_json = _canonical_json(manifest)
        values = (
            manifest.parent_model_id,
            str(status),
            str(artifact_path),
            manifest.artifact_sha256,
            manifest_json,
            manifest.created_at,
        )
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT parent_model_id,status,artifact_path,artifact_sha256,
                          manifest_json,created_at FROM ml_models WHERE model_id=?""",
                (manifest.model_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) != values:
                    raise MlDataConflict(
                        f"immutable model conflict: {manifest.model_id}"
                    )
                return False
            try:
                conn.execute(
                    """INSERT INTO ml_models(
                       model_id,parent_model_id,status,artifact_path,artifact_sha256,
                       manifest_json,created_at) VALUES (?,?,?,?,?,?,?)""",
                    (manifest.model_id, *values),
                )
            except sqlite3.IntegrityError as exc:
                raise MlDataConflict(
                    f"artifact already registered: {manifest.artifact_sha256}"
                ) from exc
            self._ensure_capacity()
        return True

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
        self._ensure_capacity()
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
        with self.transaction() as conn:
            model = conn.execute(
                "SELECT artifact_sha256 FROM ml_models WHERE model_id=?", (model_id,)
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
            self._ensure_capacity()
        return True

    def runtime_state(self) -> dict[str, object]:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT active_model_id, permission_level, updated_at
                   FROM ml_runtime_state WHERE singleton=1"""
            ).fetchone()
        if row is None:
            raise RuntimeError("ML runtime state is not initialized")
        return {
            "active_model_id": row[0],
            "permission_level": int(row[1]),
            "updated_at": str(row[2]),
        }

    def compare_and_swap_runtime(
        self,
        *,
        expected_model_id: str | None,
        expected_permission_level: int,
        new_model_id: str | None,
        new_permission_level: int,
        updated_at: str,
    ) -> bool:
        updated_at = _aware_iso(updated_at, "updated_at")
        if not 0 <= int(new_permission_level) <= 3:
            raise ValueError("permission level must be between 0 and 3")
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE ml_runtime_state
                   SET active_model_id=?, permission_level=?, updated_at=?
                   WHERE singleton=1 AND active_model_id IS ? AND permission_level=?""",
                (
                    new_model_id,
                    int(new_permission_level),
                    updated_at,
                    expected_model_id,
                    int(expected_permission_level),
                ),
            )
        return cursor.rowcount == 1

    def backup_to(self, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)

    def integrity_check(self) -> str:
        with self.connect() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row is not None else "missing"

    def _ensure_capacity(self) -> None:
        size = sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            )
            if candidate.exists()
        )
        if size >= self.max_bytes:
            raise MlCapacityError(
                f"ML database capacity reached: {size} >= {self.max_bytes}"
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aware_iso(value: str, field: str) -> str:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field}")
    return parsed.isoformat()


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=repr)
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
