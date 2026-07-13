from __future__ import annotations

import hashlib
from typing import Any

from trading_store import canonical_json


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return abs(int(float(value or 0)))
    except Exception:
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def client_order_id(event: dict[str, object], trade_date: str, strategy_version: str) -> str:
    signal_id = _text(event.get("id") or event.get("signal_id"))
    order_id = _text(event.get("order_id"))
    if not signal_id or signal_id.startswith("jq-order-"):
        return f"manual:{order_id}" if order_id else "manual:" + hashlib.sha256(
            canonical_json(event).encode("utf-8")
        ).hexdigest()[:24]
    raw = "".join((
        strategy_version, trade_date[:10], signal_id,
        _text(event.get("action")).lower(), _text(event.get("jq_code") or event.get("code")),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def fill_id(trade: dict[str, object]) -> str:
    explicit = _text(trade.get("trade_id") or trade.get("fill_id") or trade.get("id"))
    if explicit:
        return explicit
    raw = "|".join((
        _text(trade.get("order_id")), _text(trade.get("code") or trade.get("jq_code"))[:6],
        _text(trade.get("action")).lower(), _text(trade.get("datetime") or trade.get("filled_at")),
        str(_int(trade.get("amount") or trade.get("qty"))), str(_float(trade.get("price"))),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_order(
    event: dict[str, object], *, trade_date: str, strategy_version: str
) -> dict[str, object]:
    signal_id = _text(event.get("id") or event.get("signal_id"))
    if signal_id.startswith("jq-order-"):
        signal_id = ""
    status = _text(event.get("status")).split(".")[-1].lower() or "unknown"
    status = {"held": "submitted", "canceled": "cancelled", "partial_filled": "partial"}.get(status, status)
    updated_at = _text(event.get("datetime") or event.get("updated_at"))
    return {
        "client_order_id": client_order_id(event, trade_date, strategy_version),
        "signal_id": signal_id or None,
        "order_id": _text(event.get("order_id")) or None,
        "stock_code": _text(event.get("code") or event.get("jq_code"))[:6],
        "action": _text(event.get("action")).lower(),
        "target_qty": event.get("target_qty"),
        "requested_qty": _int(event.get("amount") or event.get("requested_qty")),
        "filled_qty": _int(event.get("filled") or event.get("filled_qty")),
        "average_fill_price": _float(event.get("avg_price") or event.get("price")),
        "status": status,
        "submit_count": max(1, _int(event.get("submit_count"))),
        "reason": _text(event.get("reason")),
        "first_submitted_at": updated_at or None,
        "updated_at": updated_at,
        "completed_at": updated_at if status in {"filled", "cancelled", "rejected", "risk_rejected"} else None,
        "raw_json": canonical_json(event),
    }


def normalize_fill(
    trade: dict[str, object], *, orders: dict[str, dict[str, object]]
) -> dict[str, object]:
    order_id = _text(trade.get("order_id"))
    order = orders.get(order_id, {})
    return {
        "fill_id": fill_id(trade),
        "client_order_id": order.get("client_order_id"),
        "order_id": order_id or None,
        "signal_id": order.get("signal_id") or _text(trade.get("signal_id")) or None,
        "stock_code": _text(trade.get("code") or trade.get("jq_code"))[:6],
        "action": _text(trade.get("action")).lower(),
        "qty": _int(trade.get("amount") or trade.get("qty")),
        "price": _float(trade.get("price")),
        "commission": _float(trade.get("commission")),
        "stamp_tax": _float(trade.get("stamp_tax")),
        "other_fee": _float(trade.get("other_fee")),
        "filled_at": _text(trade.get("datetime") or trade.get("filled_at")),
        "raw_json": canonical_json(trade),
    }
