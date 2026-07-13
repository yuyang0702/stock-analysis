from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config as app_config
from trading_store import TradingStore


def _code(value: Any) -> str:
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_position_migration_report(positions: list[dict[str, Any]], cycles: dict[str, dict]) -> str:
    lines = ["# JoinQuant持仓迁移报告", "", "|代码|数量|可卖|成本|冻结止损|ATR来源|模式|建仓日期可信度|enabled_rules|", "|---|---:|---:|---:|---:|---|---|---|---|"]
    for item in sorted(positions, key=lambda value: str(value.get("code") or "")):
        code = str(item.get("code") or "")
        cycle = cycles.get(code, {})
        full = bool(cycle.get("entry_signal_id") and float(cycle.get("atr14") or 0) > 0)
        mode = "完整分层退出" if full else "固定硬止损"
        atr_source = "买入信号" if float(cycle.get("atr14") or 0) > 0 else "缺失"
        date_confidence = "信号可追溯" if cycle.get("entry_signal_id") else "快照估计"
        rules = "hard_stop,+2R,trailing_stop,time_stop" if full else "hard_stop"
        lines.append(
            f"|{code}|{int(item.get('qty') or 0)}|{int(item.get('closeable_qty') or 0)}|"
            f"{float(item.get('cost_price') or 0):.2f}|{float(cycle.get('initial_stop_price') or item.get('stop_price') or 0):.2f}|"
            f"{atr_source}|{mode}|{date_confidence}|{rules}|"
        )
    return "\n".join(lines) + "\n"


def unsafe_migration_codes(positions: list[dict[str, Any]], cycles: dict[str, dict]) -> list[str]:
    return sorted(
        str(item.get("code") or "") for item in positions
        if float((cycles.get(str(item.get("code") or ""), {}) or {}).get("initial_stop_price")
                 or item.get("stop_price") or 0) <= 0
    )


def _load_snapshot(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("invalid JoinQuant account snapshot")
    return raw


def _position(item: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    code = _code(item.get("code") or item.get("jq_code"))
    avg_cost = _num(item.get("avg_cost"))
    price = _num(item.get("price"))
    stop_price = round(avg_cost * 0.965, 2) if avg_cost else None
    take_price = round(avg_cost * 1.07, 2) if avg_cost else None
    return {
        "code": code,
        "jq_code": str(item.get("jq_code") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "qty": int(_num(item.get("qty"), 0) or 0),
        "closeable_qty": int(_num(item.get("closeable_amount"), item.get("qty") or 0) or 0),
        "locked_qty": int(_num(item.get("locked_amount"), 0) or 0),
        "today_qty": int(_num(item.get("today_amount"), 0) or 0),
        "cost_price": avg_cost,
        "current_price": price,
        "stop_pct": 3.5,
        "take_pct": 7.0,
        "stop_price": stop_price,
        "take_price": take_price,
        "position_ratio": _num(item.get("position_ratio")),
        "market_value": _num(item.get("market_value")),
        "pnl": _num(item.get("pnl"), 0.0),
        "status": "holding",
        "source": "joinquant",
        "note": f"JoinQuant snapshot {snapshot.get('trade_date', '')}".strip(),
        "entry_time": str(snapshot.get("generated_at") or _now()),
        "updated_at": _now(),
        "raw": item,
    }


def sync_account_snapshot(
    account_file: Path | None = None,
    positions_file: Path | None = None,
    events_file: Path | None = None,
    store: TradingStore | None = None,
    migration_report_file: Path | None = None,
) -> int:
    account_file = account_file or app_config.JOINQUANT_ACCOUNT_FILE
    positions_file = positions_file or app_config.POSITIONS_FILE
    events_file = events_file or app_config.PORTFOLIO_EVENTS_FILE
    snapshot = _load_snapshot(account_file)

    positions = []
    for item in snapshot.get("positions", []):
        if isinstance(item, dict):
            pos = _position(item, snapshot)
            if pos["code"] and pos["qty"] > 0:
                positions.append(pos)

    payload = {
        "updated_at": _now(),
        "source": "joinquant",
        "account": {
            "cash": _num(snapshot.get("cash")),
            "available_cash": _num(snapshot.get("available_cash"), _num(snapshot.get("cash"))),
            "total_value": _num(snapshot.get("total_value")),
            "daily_turnover_pct": _num(snapshot.get("daily_turnover_pct")),
            "daily_pnl_pct": _num(snapshot.get("daily_pnl_pct")),
            "account_drawdown_pct": _num(snapshot.get("account_drawdown_pct")),
            "consecutive_losses": int(_num(snapshot.get("consecutive_losses"))),
            "pending_buy_position_pct": _num(snapshot.get("pending_buy_position_pct")),
            "pending_buy_risk_pct": _num(snapshot.get("pending_buy_risk_pct")),
            "trade_date": snapshot.get("trade_date"),
            "generated_at": snapshot.get("generated_at"),
        },
        "positions": sorted(positions, key=lambda item: item["code"]),
    }
    positions_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = positions_file.with_suffix(positions_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(positions_file)

    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _now(), "action": "joinquant_sync", "count": len(positions)}, ensure_ascii=False) + "\n")
    store = store or TradingStore(app_config.TRADING_DB_FILE)
    store.initialize()
    with store.transaction() as conn:
        store.reconcile_position_cycles(
            conn,
            positions,
            str(snapshot.get("generated_at") or snapshot.get("received_at") or _now()),
        )
        store.reconcile_order_events(conn, snapshot.get("orders", []), str(snapshot.get("generated_at") or _now()))
        store.reconcile_exit_intents(conn, positions, str(snapshot.get("generated_at") or _now()))
    if migration_report_file is not None:
        migration_report_file.parent.mkdir(parents=True, exist_ok=True)
        migration_report_file.write_text(
            build_position_migration_report(positions, store.get_active_position_cycles()), encoding="utf-8",
        )
    return len(positions)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync JoinQuant snapshot to local portfolio file")
    parser.add_argument("--account-file", type=Path, default=app_config.JOINQUANT_ACCOUNT_FILE)
    parser.add_argument("--positions-file", type=Path, default=app_config.POSITIONS_FILE)
    parser.add_argument("--events-file", type=Path, default=app_config.PORTFOLIO_EVENTS_FILE)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    count = sync_account_snapshot(
        args.account_file, args.positions_file, args.events_file,
        migration_report_file=app_config.OUTPUT_DIR / "position_migration.md",
    )
    payload = json.loads(args.positions_file.read_text(encoding="utf-8"))
    store = TradingStore(app_config.TRADING_DB_FILE)
    unsafe = unsafe_migration_codes(payload.get("positions", []), store.get_active_position_cycles())
    if unsafe:
        raise SystemExit(f"Unsafe position migration: missing stop for {','.join(unsafe)}")
    print(f"Synced {count} JoinQuant positions")


if __name__ == "__main__":
    main()
