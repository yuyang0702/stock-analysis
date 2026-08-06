from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_store import canonical_json


def _text(value: Any) -> str:
    return str(value or "").strip()


def _explicit_client_order_id(event: dict[str, object]) -> str | None:
    value = event.get("client_order_id")
    if value in (None, ""):
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("client_order_id must be a non-empty clean string")
    return value


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


def _quantity_alias(
    event: dict[str, object], keys: tuple[str, ...], name: str,
) -> int:
    values: list[int] = []
    for key in keys:
        value = event.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a non-negative integer")
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{key} must be a non-negative integer") from exc
        if (
            not number.is_finite()
            or number < 0
            or number != number.to_integral_value()
        ):
            raise ValueError(f"{key} must be a non-negative integer")
        values.append(int(number))
    if len(set(values)) > 1:
        raise ValueError(f"{name} fields conflict")
    return values[0] if values else 0


def _reported_number(event: dict[str, object], key: str) -> bool:
    if key not in event or event.get(key) in (None, ""):
        return False
    try:
        float(event[key])
        return True
    except (TypeError, ValueError):
        return False


def client_order_id(event: dict[str, object], trade_date: str, strategy_version: str) -> str:
    explicit = _explicit_client_order_id(event)
    if explicit is not None:
        return explicit
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
    qty = _quantity_alias(trade, ("amount", "qty"), "fill quantity")
    explicit = _text(trade.get("trade_id") or trade.get("fill_id") or trade.get("id"))
    if explicit:
        return explicit
    raw = "|".join((
        _text(trade.get("order_id")), _text(trade.get("code") or trade.get("jq_code"))[:6],
        _text(trade.get("action")).lower(), _text(trade.get("datetime") or trade.get("filled_at")),
        str(qty), str(_float(trade.get("price"))),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_order(
    event: dict[str, object], *, trade_date: str, strategy_version: str
) -> dict[str, object]:
    requested_qty = _quantity_alias(
        event, ("amount", "requested_qty"), "requested quantity",
    )
    filled_qty = _quantity_alias(
        event, ("filled", "filled_qty"), "filled quantity",
    )
    target_qty = (
        _quantity_alias(event, ("target_qty",), "target quantity")
        if event.get("target_qty") not in (None, "")
        else None
    )
    signal_id = _text(event.get("id") or event.get("signal_id"))
    if signal_id.startswith("jq-order-"):
        signal_id = ""
    status = _text(event.get("status")).split(".")[-1].lower() or "unknown"
    status = {
        "held": "submitted",
        "canceled": "cancelled",
        "partial_filled": "partial",
        "partially_filled": "partial",
    }.get(status, status)
    not_submitted_reason = status if status in {
        "suspended", "limit_up", "limit_down", "t_plus_one",
        "insufficient_cash", "price_moved", "gap_reentry_price_moved",
        "dry_run",
    } else ""
    if not_submitted_reason:
        status = "not_submitted"
    allowed_qty = requested_qty if requested_qty > 0 else int(target_qty or 0)
    terminal = {
        "filled", "cancelled", "rejected", "risk_rejected", "failed",
        "skipped", "not_submitted", "expired",
    }
    if filled_qty > allowed_qty:
        raise ValueError("order filled quantity exceeds order quantity")
    if status == "filled" and (
        allowed_qty <= 0 or filled_qty != allowed_qty
    ):
        raise ValueError("filled order quantity is incomplete")
    if status not in terminal and allowed_qty > 0:
        if filled_qty == allowed_qty:
            status = "filled"
        elif filled_qty > 0:
            status = "partial"
        elif status == "partial":
            status = "submitted"
    updated_at = _text(event.get("datetime") or event.get("updated_at"))
    submit_count = (
        _quantity_alias(event, ("submit_count",), "submit count")
        if event.get("submit_count") not in (None, "")
        else (0 if status == "not_submitted" else 1)
    )
    first_submitted_at = _text(event.get("first_submitted_at"))
    if submit_count == 0 and first_submitted_at:
        raise ValueError(
            "order first_submitted_at conflicts with zero submit_count"
        )
    if submit_count > 0 and not first_submitted_at:
        first_submitted_at = updated_at
    return {
        "client_order_id": client_order_id(event, trade_date, strategy_version),
        "signal_id": signal_id or None,
        "order_id": _text(event.get("order_id")) or None,
        "stock_code": _text(event.get("code") or event.get("jq_code"))[:6],
        "action": _text(event.get("action")).lower(),
        "target_qty": target_qty,
        "requested_qty": requested_qty,
        "filled_qty": filled_qty,
        "average_fill_price": _float(event.get("avg_price") or event.get("price")),
        "status": status,
        "submit_count": submit_count,
        "reason": _text(event.get("reason")) or not_submitted_reason,
        "first_submitted_at": first_submitted_at or None,
        "updated_at": updated_at,
        "completed_at": updated_at if status in terminal else None,
        "raw_json": canonical_json(event),
    }


def normalize_fill(
    trade: dict[str, object], *, orders: dict[str, dict[str, object]]
) -> dict[str, object]:
    qty = _quantity_alias(trade, ("amount", "qty"), "fill quantity")
    order_id = _text(trade.get("order_id"))
    order = orders.get(order_id, {})
    explicit_client_id = _explicit_client_order_id(trade)
    linked_client_id = order.get("client_order_id")
    if (
        explicit_client_id is not None
        and linked_client_id
        and explicit_client_id != linked_client_id
    ):
        raise ValueError("fill client_order_id conflicts with linked order")
    fee_fields = ("commission", "stamp_tax", "other_fee")
    source_fee_status = _text(trade.get("fee_data_status")).lower()
    return {
        "fill_id": fill_id(trade),
        "client_order_id": explicit_client_id or linked_client_id,
        "order_id": order_id or None,
        "signal_id": order.get("signal_id") or _text(trade.get("signal_id")) or None,
        "stock_code": _text(trade.get("code") or trade.get("jq_code"))[:6],
        "action": _text(trade.get("action")).lower(),
        "qty": qty,
        "price": _float(trade.get("price")),
        "commission": _float(trade.get("commission")),
        "stamp_tax": _float(trade.get("stamp_tax")),
        "other_fee": _float(trade.get("other_fee")),
        "fee_data_status": (
            "reported"
            if source_fee_status == "reported"
            and all(_reported_number(trade, key) for key in fee_fields)
            else "unknown"
        ),
        "filled_at": _text(trade.get("datetime") or trade.get("filled_at")),
        "raw_json": canonical_json(trade),
    }
