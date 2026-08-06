from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


EXECUTION_PLAN_VERSION = "2026-08-01.1-small-capital-live-risk"


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
    manual_stop_price: float = 0.0
    profit_protection_activated_at: str = ""
    trailing_stop_active_from: str = ""
    decision_batch_at: str = ""
    qty_step: int = 100


@dataclass(frozen=True)
class EffectiveStop:
    initial_stop_price: float
    manual_stop_price: float
    trailing_stop_price: float
    effective_stop_price: float
    source: str


@dataclass(frozen=True)
class ExitDecision:
    action: str
    target_qty: int | None
    initial_stop_price: float
    trailing_stop_price: float
    manual_stop_price: float
    effective_stop_price: float
    r_multiple: float
    reason: str


@dataclass(frozen=True)
class BuyExecutionPlan:
    version: str
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_per_share: float
    risk_reward: float
    position_pct: float
    board_type: str
    market_regime: str


_BOARD_LIMITS = {
    "main_low": (1.8, 0.06),
    "main_active": (2.0, 0.07),
    "growth": (2.5, 0.09),
}
_BOARD_RISK_BUDGET = {"main_low": 0.65, "main_active": 0.5, "growth": 0.4}


def first_take_profit_target_qty(initial_qty: int, qty_step: int) -> int:
    if (
        isinstance(initial_qty, bool)
        or isinstance(qty_step, bool)
        or not isinstance(initial_qty, int)
        or not isinstance(qty_step, int)
        or initial_qty <= 0
        or qty_step <= 0
    ):
        raise ValueError("initial_qty and qty_step must be positive integers")
    minimum_remaining = (initial_qty + 1) // 2
    aligned = ((minimum_remaining + qty_step - 1) // qty_step) * qty_step
    return min(initial_qty, aligned)


def _profit_protection_is_active(state: PositionExitState) -> bool:
    if not (
        state.profit_protection_activated_at
        and state.trailing_stop_active_from
        and state.decision_batch_at
    ):
        return False
    try:
        active_from = datetime.fromisoformat(
            state.trailing_stop_active_from.replace("Z", "+00:00")
        )
        decision_at = datetime.fromisoformat(
            state.decision_batch_at.replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False
    if (
        active_from.tzinfo is None
        or active_from.utcoffset() is None
        or decision_at.tzinfo is None
        or decision_at.utcoffset() is None
    ):
        return False
    return decision_at >= active_from


def normalize_exit_action(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = (
        ("hard_stop", ("hard_stop", "stop_loss", "硬止损", "止损")),
        ("market_risk_exit", ("market_risk", "市场风险")),
        ("trailing_stop", ("trailing_stop", "移动止盈")),
        ("take_profit_1", ("take_profit_1", "达到2r", "+2r", "首段止盈")),
        ("time_stop", ("time_stop", "时间止损", "持仓交易日达到上限")),
    )
    for action, tokens in aliases:
        if any(token in text for token in tokens):
            return action
    return text or "sell"


def exit_priority(reason_or_action: str) -> int:
    return {
        "hard_stop": 5,
        "market_risk_exit": 4,
        "trailing_stop": 3,
        "take_profit_1": 2,
        "time_stop": 1,
    }.get(normalize_exit_action(reason_or_action), 0)


def strict_market_regime(value: str) -> str:
    text = str(value or "").strip()
    if text in {"RISK_OFF", "风险释放"}:
        return "RISK_OFF"
    if text in {"CAUTION", "弱势震荡"}:
        return "CAUTION"
    if text in {"NORMAL", "强势进攻", "温和修复"}:
        return "NORMAL"
    return ""


def market_regime(value: str) -> str:
    return strict_market_regime(value) or "NORMAL"


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


def validated_initial_stop_price(
    code: str,
    fill_price: float,
    signal_stop_price: float,
    atr14: float = 0.0,
) -> float:
    """Tighten an entry stop against the actual fill; never loosen the signal stop."""
    if fill_price <= 0:
        return round(max(signal_stop_price, 0.0), 2)
    board = board_type(code, fill_price, atr14)
    max_loss_pct = _BOARD_LIMITS[board][1]
    fill_guard = fill_price * (1 - max_loss_pct)
    stop = max(signal_stop_price if signal_stop_price > 0 else 0.0, fill_guard)
    return round(min(stop, fill_price - 0.01), 2)


def resolve_effective_stop(state: PositionExitState, market_state: str) -> EffectiveStop:
    trailing_stop = 0.0
    if (
        (state.take_profit_stage >= 1 or _profit_protection_is_active(state))
        and state.atr14 > 0
    ):
        trail_mult = 2.0 if state.mode == "short" else 3.0
        if market_regime(market_state) == "RISK_OFF":
            trail_mult = max(1.5, trail_mult - 0.5)
        trailing_stop = round(max(
            state.initial_stop_price,
            state.highest_price - trail_mult * state.atr14,
        ), 2)
    candidates = [(state.initial_stop_price, "initial")]
    if state.manual_stop_price > 0:
        candidates.append((state.manual_stop_price, "manual"))
    if trailing_stop > 0:
        candidates.append((trailing_stop, "trailing"))
    effective, source = max(candidates, key=lambda item: item[0])
    return EffectiveStop(
        initial_stop_price=round(state.initial_stop_price, 2),
        manual_stop_price=round(max(state.manual_stop_price, 0.0), 2),
        trailing_stop_price=trailing_stop,
        effective_stop_price=round(effective, 2),
        source=source,
    )


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
    risk_budget_pct = trade_risk_budget_pct(board, market_state)
    position_pct = min(original_cap_pct, risk_budget_pct / stop_distance_pct * 100)
    return round(max(position_pct, 0.0), 2)


def trade_risk_budget_pct(board: str, market_state: str) -> float:
    regime = market_regime(market_state)
    if regime == "RISK_OFF":
        return 0.0
    budget = _BOARD_RISK_BUDGET.get(board, 0.5)
    return budget * 0.5 if regime == "CAUTION" else budget


def build_buy_execution_plan(
    *,
    code: str,
    entry_price: float,
    support_price: float,
    atr14: float,
    position_cap_pct: float,
    market_state: str,
) -> BuyExecutionPlan:
    regime = market_regime(market_state)
    board = board_type(code, entry_price, atr14)
    stop = initial_stop_price(entry_price, support_price, atr14, board)
    risk = round(max(entry_price - stop, 0.0), 2)
    take = round(entry_price + 2 * risk, 2) if risk > 0 else 0.0
    position = risk_position_pct(
        entry_price,
        stop,
        board,
        position_cap_pct,
        regime,
    )
    return BuyExecutionPlan(
        version=EXECUTION_PLAN_VERSION,
        entry_price=round(entry_price, 2),
        stop_loss=stop,
        take_profit=take,
        risk_per_share=risk,
        risk_reward=2.0 if risk > 0 else 0.0,
        position_pct=position,
        board_type=board,
        market_regime=regime,
    )


def evaluate_exit(
    state: PositionExitState,
    current_price: float,
    market_state: str,
) -> ExitDecision:
    risk = max(state.entry_price - state.initial_stop_price, 0.0)
    r_multiple = (current_price - state.entry_price) / risk if risk > 0 else 0.0
    resolved = resolve_effective_stop(state, market_state)

    def decision(action: str, target_qty: int | None, reason: str) -> ExitDecision:
        return ExitDecision(
            action=action,
            target_qty=target_qty,
            initial_stop_price=state.initial_stop_price,
            trailing_stop_price=resolved.trailing_stop_price,
            manual_stop_price=resolved.manual_stop_price,
            effective_stop_price=resolved.effective_stop_price,
            r_multiple=round(r_multiple, 2),
            reason=reason,
        )

    hard_stop = max(state.initial_stop_price, resolved.manual_stop_price)
    if current_price <= hard_stop:
        if resolved.manual_stop_price >= state.initial_stop_price:
            return decision("hard_stop", 0, "现价触及人工上调止损")
        return decision("hard_stop", 0, "现价触及成交校验后的冻结初始止损")
    if resolved.trailing_stop_price > 0 and current_price <= resolved.trailing_stop_price:
        return decision("trailing_stop", 0, "首段止盈后触及移动止盈")
    if state.take_profit_stage == 0 and r_multiple >= 2:
        target_qty = first_take_profit_target_qty(state.initial_qty, state.qty_step)
        if target_qty > state.current_qty:
            return decision("hold", None, "current quantity is already below the first target")
        if target_qty == state.current_qty:
            if not state.profit_protection_activated_at:
                return decision(
                    "activate_profit_protection", None,
                    "take_profit_1: 达到2R；一手持仓不卖出，激活盈利保护",
                )
            return decision("hold", None, "profit protection is already active")
        sell_qty = state.current_qty - target_qty
        sell_pct = sell_qty / state.current_qty * 100
        return decision(
            "take_profit_1",
            target_qty,
            f"take_profit_1: 达到2R；计划卖出{sell_qty}股"
            f"（占当前持仓{sell_pct:.1f}%），目标保留{target_qty}股",
        )

    stop_days = 3 if state.mode == "short" else 10
    required_progress = 0.5 if state.mode == "short" else 1.0
    if state.holding_trade_days >= stop_days and r_multiple < required_progress:
        return decision("time_stop", 0, "持仓交易日达到上限且价格进展不足")
    return decision("hold", None, "继续持有")
