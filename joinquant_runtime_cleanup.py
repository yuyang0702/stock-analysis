from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import config as app_config
from trading_backup import create_backup


INCIDENT_ID = "joinquant-backtest-20260808-20260810"
SOURCE_START = "2026-08-08 03:19:56"
SOURCE_END = "2026-08-08 03:25:43"
AFFECTED_TRADE_DATE = "2026-08-08"
CLEANUP_VERSION = "2026-08-11.1"


class CleanupRefused(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _rows(conn: sqlite3.Connection, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, values).fetchall()]


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _source_snapshot(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    received = str(payload.get("received_at") or payload.get("generated_at") or "")[:19]
    return (
        str(payload.get("trade_date") or "") == AFFECTED_TRADE_DATE
        and SOURCE_START <= received < SOURCE_END
    )


def _jsonl_partition(
    path: Path, selected: Callable[[dict[str, Any]], bool],
) -> tuple[list[str], list[str], int]:
    if not path.is_file():
        return [], [], 0
    kept: list[str] = []
    removed: list[str] = []
    invalid = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                kept.append(line)
                invalid += 1
                continue
            if isinstance(payload, dict) and selected(payload):
                removed.append(line)
            else:
                kept.append(line)
    return kept, removed, invalid


def _api_event_selected(payload: dict[str, Any]) -> bool:
    received = str(payload.get("received_at") or "")[:19]
    return SOURCE_START <= received < SOURCE_END


def _derived_event_selector(cleanup_end: str) -> Callable[[dict[str, Any]], bool]:
    def selected(payload: dict[str, Any]) -> bool:
        timestamp = str(payload.get("ts") or "")[:19]
        return (
            payload.get("action") == "joinquant_sync"
            and SOURCE_START <= timestamp <= cleanup_end
        )

    return selected


def _period_summary(
    conn: sqlite3.Connection, api_events: list[str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {"account_snapshots": {}, "strategy_runs": {}, "api_events": {}}
    if _table_exists(conn, "account_snapshots"):
        summary["account_snapshots"] = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                """SELECT trade_date, COUNT(*) FROM account_snapshots
                   WHERE trade_date BETWEEN '2026-08-08' AND '2026-08-10'
                   GROUP BY trade_date ORDER BY trade_date"""
            )
        }
    if _table_exists(conn, "strategy_runs"):
        summary["strategy_runs"] = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                """SELECT trade_date, COUNT(*) FROM strategy_runs
                   WHERE trade_date BETWEEN '2026-08-08' AND '2026-08-10'
                   GROUP BY trade_date ORDER BY trade_date"""
            )
        }
    for line in api_events:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        day = str(item.get("received_at") or "")[:10]
        if "2026-08-08" <= day <= "2026-08-10":
            summary["api_events"][day] = int(summary["api_events"].get(day, 0)) + 1
    return summary


