from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, g, jsonify, request

import config as app_config
from joinquant_sync import ingest_snapshot_payload
from notifier import WeComNotifier
from reconciliation import (
    ReconciliationDifference, ReconciliationResult, notify_reconciliation,
    persist_issue_transitions,
)
from trading_control import apply_reconciliation_control
from trading_store import TradingStore


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "signals": [], "stale": True}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "signals": [], "stale": True, "error": "invalid_json"}
    return raw if isinstance(raw, dict) else {"schema_version": 1, "signals": [], "stale": True}


def _append_api_event(path: Path, endpoint: str, status_code: int, **extra: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "endpoint": endpoint,
        "status_code": status_code,
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
    }
    payload.update(extra)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    g.api_event_logged = True


def _check_token(expected: str) -> None:
    if not expected:
        abort(503, description="JOINQUANT_SYNC_TOKEN is not configured")
    authorization = request.headers.get("Authorization", "")
    supplied = authorization[7:].strip() if authorization.startswith("Bearer ") else request.args.get("token", "")
    if supplied != expected:
        abort(403)
    if request.args.get("token"):
        request.environ["QUERY_STRING"] = "token=REDACTED"
        for key in ("RAW_URI", "REQUEST_URI"):
            raw = str(request.environ.get(key) or "")
            if "?" in raw:
                request.environ[key] = raw.split("?", 1)[0] + "?token=REDACTED"


