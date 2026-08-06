from __future__ import annotations

import argparse
import getpass
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from reconciliation import ReconciliationResult
from notification_outbox import (
    HIGH_CAPACITY_RECOVER_BYTES,
    HIGH_CAPACITY_RECOVER_ROWS,
    HIGH_CAPACITY_STOP_BYTES,
    HIGH_CAPACITY_STOP_ROWS,
    NOTIFICATION_WRITE_FAILURE_KEY,
)
from trading_store import TradingStore


class StaleControlStateError(RuntimeError):
    pass


NOTIFICATION_CAPACITY_OWNER_KEY = "notification_capacity_auto_resume_owner"


def _current(conn: object, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return default if row is None else str(row[0])


def _set_control(
    store: TradingStore, conn: object, *, key: str, value: str, action: str,
    reason: str, operator: str, reconciliation_id: str | None,
    _capacity_reconcile: bool = True,
) -> str | None:
    old = _current(conn, key, "1" if key == "buy_enabled" else "0")
    if old == value:
        return None
    store.set_system_state(conn, key, value, reason)
    event_id = str(uuid.uuid4())
    scope_row = conn.execute(
        """SELECT account_scope_id FROM reconciliation_runs
           WHERE reconciliation_id=?""",
        (reconciliation_id,),
    ).fetchone() if reconciliation_id else None
    scope = (
        str(scope_row[0]) if scope_row and scope_row[0]
        else store.registered_account_scope(conn, "joinquant", "primary")
    )
    store.insert_control_event(
        conn,
        event_id=event_id,
        action=action,
        operator=operator,
        old_value=old,
        new_value=value,
        reason=reason,
        reconciliation_id=reconciliation_id,
        created_at=datetime.now().isoformat(),
        account_scope_id=scope,
        _capacity_reconcile=_capacity_reconcile,
    )
    return event_id


def _capacity_owner(conn: object) -> dict[str, object]:
    raw = _current(conn, NOTIFICATION_CAPACITY_OWNER_KEY, "")
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def reconcile_notification_capacity(
    store: TradingStore,
    conn: object,
    *,
    now: str,
    cycle_id: str | None,
) -> dict[str, object] | None:
    if not getattr(conn, "in_transaction", False):
        raise ValueError("notification capacity reconciliation requires a transaction")
    now_text = store._notification_timestamp(now, "now")
    cycle = str(cycle_id or "").strip()
    capacity = store.notification_capacity(conn)
    pressure = bool(
        capacity.high_active_rows >= HIGH_CAPACITY_STOP_ROWS
        or capacity.high_active_bytes >= HIGH_CAPACITY_STOP_BYTES
        or capacity.high_unresolved_gap_rows > 0
        or _current(conn, NOTIFICATION_WRITE_FAILURE_KEY, "")
    )
    owner = _capacity_owner(conn)
    buy = conn.execute(
        "SELECT value, updated_at FROM system_state WHERE key='buy_enabled'",
    ).fetchone()
    buy_value = str(buy[0]) if buy else "1"
    buy_updated_at = str(buy[1]) if buy else ""

    if pressure:
        if buy_value == "1":
            event_id = _set_control(
                store, conn, key="buy_enabled", value="0", action="stop_buy",
                reason="notification high-priority capacity pressure",
                operator="NOTIFICATION_CAPACITY", reconciliation_id=None,
                _capacity_reconcile=False,
            )
            stopped = conn.execute(
                "SELECT value, updated_at FROM system_state WHERE key='buy_enabled'",
            ).fetchone()
            if event_id and stopped is not None:
                store.set_system_state(
                    conn,
                    NOTIFICATION_CAPACITY_OWNER_KEY,
                    json.dumps({
                        "owner": "notification_capacity",
                        "control_event_id": event_id,
                        "expected_value": "0",
                        "expected_updated_at": str(stopped[1]),
                        "stopped_at": now_text,
                        "last_cycle_id": cycle,
                        "low_cycle_id": "",
                        "low_checked_at": "",
                    }, ensure_ascii=False, sort_keys=True),
                    "notification-capacity-owned stop-buy",
                )
                return {"action": "stop_buy", "event_id": event_id}
        elif owner.get("owner") == "notification_capacity":
            if (
                buy_updated_at != str(owner.get("expected_updated_at") or "")
                or str(owner.get("expected_value") or "") != "0"
            ):
                store.set_system_state(
                    conn, NOTIFICATION_CAPACITY_OWNER_KEY, "",
                    "notification capacity control generation changed",
                )
                return None
            owner.update({
                "last_cycle_id": cycle or str(owner.get("last_cycle_id") or ""),
                "low_cycle_id": "",
                "low_checked_at": "",
            })
            store.set_system_state(
                conn, NOTIFICATION_CAPACITY_OWNER_KEY,
                json.dumps(owner, ensure_ascii=False, sort_keys=True),
                "notification capacity remains above recovery threshold",
            )
        return None

    if owner.get("owner") != "notification_capacity":
        return None
    if (
        buy_value != "0"
        or str(owner.get("expected_value") or "") != "0"
        or buy_updated_at != str(owner.get("expected_updated_at") or "")
    ):
        store.set_system_state(
            conn, NOTIFICATION_CAPACITY_OWNER_KEY, "",
            "notification capacity control generation changed",
        )
        return None
    if not cycle or cycle == str(owner.get("last_cycle_id") or ""):
        return None

    low = bool(
        capacity.high_active_rows < HIGH_CAPACITY_RECOVER_ROWS
        and capacity.high_active_bytes < HIGH_CAPACITY_RECOVER_BYTES
        and capacity.high_dead_rows == 0
        and capacity.high_unresolved_gap_rows == 0
        and _current(conn, "kill_switch", "0") == "0"
        and not _current(conn, "reconciliation_auto_resume_owner", "")
    )
    owner["last_cycle_id"] = cycle
    if not low:
        owner["low_cycle_id"] = ""
        owner["low_checked_at"] = ""
        store.set_system_state(
            conn, NOTIFICATION_CAPACITY_OWNER_KEY,
            json.dumps(owner, ensure_ascii=False, sort_keys=True),
            "notification capacity recovery conditions not met",
        )
        return None

    first_cycle = str(owner.get("low_cycle_id") or "")
    first_at = str(owner.get("low_checked_at") or "")
    if not first_cycle or not first_at:
        owner["low_cycle_id"] = cycle
        owner["low_checked_at"] = now_text
        store.set_system_state(
            conn, NOTIFICATION_CAPACITY_OWNER_KEY,
            json.dumps(owner, ensure_ascii=False, sort_keys=True),
            "notification capacity first low-water cycle",
        )
        return None
    try:
        elapsed = (
            datetime.fromisoformat(now_text)
            - datetime.fromisoformat(first_at)
        ).total_seconds()
    except (TypeError, ValueError, OverflowError):
        elapsed = -1
    if cycle == first_cycle or elapsed < 300:
        store.set_system_state(
            conn, NOTIFICATION_CAPACITY_OWNER_KEY,
            json.dumps(owner, ensure_ascii=False, sort_keys=True),
            "notification capacity awaiting second low-water cycle",
        )
        return None

    changed = conn.execute(
        """UPDATE system_state SET value='1', updated_at=?, reason=?
           WHERE key='buy_enabled' AND value='0' AND updated_at=?""",
        (
            now_text, "notification capacity recovered",
            str(owner["expected_updated_at"]),
        ),
    ).rowcount
    if changed != 1:
        store.set_system_state(
            conn, NOTIFICATION_CAPACITY_OWNER_KEY, "",
            "notification capacity recovery CAS lost",
        )
        return None
    event_id = str(uuid.uuid4())
    scope = store.registered_account_scope(conn, "joinquant", "primary")
    store.insert_control_event(
        conn,
        event_id=event_id,
        action="auto_resume_buy",
        operator="NOTIFICATION_CAPACITY",
        old_value="0",
        new_value="1",
        reason="two distinct five-minute low-water worker cycles",
        created_at=now_text,
        account_scope_id=scope,
        _capacity_reconcile=False,
    )
    store.set_system_state(
        conn, NOTIFICATION_CAPACITY_OWNER_KEY, "",
        "notification capacity automatic recovery complete",
    )
    return {"action": "auto_resume_buy", "event_id": event_id}


def apply_reconciliation_control(
    store: TradingStore, conn: object, result: ReconciliationResult, *, operator: str = "system"
) -> list[str]:
    persisted = conn.execute(
        """SELECT control_action FROM reconciliation_runs
           WHERE reconciliation_id=?""",
        (result.reconciliation_id,),
    ).fetchone()
    persisted_action = (
        str(persisted["control_action"] or "")
        if persisted is not None
        else result.control_action
    )
    actions: list[str] = []
    reason = f"reconciliation {result.reconciliation_id} {result.severity}"
    stop_event_id = None
    if result.severity in {"ERROR", "CRITICAL"}:
        if _current(conn, NOTIFICATION_CAPACITY_OWNER_KEY, ""):
            store.set_system_state(
                conn, NOTIFICATION_CAPACITY_OWNER_KEY, "",
                "reconciliation control precedence",
            )
        stop_event_id = _set_control(
            store, conn, key="buy_enabled", value="0", action="stop_buy", reason=reason,
            operator=operator, reconciliation_id=result.reconciliation_id,
        )
    if stop_event_id:
        actions.append("stop_buy")
    if result.severity == "ERROR" and stop_event_id:
        row = conn.execute(
            "SELECT value, updated_at FROM system_state WHERE key='buy_enabled'"
        ).fetchone()
        stopped = conn.execute(
            """SELECT finished_at, account_scope_id
               FROM reconciliation_runs WHERE reconciliation_id=?""",
            (result.reconciliation_id,),
        ).fetchone()
        store.set_system_state(
            conn, "reconciliation_auto_resume_owner",
            json.dumps({
                "owner": "reconciliation", "reconciliation_id": result.reconciliation_id,
                "control_event_id": stop_event_id,
                "expected_value": str(row["value"]),
                "expected_updated_at": str(row["updated_at"]),
                "stopped_at": str(stopped[0]) if stopped else str(row["updated_at"]),
                "account_scope_id": str(stopped[1] or "") if stopped else "",
            }, ensure_ascii=False, sort_keys=True),
            "reconciliation-owned stop-buy",
        )
    if result.severity == "CRITICAL" and _current(
        conn, "reconciliation_auto_resume_owner", ""
    ):
        store.set_system_state(
            conn, "reconciliation_auto_resume_owner", "", "critical requires manual recovery"
        )
    if result.severity == "CRITICAL" and _set_control(
        store, conn, key="kill_switch", value="1", action="kill_switch_on", reason=reason,
        operator=operator, reconciliation_id=result.reconciliation_id,
    ):
        actions.append("kill_switch_on")
    if actions:
        result.control_action = ",".join(actions)
        conn.execute(
            "UPDATE reconciliation_runs SET control_action=? WHERE reconciliation_id=?",
            (result.control_action, result.reconciliation_id),
        )
    else:
        result.control_action = persisted_action
    return actions


def unlock_eligibility(store: TradingStore, *, now: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    with store.connect() as conn:
        latest = conn.execute(
            "SELECT result FROM reconciliation_runs WHERE mode='full' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        rows = conn.execute(
            """SELECT snapshot_id FROM reconciliation_runs
               WHERE mode='full' AND result='matched' ORDER BY finished_at DESC LIMIT 2"""
        ).fetchall()
        snapshot = conn.execute(
            "SELECT generated_at FROM account_snapshots ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        unknown = conn.execute(
            """SELECT 1 FROM execution_intents AS i
               JOIN capacity_reservations AS r
                 ON r.account_scope_id=i.account_scope_id
                AND r.client_order_id=i.client_order_id
               JOIN orders AS o ON o.client_order_id=i.client_order_id
               WHERE r.status='active'
                 AND (i.status='SUBMIT_UNKNOWN' OR o.status='submit_unknown')
               LIMIT 1"""
        ).fetchone()
        terminal_reservation = conn.execute(
            """SELECT 1 FROM execution_intents AS i
               JOIN capacity_reservations AS r
                 ON r.account_scope_id=i.account_scope_id
                AND r.client_order_id=i.client_order_id
               JOIN orders AS o ON o.client_order_id=i.client_order_id
               WHERE r.status='active'
                 AND (i.status IN ('NOT_SUBMITTED','REJECTED','CANCELLED','FILLED')
                      OR o.status IN ('not_submitted','rejected','risk_rejected',
                                      'failed','skipped','cancelled','filled'))
               LIMIT 1"""
        ).fetchone()
    if latest is None or str(latest[0]) != "matched":
        reasons.append("LATEST_FULL_RECONCILIATION_NOT_MATCHED")
    if len(rows) < 2 or len({str(row[0]) for row in rows if row[0]}) < 2:
        reasons.append("TWO_DISTINCT_FULL_RECONCILIATIONS_REQUIRED")
    if snapshot is None:
        reasons.append("ACCOUNT_SNAPSHOT_REQUIRED")
    else:
        age = datetime.fromisoformat(now) - datetime.fromisoformat(str(snapshot[0]))
        if age.total_seconds() < 0 or age.total_seconds() > 600:
            reasons.append("ACCOUNT_SNAPSHOT_STALE")
    if unknown is not None:
        reasons.append("SUBMIT_UNKNOWN_PRESENT")
    if terminal_reservation is not None:
        reasons.append("TERMINAL_RESERVATION_UNRECONCILED")
    return not reasons, reasons


def control_status(store: TradingStore) -> dict[str, object]:
    store.initialize()
    with store.connect() as conn:
        states = {
            key: dict(row) if row is not None else {
                "key": key, "value": "1" if key == "buy_enabled" else "0",
                "updated_at": "", "reason": "default",
            }
            for key in ("buy_enabled", "kill_switch")
            for row in [conn.execute("SELECT * FROM system_state WHERE key=?", (key,)).fetchone()]
        }
        latest = conn.execute(
            "SELECT * FROM reconciliation_runs ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        owner = conn.execute(
            "SELECT * FROM system_state WHERE key='reconciliation_auto_resume_owner'"
        ).fetchone()
        capacity_owner = conn.execute(
            "SELECT * FROM system_state WHERE key=?",
            (NOTIFICATION_CAPACITY_OWNER_KEY,),
        ).fetchone()
        capacity = store.notification_capacity(conn)
        marker_row = conn.execute(
            "SELECT value FROM system_state WHERE key=?",
            (NOTIFICATION_WRITE_FAILURE_KEY,),
        ).fetchone()
        marker_text = str(marker_row[0] or "") if marker_row is not None else ""
        marker: dict[str, object] = {}
        if marker_text:
            try:
                parsed = json.loads(marker_text)
            except (TypeError, ValueError, RecursionError):
                parsed = {}
            if isinstance(parsed, dict):
                marker = parsed
        notification_state_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT state, COUNT(*) FROM notification_outbox GROUP BY state"
            )
        }
        recent_events = conn.execute(
            "SELECT * FROM control_events ORDER BY created_at DESC, event_id DESC LIMIT 5"
        ).fetchall()
    return {
        "controls": states,
        "automatic_recovery_owner": dict(owner) if owner else None,
        "notification_capacity_recovery_owner": (
            dict(capacity_owner) if capacity_owner else None
        ),
        "notification_health": {
            "pending": notification_state_counts.get("pending", 0),
            "leased": notification_state_counts.get("leased", 0),
            "dead": notification_state_counts.get("dead", 0),
            "unresolved_gaps": capacity.unresolved_gap_rows,
            "high_unresolved_gaps": capacity.high_unresolved_gap_rows,
            "high_dead_detail_rows": capacity.high_dead_rows,
            "dead_detail_rows": capacity.dead_rows,
            "dead_detail_bytes": capacity.dead_bytes,
            "tombstones": capacity.tombstone_rows,
            "write_failure_marker": bool(marker_text),
            "write_failure_requires_manual_resolution": bool(
                marker.get("requires_manual_resolution")
            ),
            "write_failure_event_key": str(marker.get("event_key") or ""),
        },
        "recent_control_events": [dict(row) for row in recent_events],
        "latest_reconciliation": dict(latest) if latest else None,
    }


