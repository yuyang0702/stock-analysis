from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config as app_config
from execution_contracts import BrokerPosition, BrokerSnapshot
from notification_outbox import (
    NotificationEvent,
    notification_event_key,
    redact_secret_text,
)
from order_ledger import normalize_fill, normalize_order
from reconciliation import persist_issue_transitions, reconcile_snapshot
from trading_control import apply_automatic_buy_recovery, apply_reconciliation_control
from trading_store import (
    OrderConflictError, TradingStore, canonical_json, order_allowed_quantity,
)
from exit_policy import PositionExitState, resolve_effective_stop
from execution_admission import (
    expire_ready_intents_in_transaction,
    synchronize_execution_lifecycle,
)


_SNAPSHOT_FIELDS = {
    "schema_version", "trade_date", "generated_at", "received_at", "source",
    "strategy_template_version", "template_version", "strategy_version",
    "cash", "available_cash", "total_value", "daily_turnover_pct",
    "daily_pnl_pct", "account_drawdown_pct", "realized_pnl", "intraday_pnl",
    "consecutive_losses", "pending_buy_position_pct", "pending_buy_risk_pct",
    "positions", "orders", "trades",
}
_POSITION_FIELDS = {
    "code", "jq_code", "name", "qty", "closeable_amount", "locked_amount",
    "today_amount", "avg_cost", "price", "market_value", "pnl",
    "position_ratio",
}
_ORDER_FIELDS = {
    "id", "signal_id", "client_order_id", "order_id", "code", "jq_code", "action", "amount",
    "requested_qty", "target_qty", "filled", "filled_qty", "avg_price",
    "price", "status", "reason", "datetime", "updated_at", "submit_count",
    "first_submitted_at", "completed_at", "name", "target_pct",
}
_TRADE_FIELDS = {
    "id", "trade_id", "fill_id", "client_order_id", "order_id", "signal_id", "code", "jq_code",
    "action", "amount", "qty", "price", "commission", "stamp_tax",
    "other_fee", "fee_data_status", "datetime", "filled_at",
}


def _sanitized_record(value: Any, fields: set[str]) -> Any:
    if not isinstance(value, dict):
        return value
    return {
        key: (
            redact_secret_text(item)
            if key == "reason"
            else item
            if key == "client_order_id" or not isinstance(item, (dict, list))
            else None
        )
        for key, item in value.items()
        if key in fields
    }


def sanitize_joinquant_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    sanitized = {
        key: item if not isinstance(item, (dict, list)) else None
        for key, item in value.items()
        if key in _SNAPSHOT_FIELDS and key not in {"positions", "orders", "trades"}
    }
    for name, fields in (
        ("positions", _POSITION_FIELDS),
        ("orders", _ORDER_FIELDS),
        ("trades", _TRADE_FIELDS),
    ):
        rows = value.get(name)
        if isinstance(rows, list):
            sanitized[name] = [_sanitized_record(item, fields) for item in rows]
        elif name in value:
            sanitized[name] = rows
    return sanitized


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
        "stop_pct": None,
        "take_pct": None,
        "stop_price": None,
        "take_price": None,
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


def apply_cycle_risk_fields(
    positions: list[dict[str, Any]], cycles: dict[str, dict[str, Any]],
    decision_batch_at: str | None = None,
) -> list[dict[str, Any]]:
    decision_batch_at = decision_batch_at or datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).isoformat(timespec="microseconds")
    for item in positions:
        cycle = cycles.get(item["code"])
        if not cycle:
            continue
        resolved = resolve_effective_stop(PositionExitState(
            code=item["code"], mode=str(cycle.get("mode") or "legacy_fixed"),
            initial_qty=int(cycle.get("initial_qty") or item["qty"]), current_qty=item["qty"],
            entry_price=float(cycle.get("entry_price") or item.get("cost_price") or 0),
            initial_stop_price=float(cycle.get("initial_stop_price") or 0),
            highest_price=max(float(cycle.get("highest_price") or 0), float(item.get("current_price") or 0)),
            atr14=float(cycle.get("atr14") or 0),
            take_profit_stage=int(cycle.get("take_profit_stage") or 0), holding_trade_days=0,
            manual_stop_price=float(cycle.get("manual_stop_price") or 0),
            profit_protection_activated_at=str(
                cycle.get("profit_protection_activated_at") or ""
            ),
            trailing_stop_active_from=str(cycle.get("trailing_stop_active_from") or ""),
            decision_batch_at=decision_batch_at,
        ), str(cycle.get("market_state") or "NORMAL"))
        item.update({
            "initial_stop_price": resolved.initial_stop_price,
            "manual_stop_price": resolved.manual_stop_price or None,
            "trailing_stop_price": resolved.trailing_stop_price or None,
            "effective_stop_price": resolved.effective_stop_price,
            "stop_price": resolved.effective_stop_price,
            "stop_source": resolved.source,
            "take_profit_stage": int(cycle.get("take_profit_stage") or 0),
            "position_cycle_id": cycle.get("position_cycle_id"),
        })
    return positions


