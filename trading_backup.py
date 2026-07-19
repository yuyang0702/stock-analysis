from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import config as app_config
from notifier import WeComNotifier
from trading_store import TradingStore


CORE_TABLES = (
    "schema_migrations",
    "strategy_runs",
    "signals",
    "risk_decisions",
    "system_state",
    "position_cycles",
    "order_events",
    "exit_intents",
    "trade_cooldowns",
    "orders",
    "fills",
    "account_snapshots",
    "position_snapshots",
    "daily_equity",
    "reconciliation_runs",
    "reconciliation_items",
    "control_events",
    "execution_issue_state",
    "gap_reentry_opportunities",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
    )


def validate_backup_root(db_file: Path, backup_root: Path, project_root: Path) -> tuple[Path, Path]:
    db = Path(db_file).resolve()
    root = Path(backup_root).resolve()
    project = Path(project_root).resolve()
    if not db.is_file():
        raise FileNotFoundError(f"trading database not found: {db}")
    if root == project or root.is_relative_to(project):
        raise ValueError("backup directory must be outside project")
    if root == db or db.is_relative_to(root):
        raise ValueError("backup directory must not contain the live database")
    root.mkdir(parents=True, exist_ok=True)
    return db, root


def database_facts(db_file: Path) -> dict[str, object]:
    store = TradingStore(db_file)
    with store.connect() as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "schema_migrations" not in tables:
            raise ValueError("schema_migrations table missing")
        version_row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        counts = {
            name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in CORE_TABLES
            if name in tables
        }
        check_row = conn.execute("PRAGMA integrity_check").fetchone()
    check = str(check_row[0]) if check_row is not None else "missing"
    if check != "ok":
        raise ValueError(f"integrity_check failed: {check}")
    return {
        "integrity_check": check,
        "schema_version": int(version_row[0] or 0) if version_row is not None else 0,
        "table_counts": counts,
    }


def _slot_field(tier: str) -> str:
    try:
        return {"daily": "date_slot", "weekly": "iso_week_slot", "monthly": "month_slot"}[tier]
    except KeyError as exc:
        raise ValueError(f"unsupported backup tier: {tier}") from exc


def _valid_entry(backup_root: Path, tier: str, manifest_file: Path) -> dict[str, object] | None:
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("tier") != tier:
            return None
        if not str(manifest.get(_slot_field(tier)) or ""):
            return None
        backup_file = backup_root / tier / f"{manifest_file.stem}.db"
        if not backup_file.is_file() or _sha256(backup_file) != manifest.get("sha256"):
            return None
        facts = database_facts(backup_file)
        if facts["integrity_check"] != manifest.get("integrity_check"):
            return None
        if facts["schema_version"] != manifest.get("schema_version"):
            return None
        if facts["table_counts"] != manifest.get("table_counts"):
            return None
    except Exception:
        return None
    entry = dict(manifest)
    entry["backup_file"] = str(backup_file)
    entry["manifest_file"] = str(manifest_file)
    return entry


def validated_manifests(backup_root: Path, tier: str) -> list[dict[str, object]]:
    root = Path(backup_root)
    manifest_dir = root / "manifests" / tier
    if not manifest_dir.is_dir():
        return []
    entries = [
        entry
        for path in manifest_dir.glob("*.json")
        if (entry := _valid_entry(root, tier, path)) is not None
    ]
    field = _slot_field(tier)
    return sorted(entries, key=lambda item: (str(item[field]), str(item.get("created_at") or "")))