def change_control(
    store: TradingStore, key: str, value: str, *, reason: str, operator: str,
    expected_value: str | None = None, expected_updated_at: str | None = None,
) -> bool:
    reason = reason.strip()
    if not reason:
        raise ValueError("reason is required")
    if key not in {"buy_enabled", "kill_switch"} or value not in {"0", "1"}:
        raise ValueError("invalid control state")
    store.initialize()
    with store.transaction() as conn:
        row = conn.execute("SELECT value, updated_at FROM system_state WHERE key=?", (key,)).fetchone()
        current_value = str(row[0]) if row else ("1" if key == "buy_enabled" else "0")
        current_updated_at = str(row[1]) if row else ""
        if expected_value is not None and expected_value != current_value:
            raise StaleControlStateError(f"{key} expected {expected_value}, found {current_value}")
        if expected_updated_at is not None and expected_updated_at != current_updated_at:
            raise StaleControlStateError(f"{key} state changed after it was displayed")
        action = {
            ("buy_enabled", "0"): "stop_buy",
            ("buy_enabled", "1"): "resume_buy",
            ("kill_switch", "1"): "kill_switch_on",
            ("kill_switch", "0"): "kill_switch_off",
        }[(key, value)]
        reconciliation_owner = ""
        capacity_owner = ""
        if operator != "system":
            reconciliation_owner = _current(
                conn, "reconciliation_auto_resume_owner", "",
            )
            capacity_owner = _current(
                conn, NOTIFICATION_CAPACITY_OWNER_KEY, "",
            )
            if reconciliation_owner:
                store.set_system_state(
                    conn, "reconciliation_auto_resume_owner", "",
                    "manual control precedence",
                )
            if capacity_owner:
                store.set_system_state(
                    conn, NOTIFICATION_CAPACITY_OWNER_KEY, "",
                    "manual control precedence",
                )
        event_id = _set_control(
            store, conn, key=key, value=value, action=action, reason=reason,
            operator=operator, reconciliation_id=None,
        )
        changed = bool(event_id)
        if operator != "system":
            if reconciliation_owner:
                if not changed:
                    store.insert_control_event(
                        conn,
                        event_id=str(uuid.uuid4()),
                        action="cancel_auto_resume",
                        operator=operator,
                        old_value=current_value,
                        new_value=current_value,
                        reason=reason,
                        created_at=datetime.now().isoformat(),
                    )
                    changed = True
            if capacity_owner:
                if not changed:
                    store.insert_control_event(
                        conn,
                        event_id=str(uuid.uuid4()),
                        action="cancel_notification_capacity_auto_resume",
                        operator=operator,
                        old_value=current_value,
                        new_value=current_value,
                        reason=reason,
                        created_at=datetime.now().isoformat(),
                        _capacity_reconcile=False,
                    )
                    changed = True
            if not changed and value == "0":
                store.insert_control_event(
                    conn,
                    event_id=str(uuid.uuid4()),
                    action="hold_buy_disabled",
                    operator=operator,
                    old_value="0",
                    new_value="0",
                    reason=reason,
                    created_at=datetime.now().isoformat(),
                )
                changed = True
            if changed and key == "buy_enabled" and value == "1":
                recovered_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                rows = conn.execute(
                    """SELECT issue_key FROM execution_issue_state
                       WHERE recovered_at IS NULL
                       AND state IN ('LEDGER_INTEGRITY_FAILURE','IMMUTABLE_FILL_CONFLICT')"""
                ).fetchall()
                scope = store.registered_account_scope(
                    conn, "joinquant", "primary",
                )
                for row in rows:
                    store.recover_execution_issue(
                        conn,
                        str(row[0]),
                        recovered_at,
                        account_scope_id=scope,
                    )
    return changed