def _snapshot_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    account_keys = (
        "cash", "available_cash", "total_value", "daily_turnover_pct", "daily_pnl_pct",
        "account_drawdown_pct", "realized_pnl", "consecutive_losses", "pending_buy_position_pct",
        "pending_buy_risk_pct",
    )
    position_keys = (
        "code", "jq_code", "qty", "closeable_amount", "locked_amount", "today_amount",
        "avg_cost", "price", "market_value", "pnl",
    )
    order_keys = (
        "client_order_id", "order_id", "id", "code", "jq_code", "action",
        "amount", "filled", "avg_price", "status",
    )
    trade_keys = (
        "trade_id", "fill_id", "client_order_id", "id", "order_id", "code",
        "jq_code", "action", "amount",
        "qty", "price", "commission", "stamp_tax", "other_fee", "fee_data_status",
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


def _shanghai_timestamp(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid timestamp") from exc
    shanghai = ZoneInfo("Asia/Shanghai")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=shanghai)
    else:
        parsed = parsed.astimezone(shanghai)
    return parsed.isoformat(timespec="seconds")


def _strict_decimal(
    value: object,
    name: str,
    *,
    default: object | None = None,
) -> Decimal:
    source = default if value in (None, "") else value
    try:
        result = Decimal(str(source))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _strict_signed_decimal(
    value: object,
    name: str,
    *,
    default: object = 0,
) -> Decimal:
    source = default if value in (None, "") else value
    try:
        result = Decimal(str(source))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _strict_quantity(
    value: object,
    name: str,
    *,
    default: object = 0,
) -> int:
    amount = _strict_decimal(value, name, default=default)
    if amount != amount.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    return int(amount)


def _persisted_order_signal(order: dict[str, object]) -> str:
    signal_id = str(order.get("signal_id") or "").strip()
    if signal_id:
        return signal_id
    raw = order.get("raw_json")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("signal_id", "id"):
        signal_id = str(payload.get(key) or "").strip()
        if signal_id and not signal_id.startswith("jq-order-"):
            return signal_id
    return ""


def _legacy_order(
    event: dict[str, object],
    *,
    trade_date: str,
    strategy_version: str,
    persisted_orders: dict[str, dict[str, object]],
) -> dict[str, object]:
    order = normalize_order(
        event, trade_date=trade_date, strategy_version=strategy_version,
    )
    broker_order_id = str(order.get("order_id") or "")
    previous = persisted_orders.get(broker_order_id)
    if previous is None:
        return order
    old_signal = _persisted_order_signal(previous)
    new_signal = str(order.get("signal_id") or "").strip()
    old_qty = _order_allowed_quantity(previous)
    new_qty = _order_allowed_quantity(order)
    if (
        str(previous.get("stock_code") or "") != str(order["stock_code"])
        or str(previous.get("action") or "") != str(order["action"])
        or (
            event.get("client_order_id") not in (None, "")
            and str(previous.get("client_order_id") or "")
            != str(order["client_order_id"])
        )
        or (old_signal and new_signal and old_signal != new_signal)
        or (old_qty > 0 and new_qty > 0 and old_qty != new_qty)
    ):
        raise OrderConflictError("order identity conflict")
    order["client_order_id"] = previous["client_order_id"]
    if old_signal and not new_signal:
        raw = json.loads(str(order["raw_json"]))
        raw["signal_id"] = old_signal
        order["raw_json"] = canonical_json(raw)
    if event.get("client_order_id") in (None, ""):
        raw = json.loads(str(order["raw_json"]))
        raw["client_order_id"] = previous["client_order_id"]
        order["raw_json"] = canonical_json(raw)
    return order


def _persisted_broker_orders(
    conn: Any, snapshot: dict[str, Any]
) -> dict[str, dict[str, object]]:
    order_ids = {
        str(item.get("order_id") or "").strip()
        for name in ("orders", "trades")
        for item in snapshot.get(name, [])
        if isinstance(item, dict) and str(item.get("order_id") or "").strip()
    }
    result: dict[str, dict[str, object]] = {}
    for order_id in order_ids:
        rows = conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,),
        ).fetchall()
        if len(rows) > 1:
            raise OrderConflictError("order identity resolves to multiple ledger rows")
        if rows:
            result[order_id] = dict(rows[0])
    return result