def _copy_or_link(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def promote_slot(
    backup_file: Path,
    manifest: dict[str, object],
    backup_root: Path,
    tier: str,
    slot: str,
) -> tuple[Path, Path]:
    root = Path(backup_root)
    tier_dir = root / tier
    manifest_dir = root / "manifests" / tier
    tier_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    safe_slot = slot.replace("/", "-").replace("\\", "-")
    sha256 = str(manifest["sha256"])
    stem = f"trading-{safe_slot}-{sha256[:12]}"
    final_db = tier_dir / f"{stem}.db"
    final_manifest = manifest_dir / f"{stem}.json"
    tier_manifest = dict(manifest)
    tier_manifest["tier"] = tier

    with TemporaryDirectory(dir=root, prefix=f".{tier}-") as tmp:
        temp_db = Path(tmp) / "backup.db"
        temp_manifest = Path(tmp) / "manifest.json"
        _copy_or_link(Path(backup_file), temp_db)
        if _sha256(temp_db) != sha256:
            raise ValueError(f"{tier} promotion sha256 mismatch")
        _write_json(temp_manifest, tier_manifest)
        db_existed = final_db.exists()
        temp_db.replace(final_db)
        try:
            temp_manifest.replace(final_manifest)
        except Exception:
            if not db_existed:
                final_db.unlink(missing_ok=True)
            raise

    field = _slot_field(tier)
    for entry in validated_manifests(root, tier):
        if str(entry[field]) != slot:
            continue
        old_db = Path(str(entry["backup_file"]))
        old_manifest = Path(str(entry["manifest_file"]))
        if old_db != final_db:
            old_db.unlink()
            old_manifest.unlink()
    return final_db, final_manifest


def prune_tier(backup_root: Path, tier: str, keep: int) -> dict[str, list[Path]]:
    root = Path(backup_root)
    tier_dir = root / tier
    manifest_dir = root / "manifests" / tier
    valid = validated_manifests(root, tier)
    valid_files = {
        path
        for entry in valid
        for path in (Path(str(entry["backup_file"])), Path(str(entry["manifest_file"])))
    }
    all_files = set(tier_dir.glob("*.db")) | set(manifest_dir.glob("*.json"))
    invalid = sorted(all_files - valid_files)
    field = _slot_field(tier)
    slots = sorted({str(entry[field]) for entry in valid})
    expired = set(slots[:-max(1, int(keep))])
    deleted: list[Path] = []
    for entry in valid:
        if str(entry[field]) not in expired:
            continue
        for path in (Path(str(entry["backup_file"])), Path(str(entry["manifest_file"]))):
            path.unlink()
            deleted.append(path)
    return {"deleted": deleted, "invalid": invalid}


def load_latest_status(backup_root: Path) -> dict[str, object]:
    path = Path(backup_root) / "status.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_status(backup_root: Path, command: str, result: dict[str, object]) -> None:
    status = load_latest_status(backup_root)
    previous = status.get(command)
    previous_entry = previous if isinstance(previous, dict) else {}
    entry = dict(result)
    if result.get("status") == "success":
        entry["last_success_at"] = result.get("finished_at")
    elif previous_entry.get("last_success_at"):
        entry["last_success_at"] = previous_entry["last_success_at"]
    status[command] = entry
    _atomic_write_json(Path(backup_root) / "status.json", status)


def write_latest_report(report_file: Path, result: dict[str, object]) -> None:
    counts = result.get("table_counts")
    count_lines = []
    if isinstance(counts, dict):
        count_lines = [f"- `{name}`: {value}" for name, value in sorted(counts.items())]
    lines = [
        "# SQLite 备份与恢复状态",
        "",
        f"- 命令：`{result.get('command') or '-'}`",
        f"- 状态：`{result.get('status') or '-'}`",
        f"- 阶段：`{result.get('stage') or '-'}`",
        f"- 完成时间：`{result.get('finished_at') or '-'}`",
        f"- Schema：`{result.get('schema_version') or '-'}`",
        f"- SHA-256：`{result.get('sha256') or '-'}`",
    ]
    if result.get("error"):
        lines.append(f"- 错误：`{str(result['error'])[:300]}`")
    if count_lines:
        lines.extend(["", "## 核心表计数", "", *count_lines])
    _atomic_write_text(Path(report_file), "\n".join(lines) + "\n")


def run_restore_drill(
    backup_root: Path,
    *,
    now: datetime,
    report_dir: Path,
) -> dict[str, object]:
    root = Path(backup_root)
    report_dir = Path(report_dir)
    stage = "select_backup"
    result: dict[str, object] = {
        "command": "drill",
        "status": "failed",
        "stage": stage,
        "started_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        candidates = [
            entry
            for tier in ("monthly", "weekly", "daily")
            for entry in validated_manifests(root, tier)
        ]
        if not candidates:
            raise ValueError("no valid backup available")
        selected = max(candidates, key=lambda item: str(item.get("created_at") or ""))
        source = Path(str(selected["backup_file"]))
        result["backup_id"] = source.name
        result["sha256"] = selected["sha256"]
        stage = "restore_copy"
        result["stage"] = stage
        drill_dir = root / "drill"
        drill_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=drill_dir, prefix="restore-") as tmp:
            restored = Path(tmp) / "trading.db"
            shutil.copy2(source, restored)
            stage = "verify_restore"
            result["stage"] = stage
            if _sha256(restored) != selected["sha256"]:
                raise ValueError("restored backup sha256 mismatch")
            facts = database_facts(restored)
            for key in ("integrity_check", "schema_version", "table_counts"):
                if facts[key] != selected.get(key):
                    raise ValueError(f"restored backup {key} mismatch")
        result.update({
            "status": "success",
            "stage": "complete",
            "integrity_check": facts["integrity_check"],
            "schema_version": facts["schema_version"],
            "table_counts": facts["table_counts"],
        })
    except Exception as exc:
        result["status"] = "failed"
        result["stage"] = stage
        result["error"] = str(exc)[:300]
    result["finished_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    _save_status(root, "drill", result)
    write_latest_report(report_dir / "trading_backup_latest.md", result)
    quarter = (now.month - 1) // 3 + 1
    write_latest_report(report_dir / f"trading_backup_drill_{now.year}-Q{quarter}.md", result)
    return result


def notify_failure(
    result: dict[str, object],
    *,
    webhook_url: str,
    state_file: Path,
    queue_file: Path,
) -> bool:
    if result.get("status") != "failed":
        return False
    command = str(result.get("command") or "unknown")
    stage = str(result.get("stage") or "unknown")
    notifier = WeComNotifier(
        webhook_url,
        Path(state_file),
        retry_queue_file=Path(queue_file),
    )
    return notifier.send_markdown(
        "SQLite备份异常",
        f"> 命令：{command}\n> 阶段：{stage}\n> 错误：{str(result.get('error') or '-')[:160]}",
        dedupe_key=f"trading-backup:{command}:{stage}",
    )


def create_backup(
    db_file: Path,
    backup_root: Path,
    *,
    now: datetime,
    project_root: Path,
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
) -> dict[str, object]:
    db, root = validate_backup_root(db_file, backup_root, project_root)

    with TemporaryDirectory(dir=root, prefix=".backup-") as tmp:
        temp_db = Path(tmp) / "trading.db"
        TradingStore(db).backup_to(temp_db)
        facts = database_facts(temp_db)
        sha256 = _sha256(temp_db)
        iso_year, iso_week, _ = now.isocalendar()
        result: dict[str, object] = {
            "status": "success",
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "source_db": str(db),
            "source_size": db.stat().st_size,
            "backup_size": temp_db.stat().st_size,
            "sha256": sha256,
            "schema_version": facts["schema_version"],
            "integrity_check": facts["integrity_check"],
            "table_counts": facts["table_counts"],
            "tier": "daily",
            "date_slot": now.strftime("%Y-%m-%d"),
            "iso_week_slot": f"{iso_year}-W{iso_week:02d}",
            "month_slot": now.strftime("%Y-%m"),
        }
        daily_db, daily_manifest = promote_slot(
            temp_db, result, root, "daily", str(result["date_slot"]),
        )
        promote_slot(daily_db, result, root, "weekly", str(result["iso_week_slot"]))
        promote_slot(daily_db, result, root, "monthly", str(result["month_slot"]))

    retention = {
        "daily": prune_tier(root, "daily", keep_daily),
        "weekly": prune_tier(root, "weekly", keep_weekly),
        "monthly": prune_tier(root, "monthly", keep_monthly),
    }
    result["backup_file"] = str(daily_db)
    result["manifest_file"] = str(daily_manifest)
    result["retention"] = retention
    return result


def _parse_now(value: str | None) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S") if value else datetime.now()


def _add_common_paths(parser: argparse.ArgumentParser, *, include_db: bool = False) -> None:
    if include_db:
        parser.add_argument("--db", type=Path, default=app_config.TRADING_DB_FILE)
    parser.add_argument("--backup-dir", type=Path, default=app_config.TRADING_BACKUP_DIR)
    parser.add_argument("--report-dir", type=Path, default=app_config.OUTPUT_DIR)
    parser.add_argument("--now", help="YYYY-MM-DD HH:MM:SS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verified SQLite backup and recovery drill")
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser("backup")
    _add_common_paths(backup_parser, include_db=True)
    drill_parser = commands.add_parser("drill")
    _add_common_paths(drill_parser)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--backup-dir", type=Path, default=app_config.TRADING_BACKUP_DIR)
    args = parser.parse_args(argv)

    if args.command == "status":
        status = load_latest_status(args.backup_dir)
        print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        backup = status.get("backup") if isinstance(status, dict) else None
        return 0 if isinstance(backup, dict) and backup.get("status") == "success" else 1

    now = _parse_now(args.now)
    if args.command == "drill":
        result = run_restore_drill(args.backup_dir, now=now, report_dir=args.report_dir)
    else:
        try:
            result = create_backup(
                args.db,
                args.backup_dir,
                now=now,
                project_root=app_config.BASE_DIR,
                keep_daily=app_config.TRADING_BACKUP_DAILY_KEEP,
                keep_weekly=app_config.TRADING_BACKUP_WEEKLY_KEEP,
                keep_monthly=app_config.TRADING_BACKUP_MONTHLY_KEEP,
            )
            result.update({
                "command": "backup",
                "stage": "complete",
                "finished_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as exc:
            result = {
                "command": "backup",
                "status": "failed",
                "stage": "backup",
                "error": str(exc)[:300],
                "finished_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        _save_status(args.backup_dir, "backup", result)
        write_latest_report(args.report_dir / "trading_backup_latest.md", result)

    if result.get("status") == "failed":
        notify_failure(
            result,
            webhook_url=app_config.WECOM_WEBHOOK_URL,
            state_file=app_config.CACHE_DIR / "trading_backup_notify_state.json",
            queue_file=app_config.CACHE_DIR / "notify_failed_queue.jsonl",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
