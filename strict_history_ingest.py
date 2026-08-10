"""Verify, back up, and atomically ingest one strict-history monthly package."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping
from uuid import uuid4

import config as app_config
from historical_data import HISTORY_SCHEMA_VERSION, HistoricalStore
from joinquant_strict_history_exporter import verify_strict_package
from ml_maintenance import SHANGHAI, create_database_backup, database_facts


DATA_FILES = (
    "bars.csv",
    "status.csv",
    "universe.csv",
    "features.csv",
    "decision_candidates.jsonl",
    "candidate_prices.jsonl",
    "strict_manifest.json",
)
PACKAGE_FILES = (*DATA_FILES, "metadata.json")
CSV_KINDS = {
    "bars": "bars.csv",
    "status": "status.csv",
    "universe": "universe.csv",
    "features": "features.csv",
}
TABLE_BY_KIND = {
    "bars": "daily_bars",
    "status": "daily_status",
    "universe": "daily_universe",
    "features": "point_in_time_features",
    "decision_candidates": "decision_candidates",
    "candidate_prices": "candidate_prices",
}
DATASET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MONTH_RE = re.compile(r"^20\d{2}-(?:0[1-9]|1[0-2])$")
MAX_PACKAGE_BYTES = 3_000_000_000
MAX_COMPRESSION_RATIO = 200


class StrictHistoryIngestError(RuntimeError):
    """A monthly package cannot be safely accepted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrictHistoryIngestError(f"INVALID_{label.upper()}_JSON") from exc
    if not isinstance(value, dict):
        raise StrictHistoryIngestError(f"{label.upper()}_OBJECT_REQUIRED")
    return dict(value)