def auto_resume_eligibility(
    store: TradingStore, conn: object, *, now: str, required_template: str
) -> tuple[bool, list[str], dict[str, object]]:
    del store
    reasons: list[str] = []
    owner_row = conn.execute(
        "SELECT value FROM system_state WHERE key='reconciliation_auto_resume_owner'"
    ).fetchone()
    try:
        owner = json.loads(str(owner_row[0])) if owner_row and owner_row[0] else {}
    except Exception:
        owner = {}
    if owner.get("owner") != "reconciliation":
        reasons.append("RECONCILIATION_OWNERSHIP_REQUIRED")
    buy = conn.execute(
        "SELECT value, updated_at FROM system_state WHERE key='buy_enabled'"
    ).fetchone()
    if buy is None or str(buy[0]) != "0" or str(buy[1]) != str(owner.get("expected_updated_at") or ""):
        reasons.append("CONTROL_GENERATION_CHANGED")
    if _current(conn, "kill_switch", "0") == "1":
        reasons.append("KILL_SWITCH_ACTIVE")
    stopped_at = str(owner.get("stopped_at") or "")
    account_scope_id = str(owner.get("account_scope_id") or "")
    if not account_scope_id:
        reasons.append("RECONCILIATION_SCOPE_REQUIRED")
    runs = conn.execute(
        """SELECT result, snapshot_id FROM reconciliation_runs
           WHERE account_scope_id=? AND finished_at>?
           ORDER BY finished_at DESC LIMIT 2""",
        (account_scope_id, stopped_at),
    ).fetchall() if stopped_at and account_scope_id else []
    if len(runs) < 2 or any(str(row[0]) != "matched" for row in runs) or len({str(row[1]) for row in runs if row[1]}) < 2:
        reasons.append("TWO_DISTINCT_POST_STOP_MATCHES_REQUIRED")
    snapshot = conn.execute(
        """SELECT generated_at, template_version FROM account_snapshots
           WHERE snapshot_id=?""",
        (runs[0][1],),
    ).fetchone() if runs and runs[0][1] else None
    if snapshot is None:
        reasons.append("ACCOUNT_SNAPSHOT_REQUIRED")
    else:
        age = datetime.fromisoformat(now) - datetime.fromisoformat(str(snapshot[0]))
        if age.total_seconds() < 0 or age.total_seconds() > 600:
            reasons.append("ACCOUNT_SNAPSHOT_STALE")
        if str(snapshot[1] or "") != required_template:
            reasons.append("TEMPLATE_VERSION_MISMATCH")
    if conn.execute(
        "SELECT 1 FROM execution_issue_state WHERE recovered_at IS NULL AND severity IN ('ERROR','CRITICAL') LIMIT 1"
    ).fetchone():
        reasons.append("UNRESOLVED_EXECUTION_ERROR")
    if conn.execute(
        """SELECT 1 FROM execution_intents AS i
           JOIN capacity_reservations AS r
             ON r.account_scope_id=i.account_scope_id
            AND r.client_order_id=i.client_order_id
           JOIN orders AS o ON o.client_order_id=i.client_order_id
           WHERE i.account_scope_id=? AND r.status='active'
             AND (i.status='SUBMIT_UNKNOWN' OR o.status='submit_unknown')
           LIMIT 1""",
        (account_scope_id,),
    ).fetchone():
        reasons.append("SUBMIT_UNKNOWN_PRESENT")
    if conn.execute(
        """SELECT 1 FROM execution_intents AS i
           JOIN capacity_reservations AS r
             ON r.account_scope_id=i.account_scope_id
            AND r.client_order_id=i.client_order_id
           JOIN orders AS o ON o.client_order_id=i.client_order_id
           WHERE i.account_scope_id=? AND r.status='active'
             AND (i.status IN ('NOT_SUBMITTED','REJECTED','CANCELLED','FILLED')
                  OR o.status IN ('not_submitted','rejected','risk_rejected',
                                  'failed','skipped','cancelled','filled'))
           LIMIT 1""",
        (account_scope_id,),
    ).fetchone():
        reasons.append("TERMINAL_RESERVATION_UNRECONCILED")
    return not reasons, reasons, owner


