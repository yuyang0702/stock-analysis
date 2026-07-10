from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, g, jsonify, request

import config as app_config
from notifier import WeComNotifier


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
    if request.args.get("token") != expected:
        abort(403)


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


def _filled_trade_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in payload.get("orders", [])
        if isinstance(item, dict)
        and str(item.get("action") or "").strip().lower() in {"buy", "sell"}
        and _num(item.get("filled")) > 0
    ]


def build_execution_markdown(payload: dict[str, Any]) -> str:
    orders = [item for item in payload.get("orders", []) if isinstance(item, dict)]
    positions = payload.get("positions", []) if isinstance(payload.get("positions"), list) else []
    success_status = {"held", "filled", "submitted", "open", "done"}
    failed_status = {"failed", "rejected", "cancelled", "skipped"}
    success_count = sum(1 for item in orders if str(item.get("status", "")).lower() in success_status)
    failed_count = sum(1 for item in orders if str(item.get("status", "")).lower() in failed_status)
    pending_count = max(0, len(orders) - success_count - failed_count)

    lines = [
        "#### 【JoinQuant 模拟盘】执行回报",
        f"> 时间：{payload.get('generated_at') or payload.get('received_at') or '-'}",
        f"> 总资产：{_num(payload.get('total_value')):.2f} | 现金：{_num(payload.get('cash')):.2f} | 持仓：{len(positions)}",
        f"> 委托 {len(orders)} | 成功 {success_count} | 失败 {failed_count} | 待确认 {pending_count}",
    ]
    if not orders:
        lines.append("> 本次快照没有委托记录；请在 JoinQuant 模拟盘页面核对成交。")
        return "\n".join(lines)

    for item in orders[:8]:
        action = "买入" if item.get("action") == "buy" else "卖出"
        code = item.get("jq_code") or item.get("code") or "-"
        name = _short(item.get("name"), 10)
        status = item.get("status") or "-"
        filled = item.get("filled")
        amount = item.get("amount")
        qty_text = ""
        if filled is not None or amount is not None:
            qty_text = f" | 成交 {int(_num(filled))}/{int(_num(amount))}"
        target = f" | 目标 {item.get('target_pct')}%" if item.get("target_pct") not in (None, "") else ""
        lines.append(f"- {action} {code} {name} | {status}{qty_text}{target}")
        if item.get("reason"):
            lines.append(f"  > {_short(item.get('reason'), 48)}")
    if len(orders) > 8:
        lines.append(f"> 还有 {len(orders) - 8} 条未展开，请看 JoinQuant 模拟盘委托列表。")
    return "\n".join(lines)


def _notify_execution(payload: dict[str, Any]) -> None:
    if not app_config.WECOM_WEBHOOK_URL:
        return
    md = build_execution_markdown(payload)
    digest = hashlib.sha256(md.encode("utf-8")).hexdigest()
    notifier = WeComNotifier(
        webhook_url=app_config.WECOM_WEBHOOK_URL,
        state_file=app_config.CACHE_DIR / "wecom_notify_state.json",
        cooldown_sec=app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
        timeout_sec=app_config.WECOM_TIMEOUT_SEC,
    )
    notifier.send_markdown("JoinQuant 模拟盘执行回报", md, dedupe_key=f"joinquant-exec:{digest}")


def create_app(
    token: str | None = None,
    signal_file: Path | None = None,
    account_file: Path | None = None,
    api_event_file: Path | None = None,
) -> Flask:
    app = Flask(__name__)
    expected_token = token if token is not None else app_config.JOINQUANT_SYNC_TOKEN
    signal_path = signal_file or app_config.JOINQUANT_SIGNAL_FILE
    account_path = account_file or app_config.JOINQUANT_ACCOUNT_FILE
    event_path = api_event_file or account_path.parent / "api_events.jsonl"

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
        filled_orders = _filled_trade_orders(payload)
        if filled_orders:
            notification_payload = dict(payload)
            notification_payload["orders"] = filled_orders
            _notify_execution(notification_payload)
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
