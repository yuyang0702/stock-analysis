"""Bounded maintenance commands for strict-history and trained ML evidence.

The module intentionally keeps administration and automation separate.  It may
materialize labels, train and register a challenger, report current state, and
back up/verify the two SQLite stores.  It never approves, activates, promotes,
or changes a model permission level.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import config as app_config
from historical_data import HISTORY_SCHEMA_VERSION, HistoricalStore
from ml_dataset import FEATURE_COLUMNS
from ml_labels import LabelPolicy, build_labels
from ml_store import ML_TABLES, SCHEMA_VERSION as ML_SCHEMA_VERSION, MlStore
from ml_train import TrainingConfig, train_challenger
from ml_training_data import (
    FORBIDDEN_MODEL_FEATURES,
    build_ml_splits,
    build_training_frame,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
BACKUP_TIERS = ("daily", "weekly", "monthly")
DEFAULT_FEATURE_ALLOWLIST = tuple(
    name
    for name in (*FEATURE_COLUMNS, "market_regime")
    if name not in FORBIDDEN_MODEL_FEATURES
    and name
    not in {
        "position_pct",
        "signal_action",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(SHANGHAI)
    normalized = str(value).strip().replace(" ", "T")
    parsed = datetime.fromisoformat(normalized)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


def _aware_iso(value: datetime) -> str:
    current = value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)
    return current.isoformat()


def database_facts(db_file: Path, *, expected_schema: int | None = None) -> dict[str, object]:
    """Return integrity, schema and bounded table-count evidence for one DB."""

    path = Path(db_file)
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        if "schema_migrations" not in tables:
            raise ValueError("schema_migrations table missing")
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        schema_version = int(row[0] or 0) if row is not None else 0
        if expected_schema is not None and schema_version != int(expected_schema):
            raise ValueError(
                f"schema version mismatch: expected {expected_schema}, got {schema_version}"
            )
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "missing"
        if integrity != "ok":
            raise ValueError(f"integrity_check failed: {integrity}")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            first = violations[0]
            raise ValueError(
                "foreign_key_check failed: "
                f"table={first[0]} rowid={first[1]} parent={first[2]}"
            )
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
    return {
        "schema_version": schema_version,
        "integrity_check": integrity,
        "table_counts": counts,
    }


def _validate_backup_root(db_file: Path, backup_root: Path, project_root: Path) -> tuple[Path, Path]:
    db = Path(db_file).resolve()
    root = Path(backup_root).resolve()
    project = Path(project_root).resolve()
    if not db.is_file():
        raise FileNotFoundError(f"database not found: {db}")
    if root == project or root.is_relative_to(project):
        raise ValueError("backup directory must be outside the project")
    if root == db or db.is_relative_to(root):
        raise ValueError("backup directory must not contain the live database")
    root.mkdir(parents=True, exist_ok=True)
    return db, root


def _online_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_connection, closing(
        sqlite3.connect(destination)
    ) as target:
        source_connection.backup(target)


def _slot(tier: str, now: datetime) -> str:
    if tier == "daily":
        return now.strftime("%Y-%m-%d")
    if tier == "weekly":
        year, week, _ = now.isocalendar()
        return f"{year}-W{week:02d}"
    if tier == "monthly":
        return now.strftime("%Y-%m")
    raise ValueError(f"unsupported backup tier: {tier}")


def _entry_paths(root: Path, tier: str, stem: str) -> tuple[Path, Path]:
    return root / tier / f"{stem}.db", root / "manifests" / tier / f"{stem}.json"


def _promote_backup(
    source: Path,
    manifest: Mapping[str, object],
    root: Path,
    *,
    tier: str,
    slot: str,
) -> tuple[Path, Path]:
    stem = f"{manifest['kind']}-{slot}-{str(manifest['sha256'])[:12]}"
    final_db, final_manifest = _entry_paths(root, tier, stem)
    final_db.parent.mkdir(parents=True, exist_ok=True)
    final_manifest.parent.mkdir(parents=True, exist_ok=True)
    tier_manifest = dict(manifest)
    tier_manifest.update({"tier": tier, "slot": slot})
    with TemporaryDirectory(dir=root, prefix=f".{tier}-") as temporary_dir:
        staged_db = Path(temporary_dir) / "backup.db"
        staged_manifest = Path(temporary_dir) / "manifest.json"
        try:
            os.link(source, staged_db)
        except OSError:
            shutil.copy2(source, staged_db)
        if _sha256(staged_db) != manifest["sha256"]:
            raise ValueError("backup promotion sha256 mismatch")
        staged_manifest.write_text(
            json.dumps(tier_manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        staged_db.replace(final_db)
        staged_manifest.replace(final_manifest)

    # A slot has one authoritative object; replace stale copies only after the
    # new pair is complete and validated.
    for entry in validated_manifests(
        root, tier=tier, kind=str(manifest.get("kind") or "")
    ):
        if entry.get("slot") != slot:
            continue
        old_db = Path(str(entry["backup_file"]))
        old_manifest = Path(str(entry["manifest_file"]))
        if old_db != final_db:
            old_db.unlink(missing_ok=True)
            old_manifest.unlink(missing_ok=True)
    return final_db, final_manifest


def _validated_manifest(
    root: Path,
    manifest_file: Path,
    *,
    expected_kind: str | None = None,
) -> dict[str, object] | None:
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return None
        tier = str(manifest.get("tier") or "")
        slot = str(manifest.get("slot") or "")
        kind = str(manifest.get("kind") or "")
        if (
            tier not in BACKUP_TIERS
            or not slot
            or kind not in {"ml", "history"}
            or (expected_kind is not None and kind != expected_kind)
        ):
            return None
        backup_file = root / tier / f"{manifest_file.stem}.db"
        if not backup_file.is_file() or _sha256(backup_file) != manifest.get("sha256"):
            return None
        facts = database_facts(
            backup_file, expected_schema=int(manifest.get("schema_version") or 0)
        )
        if facts["integrity_check"] != manifest.get("integrity_check"):
            return None
        if facts["table_counts"] != manifest.get("table_counts"):
            return None
    except Exception:
        return None
    result = dict(manifest)
    result["backup_file"] = str(backup_file)
    result["manifest_file"] = str(manifest_file)
    return result


def validated_manifests(
    root: Path,
    *,
    tier: str | None = None,
    kind: str | None = None,
) -> list[dict[str, object]]:
    root = Path(root)
    if kind is not None and kind not in {"ml", "history"}:
        raise ValueError("backup kind must be ml or history")
    tiers = (tier,) if tier else BACKUP_TIERS
    entries: list[dict[str, object]] = []
    for current in tiers:
        if current not in BACKUP_TIERS:
            raise ValueError(f"unsupported backup tier: {current}")
        manifest_dir = root / "manifests" / current
        if not manifest_dir.is_dir():
            continue
        entries.extend(
            entry
            for path in sorted(manifest_dir.glob("*.json"))
            if (
                entry := _validated_manifest(
                    root, path, expected_kind=kind
                )
            ) is not None
        )
    return sorted(entries, key=lambda item: (str(item.get("created_at") or ""), str(item.get("tier") or "")))


def _verify_restored_copy(
    backup_file: Path,
    manifest: Mapping[str, object],
    *,
    temporary_root: Path,
) -> dict[str, object]:
    temporary_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=temporary_root, prefix="restore-") as directory:
        restored = Path(directory) / "restored.db"
        shutil.copy2(backup_file, restored)
        if _sha256(restored) != manifest.get("sha256"):
            raise ValueError("restored backup sha256 mismatch")
        facts = database_facts(
            restored, expected_schema=int(manifest.get("schema_version") or 0)
        )
        if facts["table_counts"] != manifest.get("table_counts"):
            raise ValueError("restored backup table counts mismatch")
        return facts


def _status_file(root: Path) -> Path:
    return Path(root) / "status.json"


def _load_status(root: Path) -> dict[str, object]:
    path = _status_file(root)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_status(root: Path, key: str, value: Mapping[str, object]) -> None:
    status = _load_status(root)
    status[str(key)] = dict(value)
    _atomic_write_json(_status_file(root), status)


def retention_plan(
    backup_root: Path,
    *,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    keep_monthly: int = 12,
    kind: str | None = None,
) -> dict[str, object]:
    keep = {"daily": keep_daily, "weekly": keep_weekly, "monthly": keep_monthly}
    result: dict[str, object] = {"delete": [], "invalid": [], "keep": dict(keep)}
    delete: list[str] = []
    invalid: list[str] = []
    root = Path(backup_root)
    for tier in BACKUP_TIERS:
        count = int(keep[tier])
        if count <= 0:
            raise ValueError("backup retention must be positive")
        valid = validated_manifests(root, tier=tier, kind=kind)
        slots = sorted({str(entry["slot"]) for entry in valid})
        expired_slots = set(slots[:-count])
        for entry in valid:
            if str(entry["slot"]) in expired_slots:
                delete.extend((str(entry["backup_file"]), str(entry["manifest_file"])))
        valid_files = {
            str(path.resolve())
            for entry in valid
            for path in (Path(str(entry["backup_file"])), Path(str(entry["manifest_file"])))
        }
        candidates = list((root / tier).glob("*.db")) + list((root / "manifests" / tier).glob("*.json"))
        invalid.extend(str(path) for path in candidates if str(path.resolve()) not in valid_files)
    result["delete"] = sorted(set(delete))
    result["invalid"] = sorted(set(invalid))
    return result


def apply_retention(
    backup_root: Path,
    *,
    now: datetime,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    keep_monthly: int = 12,
    verified_sha256: str | None = None,
    kind: str | None = None,
) -> dict[str, object]:
    root = Path(backup_root)
    status = _load_status(root)
    verified = status.get("restore_check")
    if not isinstance(verified, dict) or verified.get("status") != "success":
        raise PermissionError("retention apply requires a successful restore check")
    if kind is not None and verified.get("kind") != kind:
        raise PermissionError("restore check backup kind mismatch")
    verified_at = datetime.fromisoformat(str(verified.get("verified_at") or ""))
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=SHANGHAI)
    if now.astimezone(SHANGHAI) - verified_at.astimezone(SHANGHAI) > timedelta(hours=24):
        raise PermissionError("verified backup is older than 24 hours")
    expected_sha = str(verified.get("sha256") or "")
    if verified_sha256 is not None and str(verified_sha256) != expected_sha:
        raise PermissionError("verified backup sha256 mismatch")
    if not any(
        str(entry.get("sha256")) == expected_sha
        for entry in validated_manifests(root, kind=kind)
    ):
        raise PermissionError("verified backup is no longer available")
    plan = retention_plan(
        root,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        keep_monthly=keep_monthly,
        kind=kind,
    )
    deleted: list[str] = []
    for value in plan["delete"]:
        path = Path(str(value))
        path.unlink(missing_ok=True)
        deleted.append(str(path))
    result = {
        "status": "success",
        "command": "retention-apply",
        "verified_sha256": expected_sha,
        "deleted": deleted,
        "invalid": plan["invalid"],
        "finished_at": _aware_iso(now),
    }
    _save_status(root, "retention", result)
    return result


def create_database_backup(
    kind: str,
    db_file: Path,
    backup_root: Path,
    *,
    now: datetime,
    project_root: Path,
    expected_schema: int,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    keep_monthly: int = 12,
) -> dict[str, object]:
    if kind not in {"ml", "history"}:
        raise ValueError("backup kind must be ml or history")
    db, root = _validate_backup_root(db_file, backup_root, project_root)
    with TemporaryDirectory(dir=root, prefix=".backup-") as directory:
        staged = Path(directory) / f"{kind}.db"
        _online_backup(db, staged)
        facts = database_facts(staged, expected_schema=expected_schema)
        sha256 = _sha256(staged)
        manifest: dict[str, object] = {
            "kind": kind,
            "status": "success",
            "created_at": _aware_iso(now),
            "source_db": str(db),
            "source_size": db.stat().st_size,
            "backup_size": staged.stat().st_size,
            "sha256": sha256,
            **facts,
        }
        promoted: dict[str, str] = {}
        latest_daily: Path | None = None
        for tier in BACKUP_TIERS:
            destination, manifest_path = _promote_backup(
                staged, manifest, root, tier=tier, slot=_slot(tier, now)
            )
            promoted[tier] = str(destination)
            promoted[f"{tier}_manifest"] = str(manifest_path)
            if tier == "daily":
                latest_daily = destination
        assert latest_daily is not None
        restored_facts = _verify_restored_copy(
            latest_daily,
            {**manifest, "schema_version": expected_schema},
            temporary_root=root / "restore-checks",
        )
    verification = {
        "status": "success",
        "command": "restore-check",
        "kind": kind,
        "sha256": sha256,
        "backup_file": str(latest_daily),
        "verified_at": _aware_iso(now),
        **restored_facts,
    }
    _save_status(root, "restore_check", verification)
    retention = apply_retention(
        root,
        now=now,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        keep_monthly=keep_monthly,
        verified_sha256=sha256,
        kind=kind,
    )
    result = {
        **manifest,
        "command": "backup",
        "stage": "complete",
        "backup_files": promoted,
        "restore_check": verification,
        "retention": retention,
        "finished_at": _aware_iso(now),
    }
    _save_status(root, "backup", result)
    return result


def restore_check(
    backup_root: Path,
    *,
    now: datetime,
    kind: str | None = None,
) -> dict[str, object]:
    root = Path(backup_root)
    entries = validated_manifests(root, kind=kind)
    if not entries:
        raise FileNotFoundError("no valid backup available")
    selected = max(entries, key=lambda item: str(item.get("created_at") or ""))
    facts = _verify_restored_copy(
        Path(str(selected["backup_file"])),
        selected,
        temporary_root=root / "restore-checks",
    )
    result = {
        "status": "success",
        "command": "restore-check",
        "kind": selected.get("kind"),
        "backup_file": selected["backup_file"],
        "sha256": selected["sha256"],
        "verified_at": _aware_iso(now),
        **facts,
    }
    _save_status(root, "restore_check", result)
    return result


def run_label_job(
    *,
    history_db: Path,
    ml_db: Path,
    dataset_id: str,
    as_of: str,
    lookback_days: int = 45,
    page_size: int = 5000,
    max_pages: int = 100,
) -> dict[str, object]:
    if not str(dataset_id).strip():
        raise ValueError("dataset_id is required")
    if not 10 <= int(lookback_days) <= 120:
        raise ValueError("lookback_days must be between 10 and 120")
    if not 1 <= int(page_size) <= 10_000 or not 1 <= int(max_pages) <= 100:
        raise ValueError("label pagination is out of bounds")
    if int(page_size) * int(max_pages) > int(app_config.ML_MAINTENANCE_MAX_ROWS):
        raise ValueError("label pagination exceeds maintenance row limit")
    history = HistoricalStore(history_db, max_db_bytes=app_config.ML_HISTORY_DB_MAX_BYTES)
    store = MlStore(ml_db, max_bytes=app_config.ML_DB_MAX_BYTES)
    if not Path(history_db).is_file():
        raise FileNotFoundError(f"history database not found: {history_db}")
    history.initialize()
    store.initialize()
    as_of_dt = _parse_now(as_of)
    decision_start = _aware_iso(as_of_dt - timedelta(days=int(lookback_days)))
    cursor: str | None = None
    pages = 0
    totals = {
        "candidate_count": 0,
        "filled_count": 0,
        "no_fill_count": 0,
        "pending_count": 0,
        "changed_count": 0,
    }
    while pages < int(max_pages):
        result = build_labels(
            history,
            store,
            str(dataset_id),
            _aware_iso(as_of_dt),
            app_config.SIMULATION_FEE_SCHEDULE,
            LabelPolicy(),
            decision_start=decision_start,
            decision_end=_aware_iso(as_of_dt),
            cursor=cursor,
            limit=int(page_size),
        )
        pages += 1
        for name in totals:
            totals[name] += int(getattr(result, name))
        cursor = result.next_cursor
        if not cursor:
            break
    return {
        "status": "partial" if cursor else "success",
        "command": "ml-labels",
        "dataset_id": str(dataset_id),
        "as_of": _aware_iso(as_of_dt),
        "decision_start": decision_start,
        "pages": pages,
        "truncated": bool(cursor),
        **totals,
    }


def _feature_value(raw: object) -> object:
    if isinstance(raw, Mapping) and "value" in raw:
        return raw.get("value")
    return raw


def load_training_candidates(
    history_db: Path,
    *,
    dataset_id: str,
    start_date: str,
    end_date: str,
    feature_allowlist: Sequence[str],
    max_rows: int,
) -> list[dict[str, object]]:
    if not 1 <= int(max_rows) <= 1_000_000:
        raise ValueError("max_rows must be between 1 and 1000000")
    store = HistoricalStore(history_db, max_db_bytes=app_config.ML_HISTORY_DB_MAX_BYTES)
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT * FROM decision_candidates
               WHERE dataset_id=? AND trade_date BETWEEN ? AND ?
               ORDER BY decision_at,sample_id LIMIT ?""",
            (str(dataset_id), str(start_date), str(end_date), int(max_rows) + 1),
        ).fetchall()
    if len(rows) > int(max_rows):
        raise ValueError("training candidate row limit exceeded")
    result: list[dict[str, object]] = []
    for row in rows:
        record = dict(row)
        values = json.loads(str(record.pop("features_json")))
        times = json.loads(str(record.pop("feature_times_json")))
        if "market_regime" in feature_allowlist and "market_regime" not in values:
            values["market_regime"] = record.get("market_regime")
            times["market_regime"] = record.get("decision_at")
        features = {
            name: {"value": values[name], "available_at": times[name]}
            for name in feature_allowlist
            if name in values and name in times
        }
        record["features"] = features
        for audit_name in ("rule_order", "rule_target_qty", "rule_slot_count"):
            if audit_name in values:
                record[audit_name] = _feature_value(values[audit_name])
        record["rule_score"] = _feature_value(values.get("rule_score", values.get("final_score")))
        record["candidate_content_sha256"] = record.get("content_sha256")
        result.append(record)
    return result


