from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyProfile:
    """单一交易模式的风控参数。

    这里保留最核心的几组参数，方便后续做回测和实盘调参。
    """

    mode: str
    stop_atr_mult: float
    take_profit_r: float
    max_loss_pct: float
    entry_confirm_pct: float
    position_cap_pct: float
    support_buffer_pct: float
    pressure_buffer_pct: float
    atr_floor_pct: float
    min_risk_reward: float


PROFILE_LIBRARY: dict[str, StrategyProfile] = {
    "short": StrategyProfile(
        mode="short",
        stop_atr_mult=1.8,
        take_profit_r=2.0,
        max_loss_pct=1.0,
        entry_confirm_pct=0.3,
        position_cap_pct=20.0,
        support_buffer_pct=1.0,
        pressure_buffer_pct=0.3,
        atr_floor_pct=0.8,
        min_risk_reward=1.4,
    ),
    "mid": StrategyProfile(
        mode="mid",
        stop_atr_mult=2.5,
        take_profit_r=3.0,
        max_loss_pct=1.5,
        entry_confirm_pct=1.0,
        position_cap_pct=15.0,
        support_buffer_pct=1.5,
        pressure_buffer_pct=0.5,
        atr_floor_pct=1.0,
        min_risk_reward=1.6,
    ),
}


def normalize_mode(mode: str | None) -> str:
    raw = (mode or "").strip().lower()
    if raw in {"short", "mid"}:
        return raw
    return "mid"


def build_strategy_profile(mode: str | None) -> StrategyProfile:
    """返回指定模式的默认参数。"""

    return PROFILE_LIBRARY[normalize_mode(mode)]

