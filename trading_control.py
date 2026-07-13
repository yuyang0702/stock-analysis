from __future__ import annotations

import argparse
import getpass
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from reconciliation import ReconciliationResult
from trading_store import TradingStore


class StaleControlStateError(RuntimeError):
    pass


def _current(conn: object, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return default if row is None else str(row[0])


def _set_control(
    store: TradingStore, conn: object, *, key: str, value: str, action: str,
    reason: str, operator: str, reconciliation_id: str | None,
) -> bool:
    old = _current(conn, key, "1" if key == "buy_enabled" else "0")
    if old == value:
        return False
    store.set_system_state(conn, key, value, reason)
    linked = reconciliation_id
    if linked and conn.execute(
        "SELECT 1 FROM reconciliation_runs WHERE reconciliation_id=?", (linked,)
    ).fetchone() is None:
        linked = None
    conn.execute(
        """INSERT INTO control_events(
           event_id, action, operator, old_value, new_value, reason, reconciliation_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (str(uuid.uuid4()), action, operator, old, value, reason, linked),
    )
    return True


def apply_reconciliation_control(
    store: TradingStore, conn: object, result: ReconciliationResult, *, operator: str = "system"
) -> list[str]:
    actions: list[str] = []
    reason = f"reconciliation {result.reconciliation_id} {result.severity}"
    if result.severity in {"ERROR", "CRITICAL"} and _set_control(
        store, conn, key="buy_enabled", value="0", action="stop_buy", reason=reason,
        operator=operator, reconciliation_id=result.reconciliation_id,
    ):
        actions.append("stop_buy")
    if result.severity == "CRITICAL" and _set_control(
        store, conn, key="kill_switch", value="1", action="kill_switch_on", reason=reason,
        operator=operator, reconciliation_id=result.reconciliation_id,
    ):
        actions.append("kill_switch_on")
    result.control_action = ",".join(actions)
    conn.execute(
        "UPDATE reconciliation_runs SET control_action=? WHERE reconciliation_id=?",
        (result.control_action, result.reconciliation_id),
    )
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
        unknown = conn.execute("SELECT 1 FROM orders WHERE status='submit_unknown' LIMIT 1").fetchone()
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
    return {"controls": states, "latest_reconciliation": dict(latest) if latest else None}


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
        changed = _set_control(
            store, conn, key=key, value=value, action=action, reason=reason,
            operator=operator, reconciliation_id=None,
        )
    if changed:
        _notify_control_change(action, key, value, reason, operator)
    return changed


def _notify_control_change(action: str, key: str, value: str, reason: str, operator: str) -> bool:
    import config as app_config
    from notifier import WeComNotifier

    if not app_config.WECOM_WEBHOOK_URL:
        return False
    notifier = WeComNotifier(
        app_config.WECOM_WEBHOOK_URL,
        app_config.CACHE_DIR / "wecom_notify_state.json",
        cooldown_sec=app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
        timeout_sec=app_config.WECOM_TIMEOUT_SEC,
    )
    content = (
        f"> action={action} | {key}={value}\n"
        f"> operator={operator} | reason={reason[:120]}\n"
        "```text\nbash run_ubuntu.sh trading-status\n```"
    )
    return notifier.send_markdown(
        "交易控制状态已人工变更", content,
        dedupe_key=f"trading-control:{action}:{key}:{value}:{reason[:48]}",
    )


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