def _iter_jsonl_rows(path: Path) -> Iterator[dict[str, object]]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise StrictHistoryIngestError(
                        f"JSONL_OBJECT_REQUIRED:{path.name}:{line_number}"
                    )
                yield dict(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrictHistoryIngestError(f"INVALID_JSONL:{path.name}") from exc


def _csv_count(path: Path) -> int:
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise StrictHistoryIngestError(f"INVALID_CSV:{path.name}") from exc


def _safe_extract(package: Path, destination: Path, *, max_bytes: int) -> None:
    if not package.is_file() or not zipfile.is_zipfile(package):
        raise StrictHistoryIngestError("STRICT_PACKAGE_ZIP_REQUIRED")
    if package.stat().st_size > int(max_bytes):
        raise StrictHistoryIngestError("MONTHLY_ARCHIVE_LIMIT_EXCEEDED")
    with zipfile.ZipFile(package, "r") as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)) or set(names) != set(PACKAGE_FILES):
            raise StrictHistoryIngestError("STRICT_PACKAGE_FILE_SET_MISMATCH")
        total = 0
        for member in members:
            mode = (member.external_attr >> 16) & 0o170000
            path = Path(member.filename)
            if (
                member.is_dir()
                or mode == stat.S_IFLNK
                or "\\" in member.filename
                or path.is_absolute()
                or ".." in path.parts
                or len(path.parts) != 1
            ):
                raise StrictHistoryIngestError("UNSAFE_ARCHIVE_PATH")
            total += int(member.file_size)
            if total > int(max_bytes):
                raise StrictHistoryIngestError("MONTHLY_UNCOMPRESSED_LIMIT_EXCEEDED")
            compressed = max(1, int(member.compress_size))
            if member.file_size > 10_000_000 and member.file_size / compressed > MAX_COMPRESSION_RATIO:
                raise StrictHistoryIngestError("SUSPICIOUS_ARCHIVE_COMPRESSION_RATIO")
        for member in members:
            target = destination / member.filename
            with archive.open(member, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _validated_contract(root: Path) -> dict[str, object]:
    metadata = _json_object(root / "metadata.json", "metadata")
    manifest = _json_object(root / "strict_manifest.json", "strict_manifest")
    checksums = metadata.get("sha256")
    if not isinstance(checksums, Mapping) or set(checksums) != set(DATA_FILES):
        raise StrictHistoryIngestError("METADATA_SHA256_FILE_SET_MISMATCH")
    verification = verify_strict_package(root)
    if verification.get("accepted") is not True:
        raise StrictHistoryIngestError("STRICT_PACKAGE_REJECTED")

    dataset_id = str(manifest.get("dataset_id") or "")
    month = str(metadata.get("month") or "")
    if not DATASET_ID_RE.fullmatch(dataset_id):
        raise StrictHistoryIngestError("INVALID_DATASET_ID")
    if not MONTH_RE.fullmatch(month):
        raise StrictHistoryIngestError("INVALID_DATASET_MONTH")
    if str(metadata.get("dataset_id") or "") != dataset_id:
        raise StrictHistoryIngestError("METADATA_DATASET_MISMATCH")
    if str(manifest.get("source") or "") != "strict_history":
        raise StrictHistoryIngestError("STRICT_HISTORY_SOURCE_REQUIRED")
    if str(metadata.get("strict_source") or "") != "strict_history":
        raise StrictHistoryIngestError("METADATA_STRICT_SOURCE_REQUIRED")
    if metadata.get("strict") is not True:
        raise StrictHistoryIngestError("STRICT_EXPORT_REQUIRED")
    daily_complete = metadata.get("daily_features_complete")
    daily_required = metadata.get("daily_features_required")
    if daily_required is None:
        # Legacy packages did not declare whether header-only daily features
        # were intentional, so they remain fail-closed unless complete.
        if daily_complete is not True:
            raise StrictHistoryIngestError("COMPLETE_DAILY_FEATURES_REQUIRED")
    elif type(daily_required) is not bool or type(daily_complete) is not bool:
        raise StrictHistoryIngestError("DAILY_FEATURE_CONTRACT_BOOLEAN_REQUIRED")
    elif daily_required and not daily_complete:
        raise StrictHistoryIngestError("COMPLETE_DAILY_FEATURES_REQUIRED")
    if not str(metadata.get("exporter_version") or "").strip():
        raise StrictHistoryIngestError("EXPORTER_VERSION_REQUIRED")

    row_counts = metadata.get("rows")
    if not isinstance(row_counts, Mapping) or set(row_counts) != set(TABLE_BY_KIND):
        raise StrictHistoryIngestError("METADATA_ROW_COUNTS_REQUIRED")
    actual_counts = {
        **{kind: _csv_count(root / filename) for kind, filename in CSV_KINDS.items()},
        "decision_candidates": sum(
            1 for _ in _iter_jsonl_rows(root / "decision_candidates.jsonl")
        ),
        "candidate_prices": sum(
            1 for _ in _iter_jsonl_rows(root / "candidate_prices.jsonl")
        ),
    }
    for kind, actual in actual_counts.items():
        expected = row_counts.get(kind)
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != actual:
            raise StrictHistoryIngestError(f"METADATA_ROW_COUNT_MISMATCH:{kind}")
    return {
        "dataset_id": dataset_id,
        "month": month,
        "metadata": metadata,
        "manifest": manifest,
        "row_counts": actual_counts,
        "verification": verification,
    }


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as current, closing(
        sqlite3.connect(destination)
    ) as target:
        current.backup(target)


def _fsync_file(path: Path) -> None:
    with Path(path).open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


@contextmanager
def exclusive_ingest_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "created_at": datetime.now(SHANGHAI).isoformat()},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StrictHistoryIngestError("STRICT_HISTORY_INGEST_ALREADY_RUNNING") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        path.unlink(missing_ok=True)


def _stage_archive(package: Path, final: Path, package_sha256: str) -> Path | None:
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        if _sha256(final) != package_sha256:
            raise StrictHistoryIngestError("PACKAGE_VERSION_CONFLICT")
        return None
    temporary = final.with_name(f".{final.name}.{uuid4().hex}.tmp")
    shutil.copy2(package, temporary)
    if _sha256(temporary) != package_sha256:
        temporary.unlink(missing_ok=True)
        raise StrictHistoryIngestError("ARCHIVE_COPY_HASH_MISMATCH")
    _fsync_file(temporary)
    return temporary