def inspect_incident(
    db_file: Path,
    account_file: Path,
    positions_file: Path,
    account_history_file: Path,
    api_event_file: Path,
    portfolio_event_file: Path,
    *,
    cleanup_end: str | None = None,
) -> dict[str, Any]:
    end = (cleanup_end or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))[:19]
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        snapshot_rows = _rows(
            conn,
            """SELECT * FROM account_snapshots
               WHERE received_at>=? AND received_at<? ORDER BY received_at, snapshot_id""",
            (SOURCE_START, SOURCE_END),
        )
        snapshot_ids = [str(row["snapshot_id"]) for row in snapshot_rows]
        reconciliation_rows = (
            _rows(
                conn,
                "SELECT * FROM reconciliation_runs WHERE snapshot_id IN ("
                + _placeholders(snapshot_ids)
                + ") ORDER BY started_at, reconciliation_id",
                tuple(snapshot_ids),
            )
            if snapshot_ids else []
        )
        reconciliation_ids = [str(row["reconciliation_id"]) for row in reconciliation_rows]
        position_rows = (
            _rows(
                conn,
                "SELECT * FROM position_snapshots WHERE snapshot_id IN ("
                + _placeholders(snapshot_ids) + ")",
                tuple(snapshot_ids),
            )
            if snapshot_ids else []
        )
        reconciliation_item_rows = (
            _rows(
                conn,
                "SELECT * FROM reconciliation_items WHERE reconciliation_id IN ("
                + _placeholders(reconciliation_ids) + ")",
                tuple(reconciliation_ids),
            )
            if reconciliation_ids else []
        )
        control_refs = (
            int(conn.execute(
                "SELECT COUNT(*) FROM control_events WHERE reconciliation_id IN ("
                + _placeholders(reconciliation_ids) + ")",
                tuple(reconciliation_ids),
            ).fetchone()[0])
            if reconciliation_ids else 0
        )
        issue_refs = (
            int(conn.execute(
                "SELECT COUNT(*) FROM execution_issue_state WHERE reconciliation_id IN ("
                + _placeholders(reconciliation_ids) + ")",
                tuple(reconciliation_ids),
            ).fetchone()[0])
            if reconciliation_ids else 0
        )
        broker_rows = _rows(
            conn, "SELECT * FROM broker_snapshot_current WHERE trade_date=?",
            (AFFECTED_TRADE_DATE,),
        )
        broker_scopes = [str(row["account_scope_id"]) for row in broker_rows]
        broker_snapshot_ids = [str(row["snapshot_id"]) for row in broker_rows]
        broker_positions = (
            _rows(
                conn, "SELECT * FROM broker_position_current WHERE account_scope_id IN ("
                + _placeholders(broker_scopes) + ")", tuple(broker_scopes),
            ) if broker_scopes else []
        )
        broker_orders = (
            _rows(
                conn, "SELECT * FROM broker_order_current WHERE account_scope_id IN ("
                + _placeholders(broker_scopes) + ")", tuple(broker_scopes),
            ) if broker_scopes else []
        )
        other_broker_reconciliation_refs = (
            int(conn.execute(
                """SELECT COUNT(*) FROM reconciliation_runs
                   WHERE broker_snapshot_id IN ("""
                + _placeholders(broker_snapshot_ids)
                + ") AND reconciliation_id NOT IN ("
                + (_placeholders(reconciliation_ids) if reconciliation_ids else "''")
                + ")",
                tuple(broker_snapshot_ids) + tuple(reconciliation_ids),
            ).fetchone()[0])
            if broker_snapshot_ids else 0
        )
        daily_equity = _rows(
            conn, "SELECT * FROM daily_equity WHERE trade_date=?",
            (AFFECTED_TRADE_DATE,),
        )
        material_payloads = 0
        pruned_payloads = 0
        malformed_payloads = 0
        for row in snapshot_rows:
            raw_json = str(row.get("raw_json") or "").strip()
            if not raw_json:
                pruned_payloads += 1
                continue
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                malformed_payloads += 1
                continue
            if any(payload.get(name) for name in ("positions", "orders", "trades")):
                material_payloads += 1
        nonzero_position_value_rows = sum(
            1 for row in snapshot_rows
            if abs(float(row.get("position_market_value") or 0)) > 0.000001
        )
        created_orders = int(conn.execute(
            """SELECT COUNT(*) FROM orders
               WHERE COALESCE(first_submitted_at, updated_at)>=?
                 AND COALESCE(first_submitted_at, updated_at)<?""",
            (SOURCE_START, SOURCE_END),
        ).fetchone()[0])
        created_fills = int(conn.execute(
            "SELECT COUNT(*) FROM fills WHERE filled_at>=? AND filled_at<?",
            (SOURCE_START, SOURCE_END),
        ).fetchone()[0])
        account_kept, account_removed, account_invalid = _jsonl_partition(
            account_history_file, _source_snapshot,
        )
        api_kept, api_removed, api_invalid = _jsonl_partition(
            api_event_file, _api_event_selected,
        )
        portfolio_kept, portfolio_removed, portfolio_invalid = _jsonl_partition(
            portfolio_event_file, _derived_event_selector(end),
        )
        active_account = False
        if account_file.is_file():
            try:
                active_account = _source_snapshot(
                    json.loads(account_file.read_text(encoding="utf-8"))
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        active_positions = False
        if positions_file.is_file():
            try:
                current = json.loads(positions_file.read_text(encoding="utf-8"))
                account = current.get("account") if isinstance(current, dict) else None
                active_positions = (
                    isinstance(account, dict)
                    and str(account.get("trade_date") or "") == AFFECTED_TRADE_DATE
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        blockers = {
            "control_event_references": control_refs,
            "execution_issue_references": issue_refs,
            "position_snapshot_rows": len(position_rows),
            "reconciliation_item_rows": len(reconciliation_item_rows),
            "broker_position_rows": len(broker_positions),
            "broker_order_rows": len(broker_orders),
            "other_broker_reconciliation_references": other_broker_reconciliation_refs,
            "material_snapshot_payloads": material_payloads,
            "malformed_snapshot_payloads": malformed_payloads,
            "nonzero_position_market_value_rows": nonzero_position_value_rows,
            "orders_created_in_source_window": created_orders,
            "fills_created_in_source_window": created_fills,
        }
        all_api_lines = api_kept + api_removed
        return {
            "incident_id": INCIDENT_ID,
            "cleanup_version": CLEANUP_VERSION,
            "source_start": SOURCE_START,
            "source_end": SOURCE_END,
            "cleanup_end": end,
            "period_summary": _period_summary(conn, all_api_lines),
            "db_rows": {
                "account_snapshots": snapshot_rows,
                "position_snapshots": position_rows,
                "reconciliation_runs": reconciliation_rows,
                "reconciliation_items": reconciliation_item_rows,
                "broker_snapshot_current": broker_rows,
                "broker_position_current": broker_positions,
                "broker_order_current": broker_orders,
                "daily_equity": daily_equity,
            },
            "counts": {
                "account_snapshots": len(snapshot_rows),
                "reconciliation_runs": len(reconciliation_rows),
                "broker_snapshot_current": len(broker_rows),
                "daily_equity": len(daily_equity),
                "account_history_lines": len(account_removed),
                "api_event_lines": len(api_removed),
                "derived_portfolio_event_lines": len(portfolio_removed),
                "active_account_file": int(active_account),
                "active_positions_file": int(active_positions),
            },
            "invalid_jsonl_lines_preserved": {
                "account_history": account_invalid,
                "api_events": api_invalid,
                "portfolio_events": portfolio_invalid,
            },
            "blockers": blockers,
            "warnings": {
                "retention_pruned_snapshot_payloads": pruned_payloads,
            },
            "selected_lines": {
                "account_history": account_removed,
                "api_events": api_removed,
                "portfolio_events": portfolio_removed,
            },
            "kept_lines": {
                "account_history": account_kept,
                "api_events": api_kept,
                "portfolio_events": portfolio_kept,
            },
            "active_files": {
                str(account_file): active_account,
                str(positions_file): active_positions,
            },
            "integrity_check": str(conn.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
        }
    finally:
        conn.close()


def _public_report(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in audit.items()
        if key not in {"db_rows", "selected_lines", "kept_lines", "active_files"}
    }


def _write_jsonl(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".cleanup-" + uuid.uuid4().hex)
    with temporary.open("w", encoding="utf-8", newline="") as fh:
        for line in lines:
            fh.write(line if line.endswith("\n") else line + "\n")
    os.replace(temporary, path)


def _export_rows(root: Path, rows_by_table: dict[str, list[dict[str, Any]]]) -> None:
    for table, rows in rows_by_table.items():
        path = root / "database" / f"{table}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _secure_tree(root: Path) -> None:
    try:
        root.chmod(stat.S_IRWXU)
        for path in root.rglob("*"):
            path.chmod(stat.S_IRWXU if path.is_dir() else stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def apply_incident_cleanup(
    db_file: Path,
    account_file: Path,
    positions_file: Path,
    account_history_file: Path,
    api_event_file: Path,
    portfolio_event_file: Path,
    backup_root: Path,
    project_root: Path,
    *,
    cleanup_end: str | None = None,
) -> dict[str, Any]:
    project = project_root.resolve()
    backup = backup_root.resolve()
    try:
        backup.relative_to(project)
    except ValueError:
        pass
    else:
        raise CleanupRefused("quarantine and backup root must be outside the project")
    quarantine = backup / "quarantine" / INCIDENT_ID
    completed_manifest = quarantine / "manifest.json"
    if completed_manifest.is_file():
        existing = json.loads(completed_manifest.read_text(encoding="utf-8"))
        if existing.get("status") == "completed":
            return {"status": "already_completed", "quarantine": str(quarantine)}
        raise CleanupRefused("existing quarantine is incomplete and requires manual review")
    audit = inspect_incident(
        db_file, account_file, positions_file, account_history_file,
        api_event_file, portfolio_event_file, cleanup_end=cleanup_end,
    )
    if audit["integrity_check"] != "ok" or audit["foreign_key_violations"]:
        raise CleanupRefused("database integrity is not clean before cleanup")
    nonzero_blockers = {
        key: value for key, value in audit["blockers"].items() if int(value)
    }
    if nonzero_blockers:
        raise CleanupRefused(
            "material or referenced incident data requires manual review: "
            + json.dumps(nonzero_blockers, sort_keys=True)
        )
    if not any(int(value) for value in audit["counts"].values()):
        return {"status": "already_clean", **_public_report(audit)}

    backup_result = create_backup(
        db_file, backup,
        now=datetime.now(), project_root=project,
        keep_daily=app_config.TRADING_BACKUP_DAILY_KEEP,
        keep_weekly=app_config.TRADING_BACKUP_WEEKLY_KEEP,
        keep_monthly=app_config.TRADING_BACKUP_MONTHLY_KEEP,
    )
    if (
        backup_result.get("status") != "success"
        or backup_result.get("integrity_check") != "ok"
        or not Path(str(backup_result.get("backup_file") or "")).is_file()
    ):
        raise CleanupRefused("verified pre-cleanup database backup failed")

    quarantine.mkdir(parents=True, exist_ok=False)
    originals = quarantine / "originals"
    selected = quarantine / "selected"
    _export_rows(quarantine, audit["db_rows"])
    file_map = {
        "account_snapshot_history.jsonl": account_history_file,
        "api_events.jsonl": api_event_file,
        "portfolio_events.jsonl": portfolio_event_file,
        "account_snapshot.json": account_file,
        "positions.json": positions_file,
    }
    original_hashes: dict[str, str] = {}
    for name, source in file_map.items():
        if source.is_file():
            originals.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, originals / name)
            original_hashes[name] = _sha256(originals / name)
    for name, key in (
        ("account_snapshot_history.jsonl", "account_history"),
        ("api_events.jsonl", "api_events"),
        ("portfolio_events.jsonl", "portfolio_events"),
    ):
        _write_jsonl(selected / name, audit["selected_lines"][key])

    manifest = {
        "status": "prepared",
        **_public_report(audit),
        "reason": "JoinQuant backtest used the retired live adapter against production endpoints",
        "pre_cleanup_backup": {
            "backup_file": backup_result["backup_file"],
            "manifest_file": backup_result["manifest_file"],
            "sha256": backup_result["sha256"],
            "integrity_check": backup_result["integrity_check"],
        },
        "original_file_sha256": original_hashes,
    }
    _json_dump(completed_manifest, manifest)
    _secure_tree(quarantine)

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    snapshot_ids = [str(row["snapshot_id"]) for row in audit["db_rows"]["account_snapshots"]]
    reconciliation_ids = [
        str(row["reconciliation_id"])
        for row in audit["db_rows"]["reconciliation_runs"]
    ]
    replaced: list[tuple[Path, Path]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        if reconciliation_ids:
            conn.execute(
                "DELETE FROM reconciliation_runs WHERE reconciliation_id IN ("
                + _placeholders(reconciliation_ids) + ")",
                tuple(reconciliation_ids),
            )
        if snapshot_ids:
            conn.execute(
                "DELETE FROM account_snapshots WHERE snapshot_id IN ("
                + _placeholders(snapshot_ids) + ")",
                tuple(snapshot_ids),
            )
        conn.execute("DELETE FROM daily_equity WHERE trade_date=?", (AFFECTED_TRADE_DATE,))
        conn.execute(
            "DELETE FROM broker_snapshot_current WHERE trade_date=?",
            (AFFECTED_TRADE_DATE,),
        )
        for target, key, original_name in (
            (account_history_file, "account_history", "account_snapshot_history.jsonl"),
            (api_event_file, "api_events", "api_events.jsonl"),
            (portfolio_event_file, "portfolio_events", "portfolio_events.jsonl"),
        ):
            if target.is_file():
                _write_jsonl(target, audit["kept_lines"][key])
                replaced.append((target, originals / original_name))
        for target, original_name in (
            (account_file, "account_snapshot.json"),
            (positions_file, "positions.json"),
        ):
            if audit["active_files"].get(str(target)) and target.exists():
                target.unlink()
                replaced.append((target, originals / original_name))
        if str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("database integrity check failed after cleanup")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("database foreign key check failed after cleanup")
        remaining = int(conn.execute(
            "SELECT COUNT(*) FROM account_snapshots WHERE received_at>=? AND received_at<?",
            (SOURCE_START, SOURCE_END),
        ).fetchone()[0])
        if remaining:
            raise RuntimeError("incident snapshots remain after cleanup")
        conn.commit()
    except Exception:
        conn.rollback()
        for target, original in replaced:
            if original.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
        manifest["status"] = "failed_rolled_back"
        _json_dump(completed_manifest, manifest)
        _secure_tree(quarantine)
        raise
    finally:
        conn.close()

    verification = inspect_incident(
        db_file, account_file, positions_file, account_history_file,
        api_event_file, portfolio_event_file,
        cleanup_end=str(audit["cleanup_end"]),
    )
    if any(int(value) for value in verification["counts"].values()):
        raise RuntimeError("post-cleanup verification found remaining incident data")
    manifest["status"] = "completed"
    manifest["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest["post_cleanup"] = _public_report(verification)
    manifest["active_file_sha256"] = {
        name: _sha256(path)
        for name, path in file_map.items() if path.is_file()
    }
    _json_dump(completed_manifest, manifest)
    _secure_tree(quarantine)
    return {
        "status": "completed",
        "quarantine": str(quarantine),
        "manifest": str(completed_manifest),
        "pre_cleanup_backup": manifest["pre_cleanup_backup"],
        "removed": audit["counts"],
        "period_summary": audit["period_summary"],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and quarantine the 2026-08-08 JoinQuant backtest incident"
    )
    parser.add_argument("command", choices=("inspect", "apply"))
    parser.add_argument("--db", type=Path, default=app_config.TRADING_DB_FILE)
    parser.add_argument("--account-file", type=Path, default=app_config.JOINQUANT_ACCOUNT_FILE)
    parser.add_argument("--positions-file", type=Path, default=app_config.POSITIONS_FILE)
    parser.add_argument("--account-history-file", type=Path)
    parser.add_argument("--api-event-file", type=Path)
    parser.add_argument("--portfolio-event-file", type=Path, default=app_config.PORTFOLIO_EVENTS_FILE)
    parser.add_argument("--backup-root", type=Path, default=app_config.TRADING_BACKUP_DIR)
    parser.add_argument("--project-root", type=Path, default=app_config.BASE_DIR)
    parser.add_argument("--cleanup-end", help="fixed inclusive end for derived sync events")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    history = args.account_history_file or args.account_file.parent / "account_snapshot_history.jsonl"
    api_events = args.api_event_file or args.account_file.parent / "api_events.jsonl"
    if args.command == "inspect":
        result = _public_report(inspect_incident(
            args.db, args.account_file, args.positions_file, history,
            api_events, args.portfolio_event_file, cleanup_end=args.cleanup_end,
        ))
        result["status"] = "inspection_only"
    else:
        result = apply_incident_cleanup(
            args.db, args.account_file, args.positions_file, history,
            api_events, args.portfolio_event_file, args.backup_root,
            args.project_root, cleanup_end=args.cleanup_end,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