def _legacy_broker_snapshot(
    snapshot: dict[str, Any],
    account_scope_id: str,
    received_at: str,
    persisted_orders: dict[str, dict[str, object]],
) -> BrokerSnapshot:
    required_account_fields = ("cash", "available_cash", "total_value")
    missing_account_fields = [
        name for name in required_account_fields
        if name not in snapshot or snapshot.get(name) in (None, "")
    ]
    if missing_account_fields:
        raise ValueError(
            "complete JoinQuant snapshot missing account fields: "
            + ",".join(missing_account_fields)
        )
    required_collections = ("positions", "orders", "trades")
    missing_collections = [
        name for name in required_collections if name not in snapshot
    ]
    if missing_collections:
        raise ValueError(
            "complete JoinQuant snapshot missing collections: "
            + ",".join(missing_collections)
        )
    for name in required_collections:
        if not isinstance(snapshot[name], list):
            raise ValueError(f"complete JoinQuant snapshot {name} must be a list")
    generated_at = _shanghai_timestamp(
        snapshot.get("generated_at") or snapshot.get("received_at") or received_at,
        "generated_at",
    )
    trade_date = str(snapshot.get("trade_date") or generated_at[:10])[:10]
    cash = _strict_decimal(snapshot.get("cash"), "cash")
    available_cash = _strict_decimal(
        snapshot.get("available_cash"), "available_cash", default=cash,
    )
    if available_cash > cash:
        raise ValueError("available_cash exceeds cash")
    total_equity = _strict_decimal(snapshot.get("total_value"), "total_value")
    intraday_raw = snapshot.get("intraday_pnl")
    daily_pnl_pct_raw = snapshot.get("daily_pnl_pct")
    drawdown_raw = snapshot.get("account_drawdown_pct")
    turnover_raw = snapshot.get("daily_turnover_pct")
    consecutive_losses_raw = snapshot.get("consecutive_losses")
    daily_risk_evidence_status = (
        "reported"
        if (
            (intraday_raw not in (None, "") or daily_pnl_pct_raw not in (None, ""))
            and drawdown_raw not in (None, "")
            and turnover_raw not in (None, "")
            and consecutive_losses_raw not in (None, "")
        )
        else "unknown"
    )
    if intraday_raw not in (None, ""):
        intraday_pnl = _strict_signed_decimal(
            intraday_raw, "intraday_pnl"
        )
    elif daily_pnl_pct_raw not in (None, ""):
        daily_pnl_pct = _strict_signed_decimal(
            daily_pnl_pct_raw, "daily_pnl_pct"
        )
        denominator = Decimal("100") + daily_pnl_pct
        if denominator <= 0:
            raise ValueError("daily_pnl_pct must be greater than -100")
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_UP
            intraday_pnl = (
                total_equity
                - total_equity * Decimal("100") / denominator
            )
    else:
        intraday_pnl = Decimal("0")
    account_drawdown_pct = _strict_signed_decimal(
        drawdown_raw, "account_drawdown_pct", default=0,
    )
    daily_turnover_pct = _strict_decimal(
        turnover_raw, "daily_turnover_pct", default=0,
    )
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_UP
        daily_turnover_fraction = daily_turnover_pct / Decimal("100")
    consecutive_losses = _strict_quantity(
        consecutive_losses_raw, "consecutive_losses", default=0,
    )
    positions: list[BrokerPosition] = []
    for item in snapshot.get("positions", []):
        if not isinstance(item, dict):
            raise ValueError("position must be an object")
        code = _code(item.get("code") or item.get("jq_code"))
        if not code:
            raise ValueError("position code is required")
        total_qty = _strict_quantity(item.get("qty"), "position.qty")
        positions.append(BrokerPosition.from_values(
            code=code,
            total_qty=total_qty,
            sellable_qty=_strict_quantity(
                item.get("closeable_amount"), "position.closeable_amount",
                default=total_qty,
            ),
            frozen_qty=_strict_quantity(
                item.get("locked_amount"), "position.locked_amount", default=0,
            ),
            today_buy_qty=_strict_quantity(
                item.get("today_amount"), "position.today_amount", default=0,
            ),
            average_cost=_strict_decimal(
                item.get("avg_cost"), "position.avg_cost", default=0,
            ),
            last_price=_strict_decimal(
                item.get("price"), "position.price", default=0,
            ),
            market_value=_strict_decimal(
                item.get("market_value"), "position.market_value", default=0,
            ),
        ))

    strategy_version = str(
        snapshot.get("strategy_template_version")
        or snapshot.get("template_version")
        or snapshot.get("strategy_version")
        or ""
    )
    normalized_orders: dict[str, dict[str, object]] = {}
    open_orders: list[dict[str, object]] = []
    merged_orders: list[dict[str, object]] = []
    order_indexes: dict[str, int] = {}
    terminal = {
        "filled", "cancelled", "rejected", "risk_rejected", "failed",
        "skipped", "not_submitted", "expired",
    }
    status_map = {
        "submitted": "submitted",
        "held": "submitted",
        "open": "submitted",
        "new": "submitted",
        "partial": "partially_filled",
        "partial_filled": "partially_filled",
        "partially_filled": "partially_filled",
        "pending_cancel": "pending_cancel",
        "submit_unknown": "submit_unknown",
    }
    for item in snapshot.get("orders", []):
        if not isinstance(item, dict):
            raise ValueError("order must be an object")
        explicit_client_id = str(item.get("client_order_id") or "").strip()
        order = _legacy_order(
            item, trade_date=trade_date, strategy_version=strategy_version,
            persisted_orders=persisted_orders,
        )
        order["_explicit_client_order_id"] = explicit_client_id
        order_updated_at = _event_not_after(
            _strict_event_timestamp(
                order.get("updated_at") or generated_at, "order.updated_at",
            ),
            generated_at,
            "order.updated_at",
        )
        order["updated_at"] = order_updated_at
        broker_order_id = str(order.get("order_id") or "")
        client_id = str(order["client_order_id"])
        keys = [f"client:{client_id}"]
        if broker_order_id:
            keys.append(f"broker:{broker_order_id}")
        indexes = {order_indexes[key] for key in keys if key in order_indexes}
        if len(indexes) > 1:
            raise OrderConflictError(
                "order identity resolves to multiple snapshot rows"
            )
        if indexes:
            index = indexes.pop()
            previous = merged_orders[index]
            old_qty = _order_allowed_quantity(previous)
            new_qty = _order_allowed_quantity(order)
            if (
                str(previous["stock_code"]) != str(order["stock_code"])
                or str(previous["action"]) != str(order["action"])
                or (
                    previous.get("_explicit_client_order_id")
                    and explicit_client_id
                    and str(previous["_explicit_client_order_id"])
                    != explicit_client_id
                )
                or (
                    previous.get("order_id") and order.get("order_id")
                    and str(previous["order_id"]) != str(order["order_id"])
                )
                or (old_qty > 0 and new_qty > 0 and old_qty != new_qty)
                or int(order["filled_qty"] or 0)
                < int(previous["filled_qty"] or 0)
            ):
                raise OrderConflictError("duplicate snapshot order conflict")
            if not order.get("order_id") and previous.get("order_id"):
                order["order_id"] = previous["order_id"]
            merged_orders[index] = order
        else:
            index = len(merged_orders)
            merged_orders.append(order)
        order_indexes[f"client:{client_id}"] = index
        if order.get("order_id"):
            order_indexes[f"broker:{order['order_id']}"] = index

    for order in merged_orders:
        filled_qty = int(order["filled_qty"])
        broker_order_id = str(order.get("order_id") or "")
        if broker_order_id:
            normalized_orders[broker_order_id] = order
        status = str(order["status"])
        if status in terminal:
            continue
        target_qty = _order_allowed_quantity(order)
        if target_qty <= 0:
            raise ValueError("open order quantity must be positive")
        if filled_qty > target_qty:
            raise ValueError("open order filled quantity exceeds order quantity")
        if target_qty == filled_qty:
            continue
        mapped_status = status_map.get(status)
        if mapped_status is None:
            raise ValueError(f"unsupported JoinQuant open-order status: {status}")
        if not order.get("order_id"):
            mapped_status = "submit_unknown"
        open_orders.append({
            "client_order_id": order["client_order_id"],
            "broker_order_id": order.get("order_id"),
            "stock_code": order["stock_code"],
            "side": order["action"],
            "target_qty": target_qty,
            "filled_qty": filled_qty,
            "status": mapped_status,
            "updated_at": order["updated_at"],
        })

    fills: list[dict[str, object]] = []
    for item in snapshot.get("trades", []):
        if not isinstance(item, dict):
            raise ValueError("trade must be an object")
        broker_order_id = str(item.get("order_id") or "").strip()
        order = normalized_orders.get(broker_order_id)
        if order is None:
            raise ValueError("JoinQuant fill requires a matching order")
        fill = normalize_fill(item, orders=normalized_orders)
        if (
            fill["stock_code"] != order["stock_code"]
            or fill["action"] != order["action"]
        ):
            raise ValueError("JoinQuant fill does not match its order")
        fills.append({
            "broker_fill_id": fill["fill_id"],
            "client_order_id": order["client_order_id"],
            "broker_order_id": broker_order_id,
            "stock_code": fill["stock_code"],
            "side": fill["action"],
            "qty": int(fill["qty"]),
            "price": _strict_decimal(item.get("price"), "fill.price"),
            "commission_yuan": _strict_decimal(
                item.get("commission"), "fill.commission", default=0,
            ),
            "stamp_tax_yuan": _strict_decimal(
                item.get("stamp_tax"), "fill.stamp_tax", default=0,
            ),
            "transfer_fee_yuan": 0,
            "other_fee_yuan": _strict_decimal(
                item.get("other_fee"), "fill.other_fee", default=0,
            ),
            "fee_data_status": fill["fee_data_status"],
            "filled_at": _event_not_after(
                _strict_event_timestamp(
                    fill.get("filled_at") or generated_at, "fill.filled_at",
                ),
                generated_at,
                "fill.filled_at",
            ),
        })

    compatibility = "legacy-joinquant-v1"
    return BrokerSnapshot.from_values(
        snapshot_id=f"joinquant-{snapshot_id(snapshot)}",
        account_scope_id=account_scope_id,
        trade_date=trade_date,
        broker_time=generated_at,
        generated_at=generated_at,
        total_equity=total_equity,
        cash=cash,
        available_cash=available_cash,
        frozen_cash=cash - available_cash,
        positions=tuple(positions),
        open_orders=tuple(open_orders),
        fills=tuple(fills),
        adapter_version=f"{compatibility}-adapter",
        node_version=f"{compatibility}-node",
        session_id=f"{compatibility}-session",
        capabilities_version=f"{compatibility}-capabilities",
        adapter="joinquant",
        daily_risk_evidence_status=daily_risk_evidence_status,
        intraday_pnl=intraday_pnl,
        account_drawdown_pct=account_drawdown_pct,
        daily_turnover_fraction=daily_turnover_fraction,
        consecutive_losses=consecutive_losses,
    )


