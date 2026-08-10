"""Exact, portable order economics and 100-share sizing gates."""

from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP


ECONOMICS_VERSION = "2026-08-10.1"
ECONOMIC_MAX_ROUND_TRIP_RATE = Decimal("0.004")
ECONOMIC_MIN_TARGET_COST_MULTIPLE = Decimal("3")


class StrategyEconomicsError(ValueError):
    """Stable fail-closed economic calculation error."""


def _econ_decimal(value, name, allow_zero=True):
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise StrategyEconomicsError("INVALID_DECIMAL: " + name)
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise StrategyEconomicsError("INVALID_DECIMAL: " + name)
    return result


def _econ_money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def economic_fee_total(side, price, qty, schedule):
    price_value = _econ_decimal(price, "price", allow_zero=False)
    if isinstance(qty, bool) or int(qty) <= 0:
        raise StrategyEconomicsError("INVALID_QTY")
    quantity = Decimal(int(qty))
    notional = price_value * quantity
    commission_rate = _econ_decimal(
        schedule["buy_commission_rate" if side == "buy" else "sell_commission_rate"],
        "commission_rate",
    )
    minimum = _econ_decimal(
        schedule[
            "buy_minimum_commission_yuan"
            if side == "buy" else "sell_minimum_commission_yuan"
        ],
        "minimum_commission",
    )
    commission = max(minimum, notional * commission_rate)
    stamp = (
        notional * _econ_decimal(schedule.get("stamp_tax_rate", 0), "stamp_tax_rate")
        if side == "sell" else Decimal("0")
    )
    transfer = notional * _econ_decimal(
        schedule.get("transfer_fee_rate", 0), "transfer_fee_rate"
    )
    other = notional * _econ_decimal(schedule.get("other_fee_rate", 0), "other_fee_rate")
    slippage = notional * _econ_decimal(
        schedule.get(
            "buy_slippage_rate" if side == "buy" else "sell_slippage_rate", 0
        ),
        "slippage_rate",
    )
    return sum(
        (
            _econ_money(commission),
            _econ_money(stamp),
            _econ_money(transfer),
            _econ_money(other),
            _econ_money(slippage),
        ),
        Decimal("0"),
    )


def estimate_round_trip_economics(entry_price, exit_price, qty, schedule):
    entry = _econ_decimal(entry_price, "entry_price", allow_zero=False)
    exit_value = _econ_decimal(exit_price, "exit_price", allow_zero=False)
    quantity = int(qty)
    buy_fee = economic_fee_total("buy", entry, quantity, schedule)
    sell_fee = economic_fee_total("sell", exit_value, quantity, schedule)
    cost = buy_fee + sell_fee
    entry_notional = entry * quantity
    gross = (exit_value - entry) * quantity
    return {
        "version": ECONOMICS_VERSION,
        "qty": quantity,
        "entry_notional_yuan": float(_econ_money(entry_notional)),
        "buy_cost_yuan": float(_econ_money(buy_fee)),
        "sell_cost_yuan": float(_econ_money(sell_fee)),
        "round_trip_cost_yuan": float(_econ_money(cost)),
        "round_trip_cost_rate": float(cost / entry_notional),
        "gross_profit_yuan": float(_econ_money(gross)),
        "net_profit_yuan": float(_econ_money(gross - cost)),
    }