def load_training_labels(
    store: MlStore,
    *,
    dataset_id: str,
    label_source: str,
    label_version: str,
    cost_sha256: str,
    policy_sha256: str,
    wanted_sample_ids: set[str],
    max_rows: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    cursor: str | None = None
    scanned = 0
    while scanned <= int(max_rows):
        page = store.training_label_rows(
            dataset_id=dataset_id,
            label_source=label_source,
            label_version=label_version,
            cost_sha256=cost_sha256,
            policy_sha256=policy_sha256,
            cursor=cursor,
            limit=min(1000, int(max_rows) + 1 - scanned),
        )
        if not page:
            break
        scanned += len(page)
        if scanned > int(max_rows):
            raise ValueError("training label scan limit exceeded")
        rows.extend(item for item in page if str(item.get("sample_id")) in wanted_sample_ids)
        cursor = str(page[-1]["label_id"])
        if len(page) < 1000:
            break
    if len(rows) > int(max_rows):
        raise ValueError("training label row limit exceeded")
    return rows


def run_training_job(
    *,
    history_db: Path,
    ml_db: Path,
    model_dir: Path,
    dataset_id: str,
    start_date: str,
    end_date: str,
    label_source: str,
    label_version: str,
    cost_sha256: str,
    policy_sha256: str,
    feature_allowlist: Sequence[str] = DEFAULT_FEATURE_ALLOWLIST,
    max_rows: int = 500_000,
) -> dict[str, object]:
    if not all(str(value).strip() for value in (dataset_id, start_date, end_date, label_source, label_version, cost_sha256, policy_sha256)):
        raise ValueError("complete training dataset and label contract is required")
    candidates = load_training_candidates(
        history_db,
        dataset_id=dataset_id,
        start_date=start_date,
        end_date=end_date,
        feature_allowlist=tuple(feature_allowlist),
        max_rows=max_rows,
    )
    if not candidates:
        raise ValueError("no strict training candidates found")
    store = MlStore(ml_db, max_bytes=app_config.ML_DB_MAX_BYTES)
    store.initialize()
    labels = load_training_labels(
        store,
        dataset_id=dataset_id,
        label_source=label_source,
        label_version=label_version,
        cost_sha256=cost_sha256,
        policy_sha256=policy_sha256,
        wanted_sample_ids={str(item["sample_id"]) for item in candidates},
        max_rows=max_rows,
    )
    frame = build_training_frame(candidates, labels, tuple(feature_allowlist))
    splits = build_ml_splits(frame, holdout_days=40, folds=3, embargo_days=10)
    active_parent = store.runtime_state().get("active_model_id")
    result = train_challenger(
        frame,
        splits,
        TrainingConfig(
            parent_model_id=(str(active_parent) if active_parent else None)
        ),
        model_dir,
    )
    artifact_relative = Path(result.artifact_dir).resolve().relative_to(
        Path(model_dir).resolve()
    )
    registered = store.register_model(
        result.manifest,
        artifact_path=artifact_relative.as_posix(),
        artifact_sha256=result.artifact_sha256,
        status=result.status,
    )
    return {
        "status": "success",
        "command": "ml-train",
        "model_id": result.model_id,
        "model_status": result.status,
        "registered": bool(registered),
        "approvable_l0": bool(result.approvable_l0),
        "failed_gates": list(result.failed_gates),
        "artifact_sha256": result.artifact_sha256,
        "manifest_sha256": result.manifest_sha256,
        "candidate_count": len(candidates),
        "label_count": len(labels),
        "permission_changed": False,
    }


def model_status(store: MlStore) -> dict[str, object]:
    store.initialize()
    state = store.runtime_state()
    health_reader = getattr(store, "runtime_health", None)
    health = health_reader() if callable(health_reader) else {}
    active_model = None
    approved_event = None
    model_id = state.get("active_model_id")
    if model_id:
        active_model = store.model_record(str(model_id))
        if isinstance(active_model, Mapping):
            approved_event = store.approved_model_event(
                str(model_id), str(active_model.get("artifact_sha256") or "")
            )
    main_size = store.path.stat().st_size if store.path.exists() else 0
    wal_path = Path(f"{store.path}-wal")
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0
    db_size = main_size + wal_size
    return {
        "status": "success",
        "command": "ml-model-status",
        "schema_version": store.schema_version(),
        "integrity_check": store.integrity_check(),
        "db_size_bytes": db_size,
        "db_main_size_bytes": main_size,
        "db_wal_size_bytes": wal_size,
        "db_limit_bytes": store.max_bytes,
        "detail_capacity_status": (
            "stopped" if db_size >= store.max_bytes else
            "warning" if db_size >= min(1_000_000_000, store.max_bytes) else
            "ok"
        ),
        "counts": store.counts(),
        "runtime": state,
        "runtime_health": health,
        "active_model": active_model,
        "approved_event": approved_event,
    }


def _feature_allowlist(value: str | None) -> tuple[str, ...]:
    if value is None or not str(value).strip():
        return DEFAULT_FEATURE_ALLOWLIST
    names = tuple(name.strip() for name in str(value).split(",") if name.strip())
    if not names:
        raise ValueError("feature allowlist is empty")
    if len(names) != len(set(names)):
        raise ValueError("feature allowlist contains duplicates")
    return names


def _backup_settings(kind: str) -> tuple[Path, Path, int]:
    if kind == "ml":
        return app_config.ML_DB_FILE, app_config.ML_BACKUP_DIR, ML_SCHEMA_VERSION
    if kind == "history":
        return app_config.ML_HISTORY_DB_FILE, app_config.HISTORY_BACKUP_DIR, HISTORY_SCHEMA_VERSION
    raise ValueError("backup kind must be ml or history")


def _retention_settings(kind: str) -> tuple[int, int, int]:
    if kind == "ml":
        return (
            app_config.ML_BACKUP_DAILY_KEEP,
            app_config.ML_BACKUP_WEEKLY_KEEP,
            app_config.ML_BACKUP_MONTHLY_KEEP,
        )
    return (
        app_config.HISTORY_BACKUP_DAILY_KEEP,
        app_config.HISTORY_BACKUP_WEEKLY_KEEP,
        app_config.HISTORY_BACKUP_MONTHLY_KEEP,
    )


def _kinds(value: str) -> tuple[str, ...]:
    return ("ml", "history") if value == "both" else (value,)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate bounded ML evidence maintenance")
    commands = parser.add_subparsers(dest="command", required=True)

    labels = commands.add_parser("labels")
    labels.add_argument("--dataset-id", default=app_config.ML_HISTORY_DATASET_ID)
    labels.add_argument("--as-of")
    labels.add_argument("--lookback-days", type=int, default=app_config.ML_LABEL_LOOKBACK_DAYS)
    labels.add_argument("--allow-unconfigured", action="store_true")

    train = commands.add_parser("train")
    train.add_argument("--dataset-id", default=app_config.ML_HISTORY_DATASET_ID)
    train.add_argument("--start", default=app_config.ML_TRAINING_START_DATE)
    train.add_argument("--end", default=app_config.ML_TRAINING_END_DATE)
    train.add_argument("--label-source", default=app_config.ML_LABEL_SOURCE)
    train.add_argument("--label-version", default=app_config.ML_LABEL_VERSION)
    train.add_argument("--cost-sha256", default=app_config.ML_COST_SHA256)
    train.add_argument("--policy-sha256", default=app_config.ML_POLICY_SHA256)
    train.add_argument("--features", default=app_config.ML_FEATURE_ALLOWLIST_TEXT)
    train.add_argument("--max-rows", type=int, default=app_config.ML_MAINTENANCE_MAX_ROWS)
    train.add_argument("--allow-unconfigured", action="store_true")

    commands.add_parser("model-status")
    for name in ("backup", "restore-check", "retention-dry-run", "retention-apply"):
        command = commands.add_parser(name)
        command.add_argument("--kind", choices=("ml", "history", "both"), default="both")
        command.add_argument("--now")

    args = parser.parse_args(argv)
    try:
        if args.command == "labels":
            if not str(args.dataset_id).strip() and args.allow_unconfigured:
                result = {"status": "not_configured", "command": "ml-labels"}
            else:
                result = run_label_job(
                    history_db=app_config.ML_HISTORY_DB_FILE,
                    ml_db=app_config.ML_DB_FILE,
                    dataset_id=args.dataset_id,
                    as_of=args.as_of or _aware_iso(datetime.now(SHANGHAI)),
                    lookback_days=args.lookback_days,
                )
        elif args.command == "train":
            required = (args.dataset_id, args.start, args.end, args.cost_sha256, args.policy_sha256)
            if not all(str(value).strip() for value in required) and args.allow_unconfigured:
                result = {"status": "not_configured", "command": "ml-train"}
            else:
                result = run_training_job(
                    history_db=app_config.ML_HISTORY_DB_FILE,
                    ml_db=app_config.ML_DB_FILE,
                    model_dir=app_config.ML_MODEL_DIR,
                    dataset_id=args.dataset_id,
                    start_date=args.start,
                    end_date=args.end,
                    label_source=args.label_source,
                    label_version=args.label_version,
                    cost_sha256=args.cost_sha256,
                    policy_sha256=args.policy_sha256,
                    feature_allowlist=_feature_allowlist(args.features),
                    max_rows=args.max_rows,
                )
        elif args.command == "model-status":
            result = model_status(MlStore(app_config.ML_DB_FILE, max_bytes=app_config.ML_DB_MAX_BYTES))
        else:
            now = _parse_now(args.now)
            results: dict[str, object] = {}
            for kind in _kinds(args.kind):
                db_file, root, schema = _backup_settings(kind)
                daily, weekly, monthly = _retention_settings(kind)
                if args.command == "backup":
                    results[kind] = create_database_backup(
                        kind,
                        db_file,
                        root,
                        now=now,
                        project_root=app_config.BASE_DIR,
                        expected_schema=schema,
                        keep_daily=daily,
                        keep_weekly=weekly,
                        keep_monthly=monthly,
                    )
                elif args.command == "restore-check":
                    results[kind] = restore_check(root, now=now, kind=kind)
                elif args.command == "retention-dry-run":
                    results[kind] = retention_plan(
                        root,
                        keep_daily=daily,
                        keep_weekly=weekly,
                        keep_monthly=monthly,
                        kind=kind,
                    )
                else:
                    results[kind] = apply_retention(
                        root,
                        now=now,
                        keep_daily=daily,
                        keep_weekly=weekly,
                        keep_monthly=monthly,
                        kind=kind,
                    )
            result = {"status": "success", "command": f"ml-{args.command}", "results": results}
    except Exception as exc:
        result = {
            "status": "failed",
            "command": f"ml-{args.command}",
            "error_code": type(exc).__name__,
            "error": str(exc)[:500],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("status") in {"success", "not_configured"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
