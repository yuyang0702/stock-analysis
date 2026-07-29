from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from functools import wraps

from execution_contracts import FeeSchedule, InstrumentRules, scenario_loss_yuan


DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_UP)


def _fixed_decimal_context(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext(DECIMAL_CONTEXT):
            return function(*args, **kwargs)

    return wrapped


@dataclass(frozen=True)
class GapReentryInput:
    trade_date: str
    code: str
    parent_signal_id: str
    batch_id: str
    now: str
    price: float
    limit_up_price: float
    original_entry_price: float
    original_stop_price: float
    market_state: str
    current_score: float
    required_score: float
    quote_age_sec: float
    first_open_at: str = ""
    first_open_price: float = 0.0
    first_batch_id: str = ""
    confirmation_count: int = 0
    attempt_count: int = 0
    at_limit: bool = False
    resealed: bool = False
    buy_enabled: bool = True
    kill_switch: bool = False
    health_allowed: bool = True
    reconciliation_allowed: bool = True
    has_position: bool = False
    has_pending_order: bool = False

    def __post_init__(self) -> None:
        for name in (
            "trade_date", "code", "parent_signal_id", "batch_id", "now",
            "market_state", "first_open_at", "first_batch_id",
        ):
            if type(getattr(self, name)) is not str:
                raise ValueError(f"{name} must be text")
        if self.market_state not in {"NORMAL", "CAUTION", "RISK_OFF"}:
            raise ValueError("market_state must be a known normalized regime")
        for name in (
            "price", "limit_up_price", "original_entry_price",
            "original_stop_price", "current_score", "required_score",
            "quote_age_sec", "first_open_price",
        ):
            normalized = float(_risk_amount(getattr(self, name), name))
            if not math.isfinite(normalized):
                raise ValueError(f"{name} is outside the supported float range")
            object.__setattr__(self, name, normalized)
        for name in ("confirmation_count", "attempt_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "at_limit", "resealed", "buy_enabled", "kill_switch",
            "health_allowed", "reconciliation_allowed", "has_position",
            "has_pending_order",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        datetime.fromisoformat(self.trade_date)
        datetime.fromisoformat(self.now)
        if self.first_open_at:
            datetime.fromisoformat(self.first_open_at)


@dataclass(frozen=True)
class GapReentryDecision:
    state: str
    reason: str
    allowed: bool = False
    cap_price: float = 0.0
    confirmation_count: int = 0
    attempt_count: int = 0


@dataclass(frozen=True)
class MinimumLotRiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    per_trade_risk_yuan: Decimal
    remaining_open_risk_yuan: Decimal
    lot_loss_yuan: Decimal

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool or type(self.reasons) is not tuple:
            raise ValueError("minimum-lot risk decision shape is invalid")
        for name in (
            "per_trade_risk_yuan", "remaining_open_risk_yuan", "lot_loss_yuan",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        expected = tuple(
            reason
            for exceeded, reason in (
                (self.lot_loss_yuan > self.per_trade_risk_yuan, "PER_TRADE_RISK_EXCEEDED"),
                (
                    self.lot_loss_yuan > self.remaining_open_risk_yuan,
                    "PORTFOLIO_OPEN_RISK_EXCEEDED",
                ),
            )
            if exceeded
        )
        if self.reasons != expected or self.allowed != (not expected):
            raise ValueError("minimum-lot risk decision does not match its budgets")

@dataclass(frozen=True)
class MinimumLotDecision:
    allowed: bool
    reason: str
    qty: int
    position_pct: Decimal
    risk_pct: Decimal
    risk_reasons: tuple[str, ...]
    cash_required_yuan: Decimal
    entry_price: Decimal
    stop_price: Decimal
    account_value_yuan: Decimal
    available_cash_yuan: Decimal
    per_trade_risk_yuan: Decimal
    remaining_open_risk_yuan: Decimal
    current_position_pct: Decimal
    max_total_position_pct: Decimal
    max_single_position_pct: Decimal
    fee_schedule: FeeSchedule | None
    instrument_rules: InstrumentRules

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool or type(self.reason) is not str:
            raise ValueError("minimum-lot decision shape is invalid")
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty < 0:
            raise ValueError("qty must be a non-negative integer")
        if type(self.risk_reasons) is not tuple:
            raise ValueError("risk_reasons must be a tuple")
        for name in (
            "position_pct", "risk_pct", "cash_required_yuan", "entry_price",
            "stop_price", "account_value_yuan", "available_cash_yuan",
            "per_trade_risk_yuan", "remaining_open_risk_yuan",
            "current_position_pct", "max_total_position_pct",
            "max_single_position_pct",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        if self.fee_schedule is not None and not isinstance(self.fee_schedule, FeeSchedule):
            raise ValueError("fee_schedule must be FeeSchedule or None")
        if not isinstance(self.instrument_rules, InstrumentRules):
            raise ValueError("instrument_rules must be InstrumentRules")
        expected = _minimum_lot_outcome(
            self.entry_price,
            self.stop_price,
            self.account_value_yuan,
            self.available_cash_yuan,
            self.per_trade_risk_yuan,
            self.remaining_open_risk_yuan,
            self.current_position_pct,
            self.max_total_position_pct,
            self.max_single_position_pct,
            self.fee_schedule,
            self.instrument_rules,
        )
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise ValueError("minimum-lot decision does not match its frozen inputs")


def _risk_amount(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative Decimal")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative Decimal") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal")
    return amount


@_fixed_decimal_context
def _minimum_lot_outcome(
    entry: Decimal,
    stop: Decimal,
    equity: Decimal,
    cash: Decimal,
    per_trade_risk: Decimal,
    remaining_open_risk: Decimal,
    current_position: Decimal,
    total_limit: Decimal,
    single_limit: Decimal,
    fees: FeeSchedule | None,
    rules: InstrumentRules,
) -> dict[str, object]:
    empty = {
        "allowed": False,
        "qty": 0,
        "position_pct": Decimal("0"),
        "risk_pct": Decimal("0"),
        "risk_reasons": (),
        "cash_required_yuan": Decimal("0"),
    }
    if entry <= 0 or stop <= 0 or equity <= 0 or stop >= entry:
        return {**empty, "reason": "gap_reentry_rr_invalid"}
    if fees is None:
        return {**empty, "reason": "gap_reentry_fee_schedule_required"}
    minimum_qty = (
        (rules.buy_min_qty + rules.buy_qty_step - 1) // rules.buy_qty_step
        * rules.buy_qty_step
    )
    if rules.validate_order("buy", minimum_qty, entry):
        return {**empty, "reason": "buy_execution_plan_invalid"}

    costs = fees.estimate_round_trip(entry, stop, minimum_qty)
    position_value = entry * minimum_qty
    cash_required = position_value + costs.buy.total_yuan
    position_pct = position_value / equity * 100
    lot_loss = scenario_loss_yuan(entry, stop, minimum_qty, costs)
    risk_pct = lot_loss / equity * 100
    risk = evaluate_minimum_lot_risk(
        per_trade_risk,
        remaining_open_risk,
        lot_loss,
    )
    if cash < cash_required:
        reason = "gap_reentry_insufficient_cash"
    elif position_pct > single_limit:
        reason = "buy_single_position_limit"
    elif current_position + position_pct > total_limit:
        reason = "buy_total_position_limit"
    elif len(risk.reasons) == 2:
        reason = "gap_reentry_per_trade_and_portfolio_risk_exceeded"
    elif risk.reasons:
        reason = {
            "PER_TRADE_RISK_EXCEEDED": "gap_reentry_per_trade_risk_exceeded",
            "PORTFOLIO_OPEN_RISK_EXCEEDED": "gap_reentry_portfolio_open_risk_exceeded",
        }[risk.reasons[0]]
    else:
        reason = ""
    return {
        "allowed": not reason,
        "reason": reason,
        "qty": minimum_qty if not reason else 0,
        "position_pct": position_pct,
        "risk_pct": risk_pct,
        "risk_reasons": risk.reasons,
        "cash_required_yuan": cash_required,
    }


def evaluate_minimum_lot_risk(
    per_trade_risk_yuan: Decimal,
    remaining_open_risk_yuan: Decimal,
    lot_loss_yuan: Decimal,
) -> MinimumLotRiskDecision:
    per_trade = _risk_amount(per_trade_risk_yuan, "per_trade_risk_yuan")
    remaining = _risk_amount(remaining_open_risk_yuan, "remaining_open_risk_yuan")
    loss = _risk_amount(lot_loss_yuan, "lot_loss_yuan")
    reasons = tuple(
        reason
        for exceeded, reason in (
            (loss > per_trade, "PER_TRADE_RISK_EXCEEDED"),
            (loss > remaining, "PORTFOLIO_OPEN_RISK_EXCEEDED"),
        )
        if exceeded
    )
    return MinimumLotRiskDecision(not reasons, reasons, per_trade, remaining, loss)


def reentry_cap_price(entry: float, stop: float) -> float:
    entry_value = float(entry)
    stop_value = float(stop)
    if not math.isfinite(entry_value) or not math.isfinite(stop_value):
        return 0.0
    risk = entry_value - stop_value
    cap = entry_value + 0.5 * risk
    return cap if entry_value > 0 and risk > 0 and math.isfinite(cap) else 0.0


def estimated_limit_up_price(code: str, previous_close: float) -> float:
    digits = "".join(filter(str.isdigit, str(code)))[:6]
    close = float(previous_close)
    if not math.isfinite(close) or close <= 0 or len(digits) != 6:
        return 0.0
    limit_pct = 0.30 if digits.startswith(("4", "8")) else (
        0.20 if digits.startswith(("300", "301", "688", "689")) else 0.10
    )
    value = close * (1 + limit_pct)
    return round(value + 1e-9, 2) if math.isfinite(value) else 0.0


def effective_trading_minutes(start: str, end: str) -> int:
    left = datetime.fromisoformat(start)
    right = datetime.fromisoformat(end)
    if right <= left or left.date() != right.date():
        return 0
    sessions = ((time(9, 30), time(11, 30)), (time(13), time(15)))
    total = 0.0
    for session_start, session_end in sessions:
        lower = max(left, datetime.combine(left.date(), session_start))
        upper = min(right, datetime.combine(left.date(), session_end))
        total += max(0.0, (upper - lower).total_seconds())
    return int(total // 60)


def evaluate_gap_reentry(value: GapReentryInput) -> GapReentryDecision:
    cap = reentry_cap_price(value.original_entry_price, value.original_stop_price)
    result = lambda state, reason, **kwargs: GapReentryDecision(
        state, reason, cap_price=cap, confirmation_count=value.confirmation_count,
        attempt_count=value.attempt_count, **kwargs,
    )
    if not value.parent_signal_id or cap <= 0 or value.price <= 0 or value.limit_up_price <= 0:
        return result("INELIGIBLE", "gap_reentry_parent_invalid")
    if (
        value.market_state == "RISK_OFF" or not value.buy_enabled or value.kill_switch
        or not value.health_allowed or not value.reconciliation_allowed
    ):
        return result("RISK_REJECTED", "gap_reentry_current_risk_disallowed")
    if value.has_position or value.has_pending_order:
        return result("RISK_REJECTED", "gap_reentry_pending_order")
    if value.quote_age_sec > 120:
        return result("INELIGIBLE", "gap_reentry_quote_stale")
    if value.current_score < value.required_score:
        return result("INELIGIBLE", "gap_reentry_current_score_low")
    current_time = datetime.fromisoformat(value.now).time()
    if current_time >= time(14, 50) or (not value.first_open_at and current_time > time(14, 45)):
        return result("TOO_LATE", "gap_reentry_too_late")
    if value.resealed:
        return GapReentryDecision(
            "RESEALED", "gap_reentry_resealed", cap_price=cap,
            confirmation_count=0, attempt_count=value.attempt_count,
        )
    if value.at_limit or value.price >= value.limit_up_price:
        return result("LOCKED_LIMIT", "gap_reentry_locked_limit")
    if value.attempt_count > 2:
        return result("ATTEMPTS_EXHAUSTED", "gap_reentry_attempts_exhausted")
    if value.price > cap:
        return result("TOO_FAR", "gap_reentry_too_far")
    if not value.first_open_at:
        return GapReentryDecision(
            "OPEN_OBSERVING", "gap_reentry_open_observing", cap_price=cap,
            confirmation_count=1, attempt_count=max(1, value.attempt_count),
        )
    if value.first_open_price > 0 and value.price < value.first_open_price * 0.99:
        return result("FALLING", "gap_reentry_falling")
    if value.batch_id == value.first_batch_id or effective_trading_minutes(value.first_open_at, value.now) < 5:
        return result("OPEN_OBSERVING", "gap_reentry_open_observing")
    return GapReentryDecision(
        "OPEN_CONFIRMED", "", allowed=True, cap_price=cap,
        confirmation_count=2, attempt_count=value.attempt_count,
    )


def minimum_lot_position(
    entry_price: float,
    stop_price: float,
    account_value: float,
    available_cash: float,
    per_trade_risk_yuan: Decimal,
    remaining_open_risk_yuan: Decimal,
    current_position_pct: float,
    max_total_position_pct: float,
    rules: InstrumentRules,
    max_single_position_pct: float = 100.0,
    fees: FeeSchedule | None = None,
) -> MinimumLotDecision:
    entry = _risk_amount(entry_price, "entry_price")
    stop = _risk_amount(stop_price, "stop_price")
    equity = _risk_amount(account_value, "account_value")
    cash = _risk_amount(available_cash, "available_cash")
    per_trade_risk = _risk_amount(per_trade_risk_yuan, "per_trade_risk_yuan")
    remaining_open_risk = _risk_amount(
        remaining_open_risk_yuan, "remaining_open_risk_yuan",
    )
    position = _risk_amount(current_position_pct, "current_position_pct")
    total_limit = _risk_amount(max_total_position_pct, "max_total_position_pct")
    single_limit = _risk_amount(max_single_position_pct, "max_single_position_pct")
    if fees is not None and not isinstance(fees, FeeSchedule):
        raise ValueError("fees must be FeeSchedule or None")
    if not isinstance(rules, InstrumentRules):
        raise ValueError("rules must be InstrumentRules")
    outcome = _minimum_lot_outcome(
        entry,
        stop,
        equity,
        cash,
        per_trade_risk,
        remaining_open_risk,
        position,
        total_limit,
        single_limit,
        fees,
        rules,
    )
    return MinimumLotDecision(
        **outcome,
        entry_price=entry,
        stop_price=stop,
        account_value_yuan=equity,
        available_cash_yuan=cash,
        per_trade_risk_yuan=per_trade_risk,
        remaining_open_risk_yuan=remaining_open_risk,
        current_position_pct=position,
        max_total_position_pct=total_limit,
        max_single_position_pct=single_limit,
        fee_schedule=fees,
        instrument_rules=rules,
    )
