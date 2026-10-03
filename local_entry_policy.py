"""Broker-independent entry checks used before historical or broker admission.

Inputs are frozen at the decision/open time. Daily return maps MUST end at
the preceding completed session; correlation aligns dates, not row offsets.
This module never sends an order and never estimates expected returns from a
technical target. A cost-covered target is only an economic feasibility test.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Mapping

from execution_contracts import FeeSchedule
from ml_contracts import canonical_hash
from strategy_economics import estimate_round_trip_economics, expected_net_return_gate


@dataclass(frozen=True)
class LocalEntryPolicy:
    version: str = "daily-local-entry-v1"
    max_price_gap_pct: float = 2.0
    max_portfolio_risk_pct: float = 4.0
    max_industry_pct: float = 25.0
    max_total_position_pct: float = 80.0
    max_same_industry_positions: int = 2
    max_pairwise_correlation: float = 0.9
    min_correlation_observations: int = 15
    min_target_cost_multiple: float = 3.0
    max_round_trip_cost_rate: float = 0.004
    min_expected_net_return_bps: float = 20.0
    require_correlation_history: bool = True

    def __post_init__(self):
        for name in ("max_price_gap_pct", "max_portfolio_risk_pct", "max_industry_pct",
                     "max_total_position_pct", "min_target_cost_multiple", "max_round_trip_cost_rate",
                     "min_expected_net_return_bps"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid local policy: " + name)
        if not math.isfinite(self.max_pairwise_correlation) or not 0 <= self.max_pairwise_correlation <= 1:
            raise ValueError("invalid correlation threshold")
        if self.max_same_industry_positions < 1 or self.min_correlation_observations < 3:
            raise ValueError("invalid local policy count")

    @property
    def policy_sha256(self):
        return canonical_hash(asdict(self))


@dataclass(frozen=True)
class EntryHolding:
    code: str
    quantity: int
    price: float
    stop_price: float
    industry: str
    returns: Mapping[str, float]


def aligned_correlation(left, right, minimum=15):
    dates = sorted(set(left).intersection(right))[-20:]
    if len(dates) < minimum:
        return None
    x = [float(left[day]) for day in dates]
    y = [float(right[day]) for day in dates]
    if not all(math.isfinite(value) for value in x + y):
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    dx = sum((value - mx) ** 2 for value in x)
    dy = sum((value - my) ** 2 for value in y)
    if dx <= 0 or dy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(dx * dy)


def check_local_entry(*, candidate, price, quantity, equity, holdings,
                      returns, fees: FeeSchedule, policy: LocalEntryPolicy):
    """Return a stable rejection reason or an empty string.

    Caller includes existing positions AND pending buy reservations in holdings.
    Broker-specific admission remains mandatory after this strategy-level check.
    """
    if not all(math.isfinite(float(value)) and float(value) > 0
               for value in (price, equity, candidate.entry_price, candidate.stop_loss, candidate.take_profit)):
        return "BUY_INVALID_PRICE_PLAN"
    if quantity <= 0 or quantity % 100:
        return "LOT_TOO_SMALL"
    if not candidate.stop_loss < price < candidate.take_profit:
        return "BUY_INVALID_PRICE_PLAN"
    if price > candidate.entry_price * (1 + policy.max_price_gap_pct / 100):
        return "BUY_PRICE_GAP_LIMIT"
    value = quantity * price
    holdings = tuple(holdings)
    total = sum(item.quantity * item.price for item in holdings)
    if (total + value) / equity * 100 > policy.max_total_position_pct:
        return "BUY_TOTAL_EXPOSURE_LIMIT"
    industry = str(candidate.industry).strip().lower()
    if industry and industry not in {"unknown", "uncategorized", "未知", "未分类"}:
        same = [item for item in holdings if item.industry.strip().lower() == industry]
        if len(same) >= policy.max_same_industry_positions:
            return "BUY_INDUSTRY_CONCENTRATION"
        if (sum(item.quantity * item.price for item in same) + value) / equity * 100 > policy.max_industry_pct:
            return "BUY_INDUSTRY_EXPOSURE_LIMIT"
    for item in holdings:
        correlation = aligned_correlation(returns, item.returns, policy.min_correlation_observations)
        if correlation is None and policy.require_correlation_history:
            return "BUY_CORRELATION_HISTORY_MISSING"
        if correlation is not None and correlation >= policy.max_pairwise_correlation:
            return "BUY_CORRELATED_POSITION"
    cost = estimate_round_trip_economics(price, candidate.take_profit, quantity, fees.to_dict())
    if cost["round_trip_cost_rate"] > policy.max_round_trip_cost_rate:
        return "BUY_ROUND_TRIP_COST_LIMIT"
    if cost["gross_profit_yuan"] <= policy.min_target_cost_multiple * cost["round_trip_cost_yuan"]:
        return "BUY_TARGET_COST_COVERAGE"
    expected_gross_bps = candidate.evidence.get("expected_gross_return_bps")
    if expected_gross_bps is not None:
        gate = expected_net_return_gate(
            price,
            quantity,
            expected_gross_bps,
            fees.to_dict(),
            minimum_net_return_bps=policy.min_expected_net_return_bps,
        )
        if not gate["allowed"]:
            return "BUY_EXPECTED_NET_RETURN_BELOW_THRESHOLD"
    stop_cost = float(fees.estimate_round_trip(price, candidate.stop_loss, quantity).total_yuan)
    open_risk = sum(max(item.price - item.stop_price, 0) * item.quantity for item in holdings)
    risk = (price - candidate.stop_loss) * quantity + stop_cost
    if (open_risk + risk) / equity * 100 > policy.max_portfolio_risk_pct:
        return "BUY_PORTFOLIO_RISK_LIMIT"
    return ""
