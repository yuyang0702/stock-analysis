"""Immutable, versioned contracts shared by sizing and execution paths."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import MappingProxyType
from typing import Mapping


CENT = Decimal("0.01")
ZERO = Decimal("0")


def _decimal(value: object, name: str, *, positive: bool = False, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} decimal")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


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
    return result


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
            "account_scope_id": account_scope_id,
            "adapter": adapter,
            "logical_signal_id": logical_signal_id,
            "pre_trade_result_id": pre_trade_result_id,
            "exact_order": exact_order,
            "submission_attempt_id": submission_attempt_id,
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


@dataclass(frozen=True)
class FeeBreakdown:
    schedule_version: str
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
        expected = values.pop("content_sha256", None)
        values.pop("total_yuan", None)
        result = cls(**values)
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
        if self.buy.schedule_version != self.sell.schedule_version or self.buy.qty != self.sell.qty:
            raise ValueError("round-trip fee schedule and quantity must match")

    @property
    def total_yuan(self) -> Decimal:
        return _money(self.buy.total_yuan + self.sell.total_yuan)

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "buy": self.buy.to_dict(),
            "sell": self.sell.to_dict(),
            "total_yuan": f"{self.total_yuan:.2f}",
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RoundTripCost:
        return cls(
            buy=FeeBreakdown.from_dict(value["buy"]),
            sell=FeeBreakdown.from_dict(value["sell"]),
        )


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    effective_from: str
    buy_commission_rate: Decimal
    sell_commission_rate: Decimal
    minimum_commission_yuan: Decimal
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
            "minimum_commission_yuan",
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
            minimum_commission_yuan=minimum_commission_yuan,
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
        commission_raw = max(self.minimum_commission_yuan, notional * commission_rate) if qty else ZERO
        stamp_raw = notional * self.stamp_tax_rate if normalized_side == "sell" else ZERO
        transfer_raw = notional * self.transfer_fee_rate
        other_raw = notional * self.other_fee_rate
        slippage_rate = self.buy_slippage_rate if normalized_side == "buy" else self.sell_slippage_rate
        slippage_raw = notional * slippage_rate
        raw_inputs = {
            "schedule": self.to_dict(),
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
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FeeSchedule:
        return cls(**dict(value))


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
        return cls(**dict(value))


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
            snapshot_id = f"quote-{canonical_sha256(values)[:20]}"
        return cls(snapshot_id=snapshot_id, **values)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> QuoteSnapshot:
        return cls(**dict(value))


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
            "account_drawdown_pct",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
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
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "open_orders", tuple(_freeze(item) for item in self.open_orders))
        object.__setattr__(self, "fills", tuple(_freeze(item) for item in self.fills))
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
            snapshot_id = f"broker-{canonical_sha256({**values, 'generated_at': generated_at})[:20]}"
        return cls(snapshot_id=snapshot_id, generated_at=generated_at, **values)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BrokerSnapshot:
        values = dict(value)
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
        object.__setattr__(
            self, "suggested_entry_price", _decimal(self.suggested_entry_price, "suggested_entry_price", positive=True)
        )
        for name in ("stop_price", "target_price"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
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
        return cls(**dict(value))


@dataclass(frozen=True)
class PreTradeResult:
    pre_trade_result_id: str
    candidate_id: str
    allowed: bool
    hard_blocks: tuple[str, ...]
    warnings: tuple[str, ...]
    approved_qty: int
    target_position_qty: int
    fee_schedule_version: str
    checked_at: str
    valid_until: str
    estimated_cash_yuan: Decimal = ZERO
    projected_position_value_yuan: Decimal = ZERO
    projected_industry_value_yuan: Decimal = ZERO
    projected_theme_value_yuan: Decimal = ZERO
    projected_open_risk_yuan: Decimal = ZERO
    per_trade_risk_yuan: Decimal = ZERO
    percentage_risk: Decimal = ZERO
    round_trip_cost: RoundTripCost | None = None
    broker_snapshot_id: str = "not-applicable"
    quote_snapshot_id: str = "not-applicable"
    instrument_rules_sha256: str = "not-applicable"
    strategy_version: str = "not-applicable"
    result_sha256: str = ""

    def __post_init__(self) -> None:
        supplied_hash = self.result_sha256
        for name in (
            "pre_trade_result_id",
            "candidate_id",
            "fee_schedule_version",
            "broker_snapshot_id",
            "quote_snapshot_id",
            "instrument_rules_sha256",
            "strategy_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "hard_blocks", tuple(_text(item, "hard block") for item in self.hard_blocks))
        object.__setattr__(self, "warnings", tuple(_text(item, "warning") for item in self.warnings))
        if self.allowed and self.hard_blocks:
            raise ValueError("allowed result cannot contain hard blocks")
        object.__setattr__(self, "allowed", _boolean(self.allowed, "allowed"))
        for name in ("approved_qty", "target_position_qty"):
            object.__setattr__(self, name, _qty(getattr(self, name), name))
        for name in (
            "estimated_cash_yuan",
            "projected_position_value_yuan",
            "projected_industry_value_yuan",
            "projected_theme_value_yuan",
            "projected_open_risk_yuan",
            "per_trade_risk_yuan",
            "percentage_risk",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        object.__setattr__(self, "checked_at", _timestamp(self.checked_at, "checked_at"))
        object.__setattr__(self, "valid_until", _timestamp(self.valid_until, "valid_until"))
        if datetime.fromisoformat(self.valid_until) < datetime.fromisoformat(self.checked_at):
            raise ValueError("valid_until must not precede checked_at")
        if self.round_trip_cost is not None:
            if not isinstance(self.round_trip_cost, RoundTripCost):
                raise ValueError("round_trip_cost must be a RoundTripCost")
            if self.round_trip_cost.buy.schedule_version != self.fee_schedule_version:
                raise ValueError("round_trip_cost fee schedule version mismatch")
        content = _record_dict(self)
        content.pop("result_sha256")
        actual = canonical_sha256(content)
        _verify_hash(supplied_hash, actual, "pre-trade result")
        object.__setattr__(self, "result_sha256", actual)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PreTradeResult:
        values = dict(value)
        cost = values.get("round_trip_cost")
        if isinstance(cost, Mapping):
            values["round_trip_cost"] = _round_trip_from_dict(cost)
        return cls(**values)


def _round_trip_from_dict(value: Mapping[str, object]) -> RoundTripCost:
    return RoundTripCost.from_dict(value)


@dataclass(frozen=True)
class ExecutionIntent:
    client_order_id: str
    pre_trade_result_id: str
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
            "fee_schedule_version",
            "code",
            "broker_snapshot_id",
            "broker_snapshot_sha256",
            "quote_snapshot_id",
            "quote_snapshot_sha256",
            "instrument_rules_sha256",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "side", _side(self.side))
        object.__setattr__(self, "order_qty", _qty(self.order_qty, "order_qty", positive=True))
        for name in ("expected_current_qty", "target_position_qty"):
            object.__setattr__(self, name, _qty(getattr(self, name), name))
        for name in ("limit_price", "price_cap", "stop_price"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True, optional=True))
        object.__setattr__(self, "signal_time", _timestamp(self.signal_time, "signal_time"))
        object.__setattr__(self, "expires_at", _timestamp(self.expires_at, "expires_at"))
        if datetime.fromisoformat(self.expires_at) < datetime.fromisoformat(self.signal_time):
            raise ValueError("expires_at must not precede signal_time")
        content = _record_dict(self)
        content.pop("intent_sha256")
        actual = canonical_sha256(content)
        _verify_hash(supplied_hash, actual, "execution intent")
        object.__setattr__(self, "intent_sha256", actual)

    def to_dict(self) -> dict[str, object]:
        return _record_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExecutionIntent:
        return cls(**dict(value))