def apply_automatic_buy_recovery(
    store: TradingStore, conn: object, result: ReconciliationResult, *, now: str,
    required_template: str,
) -> dict[str, object] | None:
    ok, _, owner = auto_resume_eligibility(
        store, conn, now=now, required_template=required_template
    )
    if not ok:
        return None
    result_scope = conn.execute(
        """SELECT account_scope_id FROM reconciliation_runs
           WHERE reconciliation_id=?""",
        (result.reconciliation_id,),
    ).fetchone()
    if (
        result_scope is None
        or str(result_scope[0] or "")
        != str(owner.get("account_scope_id") or "")
    ):
        return None
    cursor = conn.execute(
        """UPDATE system_state SET value='1', updated_at=?, reason=?
           WHERE key='buy_enabled' AND value='0' AND updated_at=?""",
        (
            now, f"automatic recovery after reconciliation {result.reconciliation_id}",
            str(owner["expected_updated_at"]),
        ),
    )
    if cursor.rowcount != 1:
        return None
    event_id = str(uuid.uuid4())
    store.insert_control_event(
        conn,
        event_id=event_id,
        action="auto_resume_buy",
        operator="system",
        old_value="0",
        new_value="1",
        reason="two distinct post-stop reconciliations matched",
        reconciliation_id=result.reconciliation_id,
        created_at=now,
        account_scope_id=str(owner["account_scope_id"]),
    )
    store.set_system_state(conn, "reconciliation_auto_resume_owner", "", "automatic recovery complete")
    return {"action": "auto_resume_buy", "event_id": event_id, "at": now}