def size_strategy_order(
    entry_price,
    stop_loss,
    take_profit,
    equity,
    available_cash,
    risk_budget_pct,
    position_cap_pct,
    schedule,
    lot_size=100,
    current_position_pct=0.0,
    max_total_position_pct=95.0,
    max_round_trip_rate=ECONOMIC_MAX_ROUND_TRIP_RATE,
    min_target_cost_multiple=ECONOMIC_MIN_TARGET_COST_MULTIPLE,
):
    """Size risk -> caps -> cash -> board-lot floor -> economic gate."""
    entry = _econ_decimal(entry_price, "entry_price", allow_zero=False)
    stop = _econ_decimal(stop_loss, "stop_loss", allow_zero=False)
    target = _econ_decimal(take_profit, "take_profit", allow_zero=False)
    account = _econ_decimal(equity, "equity", allow_zero=False)
    cash = _econ_decimal(available_cash, "available_cash")
    risk_budget = account * _econ_decimal(risk_budget_pct, "risk_budget_pct") / Decimal("100")
    position_cap = account * _econ_decimal(position_cap_pct, "position_cap_pct") / Decimal("100")
    remaining_total = account * max(
        Decimal("0"),
        _econ_decimal(max_total_position_pct, "max_total_position_pct")
        - _econ_decimal(current_position_pct, "current_position_pct"),
    ) / Decimal("100")
    lot = int(lot_size)
    if lot <= 0 or stop >= entry or target <= entry:
        return {
            "allowed": False,
            "reason": "factor_invalid_price_plan",
            "target_qty": 0,
            "version": ECONOMICS_VERSION,
        }
    stop_loss_per_share = entry - stop
    risk_qty = int((risk_budget / stop_loss_per_share).to_integral_value(rounding=ROUND_DOWN))
    cap_qty = int((min(position_cap, remaining_total) / entry).to_integral_value(rounding=ROUND_DOWN))
    cash_qty = int((cash / entry).to_integral_value(rounding=ROUND_DOWN))
    raw_qty = min(risk_qty, cap_qty, cash_qty)
    qty = raw_qty // lot * lot
    if qty < lot:
        return {
            "allowed": False,
            "reason": "buy_too_small_for_board_lot",
            "target_qty": 0,
            "raw_qty": raw_qty,
            "version": ECONOMICS_VERSION,
        }
    # Fees consume cash too.  Reduce by complete lots only; never round up.
    while qty >= lot:
        buy_fee = economic_fee_total("buy", entry, qty, schedule)
        if entry * qty + buy_fee <= cash:
            break
        qty -= lot
    if qty < lot:
        return {
            "allowed": False,
            "reason": "buy_insufficient_available_cash",
            "target_qty": 0,
            "version": ECONOMICS_VERSION,
        }
    stop_economics = estimate_round_trip_economics(entry, stop, qty, schedule)
    target_economics = estimate_round_trip_economics(entry, target, qty, schedule)
    while (
        qty >= lot
        and Decimal(str(abs(stop_economics["net_profit_yuan"]))) > risk_budget
    ):
        qty -= lot
        if qty < lot:
            break
        stop_economics = estimate_round_trip_economics(entry, stop, qty, schedule)
        target_economics = estimate_round_trip_economics(
            entry, target, qty, schedule
        )
    if qty < lot:
        return {
            "allowed": False,
            "reason": "factor_risk_budget_exceeded_after_cost",
            "target_qty": 0,
            "raw_qty": raw_qty,
            "version": ECONOMICS_VERSION,
        }
    maximum_rate = _econ_decimal(max_round_trip_rate, "max_round_trip_rate")
    minimum_multiple = _econ_decimal(
        min_target_cost_multiple, "min_target_cost_multiple"
    )
    if Decimal(str(target_economics["round_trip_cost_rate"])) > maximum_rate:
        reason = "factor_round_trip_cost_rate_high"
    elif Decimal(str(target_economics["gross_profit_yuan"])) <= (
        Decimal(str(target_economics["round_trip_cost_yuan"])) * minimum_multiple
    ):
        reason = "factor_target_edge_insufficient"
    else:
        reason = ""
    return {
        "allowed": not bool(reason),
        "reason": reason,
        "target_qty": qty if not reason else 0,
        "raw_qty": raw_qty,
        "position_pct": float(entry * qty / account * Decimal("100")),
        "planned_risk_yuan": float(
            _econ_money(Decimal(str(abs(stop_economics["net_profit_yuan"]))))
        ),
        "risk_budget_yuan": float(_econ_money(risk_budget)),
        "stop_economics": stop_economics,
        "target_economics": target_economics,
        "version": ECONOMICS_VERSION,
    }
