from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from flask import Flask, abort, g, jsonify, request

import config as app_config
from execution_contracts import ExecutionIntent
from joinquant_runtime_isolation import (
    RuntimeIdentityError, validate_request_identity,
    validate_snapshot_identity,
)
from joinquant_sync import (
    ingest_snapshot_payload,
    is_joinquant_event_only_payload,
    sanitize_joinquant_payload,
)
from notification_outbox import NotificationConflict
from reconciliation import (
    ReconciliationDifference, ReconciliationResult,
    persist_issue_transitions,
)
from trading_control import apply_reconciliation_control
from trading_store import FillConflictError, OrderConflictError, TradingStore


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


def _aware_instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _jq_code(code: str) -> str:
    if code.startswith("6"):
        return f"{code}.XSHG"
    if code.startswith(("4", "8")):
        return f"{code}.XBJG"
    return f"{code}.XSHE"


def _signal_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def _matches_execution_intent(signal: dict[str, Any], payload_json: str) -> bool:
    try:
        intent = ExecutionIntent.from_dict(json.loads(payload_json))
        candidate = intent.pre_trade_result.candidate
        fee = intent.pre_trade_result.execution_fee
        if intent.side != "buy" or fee is None:
            return False
        required_cash = fee.notional_yuan + fee.total_yuan
        text_fields = {
            "id": intent.source_signal_id,
            "action": "buy",
            "code": intent.code,
            "jq_code": _jq_code(intent.code),
            "account_scope_id": intent.account_scope_id,
            "client_order_id": intent.client_order_id,
            "logical_signal_id": intent.logical_signal_id,
            "execution_intent_sha256": intent.intent_sha256,
            "pre_trade_result_id": intent.pre_trade_result_id,
            "pre_trade_result_sha256": intent.pre_trade_result_sha256,
            "broker_snapshot_id": intent.broker_snapshot_id,
            "broker_snapshot_sha256": intent.broker_snapshot_sha256,
            "quote_snapshot_id": intent.quote_snapshot_id,
            "quote_snapshot_sha256": intent.quote_snapshot_sha256,
            "instrument_rules_sha256": intent.instrument_rules_sha256,
            "fee_schedule_version": intent.fee_schedule_version,
            "fee_schedule_sha256": intent.fee_schedule_sha256,
            "parameter_version": intent.parameter_version,
            "model_version": intent.model_version,
            "expires_at": intent.expires_at,
        }
        if any(str(signal.get(key) or "") != value for key, value in text_fields.items()):
            return False
        if intent.strategy_version != (
            "a_share_strategy:" + str(signal.get("execution_plan_version") or "")
        ):
            return False
        integer_fields = {
            "target_qty": intent.target_position_qty,
            "target_position": intent.target_position_qty,
            "order_qty": intent.order_qty,
            "expected_current_qty": intent.expected_current_qty,
        }
        if any(
            isinstance(signal.get(key), bool)
            or not isinstance(signal.get(key), int)
            or signal[key] != value
            for key, value in integer_fields.items()
        ):
            return False
        decimal_fields = {
            "entry_price": candidate.suggested_entry_price,
            "stop_loss": intent.stop_price,
            "take_profit": candidate.target_price,
            "price_cap": intent.price_cap,
            "limit_price": intent.limit_price,
            "required_cash_yuan": required_cash,
        }
        for key, expected in decimal_fields.items():
            actual = signal.get(key)
            if expected is None:
                if actual is not None:
                    return False
            elif _signal_decimal(actual) != expected:
                return False
        return True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _filter_executable_signals(
    payload: dict[str, Any], store: TradingStore, *, now: datetime | None = None,
) -> dict[str, Any]:
    result = dict(payload)
    raw_signals = payload.get("signals")
    signals = raw_signals if isinstance(raw_signals, list) else []
    diagnostics = dict(payload.get("diagnostics") or {})
    current = now or datetime.now().astimezone()
    try:
        store.initialize()
        with store.connect() as conn:
            controls = {
                str(row["key"]): str(row["value"])
                for row in conn.execute(
                    """SELECT key, value FROM system_state
                       WHERE key IN ('buy_enabled', 'sell_enabled', 'kill_switch')"""
                ).fetchall()
            }
            if controls.get("kill_switch") != "0":
                filtered: list[dict[str, Any]] = []
            else:
                filtered = []
                for signal in signals:
                    if not isinstance(signal, dict):
                        continue
                    action = str(signal.get("action") or "").strip().lower()
                    if action == "sell":
                        if controls.get("sell_enabled") == "1":
                            filtered.append(signal)
                        continue
                    if action != "buy" or controls.get("buy_enabled") != "1":
                        continue
                    scope = str(signal.get("account_scope_id") or "").strip()
                    client_order_id = str(signal.get("client_order_id") or "").strip()
                    if not scope or not client_order_id:
                        continue
                    row = conn.execute(
                        """SELECT o.status AS order_status, o.signal_id, o.action,
                                  o.stock_code, o.requested_qty, o.target_qty,
                                  i.status AS intent_status, i.expires_at,
                                  i.intent_sha256, i.payload_json AS intent_payload
                           FROM orders AS o
                           JOIN execution_intents AS i
                             ON i.client_order_id=o.client_order_id
                           WHERE i.account_scope_id=? AND o.client_order_id=?""",
                        (scope, client_order_id),
                    ).fetchone()
                    expires_at = _aware_instant(row["expires_at"]) if row else None
                    if (
                        row is None
                        or str(row["order_status"]).lower() != "ready"
                        or str(row["intent_status"]).upper() != "READY"
                        or str(row["signal_id"] or "") != str(signal.get("id") or "")
                        or str(row["action"]).lower() != "buy"
                        or str(row["stock_code"]) != str(signal.get("code") or "")
                        or int(row["requested_qty"]) != signal.get("order_qty")
                        or int(row["target_qty"]) != signal.get("target_qty")
                        or str(row["intent_sha256"]) != str(
                            signal.get("execution_intent_sha256") or ""
                        )
                        or not _matches_execution_intent(
                            signal, str(row["intent_payload"])
                        )
                        or expires_at is None
                        or current.astimezone(expires_at.tzinfo) >= expires_at
                    ):
                        continue
                    filtered.append(signal)
        diagnostics["execution_filter_status"] = "ok"
    except Exception:
        filtered = [
            signal for signal in signals
            if isinstance(signal, dict)
            and str(signal.get("action") or "").strip().lower() == "sell"
        ]
        diagnostics["execution_filter_status"] = "ledger_unavailable"
    diagnostics["execution_filter_removed"] = len(signals) - len(filtered)
    result["signals"] = filtered
    result["diagnostics"] = diagnostics
    return result


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
    account_fields = ("cash", "available_cash", "total_value")
    if all(payload.get(name) not in (None, "") for name in account_fields):
        missing = [
            name for name in ("positions", "orders", "trades")
            if name not in payload
        ]
        if missing:
            abort(
                400,
                description="complete snapshot missing collections: "
                + ",".join(missing),
            )
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
    ]
    if all(
        payload.get(name) not in (None, "")
        for name in ("cash", "available_cash", "total_value")
    ):
        lines.append(
            f"> 总资产：{_num(payload.get('total_value')):.2f} | "
            f"现金：{_num(payload.get('cash')):.2f} | 持仓：{len(positions)}"
        )
    else:
        lines.append("> 事件包未提供账户快照，仅展示执行事实")
    lines.append(f"> 本次新增成交：{len(executions)}")
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
    """Deprecated compatibility hook; execution facts enqueue in SQLite."""
    del payload, executions