def is_joinquant_event_only_payload(snapshot: dict[str, Any]) -> bool:
    positions = snapshot.get("positions", [])
    has_events = bool(snapshot.get("orders") or snapshot.get("trades"))
    account_fields = ("cash", "available_cash", "total_value")
    account_count = sum(name in snapshot for name in account_fields)
    return (
        positions == []
        and (
            has_events
            or account_count == 0
        )
        and account_count < len(account_fields)
    )


def _strict_event_timestamp(value: object, name: str) -> str:
    text = str(value or "").strip()
    if len(text) < 16 or text[10:11] not in {" ", "T"} or text[13:14] != ":":
        raise ValueError(f"{name} is not a valid timestamp")
    return _shanghai_timestamp(text, name)


def _event_not_after(timestamp: str, cutoff: str, name: str) -> str:
    if datetime.fromisoformat(timestamp) > datetime.fromisoformat(cutoff):
        raise ValueError(f"{name} follows snapshot generated_at")
    return timestamp


def _validate_event_only_quantities(snapshot: dict[str, Any]) -> None:
    for item in snapshot.get("orders", []):
        if not isinstance(item, dict):
            raise ValueError("order must be an object")
        for field in (
            "amount", "requested_qty", "target_qty", "filled", "filled_qty",
        ):
            if field in item:
                _strict_quantity(item[field], f"order.{field}")
    for item in snapshot.get("trades", []):
        if not isinstance(item, dict):
            raise ValueError("trade must be an object")
        normalize_fill(item, orders={})


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


def _order_allowed_quantity(order: dict[str, object]) -> int:
    return order_allowed_quantity(
        order.get("requested_qty"), order.get("target_qty"),
    )


_ORDER_TERMINAL_STATUS = {
    "filled": "FILLED",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "risk_rejected": "REJECTED",
    "failed": "REJECTED",
    "skipped": "REJECTED",
    "not_submitted": "NOT_SUBMITTED",
    "expired": "EXPIRED",
}

_TERMINAL_REASON_CODES = {
    "suspended", "limit_up", "limit_down", "t_plus_one",
    "insufficient_cash", "price_moved", "gap_reentry_price_moved",
    "dry_run", "not_submitted", "suspended_or_rejected",
    "gap_reentry_partial_fill_complete",
    "gap_reentry_resealed_or_price_moved", "user_cancel",
}


def _terminal_reason_code(status: str, reason: object) -> str:
    normalized = str(reason or "").strip().lower()
    return normalized.upper() if normalized in _TERMINAL_REASON_CODES else status


def _enqueue_fill_notification(
    store: TradingStore,
    conn: Any,
    account_scope_id: str,
    execution: dict[str, object],
    source_fact_id: str,
) -> None:
    fill_identity = str(source_fact_id)
    occurred_at = _shanghai_timestamp(
        execution.get("filled_at"), "fill notification time",
    )
    action = str(execution.get("action") or "").lower()
    side = "买入" if action == "buy" else "卖出"
    code = str(execution.get("stock_code") or "")
    qty = int(execution.get("qty") or 0)
    price = float(execution.get("price") or 0)
    event = NotificationEvent(
        event_key=notification_event_key(
            "joinquant", account_scope_id, "fill", fill_id=fill_identity,
        ),
        account_scope_id=account_scope_id,
        adapter="joinquant",
        event_type="fill",
        object_type="fill",
        object_id=fill_identity,
        source_fact_id=fill_identity,
        priority="high",
        payload_version=1,
        occurred_at=occurred_at,
        expires_at=None,
        title="JoinQuant 模拟盘成交回报",
        body=(
            f"> {side} {code} | 本次 {qty}股 @ {price:.2f}\n"
            f"> 累计 {int(execution.get('cumulative_qty') or qty)}股 | "
            f"状态 {execution.get('status') or '-'}\n"
            f"> 成交时间：{occurred_at}"
        ),
        payload={
            "fill_id": fill_identity,
            "client_order_id": str(execution.get("client_order_id") or ""),
            "order_id": str(execution.get("order_id") or ""),
            "stock_code": code,
            "action": action,
            "qty": qty,
            "cumulative_qty": int(execution.get("cumulative_qty") or qty),
            "price": price,
            "status": str(execution.get("status") or ""),
        },
        metadata={"renderer": "fill-v1"},
    )
    store.enqueue_notification_or_gap(conn, event, occurred_at)


