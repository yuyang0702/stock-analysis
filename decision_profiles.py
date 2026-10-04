"""Versioned, broker-independent daily research hypotheses."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DecisionProfile:
    name: str
    horizon_days: int
    max_chase_atr: float


RESEARCH_PROFILES = {
    "relative_strength_v1": DecisionProfile("relative_strength_v1", 3, 2.0),
    "pullback_trend_v1": DecisionProfile("pullback_trend_v1", 5, 1.5),
    "short_reversal_v1": DecisionProfile("short_reversal_v1", 3, 1.5),
    "breakout_v1": DecisionProfile("breakout_v1", 1, 2.0),
}
ALPHA_PROFILES = ("legacy", "relative_v1", "relative_v2", *RESEARCH_PROFILES)


def confirmed_benchmark_state(dates, closes):
    """Causal two-session deterioration / three-session recovery confirmation.

    The input calendar must end at the current completed session. Missing or
    invalid index prices fail closed. No stock-universe proxy replaces the index.
    """
    import math
    if len(dates) < 23 or not closes:
        return "RISK_OFF"
    state, pending, streak = "RISK_OFF", "", 0
    severity = {"NORMAL": 0, "CAUTION": 1, "RISK_OFF": 2}
    prices = []
    for day in dates:
        value = float(closes.get(day, 0.0))
        if not math.isfinite(value) or value <= 0:
            return "RISK_OFF"
        prices.append(value)
        if len(prices) < 21:
            continue
        r5, r20 = value / prices[-6] - 1, value / prices[-21] - 1
        daily = [prices[i] / prices[i - 1] - 1 for i in range(len(prices) - 20, len(prices))]
        mean = sum(daily) / len(daily)
        vol = (sum((x - mean) ** 2 for x in daily) / len(daily)) ** 0.5
        raw = ("RISK_OFF" if r20 < -0.03 or (vol > 0.025 and r5 < 0)
               else "CAUTION" if r20 <= 0 or r5 < 0 else "NORMAL")
        if raw == state:
            pending, streak = "", 0
            continue
        streak = streak + 1 if pending == raw else 1
        pending = raw
        required = 2 if severity[raw] > severity[state] else 3
        if streak >= required:
            state, pending, streak = raw, "", 0
    return state
