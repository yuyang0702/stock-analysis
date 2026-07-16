"""Independent SQLite ledger for trained-shadow-model data and model state."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
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
BTREE_RESERVE_BYTES = 128 * 1024
RUNTIME_RESERVE_BYTES = 64 * 1024
SCHEMA_BTREE_COUNT = 14
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
  actual_net_pnl REAL, market_data_sha256 TEXT NOT NULL, matured_at TEXT
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
        try:
            with self._connect_readonly() as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if {"schema_migrations", *ML_TABLES} - tables:
                    return False
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
                runtime = conn.execute(
                    """SELECT permission_level, updated_at FROM ml_runtime_state
                       WHERE singleton=1"""
                ).fetchone()
        except sqlite3.Error:
            return False
        try:
            if version is None or int(version[0] or 0) != SCHEMA_VERSION:
                return False
            if runtime is None or not 0 <= int(runtime[0]) <= 3:
                return False
            _aware_iso(str(runtime[1]), "updated_at")
        except (TypeError, ValueError):
            return False
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
        if self._file_size(self.path) > 0 and self._is_current_schema():
            return
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
                       content_sha256, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        rows = [
            (label, tuple(getattr(label, column) for column in columns))
            for label in labels
        ]
        if not rows:
            return 0
        reserved_bytes = _write_reserve_bytes(
            ((label.sample_id, *values) for label, values in rows),
            len(rows),
            btrees_per_write=2,
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
            for label, values in rows:
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
        return changed

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
            "created_at",
        )
        rows = [
            (
                prediction,
                tuple(
                    int(value) if column == "ml_filter" and value is not None else value
                    for column, value in (
                        (column, getattr(prediction, column)) for column in columns
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

    def register_model(
        self,
        manifest: ModelManifest,
        *,
        artifact_path: str,
        status: str = "challenger",
    ) -> bool:
        _parameter_bytes(status)
        _parameter_bytes(artifact_path)
        manifest_json = _canonical_json(manifest)
        values = (
            manifest.parent_model_id,
            str(status),
            str(artifact_path),
            manifest.artifact_sha256,
            manifest_json,
            manifest.created_at,
        )
        reserved_bytes = _write_reserve_bytes(
            ((manifest.model_id, *values),), 1, btrees_per_write=3
        )
        with self._audited_write(
            reserved_bytes=reserved_bytes,
            growth_bytes=reserved_bytes,
        ) as conn:
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

    def runtime_state(self) -> dict[str, object]:
        with self._connect_readonly() as conn:
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
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
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