def _import_into_stage(
    store: HistoricalStore,
    root: Path,
    dataset_id: str,
    manifest: Mapping[str, object],
    row_counts: Mapping[str, object],
) -> dict[str, int]:
    inserted = {
        kind: store.import_csv(dataset_id, kind, root / filename, "joinquant", "raw")
        for kind, filename in CSV_KINDS.items()
    }
    candidate_file = root / "decision_candidates.jsonl"
    price_file = root / "candidate_prices.jsonl"
    inserted["decision_candidates"] = store.import_candidate_cohorts_stream(
        _iter_jsonl_rows(candidate_file),
        manifest=manifest,
        expected_rows=int(row_counts["decision_candidates"]),
        payload_bytes=candidate_file.stat().st_size,
    )
    inserted["candidate_prices"] = store.import_candidate_prices_stream(
        _iter_jsonl_rows(price_file),
        manifest=manifest,
        expected_rows=int(row_counts["candidate_prices"]),
        payload_bytes=price_file.stat().st_size,
    )
    return inserted


def ingest_package(
    package: Path,
    *,
    db_path: Path,
    archive_root: Path,
    backup_root: Path,
    project_root: Path,
    max_package_bytes: int = MAX_PACKAGE_BYTES,
) -> dict[str, object]:
    package = Path(package).resolve()
    db_path = Path(db_path).resolve()
    archive_root = Path(archive_root).resolve()
    backup_root = Path(backup_root).resolve()
    project_root = Path(project_root).resolve()
    if not package.is_file():
        raise StrictHistoryIngestError("STRICT_PACKAGE_NOT_FOUND")
    package_sha256 = _sha256(package)
    staging_root = db_path.parent / ".strict-history-staging"
    staging_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=staging_root, prefix="package-") as extracted_text:
        extracted = Path(extracted_text)
        _safe_extract(package, extracted, max_bytes=max_package_bytes)
        contract = _validated_contract(extracted)
        dataset_id = str(contract["dataset_id"])
        month = str(contract["month"])
        manifest = contract["manifest"]
        assert isinstance(manifest, Mapping)

        final_archive = (
            archive_root
            / dataset_id
            / month.replace("-", "")
            / f"{dataset_id}-{month}.zip"
        )
        staged_archive = _stage_archive(package, final_archive, package_sha256)
        database_stage = staging_root / f"history-{uuid4().hex}.db"
        promoted_database = db_path.with_name(f".{db_path.name}.{uuid4().hex}.tmp")
        try:
            live_existed = db_path.is_file()
            if live_existed:
                live_store = HistoricalStore(
                    db_path, max_db_bytes=app_config.ML_HISTORY_DB_MAX_BYTES
                )
                live_store.initialize()
                before_counts = live_store.dataset_counts(dataset_id)
                _sqlite_backup(db_path, database_stage)
            else:
                stage_baseline = HistoricalStore(
                    database_stage, max_db_bytes=app_config.ML_HISTORY_DB_MAX_BYTES
                )
                stage_baseline.initialize()
                before_counts = stage_baseline.dataset_counts(dataset_id)
            backup = create_database_backup(
                "history",
                db_path if live_existed else database_stage,
                backup_root,
                now=datetime.now(SHANGHAI),
                project_root=project_root,
                expected_schema=HISTORY_SCHEMA_VERSION,
                keep_daily=app_config.HISTORY_BACKUP_DAILY_KEEP,
                keep_weekly=app_config.HISTORY_BACKUP_WEEKLY_KEEP,
                keep_monthly=app_config.HISTORY_BACKUP_MONTHLY_KEEP,
            )

            stage_store = HistoricalStore(
                database_stage, max_db_bytes=app_config.ML_HISTORY_DB_MAX_BYTES
            )
            stage_store.initialize()
            row_counts = contract["row_counts"]
            assert isinstance(row_counts, Mapping)
            inserted = _import_into_stage(
                stage_store, extracted, dataset_id, manifest, row_counts
            )
            after_counts = stage_store.dataset_counts(dataset_id)
            for kind, table in TABLE_BY_KIND.items():
                if after_counts[table] != before_counts[table] + inserted[kind]:
                    raise StrictHistoryIngestError(f"ATOMIC_IMPORT_COUNT_MISMATCH:{kind}")
                if after_counts[table] < int(contract["row_counts"][kind]):
                    raise StrictHistoryIngestError(f"PACKAGE_ROWS_NOT_PRESENT:{kind}")
            dataset_hash = stage_store.dataset_hash(dataset_id)
            stage_facts = database_facts(
                database_stage, expected_schema=HISTORY_SCHEMA_VERSION
            )

            _sqlite_backup(database_stage, promoted_database)
            promoted_facts = database_facts(
                promoted_database, expected_schema=HISTORY_SCHEMA_VERSION
            )
            promoted_store = HistoricalStore(
                promoted_database, max_db_bytes=app_config.ML_HISTORY_DB_MAX_BYTES
            )
            if promoted_store.dataset_counts(dataset_id) != after_counts:
                raise StrictHistoryIngestError("PROMOTED_DATABASE_COUNT_MISMATCH")
            if promoted_store.dataset_hash(dataset_id) != dataset_hash:
                raise StrictHistoryIngestError("PROMOTED_DATABASE_HASH_MISMATCH")
            if promoted_facts["table_counts"] != stage_facts["table_counts"]:
                raise StrictHistoryIngestError("PROMOTED_DATABASE_TABLE_COUNT_MISMATCH")
            _fsync_file(promoted_database)

            # Switching the old file to rollback journaling safely clears any
            # stale WAL sidecars before the single-file atomic replacement.
            if live_existed:
                with closing(sqlite3.connect(db_path, timeout=30)) as connection:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    mode = str(
                        connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                    )
                    if mode.lower() != "delete":
                        raise StrictHistoryIngestError("LIVE_DATABASE_BUSY")
                if Path(f"{db_path}-wal").exists() or Path(f"{db_path}-shm").exists():
                    raise StrictHistoryIngestError("LIVE_DATABASE_SIDECAR_STILL_PRESENT")

            if staged_archive is not None:
                os.replace(staged_archive, final_archive)
                staged_archive = None
            os.replace(promoted_database, db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")

            return {
                "status": "success",
                "command": "strict-history-ingest",
                "dataset_id": dataset_id,
                "month": month,
                "package": package.name,
                "package_sha256": package_sha256,
                "archive_file": str(final_archive),
                "backup_file": str(backup["backup_files"]["daily"]),
                "backup_sha256": str(backup["sha256"]),
                "inserted": inserted,
                "idempotent": sum(inserted.values()) == 0,
                "dataset_counts": after_counts,
                "dataset_hash": dataset_hash,
                "schema_version": HISTORY_SCHEMA_VERSION,
                "integrity_check": str(promoted_facts["integrity_check"]),
            }
        finally:
            database_stage.unlink(missing_ok=True)
            Path(f"{database_stage}-wal").unlink(missing_ok=True)
            Path(f"{database_stage}-shm").unlink(missing_ok=True)
            promoted_database.unlink(missing_ok=True)
            Path(f"{promoted_database}-wal").unlink(missing_ok=True)
            Path(f"{promoted_database}-shm").unlink(missing_ok=True)
            if staged_archive is not None:
                staged_archive.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify, back up, and atomically ingest one strict-history package"
    )
    parser.add_argument("--package", required=True)
    parser.add_argument("--db", default=str(app_config.ML_HISTORY_DB_FILE))
    parser.add_argument(
        "--archive-root", default=str(app_config.CACHE_DIR / "backtest" / "imports")
    )
    parser.add_argument("--backup-root", default=str(app_config.HISTORY_BACKUP_DIR))
    parser.add_argument("--project-root", default=str(app_config.BASE_DIR))
    parser.add_argument(
        "--report",
        default=str(app_config.OUTPUT_DIR / "strict_history_ingest_latest.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report_path = Path(args.report)
    lock_path = Path(args.db).parent / "strict_history_ingest.lock"
    try:
        with exclusive_ingest_lock(lock_path):
            result = ingest_package(
                Path(args.package),
                db_path=Path(args.db),
                archive_root=Path(args.archive_root),
                backup_root=Path(args.backup_root),
                project_root=Path(args.project_root),
            )
    except Exception as exc:
        result = {
            "status": "failed",
            "command": "strict-history-ingest",
            "package": Path(args.package).name,
            "error_code": type(exc).__name__,
            "error": " ".join(str(exc).split())[:500],
        }
    try:
        _atomic_json(report_path, result)
    except OSError:
        pass
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("status") == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
