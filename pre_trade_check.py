from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class RiskLimits:
    max_single_position_pct: float = 30
    max_total_position_pct: float = 95
    min_cash_reserve_pct: float = 5
    max_sector_exposure_pct: float = 60
    max_new_positions_per_day: int = 10
    max_orders_per_day: int = 50
    max_daily_turnover_pct: float = 200
    daily_loss_warn_pct: float = 5
    account_drawdown_warn_pct: float = 15


@dataclass(frozen=True)
class PortfolioState:
    total_position_pct: float = 0
    cash_reserve_pct: float = 100
    sector_exposure_pct: Mapping[str, float] = field(default_factory=dict)
    new_positions_today: int = 0
    orders_today: int = 0
    daily_turnover_pct: float = 0
    daily_pnl_pct: float = 0
    account_drawdown_pct: float = 0

    @classmethod
    def empty(cls) -> "PortfolioState":
        return cls()


@dataclass(frozen=True)
class RiskCheckResult:
    allowed: bool
    hard_blocks: tuple[str, ...]
    soft_warnings: tuple[str, ...]
    metrics: Mapping[str, Any]


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def evaluate_observation(
    signal: Mapping[str, Any], portfolio: PortfolioState, limits: RiskLimits
) -> RiskCheckResult:
    action = signal.get("action")
    position_pct = signal.get("position_pct")
    price = signal.get("price")
    invalid = (
        action not in {"buy", "sell"}
        or not _positive_number(position_pct)
        or (price is not None and not _positive_number(price))
    )
    hard_blocks = ("INVALID_ORDER_INPUT",) if invalid else ()

    warnings: list[str] = []
    if _positive_number(position_pct) and position_pct > limits.max_single_position_pct:
        warnings.append("SINGLE_POSITION_LIMIT")
    added_position_pct = position_pct if action == "buy" and _positive_number(position_pct) else 0
    projected_total_position_pct = portfolio.total_position_pct + added_position_pct
    if projected_total_position_pct > limits.max_total_position_pct:
        warnings.append("TOTAL_POSITION_LIMIT")
    if portfolio.cash_reserve_pct < limits.min_cash_reserve_pct:
        warnings.append("CASH_RESERVE_LIMIT")
    sector = signal.get("sector")
    current_sector_exposure = portfolio.sector_exposure_pct.get(str(sector), 0)
    projected_sector_exposure = current_sector_exposure + added_position_pct
    if sector is not None and projected_sector_exposure > limits.max_sector_exposure_pct:
        warnings.append("SECTOR_EXPOSURE_LIMIT")
    if portfolio.new_positions_today >= limits.max_new_positions_per_day:
        warnings.append("NEW_POSITIONS_LIMIT")
    if portfolio.orders_today >= limits.max_orders_per_day:
        warnings.append("ORDERS_LIMIT")
    if portfolio.daily_turnover_pct > limits.max_daily_turnover_pct:
        warnings.append("DAILY_TURNOVER_LIMIT")
    if portfolio.daily_pnl_pct <= -limits.daily_loss_warn_pct:
        warnings.append("DAILY_LOSS_WARNING")
    if portfolio.account_drawdown_pct <= -limits.account_drawdown_warn_pct:
        warnings.append("ACCOUNT_DRAWDOWN_WARNING")

    metrics = {
        "position_pct": position_pct,
        "total_position_pct": projected_total_position_pct,
        "cash_reserve_pct": portfolio.cash_reserve_pct,
        "sector_exposure_pct": projected_sector_exposure,
        "new_positions_today": portfolio.new_positions_today,
        "orders_today": portfolio.orders_today,
        "daily_turnover_pct": portfolio.daily_turnover_pct,
        "daily_pnl_pct": portfolio.daily_pnl_pct,
        "account_drawdown_pct": portfolio.account_drawdown_pct,
    }
    return RiskCheckResult(not hard_blocks, hard_blocks, tuple(warnings), metrics)