def create_app(
    token: str | None = None,
    signal_file: Path | None = None,
    account_file: Path | None = None,
    api_event_file: Path | None = None,
    store: TradingStore | None = None,
    require_runtime_identity: bool | None = None,
) -> Flask:
    app = Flask(__name__)
    expected_token = token if token is not None else app_config.JOINQUANT_SYNC_TOKEN
    signal_path = signal_file or app_config.JOINQUANT_SIGNAL_FILE
    account_path = account_file or app_config.JOINQUANT_ACCOUNT_FILE
    event_path = api_event_file or account_path.parent / "api_events.jsonl"
    ledger_store = store or TradingStore(
        app_config.TRADING_DB_FILE if account_file is None else account_path.parent / "trading.db"
    )
    strict_runtime = (
        token is None if require_runtime_identity is None
        else require_runtime_identity
    )

    def require_live_runtime() -> dict[str, str]:
        if not strict_runtime:
            return {
                "run_type": "sim_trade",
                "template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                "protocol_version": "1",
            }
        try:
            identity = validate_request_identity(
                request.headers, app_config.JOINQUANT_TEMPLATE_VERSION,
            )
        except RuntimeIdentityError as exc:
            g.runtime_identity_error = exc.code
            abort(409, description=exc.code)
        g.runtime_identity = identity
        return identity

    @app.after_request
    def log_api_error(response):
        if request.path.startswith("/joinquant/") and not getattr(g, "api_event_logged", False):
            endpoint = request.path.rsplit("/", 1)[-1] or "unknown"
            _append_api_event(
                event_path, endpoint, response.status_code,
                runtime_mode=str(request.headers.get("X-JoinQuant-Run-Type") or "")[:32],
                template_version=str(
                    request.headers.get("X-JoinQuant-Template-Version") or ""
                )[:80],
                runtime_identity_error=str(
                    getattr(g, "runtime_identity_error", "") or ""
                )[:80],
            )
        return response

    @app.get("/joinquant/signals")
    def signals():
        _check_token(expected_token)
        require_live_runtime()
        payload = _filter_executable_signals(_read_json(signal_path), ledger_store)
        signal_count = len(payload.get("signals", [])) if isinstance(payload.get("signals"), list) else 0
        _append_api_event(event_path, "signals", 200, signal_count=signal_count)
        return jsonify(payload)

    @app.get("/joinquant/latest")
    def latest():
        _check_token(expected_token)
        require_live_runtime()
        payload = _filter_executable_signals(_read_json(signal_path), ledger_store)
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
        runtime_identity = require_live_runtime()
        payload = sanitize_joinquant_payload(
            _validate_snapshot(request.get_json(silent=True))
        )
        if strict_runtime:
            try:
                validate_snapshot_identity(payload, runtime_identity)
            except RuntimeIdentityError as exc:
                g.runtime_identity_error = exc.code
                abort(409, description=exc.code)
        event_only = is_joinquant_event_only_payload(payload)
        try:
            ledger_result = ingest_snapshot_payload(
                payload,
                ledger_store,
                str(payload.get("received_at")),
                mode="incremental" if event_only else "full",
            )
        except Exception as exc:
            _append_api_event(
                event_path, "account_snapshot", 503,
                error_type=type(exc).__name__, error=str(exc)[:160],
            )
            immutable_code = (
                "IMMUTABLE_FILL_CONFLICT"
                if isinstance(exc, FillConflictError)
                else "LEDGER_INTEGRITY_FAILURE"
                if isinstance(exc, (NotificationConflict, OrderConflictError))
                else ""
            )
            if event_only and not immutable_code:
                return jsonify({
                    "ok": False,
                    "error": "execution_event_unavailable",
                }), 503
            reason_code = immutable_code or "LEDGER_INTEGRITY_FAILURE"
            category = "fill" if reason_code == "IMMUTABLE_FILL_CONFLICT" else "ledger"
            object_id = "callback" if immutable_code else "sqlite"
            failure = ReconciliationResult(
                hashlib.sha256(f"ledger:{type(exc).__name__}".encode("utf-8")).hexdigest()[:32],
                "mismatch", "CRITICAL", [ReconciliationDifference(
                    category, object_id, reason_code,
                    "immutable" if immutable_code else "unavailable",
                    "callback", 0, "CRITICAL",
                    {"error_code": type(exc).__name__},
                )], "", None,
            )
            try:
                ledger_store.initialize()
                with ledger_store.transaction() as conn:
                    account_scope_id = ledger_store.get_or_create_account_scope(
                        conn, "joinquant", "primary",
                    )
                    failure.reconciliation_id = hashlib.sha256(
                        (
                            f"ledger:{reason_code}:{type(exc).__name__}:"
                            f"{account_scope_id}"
                        ).encode("utf-8")
                    ).hexdigest()[:32]
                    conn.execute(
                        """INSERT OR IGNORE INTO reconciliation_runs(
                           reconciliation_id, mode, snapshot_id, started_at, finished_at, result,
                           severity, difference_count, control_action, summary_json,
                           account_scope_id
                           ) VALUES (?, 'incremental', NULL, datetime('now'), datetime('now'),
                           'mismatch', 'CRITICAL', 1, '', '{}', ?)""",
                        (failure.reconciliation_id, account_scope_id),
                    )
                    if conn.execute(
                        "SELECT 1 FROM reconciliation_items WHERE reconciliation_id=? LIMIT 1",
                        (failure.reconciliation_id,),
                    ).fetchone() is None:
                        conn.execute(
                            """INSERT INTO reconciliation_items(
                               reconciliation_id, category, object_id, reason_code, local_value,
                               platform_value, tolerance, severity, details_json
                               ) VALUES (?, ?, ?, ?, ?, 'callback', 0,
                                         'CRITICAL', ?)""",
                            (
                                failure.reconciliation_id, category, object_id,
                                reason_code,
                                "immutable" if immutable_code else "unavailable",
                                json.dumps(
                                    {"error_code": type(exc).__name__},
                                    sort_keys=True,
                                ),
                            ),
                        )
                    persist_issue_transitions(
                        ledger_store, conn, failure,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    apply_reconciliation_control(ledger_store, conn, failure)
            except Exception:
                pass
            return jsonify({
                "ok": False,
                "error": "immutable_conflict" if immutable_code else "ledger_unavailable",
            }), 503
        if not ledger_result.get("event_only"):
            _write_json(account_path, payload)
            history_path = account_path.parent / "account_snapshot_history.jsonl"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            with history_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(payload, ensure_ascii=False, default=str) + "\n"
                )
            try:
                from ml_dataset import update_order_labels

                update_order_labels(app_config.ML_SIGNAL_SAMPLE_FILE, payload)
            except Exception as exc:
                print(f"ML order label update skipped: {exc}", flush=True)
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