def _enqueue_order_terminal_notification(
    store: TradingStore,
    conn: Any,
    account_scope_id: str,
    order: dict[str, object],
) -> None:
    source_status = str(order.get("status") or "").lower()
    status = _ORDER_TERMINAL_STATUS.get(source_status)
    if status is None:
        return
    client_order_id = str(order.get("client_order_id") or "").strip()
    if not client_order_id:
        raise ValueError("terminal order client_order_id is required")
    reason_code = _terminal_reason_code(status, order.get("reason"))
    occurred_at = _shanghai_timestamp(
        order.get("completed_at") or order.get("updated_at"),
        "order terminal time",
    )
    source_fact_id, fact = _insert_order_terminal_fact(
        conn, account_scope_id, order, status, reason_code, occurred_at,
    )
    occurred_at = str(fact["occurred_at"])
    event = NotificationEvent(
        event_key=notification_event_key(
            "joinquant",
            account_scope_id,
            "order-terminal",
            client_order_id=client_order_id,
            status=status,
            reason_code=reason_code,
        ),
        account_scope_id=account_scope_id,
        adapter="joinquant",
        event_type="order_terminal",
        object_type="order",
        object_id=client_order_id,
        source_fact_id=source_fact_id,
        priority="high",
        payload_version=1,
        occurred_at=occurred_at,
        expires_at=None,
        title="JoinQuant 订单终态",
        body=(
            f"> {fact.get('action') or '-'} {fact.get('stock_code') or '-'}"
            f" | {status} | {reason_code}\n"
            f"> 委托 {int(fact['requested_qty'])}股 | "
            f"成交 {int(fact['filled_qty'])}股\n"
            f"> 业务时间：{occurred_at}"
        ),
        payload={
            "client_order_id": client_order_id,
            "order_id": str(fact.get("order_id") or ""),
            "stock_code": str(fact.get("stock_code") or ""),
            "action": str(fact.get("action") or ""),
            "status": status,
            "reason_code": reason_code,
            "requested_qty": int(fact["requested_qty"]),
            "filled_qty": int(fact["filled_qty"]),
        },
        metadata={"renderer": "order-terminal-v1"},
    )
    store.enqueue_notification_or_gap(conn, event, occurred_at)


