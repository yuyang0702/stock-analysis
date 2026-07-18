from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


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


@dataclass(frozen=True)
class GapReentryDecision:
    state: str
    reason: str
    allowed: bool = False
    cap_price: float = 0.0
    confirmation_count: int = 0
    attempt_count: int = 0


@dataclass(frozen=True)
class MinimumLotDecision:
    allowed: bool
    reason: str
    qty: int = 0
    position_pct: float = 0.0
    risk_pct: float = 0.0


def reentry_cap_price(entry: float, stop: float) -> float:
    risk = float(entry) - float(stop)
    return float(entry) + 0.5 * risk if entry > 0 and risk > 0 else 0.0


def estimated_limit_up_price(code: str, previous_close: float) -> float:
    digits = "".join(filter(str.isdigit, str(code)))[:6]
    if previous_close <= 0 or len(digits) != 6:
        return 0.0
    limit_pct = 0.30 if digits.startswith(("4", "8")) else (
        0.20 if digits.startswith(("300", "301", "688", "689")) else 0.10
    )
    return round(previous_close * (1 + limit_pct) + 1e-9, 2)


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
    risk_budget_pct: float,
    current_position_pct: float,
    max_total_position_pct: float,
    max_single_position_pct: float = 100.0,
    cost_buffer_rate: float = 0.001,
) -> MinimumLotDecision:
    cost = float(entry_price) * 100
    if min(entry_price, stop_price, account_value) <= 0 or stop_price >= entry_price:
        return MinimumLotDecision(False, "gap_reentry_rr_invalid")
    if available_cash < cost * (1 + cost_buffer_rate):
        return MinimumLotDecision(False, "gap_reentry_insufficient_cash")
    position_pct = cost / account_value * 100
    if position_pct > max_single_position_pct:
        return MinimumLotDecision(False, "gap_reentry_min_lot_risk_exceeded")
    if current_position_pct + position_pct > max_total_position_pct:
        return MinimumLotDecision(False, "buy_total_position_limit")
    risk_pct = ((entry_price - stop_price) * 100 + cost * cost_buffer_rate) / account_value * 100
    if risk_pct > risk_budget_pct:
        return MinimumLotDecision(False, "gap_reentry_min_lot_risk_exceeded")
    return MinimumLotDecision(True, "", 100, position_pct, risk_pct)
