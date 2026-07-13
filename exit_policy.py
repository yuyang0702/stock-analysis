from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionExitState:
    code: str
    mode: str
    initial_qty: int
    current_qty: int
    entry_price: float
    initial_stop_price: float
    highest_price: float
    atr14: float
    take_profit_stage: int
    holding_trade_days: int


@dataclass(frozen=True)
class ExitDecision:
    action: str
    target_qty: int | None
    initial_stop_price: float
    trailing_stop_price: float
    r_multiple: float
    reason: str


_BOARD_LIMITS = {
    "main_low": (1.8, 0.06),
    "main_active": (2.0, 0.07),
    "growth": (2.5, 0.09),
}
_BOARD_RISK_BUDGET = {"main_low": 0.65, "main_active": 0.5, "growth": 0.4}


def market_regime(value: str) -> str:
    text = str(value or "").strip()
    if text in {"RISK_OFF", "风险释放"}:
        return "RISK_OFF"
    if text in {"CAUTION", "弱势震荡"}:
        return "CAUTION"
    return "NORMAL"


def initial_stop_price(
    entry_price: float,
    support_price: float,
    atr14: float,
    board: str,
) -> float:
    if entry_price <= 0:
        return 0.0
    atr_mult, max_loss_pct = _BOARD_LIMITS.get(board, _BOARD_LIMITS["main_active"])
    candidates = []
    if support_price > 0:
        candidates.append(support_price * 0.99)
    if atr14 > 0:
        candidates.append(entry_price - atr_mult * atr14)
    technical_stop = min(candidates) if candidates else entry_price * (1 - max_loss_pct)
    stop = max(technical_stop, entry_price * (1 - max_loss_pct))
    return round(min(stop, entry_price - 0.01), 2)


def board_type(code: str, entry_price: float, atr14: float) -> str:
    if str(code).startswith(("300", "301", "688")):
        return "growth"
    if entry_price > 0 and atr14 / entry_price <= 0.02:
        return "main_low"
    return "main_active"


def risk_position_pct(
    entry_price: float,
    stop_price: float,
    board: str,
    original_cap_pct: float,
    market_state: str,
) -> float:
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price or market_state == "RISK_OFF":
        return 0.0
    stop_distance_pct = (entry_price - stop_price) / entry_price * 100
    risk_budget_pct = _BOARD_RISK_BUDGET.get(board, 0.5)
    position_pct = min(original_cap_pct, risk_budget_pct / stop_distance_pct * 100)
    if market_state == "CAUTION":
        position_pct *= 0.5
    return round(max(position_pct, 0.0), 2)


def evaluate_exit(
    state: PositionExitState,
    current_price: float,
    market_state: str,
) -> ExitDecision:
    risk = max(state.entry_price - state.initial_stop_price, 0.0)
    r_multiple = (current_price - state.entry_price) / risk if risk > 0 else 0.0
    trail_mult = 2.0 if state.mode == "short" else 3.0
    if market_state == "RISK_OFF":
        trail_mult = max(1.5, trail_mult - 0.5)
    trailing_stop = max(
        state.initial_stop_price,
        state.highest_price - trail_mult * state.atr14 if state.atr14 > 0 else state.initial_stop_price,
    )
    trailing_stop = round(trailing_stop, 2)

    def decision(action: str, target_qty: int | None, reason: str) -> ExitDecision:
        return ExitDecision(
            action=action,
            target_qty=target_qty,
            initial_stop_price=state.initial_stop_price,
            trailing_stop_price=trailing_stop,
            r_multiple=round(r_multiple, 2),
            reason=reason,
        )

    if current_price <= state.initial_stop_price:
        return decision("hard_stop", 0, "现价触及冻结初始止损")
    if state.take_profit_stage >= 1 and current_price <= trailing_stop:
        return decision("trailing_stop", 0, "首段止盈后触及移动止盈")
    if state.take_profit_stage == 0 and r_multiple >= 2:
        target_qty = state.initial_qty // 2 // 100 * 100
        return decision("take_profit_1", target_qty, "达到2R，目标降至初始持仓一半")

    stop_days = 3 if state.mode == "short" else 10
    required_progress = 0.5 if state.mode == "short" else 1.0
    if state.holding_trade_days >= stop_days and r_multiple < required_progress:
        return decision("time_stop", 0, "持仓交易日达到上限且价格进展不足")
    return decision("hold", None, "继续持有")
