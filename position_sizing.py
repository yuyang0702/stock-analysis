"""Exact board-lot sizing with shared execution-cost contracts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from execution_contracts import (
    FeeBreakdown,
    FeeSchedule,
    InstrumentRules,
    RoundTripCost,
    scenario_loss_yuan,
)


ZERO = Decimal("0")
CENT = Decimal("0.01")
RATIO_QUANTUM = Decimal("0.00000001")
REASON_ORDER = (
    "FEE_SCHEDULE_REQUIRED",
    "INVALID_STOP_DISTANCE",
    "NO_BOARD_LOT",
    "PER_TRADE_RISK_EXCEEDED",
    "PORTFOLIO_OPEN_RISK_EXCEEDED",
    "CASH_CAPACITY_EXCEEDED",
    "ECONOMIC_EDGE_INSUFFICIENT",
)


def _decimal(value: object, name: str, *, non_negative: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite Decimal") from error
    if not result.is_finite() or (non_negative and result < ZERO):
        raise ValueError(f"{name} must be a finite non-negative Decimal")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= ZERO:
        return ZERO.quantize(RATIO_QUANTUM)
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_UP
        return (numerator / denominator).quantize(RATIO_QUANTUM)


@dataclass(frozen=True)
class SizingPolicy:
    risk_pct: Decimal
    risk_cap_yuan: Decimal
    expected_gross_return: Decimal
    max_cost_edge_ratio: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "risk_pct", _decimal(self.risk_pct, "risk_pct"))
        object.__setattr__(self, "risk_cap_yuan", _decimal(self.risk_cap_yuan, "risk_cap_yuan"))
        object.__setattr__(
            self,
            "expected_gross_return",
            _decimal(self.expected_gross_return, "expected_gross_return", non_negative=False),
        )
        object.__setattr__(
            self,
            "max_cost_edge_ratio",
            _decimal(self.max_cost_edge_ratio, "max_cost_edge_ratio"),
        )


@dataclass(frozen=True)
class CapacityBudget:
    max_qty: int
    remaining_open_risk_yuan: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.max_qty, bool) or not isinstance(self.max_qty, int) or self.max_qty < 0:
            raise ValueError("max_qty must be a non-negative integer")
        object.__setattr__(
            self,
            "remaining_open_risk_yuan",
            _decimal(self.remaining_open_risk_yuan, "remaining_open_risk_yuan"),
        )


@dataclass(frozen=True)
class SizingDecision:
    allowed: bool
    reasons: tuple[str, ...]
    target_qty: int
    evaluated_qty: int
    position_value_yuan: Decimal
    buy_cash_required_yuan: Decimal
    buy_fee: FeeBreakdown | None
    percentage_risk_yuan: Decimal
    risk_cap_yuan: Decimal
    effective_per_trade_risk_yuan: Decimal
    remaining_open_risk_yuan: Decimal
    planned_stop_cost: RoundTripCost | None
    gap_cost: RoundTripCost | None
    target_cost: RoundTripCost | None
    planned_stop_loss_yuan: Decimal
    gap_loss_yuan: Decimal
    worst_case_loss_yuan: Decimal
    rule_target_price: Decimal
    gross_edge_yuan: Decimal
    expected_net_pnl_yuan: Decimal
    fee_erosion_ratio: Decimal
    cost_to_expected_edge_ratio: Decimal
    economic_trade_allowed: bool
    fee_schedule_version: str
    fee_schedule_sha256: str

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool or type(self.economic_trade_allowed) is not bool:
            raise ValueError("decision statuses must be boolean")
        if type(self.fee_schedule_version) is not str or type(self.fee_schedule_sha256) is not str:
            raise ValueError("fee schedule identity must be text")
        if type(self.reasons) is not tuple or any(type(reason) is not str for reason in self.reasons):
            raise ValueError("reasons must be an ordered tuple of strings")
        ordered = tuple(reason for reason in REASON_ORDER if reason in self.reasons)
        if self.reasons != ordered or len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reasons must be unique and use the fixed rejection order")
        for name in ("target_qty", "evaluated_qty"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        decimal_names = (
            "position_value_yuan", "buy_cash_required_yuan", "percentage_risk_yuan",
            "risk_cap_yuan", "effective_per_trade_risk_yuan",
            "remaining_open_risk_yuan", "planned_stop_loss_yuan", "gap_loss_yuan",
            "worst_case_loss_yuan", "rule_target_price", "gross_edge_yuan",
            "expected_net_pnl_yuan", "fee_erosion_ratio",
            "cost_to_expected_edge_ratio",
        )
        for name in decimal_names:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        for name in (
            "position_value_yuan", "buy_cash_required_yuan", "percentage_risk_yuan",
            "risk_cap_yuan", "effective_per_trade_risk_yuan",
            "remaining_open_risk_yuan", "planned_stop_loss_yuan", "gap_loss_yuan",
            "worst_case_loss_yuan", "fee_erosion_ratio", "cost_to_expected_edge_ratio",
        ):
            if getattr(self, name) < ZERO:
                raise ValueError(f"{name} must be non-negative")
        if self.effective_per_trade_risk_yuan != min(
            self.percentage_risk_yuan, self.risk_cap_yuan
        ):
            raise ValueError("effective_per_trade_risk_yuan does not match its limits")
        if self.allowed:
            if self.reasons or self.target_qty <= 0 or self.target_qty != self.evaluated_qty:
                raise ValueError("allowed decision quantity and reasons are inconsistent")
            if not self.economic_trade_allowed:
                raise ValueError("allowed decision must pass economics")
        elif self.target_qty != 0 or not self.reasons:
            raise ValueError("rejected decision must have zero target and ordered reasons")

        if self.evaluated_qty == 0:
            if "FEE_SCHEDULE_REQUIRED" not in self.reasons:
                raise ValueError("zero evaluated quantity is only valid without a fee schedule")
            if self.economic_trade_allowed or self.fee_schedule_version or self.fee_schedule_sha256:
                raise ValueError("missing-fee rejection cannot claim fee or economic evidence")
            if any(
                value is not None
                for value in (
                    self.buy_fee, self.planned_stop_cost, self.gap_cost, self.target_cost,
                )
            ):
                raise ValueError("missing-fee rejection cannot carry fee evidence")
            for name in (
                "position_value_yuan", "buy_cash_required_yuan", "planned_stop_loss_yuan",
                "gap_loss_yuan", "worst_case_loss_yuan", "gross_edge_yuan",
                "expected_net_pnl_yuan", "fee_erosion_ratio",
                "cost_to_expected_edge_ratio",
            ):
                if getattr(self, name) != ZERO:
                    raise ValueError(f"{name} must be zero without an evaluated quantity")
            return

        if "FEE_SCHEDULE_REQUIRED" in self.reasons:
            raise ValueError("evaluated evidence cannot report a missing fee schedule")
        if not isinstance(self.buy_fee, FeeBreakdown):
            raise ValueError("evaluated evidence requires a buy fee")
        if (
            self.buy_fee.qty != self.evaluated_qty
            or self.position_value_yuan != self.buy_fee.price * self.evaluated_qty
            or self.buy_cash_required_yuan
            != self.position_value_yuan + self.buy_fee.total_yuan
        ):
            raise ValueError("buy quantity, value and cash evidence are inconsistent")
        if (
            self.fee_schedule_version != self.buy_fee.schedule_version
            or self.fee_schedule_sha256 != self.buy_fee.fee_schedule_sha256
        ):
            raise ValueError("fee schedule identity does not match buy evidence")

        def validate_cost(cost: RoundTripCost | None, name: str) -> RoundTripCost:
            if not isinstance(cost, RoundTripCost):
                raise ValueError(f"{name} must be a RoundTripCost")
            if cost.buy != self.buy_fee or cost.buy.qty != self.evaluated_qty:
                raise ValueError(f"{name} does not match evaluated buy evidence")
            if (
                cost.buy.schedule_version != self.fee_schedule_version
                or cost.buy.fee_schedule_sha256 != self.fee_schedule_sha256
            ):
                raise ValueError(f"{name} uses a different fee schedule")
            return cost

        invalid_stop = "INVALID_STOP_DISTANCE" in self.reasons
        if invalid_stop:
            if self.planned_stop_cost is not None or self.gap_cost is not None:
                raise ValueError("invalid stop rejection cannot carry loss-scenario costs")
            if any(
                amount != ZERO
                for amount in (
                    self.planned_stop_loss_yuan,
                    self.gap_loss_yuan,
                    self.worst_case_loss_yuan,
                )
            ):
                raise ValueError("invalid stop rejection cannot carry scenario losses")
        else:
            planned = validate_cost(self.planned_stop_cost, "planned_stop_cost")
            gap = validate_cost(self.gap_cost, "gap_cost")
            planned_loss = scenario_loss_yuan(
                self.buy_fee.price, planned.sell.price, self.evaluated_qty, planned
            )
            gap_loss = scenario_loss_yuan(
                self.buy_fee.price, gap.sell.price, self.evaluated_qty, gap
            )
            if (
                self.planned_stop_loss_yuan != planned_loss
                or self.gap_loss_yuan != gap_loss
                or self.worst_case_loss_yuan != max(planned_loss, gap_loss)
            ):
                raise ValueError("loss-scenario evidence is not reproducible")
            if (
                ("PER_TRADE_RISK_EXCEEDED" in self.reasons)
                != (self.worst_case_loss_yuan > self.effective_per_trade_risk_yuan)
                or ("PORTFOLIO_OPEN_RISK_EXCEEDED" in self.reasons)
                != (self.worst_case_loss_yuan > self.remaining_open_risk_yuan)
            ):
                raise ValueError("risk rejection reasons do not match scenario loss")

        entry_price = self.buy_fee.price
        if self.gross_edge_yuan != (self.rule_target_price - entry_price) * self.evaluated_qty:
            raise ValueError("gross edge does not match evaluated quantity and target")
        if self.rule_target_price > ZERO:
            target = validate_cost(self.target_cost, "target_cost")
            if target.sell.price != self.rule_target_price:
                raise ValueError("target cost price does not match rule target")
            if self.expected_net_pnl_yuan != self.gross_edge_yuan - target.total_yuan:
                raise ValueError("expected net PnL is not reproducible")
            if self.fee_erosion_ratio != _ratio(target.total_yuan, self.position_value_yuan):
                raise ValueError("fee erosion ratio is not reproducible")
            if self.cost_to_expected_edge_ratio != _ratio(
                target.total_yuan, self.gross_edge_yuan
            ):
                raise ValueError("cost-to-edge ratio is not reproducible")
        elif (
            self.target_cost is not None
            or self.expected_net_pnl_yuan != ZERO
            or self.fee_erosion_ratio != ZERO
            or self.cost_to_expected_edge_ratio != ZERO
        ):
            raise ValueError("non-positive target cannot carry target-cost economics")
        if self.economic_trade_allowed:
            if self.gross_edge_yuan <= ZERO or self.expected_net_pnl_yuan <= ZERO:
                raise ValueError("economic approval requires positive edge and net PnL")
            if "ECONOMIC_EDGE_INSUFFICIENT" in self.reasons:
                raise ValueError("economic approval conflicts with rejection reason")
        elif "ECONOMIC_EDGE_INSUFFICIENT" not in self.reasons:
            raise ValueError("failed economics requires its stable rejection reason")


@dataclass(frozen=True)
class _Evidence:
    qty: int
    position_value: Decimal
    cash_required: Decimal
    buy_fee: FeeBreakdown
    planned_cost: RoundTripCost | None
    gap_cost: RoundTripCost | None
    target_cost: RoundTripCost | None
    planned_loss: Decimal
    gap_loss: Decimal
    worst_loss: Decimal
    target_price: Decimal
    gross_edge: Decimal
    net_pnl: Decimal
    fee_erosion_ratio: Decimal
    cost_edge_ratio: Decimal
    economic_allowed: bool


def _empty_decision(
    reasons: tuple[str, ...],
    policy: SizingPolicy,
    capacity: CapacityBudget,
    percentage_risk: Decimal,
    target_price: Decimal,
) -> SizingDecision:
    return SizingDecision(
        False, reasons, 0, 0, ZERO, ZERO, None, percentage_risk, policy.risk_cap_yuan,
        min(percentage_risk, policy.risk_cap_yuan), capacity.remaining_open_risk_yuan,
        None, None, None, ZERO, ZERO, ZERO, target_price, ZERO, ZERO,
        ZERO.quantize(RATIO_QUANTUM), ZERO.quantize(RATIO_QUANTUM), False, "", "",
    )


def _decision(
    allowed: bool,
    reasons: tuple[str, ...],
    evidence: _Evidence,
    policy: SizingPolicy,
    capacity: CapacityBudget,
    percentage_risk: Decimal,
    fees: FeeSchedule,
) -> SizingDecision:
    return SizingDecision(
        allowed=allowed,
        reasons=() if allowed else reasons,
        target_qty=evidence.qty if allowed else 0,
        evaluated_qty=evidence.qty,
        position_value_yuan=evidence.position_value,
        buy_cash_required_yuan=evidence.cash_required,
        buy_fee=evidence.buy_fee,
        percentage_risk_yuan=percentage_risk,
        risk_cap_yuan=policy.risk_cap_yuan,
        effective_per_trade_risk_yuan=min(percentage_risk, policy.risk_cap_yuan),
        remaining_open_risk_yuan=capacity.remaining_open_risk_yuan,
        planned_stop_cost=evidence.planned_cost,
        gap_cost=evidence.gap_cost,
        target_cost=evidence.target_cost,
        planned_stop_loss_yuan=evidence.planned_loss,
        gap_loss_yuan=evidence.gap_loss,
        worst_case_loss_yuan=evidence.worst_loss,
        rule_target_price=evidence.target_price,
        gross_edge_yuan=evidence.gross_edge,
        expected_net_pnl_yuan=evidence.net_pnl,
        fee_erosion_ratio=evidence.fee_erosion_ratio,
        cost_to_expected_edge_ratio=evidence.cost_edge_ratio,
        economic_trade_allowed=evidence.economic_allowed,
        fee_schedule_version=fees.version,
        fee_schedule_sha256=fees.contract_sha256,
    )


def allocate_buy_quantity(
    *,
    entry_price: Decimal,
    stop_price: Decimal,
    gap_price: Decimal,
    rules: InstrumentRules,
    fees: FeeSchedule | None,
    equity: Decimal,
    available_cash: Decimal,
    risk_pct: Decimal,
    risk_cap_yuan: Decimal,
    capacity: CapacityBudget,
    expected_gross_return: Decimal,
    max_cost_edge_ratio: Decimal,
) -> SizingDecision:
    if not isinstance(rules, InstrumentRules):
        raise ValueError("rules must be InstrumentRules")
    if not isinstance(capacity, CapacityBudget):
        raise ValueError("capacity must be CapacityBudget")
    entry = _decimal(entry_price, "entry_price")
    if entry <= ZERO:
        raise ValueError("entry_price must be positive")
    equity_amount = _decimal(equity, "equity")
    cash = _decimal(available_cash, "available_cash")
    policy = SizingPolicy(risk_pct, risk_cap_yuan, expected_gross_return, max_cost_edge_ratio)
    percentage_risk = _money(equity_amount * policy.risk_pct)
    effective_risk = min(percentage_risk, policy.risk_cap_yuan)
    target_price = entry * (Decimal("1") + policy.expected_gross_return)
    minimum_valid_qty = (
        (rules.buy_min_qty + rules.buy_qty_step - 1) // rules.buy_qty_step
        * rules.buy_qty_step
    )
    max_aligned = capacity.max_qty // rules.buy_qty_step * rules.buy_qty_step

    try:
        stop = _decimal(stop_price, "stop_price", non_negative=False)
        gap = _decimal(gap_price, "gap_price", non_negative=False)
        loss_prices_valid = ZERO < stop < entry and ZERO < gap < entry
    except ValueError:
        stop = gap = ZERO
        loss_prices_valid = False

    base = {
        "FEE_SCHEDULE_REQUIRED": fees is None,
        "INVALID_STOP_DISTANCE": not loss_prices_valid,
        "NO_BOARD_LOT": max_aligned < minimum_valid_qty,
    }
    if fees is not None and not isinstance(fees, FeeSchedule):
        raise ValueError("fees must be FeeSchedule or None")
    entry_rule_errors = rules.validate_order("buy", minimum_valid_qty, entry)
    if entry_rule_errors:
        raise ValueError(f"entry order violates instrument rules: {', '.join(entry_rule_errors)}")
    if fees is None:
        reasons = tuple(reason for reason in REASON_ORDER if base.get(reason, False))
        return _empty_decision(reasons, policy, capacity, percentage_risk, target_price)

    def evidence(qty: int) -> _Evidence:
        entry_errors = rules.validate_order("buy", qty, entry)
        if entry_errors:
            raise ValueError(f"entry order violates instrument rules: {', '.join(entry_errors)}")
        position_value = entry * qty
        buy_fee = fees.estimate("buy", entry, qty)
        cash_required = position_value + buy_fee.total_yuan
        planned_cost = gap_cost = None
        planned_loss = gap_loss = worst_loss = ZERO
        if loss_prices_valid:
            planned_cost = fees.estimate_round_trip(entry, stop, qty)
            gap_cost = fees.estimate_round_trip(entry, gap, qty)
            planned_loss = scenario_loss_yuan(entry, stop, qty, planned_cost)
            gap_loss = scenario_loss_yuan(entry, gap, qty, gap_cost)
            worst_loss = max(planned_loss, gap_loss)

        gross_edge = (target_price - entry) * qty
        target_cost = None
        net_pnl = ZERO
        erosion = edge_ratio = ZERO.quantize(RATIO_QUANTUM)
        target_rule_valid = target_price > ZERO and not rules.validate_order("buy", qty, target_price)
        if target_price > ZERO:
            target_cost = fees.estimate_round_trip(entry, target_price, qty)
            net_pnl = gross_edge - target_cost.total_yuan
            erosion = _ratio(target_cost.total_yuan, position_value)
            edge_ratio = _ratio(target_cost.total_yuan, gross_edge)
        economic_allowed = bool(
            target_rule_valid
            and gross_edge > ZERO
            and net_pnl > ZERO
            and edge_ratio <= policy.max_cost_edge_ratio
        )
        return _Evidence(
            qty, position_value, cash_required, buy_fee, planned_cost, gap_cost,
            target_cost, planned_loss, gap_loss, worst_loss, target_price, gross_edge,
            net_pnl, erosion, edge_ratio, economic_allowed,
        )

    def failures(item: _Evidence) -> dict[str, bool]:
        return {
            **base,
            "PER_TRADE_RISK_EXCEEDED": loss_prices_valid and item.worst_loss > effective_risk,
            "PORTFOLIO_OPEN_RISK_EXCEEDED": (
                loss_prices_valid and item.worst_loss > capacity.remaining_open_risk_yuan
            ),
            "CASH_CAPACITY_EXCEEDED": item.cash_required > cash,
            "ECONOMIC_EDGE_INSUFFICIENT": not item.economic_allowed,
        }

    if not any(base.values()):
        for qty in range(max_aligned, minimum_valid_qty - 1, -rules.buy_qty_step):
            item = evidence(qty)
            current = failures(item)
            if not any(current.values()):
                return _decision(True, (), item, policy, capacity, percentage_risk, fees)

    audited = evidence(minimum_valid_qty)
    rejected = failures(audited)
    reasons = tuple(reason for reason in REASON_ORDER if rejected.get(reason, False))
    return _decision(False, reasons, audited, policy, capacity, percentage_risk, fees)