def _insert_order_terminal_fact(
    conn: Any,
    account_scope_id: str,
    order: dict[str, object],
    status: str,
    reason_code: str,
    occurred_at: str,
) -> tuple[str, dict[str, object]]:
    fact = {
        "account_scope_id": account_scope_id,
        "client_order_id": str(order.get("client_order_id") or ""),
        "order_id": str(order.get("order_id") or ""),
        "stock_code": str(order.get("stock_code") or ""),
        "action": str(order.get("action") or ""),
        "status": status,
        "reason_code": reason_code,
        "requested_qty": int(_order_allowed_quantity(order)),
        "filled_qty": int(order.get("filled_qty") or 0),
        "occurred_at": occurred_at,
    }
    raw_json = canonical_json(fact)
    identity = canonical_json({
        "account_scope_id": account_scope_id,
        "client_order_id": fact["client_order_id"],
        "status": status,
        "reason_code": reason_code,
    })
    event_key = "terminal:" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()
    existing = conn.execute(
        "SELECT raw_json FROM order_events WHERE event_key=?", (event_key,),
    ).fetchone()
    if existing is not None:
        try:
            frozen = json.loads(str(existing["raw_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("terminal order fact is invalid") from exc
        if not isinstance(frozen, dict) or any(
            str(frozen.get(key) or "") != str(value)
            for key, value in {
                "account_scope_id": account_scope_id,
                "client_order_id": fact["client_order_id"],
                "status": status,
                "reason_code": reason_code,
            }.items()
        ):
            raise ValueError("terminal order immutable fact conflict")
        return event_key, frozen
    conn.execute(
        """INSERT INTO order_events(
           event_key, signal_id, order_id, stock_code, action, target_qty,
           requested_qty, filled_qty, status, reason, event_at, snapshot_at,
           raw_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_key,
            str(order.get("signal_id") or ""),
            str(order.get("order_id") or "") or None,
            str(order.get("stock_code") or ""),
            str(order.get("action") or ""),
            int(order.get("target_qty") or 0),
            int(_order_allowed_quantity(order)),
            int(order.get("filled_qty") or 0),
            status.lower(),
            reason_code,
            occurred_at,
            occurred_at,
            raw_json,
        ),
    )
    return event_key, fact


def _insert_legacy_progress_fact(
    conn: Any,
    execution: dict[str, object],
) -> bool:
    event_key = str(execution["event_id"])
    payload = canonical_json(execution)
    existing = conn.execute(
        "SELECT raw_json FROM order_events WHERE event_key=?", (event_key,),
    ).fetchone()
    if existing is not None:
        if str(existing["raw_json"]) != payload:
            raise ValueError("legacy order progress immutable fact conflict")
        return False
    conn.execute(
        """INSERT INTO order_events(
           event_key, signal_id, order_id, stock_code, action, target_qty,
           requested_qty, filled_qty, status, reason, event_at, snapshot_at,
           raw_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_key,
            str(execution.get("signal_id") or ""),
            str(execution.get("order_id") or "") or None,
            str(execution.get("stock_code") or ""),
            str(execution.get("action") or ""),
            None,
            int(execution.get("cumulative_qty") or 0),
            int(execution.get("cumulative_qty") or 0),
            str(execution.get("status") or "unknown"),
            "legacy_order_progress",
            str(execution.get("filled_at") or ""),
            str(execution.get("filled_at") or ""),
            payload,
        ),
    )
    return True


def persist_execution_events(
    store: TradingStore,
    conn: Any,
    snapshot: dict[str, Any],
    received_at: str,
    *,
    account_scope_id: str,
    allow_persisted_orders: bool,
    persisted_orders: dict[str, dict[str, object]],
) -> dict[str, Any]:
    generated_at = _shanghai_timestamp(
        snapshot.get("generated_at") or received_at, "generated_at",
    )
    trade_date = str(snapshot.get("trade_date") or generated_at[:10])[:10]
    strategy_version = str(
        snapshot.get("strategy_template_version") or snapshot.get("template_version")
        or snapshot.get("strategy_version") or ""
    )
    trade_events = [
        event for event in snapshot.get("trades", []) if isinstance(event, dict)
    ]
    # Old JoinQuant snapshots may report cumulative order progress without
    # individual trade rows. Per-order trade evidence still takes precedence.
    allow_legacy_progress = True
    trade_order_ids = {
        str(event.get("order_id") or "").strip()
        for event in trade_events
        if str(event.get("order_id") or "").strip()
    }
    new_executions: list[dict[str, object]] = []
    inserted_fill_count = 0
    orders_by_id: dict[str, dict[str, object]] = {}
    for event in snapshot.get("orders", []):
        if not isinstance(event, dict):
            raise ValueError("order must be an object")
        action = str(event.get("action") or "").strip().lower()
        if action not in {"buy", "sell"}:
            raise ValueError("order action must be buy or sell")
        if not _code(event.get("code") or event.get("jq_code")):
            raise ValueError("order code is required")
        order = _legacy_order(
            event, trade_date=trade_date, strategy_version=strategy_version,
            persisted_orders=persisted_orders,
        )
        if (
            not order.get("order_id")
            and str(order.get("status") or "").lower() in {
                "new", "open", "held", "submitted", "partial",
                "partially_filled", "pending_cancel",
            }
        ):
            order["status"] = "submit_unknown"
        order_timestamp = _strict_event_timestamp(
            order.get("updated_at") or generated_at, "order.updated_at",
        )
        _event_not_after(order_timestamp, generated_at, "order.updated_at")
        order["updated_at"] = order_timestamp
        if order.get("first_submitted_at"):
            first_submitted_at = _strict_event_timestamp(
                order["first_submitted_at"], "order.first_submitted_at",
            )
            if datetime.fromisoformat(first_submitted_at) > datetime.fromisoformat(
                order_timestamp
            ):
                raise ValueError(
                    "order.first_submitted_at follows order.updated_at"
                )
            order["first_submitted_at"] = first_submitted_at
        if order.get("completed_at"):
            order["completed_at"] = order_timestamp
        previous = conn.execute(
            """SELECT filled_qty FROM orders
               WHERE client_order_id=?
                  OR (order_id IS NOT NULL AND order_id=?)""",
            (order["client_order_id"], order.get("order_id")),
        ).fetchone()
        previous_filled = int(previous[0]) if previous is not None else 0
        current_filled = int(order.get("filled_qty") or 0)
        allowed_qty = _order_allowed_quantity(order)
        if current_filled > allowed_qty:
            raise ValueError(
                "order filled quantity exceeds order quantity"
            )
        has_linked_trade = bool(
            order.get("order_id")
            and str(order["order_id"]) in trade_order_ids
        )
        if allow_legacy_progress and not has_linked_trade and current_filled > 0:
            average_fill_price = _strict_decimal(
                event.get("avg_price")
                if event.get("avg_price") not in (None, "")
                else event.get("price"),
                "order average fill price",
            )
            if average_fill_price <= 0:
                raise ValueError(
                    "order average fill price must be positive"
                )
            order["average_fill_price"] = float(average_fill_price)
        store.upsert_order(conn, order)
        stored_order = conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?",
            (order["client_order_id"],),
        ).fetchone()
        if stored_order is not None:
            order = dict(stored_order)
        if order.get("order_id"):
            order_id = str(order["order_id"])
            orders_by_id[order_id] = order
            persisted_orders[order_id] = order
        if (
            allow_legacy_progress
            and not has_linked_trade
            and order.get("action") in {"buy", "sell"}
            and current_filled > previous_filled
        ):
            execution = {
                "event_id": f"legacy:{order['client_order_id']}:{current_filled}",
                "source": "legacy_order_progress",
                "client_order_id": order["client_order_id"],
                "order_id": order.get("order_id"),
                "signal_id": order.get("signal_id"),
                "stock_code": order.get("stock_code"),
                "action": order.get("action"),
                "qty": current_filled - previous_filled,
                "cumulative_qty": current_filled,
                "price": order.get("average_fill_price"),
                "status": order.get("status"),
                "filled_at": order.get("updated_at") or generated_at,
            }
            if _insert_legacy_progress_fact(conn, execution):
                _enqueue_fill_notification(
                    store,
                    conn,
                    account_scope_id,
                    execution,
                    str(execution["event_id"]),
                )
                new_executions.append(execution)
        if not has_linked_trade:
            _enqueue_order_terminal_notification(
                store, conn, account_scope_id, order,
            )
    for event in trade_events:
        order_id = str(event.get("order_id") or "").strip()
        order = orders_by_id.get(order_id)
        if order is None and allow_persisted_orders and order_id:
            stored = conn.execute(
                "SELECT * FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if stored is not None:
                order = dict(stored)
                orders_by_id[order_id] = order
        if order is None:
            raise ValueError("JoinQuant fill requires a matching order")
        fill = normalize_fill(event, orders=orders_by_id)
        action = str(event.get("action") or "").strip().lower()
        code = _code(event.get("code") or event.get("jq_code"))
        qty = int(fill["qty"])
        price = _strict_decimal(event.get("price"), "fill.price")
        if action not in {"buy", "sell"}:
            raise ValueError("fill action must be buy or sell")
        if not code:
            raise ValueError("fill code is required")
        if qty <= 0:
            raise ValueError("fill.amount must be positive")
        if price <= 0:
            raise ValueError("fill.price must be positive")
        filled_at = _strict_event_timestamp(
            event.get("datetime") or event.get("filled_at"),
            "fill.filled_at",
        )
        _event_not_after(filled_at, generated_at, "fill.filled_at")
        for fee_name in ("commission", "stamp_tax", "other_fee"):
            if fee_name in event and event.get(fee_name) not in (None, ""):
                _strict_decimal(event[fee_name], f"fill.{fee_name}")
        if action != order.get("action") or code != order.get("stock_code"):
            raise ValueError("JoinQuant fill does not match its order")
        fill["price"] = float(price)
        fill["filled_at"] = filled_at
        allowed_qty = _order_allowed_quantity(order)
        current_status = str(order.get("status") or "unknown")
        if current_status in {
            "rejected", "failed", "skipped", "risk_rejected",
            "not_submitted", "expired",
        }:
            raise ValueError("fill conflicts with terminal order")
        existing_fill = conn.execute(
            "SELECT 1 FROM fills WHERE fill_id=?",
            (fill["fill_id"],),
        ).fetchone()
        if existing_fill is None:
            prior_fill_qty = int(conn.execute(
                """SELECT COALESCE(SUM(qty), 0) FROM fills
                   WHERE client_order_id=? OR order_id=?""",
                (order["client_order_id"], order_id),
            ).fetchone()[0] or 0)
            if qty > allowed_qty or prior_fill_qty + qty > allowed_qty:
                raise ValueError(
                    "fill quantity exceeds linked order quantity"
                )
            if (
                current_status == "cancelled"
                and prior_fill_qty + qty > int(order.get("filled_qty") or 0)
            ):
                raise ValueError("fill conflicts with cancelled terminal order")
        inserted = store.insert_fill(conn, fill)
        inserted_fill_count += int(inserted)
        fill_totals = conn.execute(
            """SELECT COALESCE(SUM(qty), 0),
                      COALESCE(SUM(qty * price), 0)
               FROM fills
               WHERE client_order_id=? OR order_id=?""",
            (order["client_order_id"], order_id),
        ).fetchone()
        fill_qty = int(fill_totals[0] or 0)
        if fill_qty > allowed_qty:
            raise ValueError("fills exceed linked order quantity")
        cumulative_qty = max(int(order.get("filled_qty") or 0), fill_qty)
        average_fill_price = (
            float(fill_totals[1]) / fill_qty
            if fill_qty > 0
            else float(order.get("average_fill_price") or 0)
        )
        terminal = {"filled", "cancelled"}
        if current_status in terminal:
            status = current_status
        elif cumulative_qty >= allowed_qty:
            status = "filled"
        elif cumulative_qty > 0:
            status = "partial"
        else:
            status = current_status
        conn.execute(
            """UPDATE orders
               SET filled_qty=?, average_fill_price=?, status=?,
                   updated_at=max(updated_at, ?),
                   completed_at=CASE
                       WHEN ?='filled' THEN COALESCE(completed_at, ?)
                       ELSE completed_at
                   END
               WHERE client_order_id=?""",
            (
                cumulative_qty, average_fill_price, status, filled_at,
                status, filled_at, order["client_order_id"],
            ),
        )
        order["filled_qty"] = cumulative_qty
        order["average_fill_price"] = average_fill_price
        order["status"] = status
        if status == "filled":
            order["completed_at"] = order.get("completed_at") or filled_at
        if not inserted:
            continue
        legacy_prefix = f"legacy:{order['client_order_id']}:"
        legacy_covered_qty = int(conn.execute(
            """SELECT COALESCE(MAX(filled_qty), 0)
               FROM order_events
               WHERE reason='legacy_order_progress'
                 AND (order_id=? OR substr(event_key, 1, ?)=?)""",
            (order_id, len(legacy_prefix), legacy_prefix),
        ).fetchone()[0] or 0)
        covered_remaining = max(0, legacy_covered_qty - prior_fill_qty)
        notification_qty = max(0, qty - covered_remaining)
        if notification_qty == 0:
            continue
        execution = {
            "event_id": f"fill:{fill['fill_id']}",
            "source": "fill",
            "client_order_id": fill.get("client_order_id"),
            "order_id": fill.get("order_id"),
            "signal_id": fill.get("signal_id"),
            "stock_code": fill.get("stock_code"),
            "action": fill.get("action"),
            "qty": notification_qty,
            "cumulative_qty": cumulative_qty,
            "price": fill.get("price"),
            "status": status,
            "filled_at": fill.get("filled_at"),
        }
        _enqueue_fill_notification(
            store,
            conn,
            account_scope_id,
            execution,
            str(fill["fill_id"]),
        )
        new_executions.append(execution)
    for order_id in sorted(trade_order_ids):
        order = orders_by_id.get(order_id)
        if order is not None:
            _enqueue_order_terminal_notification(
                store, conn, account_scope_id, order,
            )
    return {
        "inserted_fills": inserted_fill_count,
        "new_executions": new_executions,
    }


def persist_account_snapshot(
    store: TradingStore,
    conn: Any,
    snapshot: dict[str, Any],
    received_at: str,
    persisted_orders: dict[str, dict[str, object]],
    account_scope_id: str,
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
            _num(snapshot.get("account_drawdown_pct"), 0), str(
                snapshot.get("strategy_template_version") or snapshot.get("template_version") or ""
            ),
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

    execution_result = persist_execution_events(
        store, conn, snapshot, received_at, allow_persisted_orders=False,
        persisted_orders=persisted_orders,
        account_scope_id=account_scope_id,
    )
    new_executions = execution_result["new_executions"]

    fee_row = conn.execute(
        """SELECT COALESCE(sum(commission+stamp_tax+other_fee),0),
                  sum(CASE WHEN fee_data_status='unknown' THEN 1 ELSE 0 END)
           FROM fills WHERE substr(filled_at,1,10)=?""",
        (trade_date,),
    ).fetchone()
    fees = fee_row[0]
    fee_data_status = "unknown" if int(fee_row[1] or 0) else "reported"
    realized_pnl = _num(snapshot.get("realized_pnl"))
    realized_pnl_status = "reported" if realized_pnl is not None else "unknown"
    unrealized = sum(float(_num(item.get("pnl"), 0) or 0) for item in positions)
    total_value = float(_num(snapshot.get("total_value"), 0) or 0)
    drawdown = float(_num(snapshot.get("account_drawdown_pct"), 0) or 0)
    conn.execute(
        """INSERT INTO daily_equity(
           trade_date, opening_equity, closing_equity, cash, position_market_value,
           realized_pnl, unrealized_pnl, fees, net_deposit, max_drawdown_pct,
           first_snapshot_at, last_snapshot_at, fee_data_status, realized_pnl_status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
           ON CONFLICT(trade_date) DO UPDATE SET
           closing_equity=CASE WHEN excluded.last_snapshot_at>=last_snapshot_at THEN excluded.closing_equity ELSE closing_equity END,
           cash=CASE WHEN excluded.last_snapshot_at>=last_snapshot_at THEN excluded.cash ELSE cash END,
           position_market_value=CASE WHEN excluded.last_snapshot_at>=last_snapshot_at THEN excluded.position_market_value ELSE position_market_value END,
           unrealized_pnl=CASE WHEN excluded.last_snapshot_at>=last_snapshot_at THEN excluded.unrealized_pnl ELSE unrealized_pnl END,
           fees=excluded.fees,
           fee_data_status=excluded.fee_data_status,
           realized_pnl=CASE
               WHEN excluded.realized_pnl_status='reported' THEN excluded.realized_pnl
               ELSE realized_pnl END,
           realized_pnl_status=CASE
               WHEN excluded.realized_pnl_status='reported' THEN 'reported'
               ELSE realized_pnl_status END,
           max_drawdown_pct=min(max_drawdown_pct, excluded.max_drawdown_pct),
           first_snapshot_at=min(first_snapshot_at, excluded.first_snapshot_at),
           last_snapshot_at=max(last_snapshot_at, excluded.last_snapshot_at)""",
        (
            trade_date, total_value, total_value, _num(snapshot.get("cash"), 0), market_value,
            realized_pnl or 0, unrealized, fees, drawdown, generated_at, generated_at,
            fee_data_status, realized_pnl_status,
        ),
    )
    return {
        "snapshot_id": sid,
        "retained_details": retain,
        "inserted_fills": execution_result["inserted_fills"],
        "new_executions": new_executions,
    }


def ingest_snapshot_payload(
    snapshot: dict[str, Any], store: TradingStore, received_at: str, mode: str = "incremental"
) -> dict[str, Any]:
    snapshot = sanitize_joinquant_payload(snapshot)
    store.initialize()
    event_only = is_joinquant_event_only_payload(snapshot)
    if event_only:
        _validate_event_only_quantities(snapshot)
        with store.transaction() as conn:
            persisted_orders = _persisted_broker_orders(conn, snapshot)
            account_scope_id = store.get_or_create_account_scope(
                conn, "joinquant", "primary",
            )
            execution_result = persist_execution_events(
                store, conn, snapshot, received_at,
                account_scope_id=account_scope_id,
                allow_persisted_orders=True,
                persisted_orders=persisted_orders,
            )
        return {
            "event_only": True,
            "snapshot_id": None,
            **execution_result,
        }
    positions = [
        _position(item, snapshot) for item in snapshot.get("positions", []) if isinstance(item, dict)
    ]
    positions = [item for item in positions if item["code"] and item["qty"] > 0]
    snapshot_at = str(snapshot.get("generated_at") or snapshot.get("received_at") or received_at)
    with store.transaction() as conn:
        persisted_orders = _persisted_broker_orders(conn, snapshot)
        account_scope_id = store.get_or_create_account_scope(
            conn, "joinquant", "primary",
        )
        broker_snapshot = _legacy_broker_snapshot(
            snapshot, account_scope_id, received_at, persisted_orders,
        )
        result = persist_account_snapshot(
            store, conn, snapshot, received_at, persisted_orders,
            account_scope_id,
        )
        store.replace_current_broker_snapshot(conn, broker_snapshot)
        store.reconcile_position_cycles(conn, positions, snapshot_at)
        store.reconcile_order_events(conn, snapshot.get("orders", []), snapshot_at)
        store.reconcile_exit_intents(conn, positions, snapshot_at)
        reconciliation = reconcile_snapshot(
            store, conn, snapshot, snapshot_id=result["snapshot_id"],
            broker_snapshot=broker_snapshot, mode=mode, now=received_at,
        )
        lifecycle = synchronize_execution_lifecycle(
            store,
            conn,
            account_scope_id,
            now=_shanghai_timestamp(received_at, "received_at"),
            reconciliation_id=(
                reconciliation.reconciliation_id
                if mode == "full"
                and reconciliation.result == "matched"
                and not reconciliation.differences
                else None
            ),
        )
        expired_ready_intents = expire_ready_intents_in_transaction(
            store, conn, _shanghai_timestamp(received_at, "received_at"),
        )
        persist_issue_transitions(store, conn, reconciliation, received_at)
        actions = apply_reconciliation_control(store, conn, reconciliation)
        automatic_recovery = None
        if reconciliation.result == "matched":
            automatic_recovery = apply_automatic_buy_recovery(
                store, conn, reconciliation, now=received_at,
                required_template=app_config.JOINQUANT_TEMPLATE_VERSION,
            )
            if automatic_recovery:
                actions.append("auto_resume_buy")
                reconciliation.control_action = ",".join(actions)
                reconciliation.transitions.append({
                    "issue_key": "control:buy_enabled", "previous_state": "0",
                    "state": "RECOVERED", "severity": "INFO",
                    "transitioned": True, "transition": "RECOVERED",
                })
        result["reconciliation"] = reconciliation
        result["execution_lifecycle"] = lifecycle
        result["expired_ready_intents"] = expired_ready_intents
        result["control_actions"] = actions
        result["automatic_recovery"] = automatic_recovery
        today = received_at[:10]
        last_pruned = conn.execute(
            "SELECT value FROM system_state WHERE key='execution_history_last_pruned'"
        ).fetchone()
        if last_pruned is None or str(last_pruned[0]) != today:
            cutoff = (datetime.fromisoformat(received_at).date() - timedelta(days=366)).isoformat()
            store.prune_execution_history(conn, cutoff, received_at)
            store.set_system_state(conn, "execution_history_last_pruned", today, "366-day hot retention")
    result["event_only"] = False
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
    snapshot = sanitize_joinquant_payload(_load_snapshot(account_file))

    positions = []
    for item in snapshot.get("positions", []):
        if isinstance(item, dict):
            pos = _position(item, snapshot)
            if pos["code"] and pos["qty"] > 0:
                positions.append(pos)

    store = store or TradingStore(app_config.TRADING_DB_FILE)
    ingest_snapshot_payload(snapshot, store, str(snapshot.get("received_at") or _now()))
    apply_cycle_risk_fields(positions, store.get_active_position_cycles())
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