def _validate_snapshot(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        abort(400, description="payload must be an object")
    if payload.get("schema_version") != 1:
        abort(400, description="schema_version must be 1")
    if not isinstance(payload.get("positions", []), list):
        abort(400, description="positions must be a list")
    if not isinstance(payload.get("trades", []), list):
        abort(400, description="trades must be a list")
    if not isinstance(payload.get("orders", []), list):
        abort(400, description="orders must be a list")
    payload.setdefault("source", "joinquant")
    payload.setdefault("received_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return payload


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _short(value: Any, limit: int = 36) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_execution_markdown(
    payload: dict[str, Any], executions: list[dict[str, Any]]
) -> str:
    positions = payload.get("positions", []) if isinstance(payload.get("positions"), list) else []
    lines = [
        "#### 【JoinQuant 模拟盘】执行回报",
        f"> 账户快照：{payload.get('generated_at') or payload.get('received_at') or '-'}",
        f"> 总资产：{_num(payload.get('total_value')):.2f} | 现金：{_num(payload.get('cash')):.2f} | 持仓：{len(positions)}",
        f"> 本次新增成交：{len(executions)}",
    ]
    for item in executions:
        action = "买入" if item.get("action") == "buy" else "卖出"
        lines.append(
            f"- {action} {item.get('stock_code') or '-'} | 本次 {int(_num(item.get('qty')))}股 "
            f"@ {_num(item.get('price')):.2f} | 累计 {int(_num(item.get('cumulative_qty')))}股 | "
            f"{item.get('status') or '-'}"
        )
        lines.append(
            f"  > 成交时间：{item.get('filled_at') or '-'} | "
            f"订单：{_short(item.get('order_id')) or '-'}"
        )
    return "\n".join(lines)


def _notify_execution(payload: dict[str, Any], executions: list[dict[str, Any]]) -> None:
    if not app_config.WECOM_WEBHOOK_URL or not executions:
        return
    md = build_execution_markdown(payload, executions)
    identity = "|".join(sorted(str(item["event_id"]) for item in executions))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    notifier = WeComNotifier(
        webhook_url=app_config.WECOM_WEBHOOK_URL,
        state_file=app_config.CACHE_DIR / "wecom_notify_state.json",
        cooldown_sec=app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
        timeout_sec=app_config.WECOM_TIMEOUT_SEC,
    )
    notifier.send_markdown("JoinQuant 模拟盘执行回报", md, dedupe_key=f"joinquant-exec:{digest}")


def _reconciliation_notifier() -> WeComNotifier:
    return WeComNotifier(
        webhook_url=app_config.WECOM_WEBHOOK_URL,
        state_file=app_config.CACHE_DIR / "wecom_notify_state.json",
        cooldown_sec=app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
        timeout_sec=app_config.WECOM_TIMEOUT_SEC,
    )


def create_app(
    token: str | None = None,
    signal_file: Path | None = None,
    account_file: Path | None = None,
    api_event_file: Path | None = None,
    store: TradingStore | None = None,
) -> Flask:
    app = Flask(__name__)
    expected_token = token if token is not None else app_config.JOINQUANT_SYNC_TOKEN
    signal_path = signal_file or app_config.JOINQUANT_SIGNAL_FILE
    account_path = account_file or app_config.JOINQUANT_ACCOUNT_FILE
    event_path = api_event_file or account_path.parent / "api_events.jsonl"
    ledger_store = store or TradingStore(
        app_config.TRADING_DB_FILE if account_file is None else account_path.parent / "trading.db"
    )

    @app.after_request
    def log_api_error(response):
        if request.path.startswith("/joinquant/") and not getattr(g, "api_event_logged", False):
            endpoint = request.path.rsplit("/", 1)[-1] or "unknown"
            _append_api_event(event_path, endpoint, response.status_code)
        return response

    @app.get("/joinquant/signals")
    def signals():
        _check_token(expected_token)
        payload = _read_json(signal_path)
        signal_count = len(payload.get("signals", [])) if isinstance(payload.get("signals"), list) else 0
        _append_api_event(event_path, "signals", 200, signal_count=signal_count)
        return jsonify(payload)

    @app.get("/joinquant/latest")
    def latest():
        _check_token(expected_token)
        payload = _read_json(signal_path)
        signal_count = len(payload.get("signals", [])) if isinstance(payload.get("signals"), list) else 0
        _append_api_event(event_path, "latest", 200, signal_count=signal_count)
        return jsonify(
            {
                "schema_version": payload.get("schema_version", 1),
                "generated_at": payload.get("generated_at"),
                "trade_date": payload.get("trade_date"),
                "run_id": payload.get("run_id"),
                "signal_count": signal_count,
                "stale": bool(payload.get("stale", False)),
            }
        )

    @app.post("/joinquant/account_snapshot")
    def account_snapshot():
        _check_token(expected_token)
        payload = _validate_snapshot(request.get_json(silent=True))
        try:
            ledger_result = ingest_snapshot_payload(
                payload, ledger_store, str(payload.get("received_at")), mode="incremental"
            )
        except Exception as exc:
            _append_api_event(
                event_path, "account_snapshot", 503,
                error_type=type(exc).__name__, error=str(exc)[:160],
            )
            failure = ReconciliationResult(
                hashlib.sha256(f"ledger:{type(exc).__name__}".encode("utf-8")).hexdigest()[:32],
                "mismatch", "CRITICAL", [ReconciliationDifference(
                    "ledger", "sqlite", "LEDGER_INTEGRITY_FAILURE", "unavailable", "callback", 0,
                    "CRITICAL", {},
                )], "", None,
            )
            try:
                ledger_store.initialize()
                with ledger_store.transaction() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO reconciliation_runs(
                           reconciliation_id, mode, snapshot_id, started_at, finished_at, result,
                           severity, difference_count, control_action, summary_json
                           ) VALUES (?, 'incremental', NULL, datetime('now'), datetime('now'),
                           'mismatch', 'CRITICAL', 1, '', '{}')""",
                        (failure.reconciliation_id,),
                    )
                    if conn.execute(
                        "SELECT 1 FROM reconciliation_items WHERE reconciliation_id=? LIMIT 1",
                        (failure.reconciliation_id,),
                    ).fetchone() is None:
                        conn.execute(
                            """INSERT INTO reconciliation_items(
                               reconciliation_id, category, object_id, reason_code, local_value,
                               platform_value, tolerance, severity, details_json
                               ) VALUES (?, 'ledger', 'sqlite', 'LEDGER_INTEGRITY_FAILURE',
                               'unavailable', 'callback', 0, 'CRITICAL', '{}')""",
                            (failure.reconciliation_id,),
                        )
                    persist_issue_transitions(
                        ledger_store, conn, failure,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    apply_reconciliation_control(ledger_store, conn, failure)
            except Exception:
                pass
            try:
                failure_controls = {
                    "buy_enabled": ledger_store.get_system_state("buy_enabled", "0"),
                    "kill_switch": ledger_store.get_system_state("kill_switch", "0"),
                }
            except Exception:
                failure_controls = {"buy_enabled": "0", "kill_switch": "0"}
            notify_reconciliation(
                failure,
                failure_controls,
                notifier=_reconciliation_notifier(),
                store=ledger_store,
                now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            return jsonify({"ok": False, "error": "ledger_unavailable"}), 503
        _write_json(account_path, payload)
        history_path = account_path.parent / "account_snapshot_history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        try:
            from ml_dataset import update_order_labels

            update_order_labels(app_config.ML_SIGNAL_SAMPLE_FILE, payload)
        except Exception as exc:
            print(f"ML order label update skipped: {exc}", flush=True)
        new_executions = list(ledger_result.get("new_executions") or [])
        if new_executions:
            _notify_execution(payload, new_executions)
        reconciliation = ledger_result.get("reconciliation")
        if reconciliation is not None and reconciliation.transitions:
            notify_reconciliation(
                reconciliation,
                {
                    "buy_enabled": ledger_store.get_system_state("buy_enabled", "1"),
                    "kill_switch": ledger_store.get_system_state("kill_switch", "0"),
                },
                notifier=_reconciliation_notifier(),
                store=ledger_store,
                now=str(payload.get("received_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        _append_api_event(
            event_path,
            "account_snapshot",
            200,
            position_count=len(payload.get("positions", [])),
            order_count=len(payload.get("orders", [])),
        )
        return jsonify({"ok": True, "positions": len(payload.get("positions", []))})

    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JoinQuant signal server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    create_app().run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
