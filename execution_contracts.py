"""Immutable, versioned contracts shared by sizing and execution paths."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from types import MappingProxyType
from typing import Mapping


CENT = Decimal("0.01")
RATIO_QUANTUM = Decimal("0.00000001")
ZERO = Decimal("0")
UNCATEGORIZED = "__UNCATEGORIZED__"


def _decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
    optional: bool = False,
    signed: bool = False,
) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite() or (not signed and result < 0) or (positive and result <= 0):
        qualifier = "positive" if positive else "finite" if signed else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} decimal")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _ratio(numerator: Decimal, denominator: Decimal, name: str) -> Decimal:
    if denominator <= ZERO:
        raise ValueError(f"{name} denominator must be positive")
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_UP
        return (numerator / denominator).quantize(RATIO_QUANTUM)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _text(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} is required")
    return result


def _classification(value: object, name: str) -> str:
    if value is None:
        return UNCATEGORIZED
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value.strip() or UNCATEGORIZED


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return _text(value, name)


def _evidence_status(value: object, name: str) -> str:
    status = _text(value, name).lower()
    if status not in {"available", "unavailable"}:
        raise ValueError(f"{name} must be available or unavailable")
    return status


def _evidence_sha256(value: object, name: str, status: str) -> str:
    text = _text(value, name)
    if status == "unavailable":
        if text.lower() != "not-applicable":
            raise ValueError(f"{name} must be not-applicable when evidence is unavailable")
        return "not-applicable"
    return _sha256_text(text, name)


def _date_text(value: object, name: str) -> str:
    result = _text(value, name)
    try:
        parsed = date.fromisoformat(result)
    except ValueError as error:
        raise ValueError(f"{name} must be YYYY-MM-DD") from error
    if parsed.isoformat() != result:
        raise ValueError(f"{name} must be YYYY-MM-DD")
    return result


def _timestamp(value: object, name: str) -> str:
    result = _text(value, name)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _qty(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be {qualifier}")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _side(value: object) -> str:
    result = _text(value, "side").lower()
    if result not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    return result


def _canonicalize(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON does not allow non-finite decimals")
        return _decimal_text(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not allow non-finite floats")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonicalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be strings")
        return {key: _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if hasattr(value, "to_dict"):
        return _canonicalize(value.to_dict())
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def logical_signal_id(
    account_scope_id: str,
    trade_date: str,
    strategy_id: str,
    strategy_version: str,
    code: str,
    side: str,
    setup_type: str,
) -> str:
    return canonical_sha256(
        {
            "account_scope_id": account_scope_id,
            "trade_date": trade_date,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "code": code,
            "side": side,
            "setup_type": setup_type,
        }
    )[:20]


def client_order_id(
    account_scope_id: str,
    adapter: str,
    logical_signal_id: str,
    pre_trade_result_id: str,
    exact_order: object,
    submission_attempt_id: str,
) -> str:
    return canonical_sha256(
        {
            "account_scope_id": _text(account_scope_id, "account_scope_id"),
            "adapter": _text(adapter, "adapter"),
            "logical_signal_id": _text(logical_signal_id, "logical_signal_id"),
            "pre_trade_result_id": _text(pre_trade_result_id, "pre_trade_result_id"),
            "exact_order": _normalize_exact_order(exact_order),
            "submission_attempt_id": _text(submission_attempt_id, "submission_attempt_id"),
        }
    )[:32]


def _record_dict(value: object) -> dict[str, object]:
    return {field.name: _thaw(getattr(value, field.name)) for field in fields(value)}


def _thaw(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _record_dict(value)
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in sorted(value.items())})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _verify_hash(expected: object, actual: str, name: str) -> None:
    if expected not in (None, "") and str(expected) != actual:
        raise ValueError(f"{name} content hash conflict")


def _sha256_text(value: object, name: str) -> str:
    result = _text(value, name).lower()
    if len(result) != 64:
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    try:
        int(result, 16)
    except ValueError as error:
        raise ValueError(f"{name} must be a SHA-256 hex digest") from error
    return result


_EXACT_ORDER_FIELDS = frozenset(
    {
        "code",
        "side",
        "order_qty",
        "expected_current_qty",
        "target_position_qty",
        "limit_price",
        "price_cap",
        "stop_price",
        "expires_at",
    }
)


def _normalize_exact_order(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _EXACT_ORDER_FIELDS:
        raise ValueError("exact_order fields do not match the normalized contract")
    return {
        "code": _text(value["code"], "code"),
        "side": _side(value["side"]),
        "order_qty": _qty(value["order_qty"], "order_qty", positive=True),
        "expected_current_qty": _qty(value["expected_current_qty"], "expected_current_qty"),
        "target_position_qty": _qty(value["target_position_qty"], "target_position_qty"),
        "limit_price": _decimal(value["limit_price"], "limit_price", positive=True, optional=True),
        "price_cap": _decimal(value["price_cap"], "price_cap", positive=True, optional=True),
        "stop_price": _decimal(value["stop_price"], "stop_price", positive=True, optional=True),
        "expires_at": _timestamp(value["expires_at"], "expires_at"),
    }


_OPEN_ORDER_FIELDS = frozenset(
    {
        "client_order_id",
        "broker_order_id",
        "stock_code",
        "side",
        "target_qty",
        "filled_qty",
        "status",
        "updated_at",
    }
)
_OPEN_ORDER_STATUSES = frozenset(
    {"submit_unknown", "submitted", "partially_filled", "pending_cancel"}
)
_FILL_FIELDS = frozenset(
    {
        "broker_fill_id",
        "client_order_id",
        "broker_order_id",
        "stock_code",
        "side",
        "qty",
        "price",
        "commission_yuan",
        "stamp_tax_yuan",
        "transfer_fee_yuan",
        "other_fee_yuan",
        "fee_data_status",
        "filled_at",
    }
)


def _record_mapping(value: object, name: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        payload = value
    elif callable(getattr(value, "to_dict", None)):
        payload = value.to_dict()
    else:
        raise ValueError(f"{name} must be a normalized mapping")
    if not isinstance(payload, Mapping):
        raise ValueError(f"{name}.to_dict() must return a mapping")
    return dict(payload)


def _normalize_open_order(value: object) -> Mapping[str, object]:
    payload = _record_mapping(value, "open order")
    if set(payload) != _OPEN_ORDER_FIELDS:
        raise ValueError("open order fields do not match the normalized contract")
    target_qty = _qty(payload["target_qty"], "target_qty", positive=True)
    filled_qty = _qty(payload["filled_qty"], "filled_qty")
    if filled_qty >= target_qty:
        raise ValueError("open order filled_qty must be below target_qty")
    status = _text(payload["status"], "status").lower()
    if status not in _OPEN_ORDER_STATUSES:
        raise ValueError("open order status is not normalized")
    broker_order_id = payload["broker_order_id"]
    if broker_order_id is not None or status != "submit_unknown":
        broker_order_id = _text(broker_order_id, "broker_order_id")
    return _freeze(
        {
            "client_order_id": _text(payload["client_order_id"], "client_order_id"),
            "broker_order_id": broker_order_id,
            "stock_code": _text(payload["stock_code"], "stock_code"),
            "side": _side(payload["side"]),
            "target_qty": target_qty,
            "filled_qty": filled_qty,
            "status": status,
            "updated_at": _timestamp(payload["updated_at"], "updated_at"),
        }
    )


def _normalize_fill(value: object) -> Mapping[str, object]:
    payload = _record_mapping(value, "fill")
    if set(payload) != _FILL_FIELDS:
        raise ValueError("fill fields do not match the normalized contract")
    fee_status = _text(payload["fee_data_status"], "fee_data_status").lower()
    if fee_status not in {"reported", "unknown"}:
        raise ValueError("fill fee_data_status is not normalized")
    return _freeze(
        {
            "broker_fill_id": _text(payload["broker_fill_id"], "broker_fill_id"),
            "client_order_id": _text(payload["client_order_id"], "client_order_id"),
            "broker_order_id": _text(payload["broker_order_id"], "broker_order_id"),
            "stock_code": _text(payload["stock_code"], "stock_code"),
            "side": _side(payload["side"]),
            "qty": _qty(payload["qty"], "qty", positive=True),
            "price": _decimal(payload["price"], "price", positive=True),
            "commission_yuan": _decimal(payload["commission_yuan"], "commission_yuan"),
            "stamp_tax_yuan": _decimal(payload["stamp_tax_yuan"], "stamp_tax_yuan"),
            "transfer_fee_yuan": _decimal(payload["transfer_fee_yuan"], "transfer_fee_yuan"),
            "other_fee_yuan": _decimal(payload["other_fee_yuan"], "other_fee_yuan"),
            "fee_data_status": fee_status,
            "filled_at": _timestamp(payload["filled_at"], "filled_at"),
        }
    )


@dataclass(frozen=True)
class FeeBreakdown:
    schedule_version: str
    fee_schedule_sha256: str
    side: str
    price: Decimal
    qty: int
    notional_yuan: Decimal
    commission_yuan: Decimal
    stamp_tax_yuan: Decimal
    transfer_fee_yuan: Decimal
    other_fee_yuan: Decimal
    slippage_yuan: Decimal
    input_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "schedule_version", _text(self.schedule_version, "schedule_version"))
        object.__setattr__(
            self,
            "fee_schedule_sha256",
            _sha256_text(self.fee_schedule_sha256, "fee_schedule_sha256"),
        )
        object.__setattr__(self, "side", _side(self.side))
        object.__setattr__(self, "qty", _qty(self.qty, "qty"))
        object.__setattr__(self, "price", _decimal(self.price, "price", positive=self.qty > 0))
        object.__setattr__(self, "notional_yuan", _decimal(self.notional_yuan, "notional_yuan"))
        if self.notional_yuan != self.price * self.qty:
            raise ValueError("notional_yuan must equal price multiplied by qty")
        for name in (
            "commission_yuan",
            "stamp_tax_yuan",
            "transfer_fee_yuan",
            "other_fee_yuan",
            "slippage_yuan",
        ):
            amount = _decimal(getattr(self, name), name)
            if amount != _money(amount):
                raise ValueError(f"{name} must be rounded to Fen")
            object.__setattr__(self, name, amount)
        object.__setattr__(self, "input_sha256", _sha256_text(self.input_sha256, "input_sha256"))

    @property
    def total_yuan(self) -> Decimal:
        return _money(
            self.commission_yuan
            + self.stamp_tax_yuan
            + self.transfer_fee_yuan
            + self.other_fee_yuan
            + self.slippage_yuan
        )

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(_record_dict(self))

    def components_dict(self) -> dict[str, str]:
        return {
            "commission_yuan": f"{self.commission_yuan:.2f}",
            "stamp_tax_yuan": f"{self.stamp_tax_yuan:.2f}",
            "transfer_fee_yuan": f"{self.transfer_fee_yuan:.2f}",
            "other_fee_yuan": f"{self.other_fee_yuan:.2f}",
            "slippage_yuan": f"{self.slippage_yuan:.2f}",
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **_record_dict(self),
            "total_yuan": f"{self.total_yuan:.2f}",
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeeBreakdown:
        values = dict(value)
        expected_fields = {field.name for field in fields(cls)} | {
            "total_yuan",
            "content_sha256",
        }
        if set(values) != expected_fields:
            raise ValueError("fee breakdown fields do not match the signed contract")
        expected = _sha256_text(values.pop("content_sha256", None), "content_sha256")
        supplied_total = _decimal(values.pop("total_yuan"), "total_yuan")
        result = cls(**values)
        if supplied_total != result.total_yuan:
            raise ValueError("total_yuan does not match fee breakdown components")
        _verify_hash(expected, result.content_sha256, "fee breakdown")
        return result


@dataclass(frozen=True)
class RoundTripCost:
    buy: FeeBreakdown
    sell: FeeBreakdown

    def __post_init__(self) -> None:
        if not isinstance(self.buy, FeeBreakdown) or not isinstance(self.sell, FeeBreakdown):
            raise ValueError("round-trip sides must be fee breakdowns")
        if self.buy.side != "buy" or self.sell.side != "sell":
            raise ValueError("round-trip sides must be buy then sell")
        if (
            self.buy.schedule_version != self.sell.schedule_version
            or self.buy.fee_schedule_sha256 != self.sell.fee_schedule_sha256
            or self.buy.qty != self.sell.qty
        ):
            raise ValueError("round-trip fee schedule contract and quantity must match")

    @property
    def total_yuan(self) -> Decimal:
        return _money(self.buy.total_yuan + self.sell.total_yuan)

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self._content_dict())

    def _content_dict(self) -> dict[str, object]:
        return {
            "buy": self.buy.to_dict(),
            "sell": self.sell.to_dict(),
            "total_yuan": f"{self.total_yuan:.2f}",
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._content_dict(),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RoundTripCost:
        if set(value) != {"buy", "sell", "total_yuan", "content_sha256"}:
            raise ValueError("round-trip cost fields do not match the signed contract")
        expected = _sha256_text(value.get("content_sha256"), "content_sha256")
        supplied_total = _decimal(value.get("total_yuan"), "total_yuan")
        result = cls(
            buy=FeeBreakdown.from_dict(value["buy"]),
            sell=FeeBreakdown.from_dict(value["sell"]),
        )
        if supplied_total != result.total_yuan:
            raise ValueError("total_yuan does not match round-trip cost components")
        _verify_hash(expected, result.content_sha256, "round-trip cost")
        return result


def scenario_loss_yuan(
    entry_price: Decimal,
    exit_price: Decimal,
    qty: int,
    round_trip_cost: RoundTripCost,
) -> Decimal:
    normalized_entry = _decimal(entry_price, "entry_price", positive=True)
    normalized_exit = _decimal(exit_price, "exit_price", positive=True)
    normalized_qty = _qty(qty, "qty", positive=True)
    if normalized_exit >= normalized_entry:
        raise ValueError("exit_price must be below entry_price for a loss scenario")
    if not isinstance(round_trip_cost, RoundTripCost):
        raise ValueError("round_trip_cost must be a RoundTripCost")
    if round_trip_cost.buy.qty != normalized_qty:
        raise ValueError("round_trip_cost quantity does not match qty")
    if (
        round_trip_cost.buy.price != normalized_entry
        or round_trip_cost.sell.price != normalized_exit
    ):
        raise ValueError("round_trip_cost prices do not match the loss scenario")
    return _money(
        (normalized_entry - normalized_exit) * normalized_qty
        + round_trip_cost.total_yuan
    )


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    effective_from: str
    buy_commission_rate: Decimal
    sell_commission_rate: Decimal
    buy_minimum_commission_yuan: Decimal
    sell_minimum_commission_yuan: Decimal
    stamp_tax_rate: Decimal
    transfer_fee_rate: Decimal
    other_fee_rate: Decimal
    buy_slippage_rate: Decimal
    sell_slippage_rate: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _text(self.version, "version"))
        object.__setattr__(self, "effective_from", _date_text(self.effective_from, "effective_from"))
        for name in (
            "buy_commission_rate",
            "sell_commission_rate",
            "buy_minimum_commission_yuan",
            "sell_minimum_commission_yuan",
            "stamp_tax_rate",
            "transfer_fee_rate",
            "other_fee_rate",
            "buy_slippage_rate",
            "sell_slippage_rate",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))

    @classmethod
    def simulation(
        cls,
        *,
        version: str = "simulation-only-v1",
        effective_from: str = "2026-01-01",
        commission_rate: object = "0.0003",
        minimum_commission_yuan: object = "5",
        buy_minimum_commission_yuan: object | None = None,
        sell_minimum_commission_yuan: object | None = None,
        stamp_tax_rate: object = "0.0005",
        slippage_rate: object = "0.001",
        transfer_fee_rate: object = "0",
        other_fee_rate: object = "0",
    ) -> FeeSchedule:
        return cls(
            version=version,
            effective_from=effective_from,
            buy_commission_rate=commission_rate,
            sell_commission_rate=commission_rate,
            buy_minimum_commission_yuan=(
                minimum_commission_yuan
                if buy_minimum_commission_yuan is None
                else buy_minimum_commission_yuan
            ),
            sell_minimum_commission_yuan=(
                minimum_commission_yuan
                if sell_minimum_commission_yuan is None
                else sell_minimum_commission_yuan
            ),
            stamp_tax_rate=stamp_tax_rate,
            transfer_fee_rate=transfer_fee_rate,
            other_fee_rate=other_fee_rate,
            buy_slippage_rate=slippage_rate,
            sell_slippage_rate=slippage_rate,
        )

    def estimate(self, side: str, price: Decimal, qty: int) -> FeeBreakdown:
        normalized_side = _side(side)
        normalized_qty = _qty(qty, "qty")
        normalized_price = _decimal(price, "price", positive=normalized_qty > 0)
        notional = normalized_price * normalized_qty
        commission_rate = (
            self.buy_commission_rate if normalized_side == "buy" else self.sell_commission_rate
        )
        minimum_commission = (
            self.buy_minimum_commission_yuan
            if normalized_side == "buy"
            else self.sell_minimum_commission_yuan
        )
        commission_raw = max(minimum_commission, notional * commission_rate) if qty else ZERO
        stamp_raw = notional * self.stamp_tax_rate if normalized_side == "sell" else ZERO
        transfer_raw = notional * self.transfer_fee_rate
        other_raw = notional * self.other_fee_rate
        slippage_rate = self.buy_slippage_rate if normalized_side == "buy" else self.sell_slippage_rate
        slippage_raw = notional * slippage_rate
        raw_inputs = {
            "schedule": self._content_dict(),
            "side": normalized_side,
            "price": normalized_price,
            "qty": normalized_qty,
            "notional": notional,
            "commission": commission_raw,
            "stamp_tax": stamp_raw,
            "transfer_fee": transfer_raw,
            "other_fee": other_raw,
            "slippage": slippage_raw,
        }
        return FeeBreakdown(
            schedule_version=self.version,
            fee_schedule_sha256=self.contract_sha256,
            side=normalized_side,
            price=normalized_price,
            qty=normalized_qty,
            notional_yuan=notional,
            commission_yuan=_money(commission_raw),
            stamp_tax_yuan=_money(stamp_raw),
            transfer_fee_yuan=_money(transfer_raw),
            other_fee_yuan=_money(other_raw),
            slippage_yuan=_money(slippage_raw),
            input_sha256=canonical_sha256(raw_inputs),
        )

    def estimate_round_trip(self, entry_price: Decimal, exit_price: Decimal, qty: int) -> RoundTripCost:
        return RoundTripCost(self.estimate("buy", entry_price, qty), self.estimate("sell", exit_price, qty))

    def to_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "contract_sha256": self.contract_sha256}

    def _content_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @property
    def contract_sha256(self) -> str:
        return canonical_sha256(self._content_dict())

    def derive_variant(self, label: str, **changes: object) -> FeeSchedule:
        values = self.to_dict()
        values.update(changes)
        values.pop("version", None)
        values.pop("contract_sha256", None)
        suffix = canonical_sha256(values)[:12]
        return FeeSchedule(version=f"{self.version}-{_text(label, 'label')}-{suffix}", **values)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeeSchedule:
        values = dict(value)
        expected = _sha256_text(values.pop("contract_sha256", None), "contract_sha256")
        legacy_minimum = values.pop("minimum_commission_yuan", None)
        if legacy_minimum is not None:
            if "buy_minimum_commission_yuan" in values or "sell_minimum_commission_yuan" in values:
                raise ValueError("mixed legacy and current fee schedule schemas")
            values["buy_minimum_commission_yuan"] = legacy_minimum
            values["sell_minimum_commission_yuan"] = legacy_minimum
        result = cls(**values)
        _verify_hash(expected, result.contract_sha256, "fee schedule")
        return result


@dataclass(frozen=True)
class InstrumentRules:
    code: str
    exchange: str
    board: str
    security_type: str
    buy_min_qty: int
    buy_qty_step: int
    odd_lot_sell_allowed: bool
    price_tick: Decimal
    limit_up_price: Decimal | None
    limit_down_price: Decimal | None
    suspended: bool
    special_status: str
    source: str
    as_of: str
    valid_until: str
    rules_sha256: str = ""

    def __post_init__(self) -> None:
        supplied_hash = self.rules_sha256
        for name in ("code", "exchange", "board", "security_type", "source"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if type(self.special_status) is not str:
            raise ValueError("special_status must be text")
        object.__setattr__(self, "special_status", _text(self.special_status, "special_status"))
        object.__setattr__(self, "buy_min_qty", _qty(self.buy_min_qty, "buy_min_qty", positive=True))
        object.__setattr__(self, "buy_qty_step", _qty(self.buy_qty_step, "buy_qty_step", positive=True))
        object.__setattr__(
            self, "odd_lot_sell_allowed", _boolean(self.odd_lot_sell_allowed, "odd_lot_sell_allowed")
        )
        object.__setattr__(self, "price_tick", _decimal(self.price_tick, "price_tick", positive=True))
        for name in ("limit_up_price", "limit_down_price"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True, optional=True))
        object.__setattr__(self, "suspended", _boolean(self.suspended, "suspended"))
        object.__setattr__(self, "as_of", _timestamp(self.as_of, "as_of"))
        object.__setattr__(self, "valid_until", _timestamp(self.valid_until, "valid_until"))
        if datetime.fromisoformat(self.valid_until) < datetime.fromisoformat(self.as_of):
            raise ValueError("valid_until must not precede as_of")
        content = _record_dict(self)
        content.pop("rules_sha256")
        actual = canonical_sha256(content)
        _verify_hash(supplied_hash, actual, "instrument rules")
        object.__setattr__(self, "rules_sha256", actual)

    @classmethod
    def a_share(
        cls,
        code: str,
        *,
        buy_min_qty: int = 100,
        buy_qty_step: int = 100,
        odd_lot_sell_allowed: bool = True,
        price_tick: Decimal = Decimal("0.01"),
        exchange: str = "simulation",
        board: str = "a_share",
        source: str = "simulation-static-a-share-v1",
        as_of: str = "1970-01-01T00:00:00+00:00",
        valid_until: str = "9999-12-31T23:59:59+00:00",
        limit_up_price: Decimal | None = None,
        limit_down_price: Decimal | None = None,
        suspended: bool = False,
        special_status: str = "normal",
    ) -> InstrumentRules:
        return cls(
            code=code,
            exchange=exchange,
            board=board,
            security_type="stock",
            buy_min_qty=buy_min_qty,
            buy_qty_step=buy_qty_step,
            odd_lot_sell_allowed=odd_lot_sell_allowed,
            price_tick=price_tick,
            limit_up_price=limit_up_price,
            limit_down_price=limit_down_price,
            suspended=suspended,
            special_status=special_status,
            source=source,
            as_of=as_of,
            valid_until=valid_until,
        )

    def validate_order(self, side: str, qty: int, price: Decimal) -> tuple[str, ...]:
        try:
            normalized_side = _side(side)
        except ValueError:
            return ("SIDE_INVALID",)
        try:
            normalized_qty = _qty(qty, "qty")
        except ValueError:
            return ("QTY_INVALID",)
        try:
            normalized_price = _decimal(price, "price", positive=True)
        except ValueError:
            return ("PRICE_INVALID",)
        reasons: list[str] = []
        if normalized_side == "buy":
            if normalized_qty < self.buy_min_qty:
                reasons.append("BUY_QTY_BELOW_MIN")
            elif normalized_qty % self.buy_qty_step:
                reasons.append("BUY_QTY_STEP_INVALID")
        elif normalized_qty == 0:
            reasons.append("SELL_QTY_ZERO")
        elif not self.odd_lot_sell_allowed and normalized_qty % self.buy_qty_step:
            reasons.append("SELL_QTY_STEP_INVALID")
        if normalized_price % self.price_tick:
            reasons.append("PRICE_TICK_INVALID")
        if self.suspended:
            reasons.append("INSTRUMENT_SUSPENDED")
        if self.limit_up_price is not None and normalized_price > self.limit_up_price:
            reasons.append("PRICE_ABOVE_LIMIT")
        if self.limit_down_price is not None and normalized_price < self.limit_down_price:
            reasons.append("PRICE_BELOW_LIMIT")
        return tuple(reasons)

    def is_fresh(self, at: str) -> bool:
        value = datetime.fromisoformat(_timestamp(at, "at").replace("Z", "+00:00"))
        return datetime.fromisoformat(self.as_of) <= value <= datetime.fromisoformat(self.valid_until)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InstrumentRules:
        values = dict(value)
        values["rules_sha256"] = _sha256_text(values.get("rules_sha256"), "rules_sha256")
        return cls(**values)


@dataclass(frozen=True)
class QuoteSnapshot:
    snapshot_id: str
    code: str
    quote_time: str
    last_price: Decimal
    bid_price: Decimal | None
    ask_price: Decimal | None
    limit_up_price: Decimal | None
    limit_down_price: Decimal | None
    suspended: bool = False
    quote_sha256: str = ""

    def __post_init__(self) -> None:
        supplied_hash = self.quote_sha256
        for name in ("snapshot_id", "code"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "quote_time", _timestamp(self.quote_time, "quote_time"))
        object.__setattr__(self, "last_price", _decimal(self.last_price, "last_price", positive=True))
        for name in ("bid_price", "ask_price", "limit_up_price", "limit_down_price"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True, optional=True))
        object.__setattr__(self, "suspended", _boolean(self.suspended, "suspended"))
        content = _record_dict(self)
        content.pop("quote_sha256")
        actual = canonical_sha256(content)
        _verify_hash(supplied_hash, actual, "quote snapshot")
        object.__setattr__(self, "quote_sha256", actual)

    @classmethod
    def from_values(cls, *, snapshot_id: str | None = None, **values: object) -> QuoteSnapshot:
        if snapshot_id is None:
            normalized = cls(snapshot_id="generated", **values).to_dict()
            normalized.pop("snapshot_id")
            normalized.pop("quote_sha256")
            snapshot_id = f"quote-{canonical_sha256(normalized)[:20]}"
        return cls(snapshot_id=snapshot_id, **values)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> QuoteSnapshot:
        values = dict(value)
        values["quote_sha256"] = _sha256_text(values.get("quote_sha256"), "quote_sha256")
        return cls(**values)


@dataclass(frozen=True)
class BrokerPosition:
    code: str
    total_qty: int
    sellable_qty: int
    frozen_qty: int = 0
    today_buy_qty: int = 0
    average_cost: Decimal = ZERO
    last_price: Decimal = ZERO
    market_value: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _text(self.code, "code"))
        for name in ("total_qty", "sellable_qty", "frozen_qty", "today_buy_qty"):
            object.__setattr__(self, name, _qty(getattr(self, name), name))
        if self.sellable_qty + self.frozen_qty > self.total_qty or self.today_buy_qty > self.total_qty:
            raise ValueError("broker position quantities exceed total_qty")
        for name in ("average_cost", "last_price", "market_value"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))

    @classmethod
    def from_values(cls, **values: object) -> BrokerPosition:
        if "market_value" not in values:
            values["market_value"] = Decimal(str(values.get("last_price", 0))) * int(values["total_qty"])
        return cls(**values)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BrokerPosition:
        return cls(**dict(value))


@dataclass(frozen=True)
class BrokerSnapshot:
    snapshot_id: str
    account_scope_id: str
    trade_date: str
    broker_time: str
    generated_at: str
    total_equity: Decimal
    cash: Decimal
    available_cash: Decimal
    frozen_cash: Decimal
    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[object, ...]
    fills: tuple[object, ...]
    adapter_version: str
    node_version: str
    session_id: str
    capabilities_version: str
    intraday_pnl: Decimal = ZERO
    account_drawdown_pct: Decimal = ZERO
    snapshot_sha256: str = ""

    def __post_init__(self) -> None:
        supplied_hash = self.snapshot_sha256
        for name in (
            "snapshot_id",
            "account_scope_id",
            "adapter_version",
            "node_version",
            "session_id",
            "capabilities_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "trade_date", _date_text(self.trade_date, "trade_date"))
        object.__setattr__(self, "broker_time", _timestamp(self.broker_time, "broker_time"))
        object.__setattr__(self, "generated_at", _timestamp(self.generated_at, "generated_at"))
        for name in (
            "total_equity",
            "cash",
            "available_cash",
            "frozen_cash",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        object.__setattr__(
            self,
            "account_drawdown_pct",
            _decimal(self.account_drawdown_pct, "account_drawdown_pct", signed=True),
        )
        intraday = self.intraday_pnl if isinstance(self.intraday_pnl, Decimal) else Decimal(str(self.intraday_pnl))
        if not intraday.is_finite():
            raise ValueError("intraday_pnl must be finite")
        object.__setattr__(self, "intraday_pnl", intraday)
        if self.available_cash + self.frozen_cash > self.cash:
            raise ValueError("available_cash plus frozen_cash exceeds cash")
        positions = tuple(
            item if isinstance(item, BrokerPosition) else BrokerPosition.from_dict(item)
            for item in self.positions
        )
        if len({item.code for item in positions}) != len(positions):
            raise ValueError("duplicate broker position code")
        positions = tuple(sorted(positions, key=lambda item: item.code))
        object.__setattr__(self, "positions", positions)
        open_orders = tuple(
            sorted(
                (_normalize_open_order(item) for item in self.open_orders),
                key=lambda item: (item["client_order_id"], item["broker_order_id"] or ""),
            )
        )
        fills = tuple(
            sorted(
                (_normalize_fill(item) for item in self.fills),
                key=lambda item: item["broker_fill_id"],
            )
        )
        if len({item["client_order_id"] for item in open_orders}) != len(open_orders):
            raise ValueError("duplicate broker open-order client_order_id")
        if len({item["broker_fill_id"] for item in fills}) != len(fills):
            raise ValueError("duplicate broker fill ID")
        object.__setattr__(self, "open_orders", open_orders)
        object.__setattr__(self, "fills", fills)
        content = _record_dict(self)
        content.pop("snapshot_sha256")
        actual = canonical_sha256(content)
        _verify_hash(supplied_hash, actual, "broker snapshot")
        object.__setattr__(self, "snapshot_sha256", actual)

    @classmethod
    def from_values(
        cls,
        *,
        snapshot_id: str | None = None,
        generated_at: str | None = None,
        **values: object,
    ) -> BrokerSnapshot:
        generated_at = generated_at or str(values["broker_time"])
        if snapshot_id is None:
            normalized = cls(
                snapshot_id="generated", generated_at=generated_at, **values
            ).to_dict()
            normalized.pop("snapshot_id")
            normalized.pop("snapshot_sha256")
            snapshot_id = f"broker-{canonical_sha256(normalized)[:20]}"
            normalized["snapshot_id"] = snapshot_id
            return cls(**normalized)
        return cls(snapshot_id=snapshot_id, generated_at=generated_at, **values)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BrokerSnapshot:
        values = dict(value)
        values["snapshot_sha256"] = _sha256_text(
            values.get("snapshot_sha256"), "snapshot_sha256"
        )
        values["positions"] = tuple(BrokerPosition.from_dict(item) for item in values.get("positions", ()))
        return cls(**values)


@dataclass(frozen=True)
class StrategyOrderCandidate:
    candidate_id: str
    logical_signal_id: str
    account_scope_id: str
    source_signal_id: str
    source_run_id: str
    strategy_id: str
    strategy_version: str
    parameter_version: str
    model_version: str
    fee_schedule_version: str
    code: str
    side: str
    setup_type: str
    suggested_entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    signal_time: str
    frozen_valid_until: str
    industry: str
    theme: str
    uncategorized: bool
    buy_gap_price: Decimal | None
    buy_price_cap: Decimal | None
    requested_target_position_qty: int | None
    exit_owner_id: str | None
    exit_action: str | None
    exit_priority: int | None
    sell_limit_price: Decimal | None
    sell_price_floor: Decimal | None
    payload_sha256: str = ""

    def __post_init__(self) -> None:
        supplied_hash = self.payload_sha256
        for name in (
            "candidate_id",
            "logical_signal_id",
            "account_scope_id",
            "source_signal_id",
            "source_run_id",
            "strategy_id",
            "strategy_version",
            "parameter_version",
            "model_version",
            "fee_schedule_version",
            "code",
            "setup_type",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "side", _side(self.side))
        object.__setattr__(self, "industry", _classification(self.industry, "industry"))
        object.__setattr__(self, "theme", _classification(self.theme, "theme"))
        object.__setattr__(self, "uncategorized", _boolean(self.uncategorized, "uncategorized"))
        expected_uncategorized = UNCATEGORIZED in {self.industry, self.theme}
        if self.uncategorized != expected_uncategorized:
            raise ValueError("uncategorized must match normalized industry/theme")
        object.__setattr__(
            self, "suggested_entry_price", _decimal(self.suggested_entry_price, "suggested_entry_price", positive=True)
        )
        for name in ("stop_price", "target_price"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name in (
            "buy_gap_price",
            "buy_price_cap",
            "sell_limit_price",
            "sell_price_floor",
        ):
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), name, positive=True, optional=True),
            )
        if self.requested_target_position_qty is not None:
            object.__setattr__(
                self,
                "requested_target_position_qty",
                _qty(self.requested_target_position_qty, "requested_target_position_qty"),
            )
        object.__setattr__(
            self, "exit_owner_id", _optional_text(self.exit_owner_id, "exit_owner_id")
        )
        object.__setattr__(
            self, "exit_action", _optional_text(self.exit_action, "exit_action")
        )
        if self.exit_priority is not None:
            object.__setattr__(
                self, "exit_priority", _qty(self.exit_priority, "exit_priority")
            )
        if self.side == "buy" and not ZERO < self.stop_price < self.suggested_entry_price:
            raise ValueError("buy stop_price must be positive and below suggested_entry_price")
        if self.side == "buy":
            if self.buy_gap_price is None:
                raise ValueError("buy_gap_price is required for a buy candidate")
            if self.buy_price_cap is None:
                raise ValueError("buy_price_cap is required for a buy candidate")
            if not self.stop_price < self.buy_price_cap:
                raise ValueError("buy stop_price must be below buy_price_cap")
            if not self.buy_gap_price < self.buy_price_cap:
                raise ValueError("buy_gap_price must be below buy_price_cap")
            if not self.target_price > self.buy_price_cap:
                raise ValueError("buy target_price must be above buy_price_cap")
            if any(
                value is not None
                for value in (
                    self.requested_target_position_qty,
                    self.exit_owner_id,
                    self.exit_action,
                    self.exit_priority,
                    self.sell_limit_price,
                    self.sell_price_floor,
                )
            ):
                raise ValueError("buy candidate contains side-inapplicable sell fields")
        else:
            if self.buy_gap_price is not None or self.buy_price_cap is not None:
                raise ValueError("sell candidate contains side-inapplicable buy fields")
            if self.requested_target_position_qty is None:
                raise ValueError("requested_target_position_qty is required for a sell candidate")
            if self.exit_owner_id is None:
                raise ValueError("exit_owner_id is required for a sell candidate")
            if self.exit_action is None:
                raise ValueError("exit_action is required for a sell candidate")
            if self.exit_priority is None:
                raise ValueError("exit_priority is required for a sell candidate")
            if self.sell_limit_price is None:
                raise ValueError("sell_limit_price is required for a sell candidate")
            if (
                self.sell_price_floor is not None
                and self.sell_price_floor > self.sell_limit_price
            ):
                raise ValueError("sell_price_floor must not exceed sell_limit_price")
        object.__setattr__(self, "signal_time", _timestamp(self.signal_time, "signal_time"))
        object.__setattr__(
            self, "frozen_valid_until", _timestamp(self.frozen_valid_until, "frozen_valid_until")
        )
        if datetime.fromisoformat(self.frozen_valid_until) < datetime.fromisoformat(self.signal_time):
            raise ValueError("frozen_valid_until must not precede signal_time")
        content = _record_dict(self)
        content.pop("payload_sha256")
        actual = canonical_sha256(content)
        _verify_hash(supplied_hash, actual, "strategy candidate")
        object.__setattr__(self, "payload_sha256", actual)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StrategyOrderCandidate:
        values = dict(value)
        values["payload_sha256"] = _sha256_text(
            values.get("payload_sha256"), "payload_sha256"
        )
        return cls(**values)


@dataclass(frozen=True)
class PreTradeResult:
    pre_trade_result_id: str
    candidate_id: str
    candidate: StrategyOrderCandidate
    allowed: bool
    hard_blocks: tuple[str, ...]
    warnings: tuple[str, ...]
    approved_qty: int
    target_position_qty: int
    fee_schedule_version: str
    fee_schedule_sha256: str
    fee_evidence_status: str
    rule_evidence_status: str
    checked_at: str
    valid_until: str
    projected_available_cash_yuan: Decimal
    projected_single_position_value_yuan: Decimal
    projected_total_position_value_yuan: Decimal
    projected_industry_value_yuan: Decimal
    projected_theme_value_yuan: Decimal
    projected_uncategorized_value_yuan: Decimal
    projected_open_risk_yuan: Decimal
    actual_trade_risk_fraction: Decimal
    per_trade_risk_yuan: Decimal = ZERO
    approved_limit_price: Decimal | None = None
    approved_price_cap: Decimal | None = None
    submission_attempt_id: str = "not-applicable"
    execution_fee: FeeBreakdown | None = None
    round_trip_cost: RoundTripCost | None = None
    target_round_trip_cost: RoundTripCost | None = None
    planned_stop_loss_yuan: Decimal | None = None
    gap_price: Decimal | None = None
    gap_round_trip_cost: RoundTripCost | None = None
    gap_loss_yuan: Decimal | None = None
    fee_erosion_ratio: Decimal | None = None
    cost_to_expected_edge_ratio: Decimal | None = None
    broker_snapshot_id: str = "not-applicable"
    broker_snapshot_sha256: str = "not-applicable"
    quote_snapshot_id: str = "not-applicable"
    quote_snapshot_sha256: str = "not-applicable"
    instrument_rules_sha256: str = "not-applicable"
    strategy_version: str = "not-applicable"
    result_sha256: str = ""

    def __post_init__(self) -> None:
        supplied_hash = self.result_sha256
        for name in (
            "pre_trade_result_id",
            "candidate_id",
            "broker_snapshot_id",
            "quote_snapshot_id",
            "strategy_version",
            "submission_attempt_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "fee_evidence_status",
            _evidence_status(self.fee_evidence_status, "fee_evidence_status"),
        )
        object.__setattr__(
            self,
            "rule_evidence_status",
            _evidence_status(self.rule_evidence_status, "rule_evidence_status"),
        )
        fee_schedule_version = _text(self.fee_schedule_version, "fee_schedule_version")
        if self.fee_evidence_status == "unavailable":
            if fee_schedule_version.lower() != "not-applicable":
                raise ValueError(
                    "fee_schedule_version must be not-applicable when fee evidence is unavailable"
                )
            fee_schedule_version = "not-applicable"
        object.__setattr__(self, "fee_schedule_version", fee_schedule_version)
        object.__setattr__(
            self,
            "fee_schedule_sha256",
            _evidence_sha256(
                self.fee_schedule_sha256,
                "fee_schedule_sha256",
                self.fee_evidence_status,
            ),
        )
        for name in ("broker_snapshot_sha256", "quote_snapshot_sha256"):
            value = _text(getattr(self, name), name)
            object.__setattr__(
                self,
                name,
                "not-applicable" if value.lower() == "not-applicable" else _sha256_text(value, name),
            )
        object.__setattr__(
            self,
            "instrument_rules_sha256",
            _evidence_sha256(
                self.instrument_rules_sha256,
                "instrument_rules_sha256",
                self.rule_evidence_status,
            ),
        )
        object.__setattr__(self, "allowed", _boolean(self.allowed, "allowed"))
        candidate = (
            self.candidate
            if isinstance(self.candidate, StrategyOrderCandidate)
            else StrategyOrderCandidate.from_dict(self.candidate)
        )
        if candidate.candidate_id != self.candidate_id:
            raise ValueError("candidate_id does not match normalized candidate")
        if (
            self.fee_evidence_status == "available"
            and candidate.fee_schedule_version != self.fee_schedule_version
        ):
            raise ValueError("fee_schedule_version does not match normalized candidate")
        if candidate.strategy_version != self.strategy_version:
            raise ValueError("strategy_version does not match normalized candidate")
        object.__setattr__(self, "candidate", candidate)
        object.__setattr__(self, "hard_blocks", tuple(_text(item, "hard block") for item in self.hard_blocks))
        object.__setattr__(self, "warnings", tuple(_text(item, "warning") for item in self.warnings))
        if self.allowed and self.hard_blocks:
            raise ValueError("allowed result cannot contain hard blocks")
        for name in ("approved_qty", "target_position_qty"):
            object.__setattr__(self, name, _qty(getattr(self, name), name))
        for name in (
            "projected_available_cash_yuan",
            "projected_single_position_value_yuan",
            "projected_total_position_value_yuan",
            "projected_industry_value_yuan",
            "projected_theme_value_yuan",
            "projected_uncategorized_value_yuan",
            "projected_open_risk_yuan",
            "per_trade_risk_yuan",
            "actual_trade_risk_fraction",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.actual_trade_risk_fraction > Decimal("1"):
            raise ValueError("actual_trade_risk_fraction must not exceed 1")
        for name in (
            "projected_single_position_value_yuan",
            "projected_industry_value_yuan",
            "projected_theme_value_yuan",
            "projected_uncategorized_value_yuan",
        ):
            if getattr(self, name) > self.projected_total_position_value_yuan:
                raise ValueError(f"{name} must not exceed projected_total_position_value_yuan")
        for name in (
            "planned_stop_loss_yuan",
            "gap_loss_yuan",
            "fee_erosion_ratio",
            "cost_to_expected_edge_ratio",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, optional=True))
        for name in ("approved_limit_price", "approved_price_cap"):
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), name, positive=True, optional=True),
            )
        object.__setattr__(
            self,
            "gap_price",
            _decimal(self.gap_price, "gap_price", positive=True, optional=True),
        )
        object.__setattr__(self, "checked_at", _timestamp(self.checked_at, "checked_at"))
        object.__setattr__(self, "valid_until", _timestamp(self.valid_until, "valid_until"))
        checked_at = datetime.fromisoformat(self.checked_at)
        valid_until = datetime.fromisoformat(self.valid_until)
        signal_time = datetime.fromisoformat(candidate.signal_time)
        frozen_valid_until = datetime.fromisoformat(candidate.frozen_valid_until)
        if valid_until < checked_at:
            raise ValueError("valid_until must not precede checked_at")
        if checked_at < signal_time:
            raise ValueError("checked_at must not precede candidate signal_time")
        if self.allowed and (checked_at > frozen_valid_until or valid_until > frozen_valid_until):
            raise ValueError("allowed result must remain within candidate frozen_valid_until")
        if not self.allowed and valid_until != checked_at:
            raise ValueError("rejected result valid_until must equal checked_at")
        execution_fee = self.execution_fee
        if execution_fee is not None and not isinstance(execution_fee, FeeBreakdown):
            raise ValueError("execution_fee must be a FeeBreakdown")
        if self.fee_evidence_status == "unavailable" and execution_fee is not None:
            raise ValueError("execution_fee must be absent when fee evidence is unavailable")
        if execution_fee is not None and (
            execution_fee.schedule_version != self.fee_schedule_version
            or execution_fee.fee_schedule_sha256 != self.fee_schedule_sha256
            or execution_fee.side != candidate.side
            or execution_fee.qty != self.approved_qty
        ):
            raise ValueError("execution_fee side, fee schedule contract or quantity mismatch")
        if self.allowed:
            if candidate.side == "buy" and (
                self.fee_evidence_status != "available"
                or self.rule_evidence_status != "available"
            ):
                raise ValueError("allowed buy requires available fee and rule evidence")
            if candidate.side == "sell":
                required_warnings = {
                    "fee_evidence_status": "SELL_FEE_EVIDENCE_UNAVAILABLE",
                    "rule_evidence_status": "SELL_RULE_EVIDENCE_UNAVAILABLE",
                }
                for status_name, warning in required_warnings.items():
                    unavailable = getattr(self, status_name) == "unavailable"
                    if (warning in self.warnings) != unavailable:
                        raise ValueError(f"{warning} must match {status_name}")
            if self.broker_snapshot_id.lower() == "not-applicable":
                raise ValueError("allowed result requires broker_snapshot_id")
            if self.broker_snapshot_sha256 == "not-applicable":
                raise ValueError("allowed result requires broker_snapshot_sha256")
            if self.quote_snapshot_id.lower() == "not-applicable":
                raise ValueError("allowed result requires quote_snapshot_id")
            if self.quote_snapshot_sha256 == "not-applicable":
                raise ValueError("allowed result requires quote_snapshot_sha256")
            if (
                self.rule_evidence_status == "available"
                and self.instrument_rules_sha256 == "not-applicable"
            ):
                raise ValueError("allowed result requires instrument_rules_sha256")
            if self.approved_qty <= 0:
                raise ValueError("allowed result requires positive approved_qty")
            if self.submission_attempt_id.lower() == "not-applicable":
                raise ValueError("allowed result requires submission_attempt_id")
            if self.fee_evidence_status == "available" and execution_fee is None:
                raise ValueError("allowed result requires execution_fee")
            if self.approved_limit_price is None and self.approved_price_cap is None:
                raise ValueError("allowed result requires approved price protection")
            if candidate.side == "sell" and self.approved_limit_price is None:
                raise ValueError("allowed sell result requires approved_limit_price")
            if (
                candidate.side == "buy"
                and self.approved_price_cap != candidate.buy_price_cap
            ):
                raise ValueError("approved_price_cap does not match buy candidate")
            if (
                candidate.side == "sell"
                and self.approved_limit_price != candidate.sell_limit_price
            ):
                raise ValueError("approved_limit_price does not match sell candidate")
            if (
                candidate.side == "sell"
                and self.approved_price_cap != candidate.sell_price_floor
            ):
                raise ValueError("approved_price_cap does not match sell candidate")
            if self.approved_limit_price is not None and self.approved_price_cap is not None:
                if candidate.side == "buy" and self.approved_limit_price > self.approved_price_cap:
                    raise ValueError("approved buy limit price must not exceed price cap")
                if candidate.side == "sell" and self.approved_limit_price < self.approved_price_cap:
                    raise ValueError("approved sell limit price must not be below price cap")
            if (
                execution_fee is not None
                and self.approved_limit_price is not None
                and execution_fee.price != self.approved_limit_price
            ):
                raise ValueError("execution_fee price does not match approved_limit_price")
            if execution_fee is not None and self.approved_price_cap is not None:
                if (
                    candidate.side == "buy"
                    and self.approved_limit_price is None
                    and execution_fee.price != self.approved_price_cap
                ):
                    raise ValueError("execution_fee price must match cap-only approved_price_cap")
                if candidate.side == "buy" and execution_fee.price > self.approved_price_cap:
                    raise ValueError("execution_fee price exceeds approved_price_cap")
                if candidate.side == "sell" and execution_fee.price < self.approved_price_cap:
                    raise ValueError("execution_fee price is below approved_price_cap")
            if candidate.side == "buy" and self.target_position_qty <= 0:
                raise ValueError("allowed buy result requires positive target_position_qty")
            if candidate.side == "buy" and self.round_trip_cost is None:
                raise ValueError("allowed buy result requires round_trip_cost")
            if candidate.side == "buy":
                for name in (
                    "planned_stop_loss_yuan",
                    "target_round_trip_cost",
                    "gap_price",
                    "gap_round_trip_cost",
                    "gap_loss_yuan",
                    "fee_erosion_ratio",
                    "cost_to_expected_edge_ratio",
                ):
                    if getattr(self, name) is None:
                        raise ValueError(f"allowed buy result requires {name}")
                if self.gap_price != candidate.buy_gap_price:
                    raise ValueError("gap_price does not match buy candidate")
            if candidate.side == "buy" and self.actual_trade_risk_fraction <= ZERO:
                raise ValueError("allowed buy requires positive actual_trade_risk_fraction")
            if candidate.side == "buy":
                classification_projections = (
                    (
                        candidate.industry != UNCATEGORIZED,
                        "projected_industry_value_yuan",
                    ),
                    (
                        candidate.theme != UNCATEGORIZED,
                        "projected_theme_value_yuan",
                    ),
                    (candidate.uncategorized, "projected_uncategorized_value_yuan"),
                )
                for applies, name in classification_projections:
                    if applies and getattr(self, name) < self.projected_single_position_value_yuan:
                        raise ValueError(
                            f"{name} must include projected_single_position_value_yuan"
                        )
                if self.projected_open_risk_yuan < self.per_trade_risk_yuan:
                    raise ValueError(
                        "projected_open_risk_yuan must include per_trade_risk_yuan"
                    )
            if candidate.side == "sell" and self.actual_trade_risk_fraction != ZERO:
                raise ValueError("allowed sell actual_trade_risk_fraction must be zero")
        else:
            if self.approved_qty != 0:
                raise ValueError("rejected result approved_qty must be zero")
            if self.submission_attempt_id.lower() != "not-applicable":
                raise ValueError("rejected result submission_attempt_id must be not-applicable")
            if self.approved_limit_price is not None or self.approved_price_cap is not None:
                raise ValueError("rejected result cannot carry approved price protection")
            absent_fields = (
                "execution_fee",
                "round_trip_cost",
                "target_round_trip_cost",
                "planned_stop_loss_yuan",
                "gap_price",
                "gap_round_trip_cost",
                "gap_loss_yuan",
                "fee_erosion_ratio",
                "cost_to_expected_edge_ratio",
            )
            for name in absent_fields:
                if getattr(self, name) is not None:
                    raise ValueError(f"rejected result cannot carry {name}")
            if self.per_trade_risk_yuan != ZERO:
                raise ValueError("rejected result per_trade_risk_yuan must be zero")
            if self.actual_trade_risk_fraction != ZERO:
                raise ValueError("rejected result actual_trade_risk_fraction must be zero")
        if self.round_trip_cost is not None:
            if not isinstance(self.round_trip_cost, RoundTripCost):
                raise ValueError("round_trip_cost must be a RoundTripCost")
            if (
                self.round_trip_cost.buy.schedule_version != self.fee_schedule_version
                or self.round_trip_cost.buy.fee_schedule_sha256 != self.fee_schedule_sha256
                or self.round_trip_cost.buy.qty != self.approved_qty
            ):
                raise ValueError("round_trip_cost fee schedule contract or quantity mismatch")
            applicable_fee = (
                self.round_trip_cost.buy
                if candidate.side == "buy"
                else self.round_trip_cost.sell
            )
            if self.allowed and execution_fee != applicable_fee:
                raise ValueError("execution_fee does not match the applicable round_trip_cost fee")
            if candidate.side == "buy" and self.round_trip_cost.sell.price != candidate.stop_price:
                raise ValueError("round_trip_cost sell price must equal candidate stop_price")
        if self.target_round_trip_cost is not None:
            if not isinstance(self.target_round_trip_cost, RoundTripCost):
                raise ValueError("target_round_trip_cost must be a RoundTripCost")
            if (
                self.target_round_trip_cost.buy.schedule_version != self.fee_schedule_version
                or self.target_round_trip_cost.buy.fee_schedule_sha256 != self.fee_schedule_sha256
                or self.target_round_trip_cost.buy.qty != self.approved_qty
            ):
                raise ValueError("target_round_trip_cost fee schedule contract or quantity mismatch")
            if self.target_round_trip_cost.sell.price != candidate.target_price:
                raise ValueError("target_round_trip_cost sell price must equal candidate target_price")
        if self.gap_round_trip_cost is not None:
            if not isinstance(self.gap_round_trip_cost, RoundTripCost):
                raise ValueError("gap_round_trip_cost must be a RoundTripCost")
            if (
                self.gap_round_trip_cost.buy.schedule_version != self.fee_schedule_version
                or self.gap_round_trip_cost.buy.fee_schedule_sha256 != self.fee_schedule_sha256
                or self.gap_round_trip_cost.buy.qty != self.approved_qty
            ):
                raise ValueError("gap_round_trip_cost fee schedule contract or quantity mismatch")
            if self.gap_price is None:
                raise ValueError("gap_round_trip_cost requires gap_price")
            if self.gap_round_trip_cost.sell.price != self.gap_price:
                raise ValueError("gap_round_trip_cost sell price must equal gap_price")
        if self.allowed and candidate.side == "buy":
            if self.gap_price >= execution_fee.price:
                raise ValueError("gap_price must be below the approved execution price")
            if self.target_round_trip_cost.buy != execution_fee:
                raise ValueError("target_round_trip_cost buy fee must match execution_fee")
            if self.gap_round_trip_cost.buy != execution_fee:
                raise ValueError("gap_round_trip_cost buy fee must match execution_fee")
            planned_loss = scenario_loss_yuan(
                execution_fee.price,
                candidate.stop_price,
                self.approved_qty,
                self.round_trip_cost,
            )
            if self.planned_stop_loss_yuan != planned_loss:
                raise ValueError("planned_stop_loss_yuan does not match frozen price and fee evidence")
            gap_loss = scenario_loss_yuan(
                execution_fee.price,
                self.gap_price,
                self.approved_qty,
                self.gap_round_trip_cost,
            )
            if self.gap_loss_yuan != gap_loss:
                raise ValueError("gap_loss_yuan does not match frozen price and fee evidence")
            if self.per_trade_risk_yuan != max(planned_loss, gap_loss):
                raise ValueError(
                    "per_trade_risk_yuan must equal the larger planned_stop_loss_yuan or gap_loss_yuan"
                )

            expected_fee_erosion = _ratio(
                self.target_round_trip_cost.total_yuan,
                execution_fee.notional_yuan,
                "fee_erosion_ratio",
            )
            expected_edge_yuan = (
                candidate.target_price - execution_fee.price
            ) * self.approved_qty
            expected_cost_to_edge = _ratio(
                self.target_round_trip_cost.total_yuan,
                expected_edge_yuan,
                "cost_to_expected_edge_ratio",
            )
            if self.fee_erosion_ratio != expected_fee_erosion:
                raise ValueError("fee_erosion_ratio does not match frozen fee and notional evidence")
            if self.cost_to_expected_edge_ratio != expected_cost_to_edge:
                raise ValueError(
                    "cost_to_expected_edge_ratio does not match frozen cost and expected edge evidence"
                )
        actual = canonical_sha256(self._content_dict())
        _verify_hash(supplied_hash, actual, "pre-trade result")
        object.__setattr__(self, "result_sha256", actual)

    def _content_dict(self) -> dict[str, object]:
        content = _record_dict(self)
        content.pop("result_sha256")
        if self.execution_fee is not None:
            content["execution_fee"] = self.execution_fee.to_dict()
        if self.round_trip_cost is not None:
            content["round_trip_cost"] = self.round_trip_cost.to_dict()
        if self.target_round_trip_cost is not None:
            content["target_round_trip_cost"] = self.target_round_trip_cost.to_dict()
        if self.gap_round_trip_cost is not None:
            content["gap_round_trip_cost"] = self.gap_round_trip_cost.to_dict()
        return content

    def to_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "result_sha256": self.result_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PreTradeResult:
        values = dict(value)
        if set(values) != {field.name for field in fields(cls)}:
            raise ValueError("pre-trade result fields do not match the signed contract")
        values["result_sha256"] = _sha256_text(
            values.get("result_sha256"), "result_sha256"
        )
        candidate = values.get("candidate")
        if isinstance(candidate, Mapping):
            values["candidate"] = StrategyOrderCandidate.from_dict(candidate)
        execution_fee = values.get("execution_fee")
        if isinstance(execution_fee, Mapping):
            values["execution_fee"] = FeeBreakdown.from_dict(execution_fee)
        cost = values.get("round_trip_cost")
        if isinstance(cost, Mapping):
            values["round_trip_cost"] = RoundTripCost.from_dict(cost)
        target_cost = values.get("target_round_trip_cost")
        if isinstance(target_cost, Mapping):
            values["target_round_trip_cost"] = RoundTripCost.from_dict(target_cost)
        gap_cost = values.get("gap_round_trip_cost")
        if isinstance(gap_cost, Mapping):
            values["gap_round_trip_cost"] = RoundTripCost.from_dict(gap_cost)
        return cls(**values)


@dataclass(frozen=True)
class ExecutionIntent:
    client_order_id: str
    pre_trade_result_id: str
    pre_trade_result: PreTradeResult
    pre_trade_result_sha256: str
    submission_attempt_id: str
    account_scope_id: str
    adapter: str
    logical_signal_id: str
    source_signal_id: str
    strategy_id: str
    strategy_version: str
    parameter_version: str
    model_version: str
    fee_schedule_version: str
    fee_schedule_sha256: str
    fee_evidence_status: str
    rule_evidence_status: str
    code: str
    side: str
    order_qty: int
    expected_current_qty: int
    target_position_qty: int
    limit_price: Decimal | None
    price_cap: Decimal | None
    stop_price: Decimal | None
    signal_time: str
    expires_at: str
    broker_snapshot_id: str
    broker_snapshot_sha256: str
    quote_snapshot_id: str
    quote_snapshot_sha256: str
    instrument_rules_sha256: str
    intent_sha256: str = ""

    def __post_init__(self) -> None:
        supplied_hash = self.intent_sha256
        for name in (
            "client_order_id",
            "pre_trade_result_id",
            "submission_attempt_id",
            "account_scope_id",
            "adapter",
            "logical_signal_id",
            "source_signal_id",
            "strategy_id",
            "strategy_version",
            "parameter_version",
            "model_version",
            "code",
            "broker_snapshot_id",
            "quote_snapshot_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "fee_evidence_status",
            _evidence_status(self.fee_evidence_status, "fee_evidence_status"),
        )
        object.__setattr__(
            self,
            "rule_evidence_status",
            _evidence_status(self.rule_evidence_status, "rule_evidence_status"),
        )
        fee_schedule_version = _text(self.fee_schedule_version, "fee_schedule_version")
        if self.fee_evidence_status == "unavailable":
            if fee_schedule_version.lower() != "not-applicable":
                raise ValueError(
                    "fee_schedule_version must be not-applicable when fee evidence is unavailable"
                )
            fee_schedule_version = "not-applicable"
        object.__setattr__(self, "fee_schedule_version", fee_schedule_version)
        for name in (
            "pre_trade_result_sha256",
            "broker_snapshot_sha256",
            "quote_snapshot_sha256",
        ):
            object.__setattr__(self, name, _sha256_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "fee_schedule_sha256",
            _evidence_sha256(
                self.fee_schedule_sha256,
                "fee_schedule_sha256",
                self.fee_evidence_status,
            ),
        )
        object.__setattr__(
            self,
            "instrument_rules_sha256",
            _evidence_sha256(
                self.instrument_rules_sha256,
                "instrument_rules_sha256",
                self.rule_evidence_status,
            ),
        )
        object.__setattr__(self, "side", _side(self.side))
        object.__setattr__(self, "order_qty", _qty(self.order_qty, "order_qty", positive=True))
        for name in ("expected_current_qty", "target_position_qty"):
            object.__setattr__(self, name, _qty(getattr(self, name), name))
        for name in ("limit_price", "price_cap", "stop_price"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True, optional=True))
        object.__setattr__(self, "signal_time", _timestamp(self.signal_time, "signal_time"))
        object.__setattr__(self, "expires_at", _timestamp(self.expires_at, "expires_at"))
        signal_time = datetime.fromisoformat(self.signal_time)
        expires_at = datetime.fromisoformat(self.expires_at)
        if expires_at < signal_time:
            raise ValueError("expires_at must not precede signal_time")
        if self.limit_price is None and self.price_cap is None:
            raise ValueError("price protection requires limit_price or price_cap")
        if self.side == "sell" and self.limit_price is None:
            raise ValueError("sell price protection requires limit_price")
        if self.side == "buy" and self.stop_price is None:
            raise ValueError("buy requires stop_price")
        if self.limit_price is not None and self.price_cap is not None:
            if self.side == "buy" and self.limit_price > self.price_cap:
                raise ValueError("buy limit_price must not exceed price_cap")
            if self.side == "sell" and self.limit_price < self.price_cap:
                raise ValueError("sell limit_price must not be below price_cap")
        if self.side == "buy":
            if self.target_position_qty != self.expected_current_qty + self.order_qty:
                raise ValueError("buy target_position_qty must equal current quantity plus order_qty")
        else:
            if self.order_qty > self.expected_current_qty:
                raise ValueError("sell order_qty cannot oversell expected_current_qty")
            if self.target_position_qty != self.expected_current_qty - self.order_qty:
                raise ValueError("sell target_position_qty must equal current quantity minus order_qty")
        result = (
            self.pre_trade_result
            if isinstance(self.pre_trade_result, PreTradeResult)
            else PreTradeResult.from_dict(self.pre_trade_result)
        )
        object.__setattr__(self, "pre_trade_result", result)
        if not result.allowed:
            raise ValueError("execution intent requires an allowed pre_trade_result")
        for name, actual_value, expected_value in (
            ("pre_trade_result_id", self.pre_trade_result_id, result.pre_trade_result_id),
            ("pre_trade_result_sha256", self.pre_trade_result_sha256, result.result_sha256),
            ("submission_attempt_id", self.submission_attempt_id, result.submission_attempt_id),
            ("fee_evidence_status", self.fee_evidence_status, result.fee_evidence_status),
            ("rule_evidence_status", self.rule_evidence_status, result.rule_evidence_status),
            ("fee_schedule_version", self.fee_schedule_version, result.fee_schedule_version),
            ("fee_schedule_sha256", self.fee_schedule_sha256, result.fee_schedule_sha256),
            ("broker_snapshot_id", self.broker_snapshot_id, result.broker_snapshot_id),
            ("broker_snapshot_sha256", self.broker_snapshot_sha256, result.broker_snapshot_sha256),
            ("quote_snapshot_id", self.quote_snapshot_id, result.quote_snapshot_id),
            ("quote_snapshot_sha256", self.quote_snapshot_sha256, result.quote_snapshot_sha256),
            ("instrument_rules_sha256", self.instrument_rules_sha256, result.instrument_rules_sha256),
            ("limit_price", self.limit_price, result.approved_limit_price),
            ("price_cap", self.price_cap, result.approved_price_cap),
            ("order_qty", self.order_qty, result.approved_qty),
            ("target_position_qty", self.target_position_qty, result.target_position_qty),
        ):
            if actual_value != expected_value:
                raise ValueError(f"{name} does not match pre_trade_result")
        candidate = result.candidate
        for name in (
            "account_scope_id",
            "logical_signal_id",
            "source_signal_id",
            "strategy_id",
            "strategy_version",
            "parameter_version",
            "model_version",
            "code",
            "side",
            "signal_time",
        ):
            if getattr(self, name) != getattr(candidate, name):
                raise ValueError(f"{name} does not match pre_trade_result candidate")
        if self.stop_price is not None and self.stop_price != candidate.stop_price:
            raise ValueError("stop_price does not match pre_trade_result candidate")
        if expires_at < datetime.fromisoformat(result.checked_at):
            raise ValueError("expires_at must not precede pre_trade_result checked_at")
        if expires_at != datetime.fromisoformat(result.valid_until):
            raise ValueError("expires_at must equal pre_trade_result valid_until")
        if expires_at > datetime.fromisoformat(candidate.frozen_valid_until):
            raise ValueError("expires_at exceeds candidate frozen_valid_until")
        exact_order = {
            "code": self.code,
            "side": self.side,
            "order_qty": self.order_qty,
            "expected_current_qty": self.expected_current_qty,
            "target_position_qty": self.target_position_qty,
            "limit_price": self.limit_price,
            "price_cap": self.price_cap,
            "stop_price": self.stop_price,
            "expires_at": self.expires_at,
        }
        expected_client_order_id = client_order_id(
            self.account_scope_id,
            self.adapter,
            self.logical_signal_id,
            self.pre_trade_result_id,
            exact_order,
            self.submission_attempt_id,
        )
        if self.client_order_id != expected_client_order_id:
            raise ValueError("client_order_id does not match the canonical execution order")
        actual = canonical_sha256(self._content_dict())
        _verify_hash(supplied_hash, actual, "execution intent")
        object.__setattr__(self, "intent_sha256", actual)

    def _content_dict(self) -> dict[str, object]:
        content = _record_dict(self)
        content.pop("intent_sha256")
        content["pre_trade_result"] = self.pre_trade_result.to_dict()
        return content

    def to_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "intent_sha256": self.intent_sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExecutionIntent:
        values = dict(value)
        if set(values) != {field.name for field in fields(cls)}:
            raise ValueError("execution intent fields do not match the signed contract")
        values["intent_sha256"] = _sha256_text(
            values.get("intent_sha256"), "intent_sha256"
        )
        result = values.get("pre_trade_result")
        if isinstance(result, Mapping):
            values["pre_trade_result"] = PreTradeResult.from_dict(result)
        return cls(**values)
