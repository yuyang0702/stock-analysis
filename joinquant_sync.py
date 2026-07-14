from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import config as app_config
from order_ledger import normalize_fill, normalize_order
from reconciliation import reconcile_snapshot
from trading_control import apply_reconciliation_control
from trading_store import TradingStore, canonical_json


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


def _snapshot_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    account_keys = (
        "cash", "available_cash", "total_value", "daily_turnover_pct", "daily_pnl_pct",
        "account_drawdown_pct", "consecutive_losses", "pending_buy_position_pct",
        "pending_buy_risk_pct",
    )
    position_keys = (
        "code", "jq_code", "qty", "closeable_amount", "locked_amount", "today_amount",
        "avg_cost", "price", "market_value", "pnl",
    )
    order_keys = ("order_id", "id", "code", "jq_code", "action", "amount", "filled", "avg_price", "status")
    trade_keys = (
        "trade_id", "fill_id", "id", "order_id", "code", "jq_code", "action", "amount",
        "qty", "price", "commission", "stamp_tax", "other_fee",
    )

    def rows(name: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        values = [
            {key: item.get(key) for key in keys if key in item}
            for item in snapshot.get(name, []) if isinstance(item, dict)
        ]
        return sorted(values, key=canonical_json)

    return {
        "account": {key: snapshot.get(key) for key in account_keys},
        "positions": rows("positions", position_keys),
        "orders": rows("orders", order_keys),
        "trades": rows("trades", trade_keys),
    }


def snapshot_id(snapshot: dict[str, Any]) -> str:
    stable = dict(snapshot)
    stable.pop("received_at", None)
    return hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()[:32]


def should_retain_details(conn: Any, snapshot: dict[str, Any], state_hash: str | None = None) -> bool:
    state_hash = state_hash or hashlib.sha256(
        canonical_json(_snapshot_state(snapshot)).encode("utf-8")
    ).hexdigest()
    row = conn.execute(
        "SELECT state_hash FROM account_snapshots WHERE retained_details=1 ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    generated = datetime.fromisoformat(str(snapshot.get("generated_at") or _now()))
    checkpoint = generated.minute == 0
    if generated.time().isoformat() >= "15:00:00":
        close_row = conn.execute(
            """SELECT 1 FROM account_snapshots WHERE trade_date=? AND retained_details=1
               AND substr(generated_at,12,8)>='15:00:00' LIMIT 1""",
            (str(snapshot.get("trade_date") or generated.date().isoformat())[:10],),
        ).fetchone()
        checkpoint = checkpoint or close_row is None
    return row is None or str(row[0]) != state_hash or checkpoint


def persist_account_snapshot(
    store: TradingStore, conn: Any, snapshot: dict[str, Any], received_at: str
) -> dict[str, Any]:
    sid = snapshot_id(snapshot)
    state_hash = hashlib.sha256(canonical_json(_snapshot_state(snapshot)).encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT retained_details FROM account_snapshots WHERE snapshot_id=?", (sid,)
    ).fetchone()
    retain = bool(existing[0]) if existing is not None else should_retain_details(conn, snapshot, state_hash)
    positions = [item for item in snapshot.get("positions", []) if isinstance(item, dict)]
    market_value = sum(float(_num(item.get("market_value"), 0) or 0) for item in positions)
    generated_at = str(snapshot.get("generated_at") or received_at)
    trade_date = str(snapshot.get("trade_date") or generated_at[:10])[:10]
    conn.execute(
        """INSERT OR IGNORE INTO account_snapshots(
           snapshot_id, trade_date, generated_at, received_at, cash, available_cash, total_value,
           position_market_value, daily_turnover_pct, daily_pnl_pct, account_drawdown_pct,
           template_version, state_hash, retained_details, raw_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sid, trade_date, generated_at, received_at, _num(snapshot.get("cash"), 0),
            _num(snapshot.get("available_cash"), _num(snapshot.get("cash"), 0)),
            _num(snapshot.get("total_value"), 0), market_value,
            _num(snapshot.get("daily_turnover_pct"), 0), _num(snapshot.get("daily_pnl_pct"), 0),
            _num(snapshot.get("account_drawdown_pct"), 0), str(snapshot.get("template_version") or ""),
            state_hash, int(retain), canonical_json(snapshot) if retain else None,
        ),
    )
    if retain:
        for item in positions:
            code = _code(item.get("code") or item.get("jq_code"))
            if not code:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO position_snapshots(
                   snapshot_id, stock_code, qty, closeable_qty, locked_qty, today_qty,
                   avg_cost, price, market_value, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid, code, int(_num(item.get("qty"), 0) or 0),
                    int(_num(item.get("closeable_amount"), item.get("qty") or 0) or 0),
                    int(_num(item.get("locked_amount"), 0) or 0),
                    int(_num(item.get("today_amount"), 0) or 0), _num(item.get("avg_cost"), 0),
                    _num(item.get("price"), 0), _num(item.get("market_value"), 0),
                    _num(item.get("pnl"), 0),
                ),
            )

    strategy_version = str(snapshot.get("template_version") or snapshot.get("strategy_version") or "")
    trade_events = [event for event in snapshot.get("trades", []) if isinstance(event, dict)]
    has_trades = bool(trade_events)
    new_executions: list[dict[str, object]] = []
    orders_by_id: dict[str, dict[str, object]] = {}
    for event in snapshot.get("orders", []):
        if not isinstance(event, dict):
            continue
        order = normalize_order(event, trade_date=trade_date, strategy_version=strategy_version)
        previous = conn.execute(
            "SELECT filled_qty FROM orders WHERE client_order_id=?",
            (order["client_order_id"],),
        ).fetchone()
        previous_filled = int(previous[0]) if previous is not None else 0
        store.upsert_order(conn, order)
        if order.get("order_id"):
            orders_by_id[str(order["order_id"])] = order
        current_filled = int(order.get("filled_qty") or 0)
        if not has_trades and current_filled > previous_filled:
            new_executions.append({
                "event_id": f"legacy:{order['client_order_id']}:{current_filled}",
                "source": "legacy_order_progress",
                "order_id": order.get("order_id"),
                "signal_id": order.get("signal_id"),
                "stock_code": order.get("stock_code"),
                "action": order.get("action"),
                "qty": current_filled - previous_filled,
                "cumulative_qty": current_filled,
                "price": order.get("average_fill_price"),
                "status": order.get("status"),
                "filled_at": order.get("updated_at") or generated_at,
            })
    for event in trade_events:
        fill = normalize_fill(event, orders=orders_by_id)
        if not store.insert_fill(conn, fill):
            continue
        order = orders_by_id.get(str(fill.get("order_id") or ""), {})
        new_executions.append({
            "event_id": f"fill:{fill['fill_id']}",
            "source": "fill",
            "order_id": fill.get("order_id"),
            "signal_id": fill.get("signal_id"),
            "stock_code": fill.get("stock_code"),
            "action": fill.get("action"),
            "qty": fill.get("qty"),
            "cumulative_qty": order.get("filled_qty"),
            "price": fill.get("price"),
            "status": order.get("status") or "filled",
            "filled_at": fill.get("filled_at"),
        })

    fees = conn.execute(
        "SELECT COALESCE(sum(commission+stamp_tax+other_fee),0) FROM fills WHERE substr(filled_at,1,10)=?",
        (trade_date,),
    ).fetchone()[0]
    unrealized = sum(float(_num(item.get("pnl"), 0) or 0) for item in positions)
    total_value = float(_num(snapshot.get("total_value"), 0) or 0)
    drawdown = float(_num(snapshot.get("account_drawdown_pct"), 0) or 0)
    conn.execute(
        """INSERT INTO daily_equity(
           trade_date, opening_equity, closing_equity, cash, position_market_value,
           realized_pnl, unrealized_pnl, fees, net_deposit, max_drawdown_pct,
           first_snapshot_at, last_snapshot_at
           ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?)
           ON CONFLICT(trade_date) DO UPDATE SET
           closing_equity=CASE WHEN excluded.last_snapshot_at>=last_snapshot_at THEN excluded.closing_equity ELSE closing_equity END,
           cash=CASE WHEN excluded.last_snapshot_at>=last_snapshot_at THEN excluded.cash ELSE cash END,
           position_market_value=CASE WHEN excluded.last_snapshot_at>=last_snapshot_at THEN excluded.position_market_value ELSE position_market_value END,
           unrealized_pnl=CASE WHEN excluded.last_snapshot_at>=last_snapshot_at THEN excluded.unrealized_pnl ELSE unrealized_pnl END,
           fees=excluded.fees,
           max_drawdown_pct=min(max_drawdown_pct, excluded.max_drawdown_pct),
           first_snapshot_at=min(first_snapshot_at, excluded.first_snapshot_at),
           last_snapshot_at=max(last_snapshot_at, excluded.last_snapshot_at)""",
        (
            trade_date, total_value, total_value, _num(snapshot.get("cash"), 0), market_value,
            unrealized, fees, drawdown, generated_at, generated_at,
        ),
    )
    return {
        "snapshot_id": sid,
        "retained_details": retain,
        "inserted_fills": sum(event["source"] == "fill" for event in new_executions),
        "new_executions": new_executions,
    }


def ingest_snapshot_payload(
    snapshot: dict[str, Any], store: TradingStore, received_at: str, mode: str = "incremental"
) -> dict[str, Any]:
    store.initialize()
    positions = [
        _position(item, snapshot) for item in snapshot.get("positions", []) if isinstance(item, dict)
    ]
    positions = [item for item in positions if item["code"] and item["qty"] > 0]
    snapshot_at = str(snapshot.get("generated_at") or snapshot.get("received_at") or received_at)
    with store.transaction() as conn:
        result = persist_account_snapshot(store, conn, snapshot, received_at)
        store.reconcile_position_cycles(conn, positions, snapshot_at)
        store.reconcile_order_events(conn, snapshot.get("orders", []), snapshot_at)
        store.reconcile_exit_intents(conn, positions, snapshot_at)
        reconciliation = reconcile_snapshot(
            store, conn, snapshot, snapshot_id=result["snapshot_id"], mode=mode, now=received_at,
        )
        actions = apply_reconciliation_control(store, conn, reconciliation)
        result["reconciliation"] = reconciliation
        result["control_actions"] = actions
        today = received_at[:10]
        last_pruned = conn.execute(
            "SELECT value FROM system_state WHERE key='execution_history_last_pruned'"
        ).fetchone()
        if last_pruned is None or str(last_pruned[0]) != today:
            cutoff = (datetime.fromisoformat(received_at).date() - timedelta(days=366)).isoformat()
            store.prune_execution_history(conn, cutoff, received_at)
            store.set_system_state(conn, "execution_history_last_pruned", today, "366-day hot retention")
    return result


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
            "consecutive_losses": int(_num(snapshot.get("consecutive_losses"), 0) or 0),
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
    ingest_snapshot_payload(snapshot, store, str(snapshot.get("received_at") or _now()))
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