def run_full_reconciliation(store: TradingStore, account_file: Path, now: str) -> object:
    from joinquant_sync import ingest_snapshot_payload

    payload = json.loads(account_file.read_text(encoding="utf-8"))
    return ingest_snapshot_payload(payload, store, now, mode="full")["reconciliation"]


def _unlock_wizard(store: TradingStore, account_file: Path) -> int:
    if not sys.stdin.isatty():
        print("unlock requires an interactive terminal", file=sys.stderr)
        return 2
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(json.dumps(control_status(store), ensure_ascii=False, indent=2, default=str))
    run_full_reconciliation(store, account_file, now)
    eligible, reasons = unlock_eligibility(store, now=now)
    if not eligible:
        print("unlock refused: " + ",".join(reasons), file=sys.stderr)
        return 3
    reason = input("解锁原因：").strip()
    if not reason:
        print("unlock reason is required", file=sys.stderr)
        return 4
    if input("输入 UNLOCK 确认：").strip() != "UNLOCK":
        print("unlock cancelled", file=sys.stderr)
        return 5
    operator = getpass.getuser()
    status = control_status(store)["controls"]
    kill = status["kill_switch"]
    buy = status["buy_enabled"]
    change_control(
        store, "kill_switch", "0", reason=reason, operator=operator,
        expected_value=str(kill["value"]), expected_updated_at=str(kill["updated_at"]),
    )
    if input("输入 RESUME_BUY 二次确认恢复买入：").strip() != "RESUME_BUY":
        print("kill switch disabled; buy remains disabled", file=sys.stderr)
        return 6
    change_control(
        store, "buy_enabled", "1", reason=reason, operator=operator,
        expected_value=str(buy["value"]), expected_updated_at=str(buy["updated_at"]),
    )
    print("unlock complete: kill_switch=0, buy_enabled=1")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trading controls and ledger reconciliation")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--account-file", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("reconcile")
    sub.add_parser("unlock")
    for command in ("stop-buy", "resume-buy", "kill-switch-on", "kill-switch-off"):
        item = sub.add_parser(command)
        item.add_argument("--reason", required=True)
        item.add_argument("--expected-value")
        item.add_argument("--expected-updated-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    import config as app_config

    args = build_arg_parser().parse_args(argv)
    store = TradingStore(args.db or app_config.TRADING_DB_FILE)
    account_file = args.account_file or app_config.JOINQUANT_ACCOUNT_FILE
    if args.command == "status":
        print(json.dumps(control_status(store), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.command == "reconcile":
        result = run_full_reconciliation(
            store, account_file, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2, default=str))
        return 0 if result.result == "matched" else 1
    if args.command == "unlock":
        return _unlock_wizard(store, account_file)
    key, value = {
        "stop-buy": ("buy_enabled", "0"),
        "resume-buy": ("buy_enabled", "1"),
        "kill-switch-on": ("kill_switch", "1"),
        "kill-switch-off": ("kill_switch", "0"),
    }[args.command]
    if args.command == "resume-buy":
        eligible, reasons = unlock_eligibility(
            store, now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        if not eligible:
            print("resume refused: " + ",".join(reasons), file=sys.stderr)
            return 3
    if args.command in {"resume-buy", "kill-switch-off"} and (
        args.expected_value is None or args.expected_updated_at is None
    ):
        print("expected-value and expected-updated-at are required", file=sys.stderr)
        return 4
    changed = change_control(
        store, key, value, reason=args.reason, operator=getpass.getuser(),
        expected_value=args.expected_value, expected_updated_at=args.expected_updated_at,
    )
    print(f"{key}={value} changed={int(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
