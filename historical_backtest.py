"""Deterministic daily A-share historical matching engine."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Mapping

import config as app_config
from benchmark_data import load_benchmark_csv
from execution_contracts import FeeBreakdown, FeeSchedule, canonical_json
from exit_policy import (
    PositionExitState,
    evaluate_exit,
    first_take_profit_target_qty,
    resolve_effective_stop,
)
from historical_data import (
    STRICT_FEATURES,
    HistoricalDataValidationError,
    HistoricalStore,
    validate_dataset,
)
from historical_strategy import (
    Candidate,
    generate_candidates_at,
    generate_daily_candidates,
    price_core_market_state,
)
from ml_contracts import CandidateSample, canonical_hash
from local_entry_policy import (
    EntryHolding,
    LocalEntryPolicy,
    aligned_correlation,
    check_local_entry,
)


@dataclass(frozen=True)
class HistoricalBacktestConfig:
    initial_cash: float = 100_000.0
    commission_rate: float = float(app_config.SIMULATION_FEE_SCHEDULE.buy_commission_rate)
    minimum_commission: float = float(
        app_config.SIMULATION_FEE_SCHEDULE.buy_minimum_commission_yuan
    )
    stamp_tax_rate: float = float(app_config.SIMULATION_FEE_SCHEDULE.stamp_tax_rate)
    slippage_bps: float = float(app_config.SIMULATION_FEE_SCHEDULE.buy_slippage_rate * 10_000)
    max_positions: int = 8
    mode: str = "price_core"
    parameter_version: str = "v1"
    min_score: float = 75.0
    caution_min_score: float = 85.0
    cooldown_days: int = 3
    max_new_positions_per_day: int = 10
    require_trend_confirmation: bool = False
    require_breakout_confirmation: bool = False
    max_chase_atr: float = 0.0
    max_entry_score: float = 100.0
    signal_confirmation_days: int = 1
    # Direct library callers historically allowed arbitrary test position
    # sizes. CLI and the production research profile pass the safer 4% cap.
    max_portfolio_risk_pct: float = 100.0
    max_same_industry_positions: int = 2
    max_pairwise_correlation: float = 0.9
    market_risk_exit_enabled: bool = False
    local_entry_gates_enabled: bool = False
    alpha_profile: str = "legacy"
    benchmark_closes: Mapping[str, float] | None = field(default=None, repr=False)
    slippage_model: str = "fixed"
    max_participation_pct: float = 100.0
    min_holding_days: int = 0
    fee_schedule: FeeSchedule | None = None
    entry_fee_schedule: FeeSchedule | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.initial_cash)) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")
        if int(self.max_positions) != self.max_positions or self.max_positions <= 0:
            raise ValueError("max_positions must be a positive integer")
        if not math.isfinite(float(self.min_score)):
            raise ValueError("min_score must be finite")
        if not math.isfinite(float(self.caution_min_score)):
            raise ValueError("caution_min_score must be finite")
        if int(self.cooldown_days) != self.cooldown_days or self.cooldown_days < 0:
            raise ValueError("cooldown_days must be a non-negative integer")
        if int(self.max_new_positions_per_day) != self.max_new_positions_per_day or self.max_new_positions_per_day <= 0:
            raise ValueError("max_new_positions_per_day must be a positive integer")
        if int(self.signal_confirmation_days) != self.signal_confirmation_days or self.signal_confirmation_days <= 0:
            raise ValueError("signal_confirmation_days must be a positive integer")
        if not math.isfinite(float(self.max_chase_atr)) or self.max_chase_atr < 0:
            raise ValueError("max_chase_atr must be finite and non-negative")
        if not math.isfinite(float(self.max_entry_score)):
            raise ValueError("max_entry_score must be finite")
        if not math.isfinite(float(self.max_portfolio_risk_pct)) or self.max_portfolio_risk_pct < 0:
            raise ValueError("max_portfolio_risk_pct must be finite and non-negative")
        if int(self.max_same_industry_positions) != self.max_same_industry_positions or self.max_same_industry_positions <= 0:
            raise ValueError("max_same_industry_positions must be a positive integer")
        if not math.isfinite(float(self.max_pairwise_correlation)) or not 0 <= self.max_pairwise_correlation <= 1:
            raise ValueError("max_pairwise_correlation must be between 0 and 1")
        if self.alpha_profile not in {"legacy", "relative_v1", "relative_v2"}:
            raise ValueError("unknown alpha_profile")
        if self.slippage_model not in {"fixed", "liquidity_v1"}:
            raise ValueError("unknown slippage_model")
        if not math.isfinite(float(self.max_participation_pct)) or self.max_participation_pct <= 0:
            raise ValueError("max_participation_pct must be finite and positive")
        if int(self.min_holding_days) != self.min_holding_days or self.min_holding_days < 0:
            raise ValueError("min_holding_days must be a non-negative integer")
        if not str(self.mode).strip():
            raise ValueError("mode is required")
        if not str(self.parameter_version).strip():
            raise ValueError("parameter_version is required")

    def resolved_fee_schedule(self) -> FeeSchedule:
        if self.fee_schedule is not None:
            return self.fee_schedule
        base = app_config.SIMULATION_FEE_SCHEDULE
        if (
            self.commission_rate == float(base.buy_commission_rate)
            and self.minimum_commission == float(base.buy_minimum_commission_yuan)
            and self.stamp_tax_rate == float(base.stamp_tax_rate)
            and self.slippage_bps == float(base.buy_slippage_rate * 10_000)
        ):
            return base
        no_costs = (
            self.commission_rate == self.minimum_commission == self.stamp_tax_rate == self.slippage_bps == 0
        )
        return base.derive_variant(
            "historical-compat",
            buy_commission_rate=self.commission_rate,
            sell_commission_rate=self.commission_rate,
            buy_minimum_commission_yuan=self.minimum_commission,
            sell_minimum_commission_yuan=self.minimum_commission,
            stamp_tax_rate=self.stamp_tax_rate,
            buy_slippage_rate=self.slippage_bps / 10_000,
            sell_slippage_rate=self.slippage_bps / 10_000,
            transfer_fee_rate=0 if no_costs else base.transfer_fee_rate,
            other_fee_rate=0 if no_costs else base.other_fee_rate,
        )

    def resolved_entry_fee_schedule(self) -> FeeSchedule:
        return self.entry_fee_schedule or self.resolved_fee_schedule()


@dataclass
class HistoricalPosition:
    code: str
    quantity: int
    initial_quantity: int
    entry_price: float
    stop_loss: float
    take_profit: float
    atr14: float
    mode: str
    market_regime: str
    industry: str
    theme: str
    buy_date: str
    highest_price: float
    entry_fee_remaining_yuan: float = 0.0
    take_profit_stage: int = 0
    profit_protection_activated_at: str = ""
    trailing_stop_active_from: str = ""
    last_adjust_factor: float = 1.0
    holding_trade_days: int = 0
    recent_returns: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingOrder:
    decision_date: str
    candidate: Candidate


@dataclass(frozen=True)
class PendingSell:
    decision_date: str
    code: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class HistoricalTrade:
    decision_date: str
    trade_date: str
    code: str
    action: str
    quantity: int
    price: float
    fee: float
    reason: str
    pnl: float | None = None
    holding_days: int = 0
    strategy_mode: str = "unknown"
    market_regime: str = "unknown"
    score: float = 0.0
    industry: str = "unknown"
    theme: str = "unknown"
    fee_schedule_version: str = "not-applicable"
    commission_yuan: float = 0.0
    stamp_tax_yuan: float = 0.0
    transfer_fee_yuan: float = 0.0
    other_fee_yuan: float = 0.0
    slippage_yuan: float = 0.0
    entry_fee_allocated_yuan: float = 0.0

    @property
    def fee_components(self) -> dict[str, float]:
        return {
            "commission_yuan": self.commission_yuan,
            "stamp_tax_yuan": self.stamp_tax_yuan,
            "transfer_fee_yuan": self.transfer_fee_yuan,
            "other_fee_yuan": self.other_fee_yuan,
            "slippage_yuan": self.slippage_yuan,
        }


@dataclass(frozen=True)
class EquityPoint:
    trade_date: str
    equity: float
    cash: float


@dataclass
class HistoricalBacktestResult:
    trades: list[HistoricalTrade] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    blocked_counts: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestMetrics:
    net_return: float
    annualized_return: float
    max_drawdown: float
    volatility: float
    calmar: float
    win_rate: float
    average_win: float
    average_loss: float
    profit_factor: float
    tail_loss_5pct: float
    turnover: float
    average_holding_days: float
    net_profit_without_top3: float
    profit_factor_without_top3: float


@dataclass(frozen=True)
class WalkForwardWindow:
    training_start: str
    training_end: str
    validation_start: str
    validation_end: str


@dataclass(frozen=True)
class DecisionTimeReplayBatch:
    decision_at: str
    cohort_sha256: str
    candidate_count: int
    selected_count: int


@dataclass(frozen=True)
class DecisionTimeReplayResult:
    dataset_id: str
    dataset_sha256: str
    start_at: str
    end_at: str
    strategy_config_sha256: str
    batches: tuple[DecisionTimeReplayBatch, ...]

    @property
    def candidate_count(self) -> int:
        return sum(batch.candidate_count for batch in self.batches)

    @property
    def selected_count(self) -> int:
        return sum(batch.selected_count for batch in self.batches)


_STRICT_REPLAY_VERSION_FIELDS = (
    "strategy_version",
    "parameter_version",
    "feature_schema_version",
    "market_data_version",
    "code_hash",
    "generator_hash",
)


def run_decision_time_replay(
    store: HistoricalStore,
    dataset_id: str,
    start_at: str,
    end_at: str,
    *,
    strategy_config: Mapping[str, object],
    expected_dataset_hash: str,
) -> DecisionTimeReplayResult:
    """Replay imported five-minute cohorts at their exact decision timestamps."""
    dataset = str(dataset_id).strip()
    if not dataset:
        raise HistoricalDataValidationError("STRICT_DATASET_REQUIRED")
    normalized_config = _strict_replay_config(strategy_config)
    expected_hash = _strict_sha256(expected_dataset_hash, "expected_dataset_hash")
    actual_hash = store.dataset_hash(dataset)
    if actual_hash != expected_hash:
        raise HistoricalDataValidationError("STRICT_DATASET_HASH_MISMATCH")

    normalized_start = _aware_replay_timestamp(start_at, "start_at")
    normalized_end = _aware_replay_timestamp(end_at, "end_at")
    decision_times = store.decision_times(dataset, normalized_start, normalized_end)
    if not decision_times:
        raise HistoricalDataValidationError("STRICT_DECISION_TIMES_NOT_FOUND")

    batches = []
    for decision_at in decision_times:
        samples = tuple(
            generate_candidates_at(
                store,
                dataset,
                decision_at,
                normalized_config,
            )
        )
        batches.append(
            DecisionTimeReplayBatch(
                decision_at=decision_at,
                cohort_sha256=canonical_hash(
                    [canonical_hash(sample) for sample in samples]
                ),
                candidate_count=len(samples),
                selected_count=sum(sample.selected for sample in samples),
            )
        )
    return DecisionTimeReplayResult(
        dataset_id=dataset,
        dataset_sha256=actual_hash,
        start_at=normalized_start,
        end_at=normalized_end,
        strategy_config_sha256=canonical_hash(normalized_config),
        batches=tuple(batches),
    )


def _strict_replay_config(value: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise HistoricalDataValidationError("STRICT_STRATEGY_CONFIG_REQUIRED")
    missing = [
        field
        for field in _STRICT_REPLAY_VERSION_FIELDS
        if not str(value.get(field) or "").strip()
    ]
    if missing:
        raise HistoricalDataValidationError(
            "STRICT_STRATEGY_CONFIG_INCOMPLETE: " + ",".join(missing)
        )
    return {
        field: str(value[field]).strip()
        for field in _STRICT_REPLAY_VERSION_FIELDS
    }


def _strict_sha256(value: object, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise HistoricalDataValidationError(f"INVALID_{label.upper()}")
    return text


def _aware_replay_timestamp(value: object, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise HistoricalDataValidationError(f"INVALID_{label.upper()}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalDataValidationError("TIMEZONE_AWARE_TIMESTAMP_REQUIRED")
    return parsed.isoformat()


def _execution_fee_schedule(
    config: HistoricalBacktestConfig,
    base: FeeSchedule,
    row: Mapping[str, object],
    price: float,
    quantity: int,
    side: str,
) -> FeeSchedule:
    """Return a deterministic fee contract for one simulated fill.

    ``liquidity_v1`` keeps the configured commission/tax contract and scales
    only slippage using participation and the observed daily range.  This is
    deliberately conservative and remains a research model until broker
    fills are available.
    """
    if config.slippage_model == "fixed":
        return base
    amount = max(float(row.get("amount") or 0.0), 0.0)
    notional = max(float(price) * max(int(quantity), 0), 0.0)
    participation = notional / amount if amount > 0 else 1.0
    close = max(float(row.get("close") or price), 0.0001)
    high = float(row.get("high") or close)
    low = float(row.get("low") or close)
    range_pct = max(high - low, 0.0) / close
    base_bps = float(base.buy_slippage_rate if side == "buy" else base.sell_slippage_rate) * 10000
    impact_bps = base_bps * (
        1.0
        + min(4.0, participation / 0.01)
        + min(2.0, range_pct / 0.03)
    )
    change = {
        "buy_slippage_rate": Decimal(str(impact_bps / 10000))
        if side == "buy" else base.buy_slippage_rate,
        "sell_slippage_rate": Decimal(str(impact_bps / 10000))
        if side == "sell" else base.sell_slippage_rate,
    }
    return base.derive_variant(f"liquidity-{side}", **change)


def run_historical_backtest(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    config: HistoricalBacktestConfig,
) -> HistoricalBacktestResult:
    dates = store.trade_dates(dataset_id, start, end)
    result = HistoricalBacktestResult()
    cash = float(config.initial_cash)
    fees = config.resolved_fee_schedule()
    positions: dict[str, HistoricalPosition] = {}
    pending: list[PendingOrder] = []
    pending_sells: list[PendingSell] = []
    date_index = {trade_date: index for index, trade_date in enumerate(dates)}
    cooldown_until: dict[str, int] = {}
    signal_streak: dict[str, int] = {}
    previous_candidate_codes: set[str] = set()

    for trade_date in dates:
        current_index = date_index[trade_date]
        new_positions_today = 0
        rows = {str(row["code"]): row for row in store.daily_slice(dataset_id, trade_date)}
        current_market_state = (
            price_core_market_state(store, dataset_id, trade_date)
            if config.mode == "price_core" and positions and config.market_risk_exit_enabled
            else ""
        )

        for code, position in positions.items():
            row = rows.get(code)
            if row is None:
                continue
            factor = float(row["adjust_factor"])
            if factor <= 0 or factor == position.last_adjust_factor:
                continue
            ratio = factor / position.last_adjust_factor
            position.quantity = int(position.quantity * ratio / 100) * 100
            position.initial_quantity = int(position.initial_quantity * ratio / 100) * 100
            position.entry_price /= ratio
            position.stop_loss /= ratio
            position.take_profit /= ratio
            position.highest_price /= ratio
            position.atr14 /= ratio
            position.last_adjust_factor = factor

        # Exit decisions from the prior close always consume cash/positions before new buys.
        for order in sorted(pending_sells, key=lambda item: item.code):
            position = positions.get(order.code)
            row = rows.get(order.code)
            if position is None:
                continue
            if row is None or bool(row["suspended"]):
                _blocked(result, "SUSPENDED")
                continue
            if float(row["open"]) <= float(row["limit_down"]):
                _blocked(result, "LIMIT_DOWN_SELL_BLOCKED")
                continue
            quantity = min(order.quantity, position.quantity)
            price = round(float(row["open"]), 4)
            value = price * quantity
            execution_fees = _execution_fee_schedule(config, fees, row, price, quantity, "sell")
            breakdown = execution_fees.estimate("sell", Decimal(str(price)), quantity)
            fee = float(breakdown.total_yuan)
            entry_fee = _allocate_entry_fee(position, quantity)
            cash += value - fee
            pnl = round((price - position.entry_price) * quantity - fee - entry_fee, 2)
            result.trades.append(
                HistoricalTrade(
                    order.decision_date, trade_date, order.code, "sell", quantity, price, fee,
                    order.reason, pnl, holding_days=position.holding_trade_days,
                    strategy_mode=position.mode, market_regime=position.market_regime,
                    industry=position.industry, theme=position.theme,
                    fee_schedule_version=execution_fees.version,
                    entry_fee_allocated_yuan=entry_fee,
                    **_fee_fields(breakdown),
                )
            )
            position.quantity -= quantity
            if position.quantity <= 0:
                cooldown_until[order.code] = current_index + int(config.cooldown_days)
                del positions[order.code]
            elif order.reason == "TAKE_PROFIT_1":
                position.take_profit_stage = 1
        pending_sells = []

        # Orders decided at the prior close live for one open only.
        for order in sorted(pending, key=lambda item: (-item.candidate.score, item.candidate.code)):
            candidate = order.candidate
            row = rows.get(candidate.code)
            if row is None or bool(row["suspended"]):
                _blocked(result, "SUSPENDED")
                continue
            open_price = float(row["open"])
            if open_price >= float(row["limit_up"]):
                _blocked(result, "LIMIT_UP_BUY_BLOCKED")
                continue
            if candidate.code in positions or len(positions) >= config.max_positions:
                continue
            if new_positions_today >= config.max_new_positions_per_day:
                _blocked(result, "BUY_DAILY_NEW_POSITIONS_LIMIT")
                continue
            price = round(open_price, 4)
            if config.local_entry_gates_enabled:
                if (
                    candidate.industry
                    and candidate.industry.lower() != "unknown"
                    and sum(
                        1
                        for position in positions.values()
                        if position.industry.lower() == candidate.industry.lower()
                    ) >= config.max_same_industry_positions
                ):
                    _blocked(result, "BUY_INDUSTRY_CONCENTRATION")
                    continue
                if _candidate_correlated_with_positions(
                    candidate, positions, config.max_pairwise_correlation
                ):
                    _blocked(result, "BUY_CORRELATED_POSITION")
                    continue
            # At the open, today's close/high/low are unavailable for sizing.
            open_rows = {code: {"close": bar["open"]} for code, bar in rows.items()}
            account_value = _account_value(cash, positions, open_rows)
            target = account_value * candidate.position_pct / 100
            quantity = int(target / price / 100) * 100
            if quantity <= 0:
                _blocked(result, "LOT_TOO_SMALL")
                continue
            if config.slippage_model == "liquidity_v1":
                amount = float(row.get("amount") or 0.0)
                if amount <= 0:
                    _blocked(result, "BUY_LIQUIDITY_MISSING")
                    continue
                max_quantity = int(
                    amount * float(config.max_participation_pct) / 100 / price / 100
                ) * 100
                if max_quantity <= 0:
                    _blocked(result, "BUY_LIQUIDITY_LIMIT")
                    continue
                if quantity > max_quantity:
                    quantity = max_quantity
            if config.local_entry_gates_enabled:
                holdings = tuple(
                    EntryHolding(
                        code=position.code,
                        quantity=position.quantity,
                        price=float(open_rows.get(position.code, {}).get("close", position.entry_price)),
                        stop_price=position.stop_loss,
                        industry=position.industry,
                        returns=position.recent_returns,
                    )
                    for position in positions.values()
                )
                reason = check_local_entry(
                    candidate=candidate,
                    price=price,
                    quantity=quantity,
                    equity=account_value,
                    holdings=holdings,
                    returns=_prior_returns(store, dataset_id, candidate.code, order.decision_date),
                    fees=config.resolved_entry_fee_schedule(),
                    policy=local_policy_for_config(config),
                )
                if reason:
                    _blocked(result, reason)
                    continue
            execution_fees = _execution_fee_schedule(config, fees, row, price, quantity, "buy")
            breakdown = execution_fees.estimate("buy", Decimal(str(price)), quantity)
            fee = float(breakdown.total_yuan)
            while quantity > 0 and price * quantity + fee > cash:
                quantity -= 100
                execution_fees = _execution_fee_schedule(config, fees, row, price, quantity, "buy")
                breakdown = execution_fees.estimate("buy", Decimal(str(price)), quantity)
                fee = float(breakdown.total_yuan)
            if quantity <= 0:
                _blocked(result, "INSUFFICIENT_CASH")
                continue
            cash -= price * quantity + fee
            positions[candidate.code] = HistoricalPosition(
                code=candidate.code,
                quantity=quantity,
                initial_quantity=quantity,
                entry_price=price,
                stop_loss=candidate.stop_loss,
                take_profit=candidate.take_profit,
                atr14=candidate.atr14,
                mode=candidate.mode,
                market_regime=candidate.market_regime,
                industry=candidate.industry,
                theme=candidate.theme,
                buy_date=trade_date,
                highest_price=float(row["high"]),
                entry_fee_remaining_yuan=fee,
                last_adjust_factor=float(row["adjust_factor"]),
                recent_returns=_prior_returns(store, dataset_id, candidate.code, order.decision_date),
            )
            result.trades.append(
                HistoricalTrade(
                    order.decision_date, trade_date, candidate.code, "buy", quantity, price, fee,
                    "SIGNAL", fee_schedule_version=execution_fees.version, **_fee_fields(breakdown),
                )
            )
            new_positions_today += 1
        pending = []

        # Point-in-time ordering: hard stop, prior-batch trailing stop, then profit-taking;
        # today's new high can tighten only the next decision batch.
        for code in sorted(tuple(positions)):
            position = positions[code]
            row = rows.get(code)
            if row is None or bool(row["suspended"]) or position.buy_date == trade_date:
                continue
            prior_highest_price = position.highest_price
            stop = resolve_effective_stop(
                PositionExitState(
                    code=code,
                    mode=position.mode,
                    initial_qty=position.initial_quantity,
                    current_qty=position.quantity,
                    entry_price=position.entry_price,
                    initial_stop_price=position.stop_loss,
                    highest_price=prior_highest_price,
                    atr14=position.atr14,
                    take_profit_stage=position.take_profit_stage,
                    holding_trade_days=position.holding_trade_days,
                    profit_protection_activated_at=(
                        position.profit_protection_activated_at
                    ),
                    trailing_stop_active_from=position.trailing_stop_active_from,
                    decision_batch_at=f"{trade_date}T15:00:00+08:00",
                ),
                position.market_regime,
            )
            position.highest_price = max(prior_highest_price, float(row["high"]))
            reason = ""
            raw_price = 0.0
            if float(row["low"]) <= position.stop_loss:
                reason = "HARD_STOP"
                raw_price = min(float(row["open"]), position.stop_loss)
            elif (
                stop.trailing_stop_price > 0
                and float(row["low"]) <= stop.trailing_stop_price
            ):
                reason = "TRAILING_STOP"
                raw_price = min(float(row["open"]), stop.trailing_stop_price)
            elif (
                position.take_profit_stage == 0
                and not position.profit_protection_activated_at
                and position.take_profit > 0
                and float(row["high"]) >= position.take_profit
            ):
                reason = "TAKE_PROFIT_1"
                raw_price = max(float(row["open"]), position.take_profit)
            if not reason:
                continue
            if float(row["open"]) <= float(row["limit_down"]):
                _blocked(result, "LIMIT_DOWN_SELL_BLOCKED")
                continue
            price = round(raw_price, 4)
            if reason == "TAKE_PROFIT_1" and position.take_profit_stage == 0:
                target_quantity = first_take_profit_target_qty(position.initial_quantity, 100)
                if target_quantity >= position.quantity:
                    if target_quantity == position.quantity:
                        position.profit_protection_activated_at = (
                            f"{trade_date}T15:00:00+08:00"
                        )
                        position.trailing_stop_active_from = (
                            f"{trade_date}T15:00:00.000001+08:00"
                        )
                    continue
                quantity = position.quantity - target_quantity
            else:
                quantity = position.quantity
            value = price * quantity
            execution_fees = _execution_fee_schedule(config, fees, row, price, quantity, "sell")
            breakdown = execution_fees.estimate("sell", Decimal(str(price)), quantity)
            fee = float(breakdown.total_yuan)
            entry_fee = _allocate_entry_fee(position, quantity)
            cash += value - fee
            pnl = round((price - position.entry_price) * quantity - fee - entry_fee, 2)
            result.trades.append(
                HistoricalTrade(
                    trade_date, trade_date, code, "sell", quantity, price, fee, reason, pnl,
                    holding_days=position.holding_trade_days, strategy_mode=position.mode,
                    market_regime=position.market_regime, industry=position.industry, theme=position.theme,
                    fee_schedule_version=execution_fees.version,
                    entry_fee_allocated_yuan=entry_fee,
                    **_fee_fields(breakdown),
                )
            )
            if quantity >= position.quantity:
                cooldown_until[code] = current_index + int(config.cooldown_days)
                del positions[code]
            else:
                position.quantity -= quantity
                position.take_profit_stage = 1

        for code in sorted(positions):
            position = positions[code]
            row = rows.get(code)
            if row is None or position.buy_date == trade_date:
                continue
            position.holding_trade_days += 1
            if position.holding_trade_days < int(config.min_holding_days):
                continue
            if config.market_risk_exit_enabled and current_market_state == "RISK_OFF":
                pending_sells.append(
                    PendingSell(trade_date, code, position.quantity, "MARKET_RISK_EXIT")
                )
                continue
            decision = evaluate_exit(
                PositionExitState(
                    code=code,
                    mode=position.mode,
                    initial_qty=position.initial_quantity,
                    current_qty=position.quantity,
                    entry_price=position.entry_price,
                    initial_stop_price=position.stop_loss,
                    highest_price=position.highest_price,
                    atr14=position.atr14,
                    take_profit_stage=position.take_profit_stage,
                    holding_trade_days=position.holding_trade_days,
                    profit_protection_activated_at=(
                        position.profit_protection_activated_at
                    ),
                    trailing_stop_active_from=position.trailing_stop_active_from,
                    decision_batch_at=f"{trade_date}T15:00:00+08:00",
                ),
                float(row["close"]),
                position.market_regime,
            )
            if decision.action == "activate_profit_protection":
                position.profit_protection_activated_at = (
                    f"{trade_date}T15:00:00+08:00"
                )
                position.trailing_stop_active_from = (
                    f"{trade_date}T15:00:00.000001+08:00"
                )
            elif decision.action != "hold":
                target = decision.target_qty if decision.target_qty is not None else position.quantity
                quantity = position.quantity if target == 0 else max(position.quantity - target, 0)
                if quantity:
                    pending_sells.append(PendingSell(trade_date, code, quantity, decision.action.upper()))

        candidates = generate_daily_candidates(
            store,
            dataset_id,
            trade_date,
            mode=config.mode,
            parameter_version=config.parameter_version,
            min_score=config.min_score,
            caution_min_score=config.caution_min_score,
            require_trend_confirmation=config.require_trend_confirmation,
            require_breakout_confirmation=config.require_breakout_confirmation,
            max_chase_atr=config.max_chase_atr,
            max_entry_score=config.max_entry_score,
            alpha_profile=config.alpha_profile,
            benchmark_closes=config.benchmark_closes,
            cooldown_codes={
                code for code, until in cooldown_until.items()
                if current_index <= int(until)
            },
        )
        candidate_codes = {candidate.code for candidate in candidates}
        for code in list(signal_streak):
            if code not in candidate_codes:
                del signal_streak[code]
        for code in candidate_codes:
            signal_streak[code] = signal_streak.get(code, 0) + (1 if code in previous_candidate_codes else 0)
            if code not in previous_candidate_codes:
                signal_streak[code] = 1
        confirmed = [
            candidate for candidate in candidates
            if signal_streak.get(candidate.code, 0) >= config.signal_confirmation_days
        ]
        previous_candidate_codes = candidate_codes
        pending = [PendingOrder(trade_date, candidate) for candidate in confirmed]
        result.equity.append(
            EquityPoint(trade_date, round(_account_value(cash, positions, rows), 2), round(cash, 2))
        )
    result.metadata.update(
        {
            "fee_schedule_version": fees.version,
            "fee_schedule_sha256": fees.contract_sha256,
            "entry_fee_schedule_version": config.resolved_entry_fee_schedule().version,
            "entry_fee_schedule_sha256": config.resolved_entry_fee_schedule().contract_sha256,
            "cooldown_days": int(config.cooldown_days),
            "max_new_positions_per_day": int(config.max_new_positions_per_day),
            "caution_min_score": float(config.caution_min_score),
            "require_trend_confirmation": bool(config.require_trend_confirmation),
            "require_breakout_confirmation": bool(config.require_breakout_confirmation),
            "max_chase_atr": float(config.max_chase_atr),
            "max_entry_score": float(config.max_entry_score),
            "signal_confirmation_days": int(config.signal_confirmation_days),
            "max_portfolio_risk_pct": float(config.max_portfolio_risk_pct),
            "max_same_industry_positions": int(config.max_same_industry_positions),
            "max_pairwise_correlation": float(config.max_pairwise_correlation),
            "market_risk_exit_enabled": bool(config.market_risk_exit_enabled),
            "local_entry_gates_enabled": bool(config.local_entry_gates_enabled),
            "alpha_profile": config.alpha_profile,
            "benchmark_data_present": bool(config.benchmark_closes),
            "slippage_model": config.slippage_model,
            "max_participation_pct": float(config.max_participation_pct),
            "min_holding_days": int(config.min_holding_days),
            "local_entry_policy_sha256": local_policy_for_config(config).policy_sha256,
            "fee_components": {
                key: round(sum(trade.fee_components[key] for trade in result.trades), 2)
                for key in (
                    "commission_yuan",
                    "stamp_tax_yuan",
                    "transfer_fee_yuan",
                    "other_fee_yuan",
                    "slippage_yuan",
                )
            },
        }
    )
    return result


def _fee_fields(value: FeeBreakdown) -> dict[str, float]:
    return {key: float(amount) for key, amount in value.components_dict().items()}


def _allocate_entry_fee(position: HistoricalPosition, quantity: int) -> float:
    if quantity >= position.quantity:
        allocated = position.entry_fee_remaining_yuan
    else:
        allocated = round(
            position.entry_fee_remaining_yuan * quantity / position.quantity, 2
        )
    position.entry_fee_remaining_yuan = round(
        position.entry_fee_remaining_yuan - allocated, 2
    )
    return allocated


def _account_value(cash: float, positions: dict[str, HistoricalPosition], rows: dict[str, dict]) -> float:
    return cash + sum(
        position.quantity * float(rows.get(code, {}).get("close", position.entry_price))
        for code, position in positions.items()
    )


def _prior_returns(store, dataset_id, code, decision_date):
    history = store.history_until(dataset_id, code, decision_date, 20)
    return {
        str(bar["trade_date"]): float(bar["close"]) / float(bar["prev_close"]) - 1
        for bar in history if float(bar["prev_close"]) > 0
    }


def local_policy_for_config(config):
    return LocalEntryPolicy(
        max_portfolio_risk_pct=config.max_portfolio_risk_pct,
        max_same_industry_positions=config.max_same_industry_positions,
        max_pairwise_correlation=config.max_pairwise_correlation,
    )


def _candidate_correlated_with_positions(candidate, positions, threshold):
    candidate_returns = candidate.evidence.get("recent_returns", {})
    if not candidate_returns:
        return False
    for position in positions.values():
        correlation = aligned_correlation(candidate_returns, position.recent_returns, 5)
        if correlation is not None and correlation >= threshold:
            return True
    return False


def _blocked(result: HistoricalBacktestResult, reason: str) -> None:
    result.blocked_counts[reason] = result.blocked_counts.get(reason, 0) + 1


def compute_metrics(
    equity: Iterable[EquityPoint], trades: Iterable[HistoricalTrade]
) -> BacktestMetrics:
    points = list(equity)
    rows = list(trades)
    values = [point.equity for point in points]
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values)) if values[index - 1]]
    net_return = values[-1] / values[0] - 1 if len(values) >= 2 and values[0] else 0.0
    annualized = (1 + net_return) ** (252 / max(len(returns), 1)) - 1 if 1 + net_return > 0 else -1.0
    peak = values[0] if values else 0.0
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    closed = [trade for trade in rows if trade.action == "sell" and trade.pnl is not None]
    pnls = [float(trade.pnl) for trade in closed]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = gross_win / gross_loss if gross_loss else (math.inf if gross_win else 0.0)
    sorted_returns = sorted(returns)
    tail_count = max(1, math.ceil(len(sorted_returns) * 0.05)) if sorted_returns else 0
    tail_loss = -sum(sorted_returns[:tail_count]) / tail_count if tail_count else 0.0
    average_equity = sum(values) / len(values) if values else 0.0
    turnover = sum(trade.price * trade.quantity for trade in rows) / average_equity if average_equity else 0.0
    remaining = list(pnls)
    for value in sorted((item for item in remaining if item > 0), reverse=True)[:3]:
        remaining.remove(value)
    robust_wins = sum(value for value in remaining if value > 0)
    robust_loss = -sum(value for value in remaining if value < 0)
    robust_pf = robust_wins / robust_loss if robust_loss else (math.inf if robust_wins else 0.0)
    return BacktestMetrics(
        net_return=net_return,
        annualized_return=annualized,
        max_drawdown=max_drawdown,
        volatility=volatility,
        calmar=annualized / max_drawdown if max_drawdown else 0.0,
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        average_win=sum(wins) / len(wins) if wins else 0.0,
        average_loss=sum(losses) / len(losses) if losses else 0.0,
        profit_factor=profit_factor,
        tail_loss_5pct=tail_loss,
        turnover=turnover,
        average_holding_days=sum(trade.holding_days for trade in closed) / len(closed) if closed else 0.0,
        net_profit_without_top3=sum(remaining),
        profit_factor_without_top3=robust_pf,
    )


def build_walk_forward_windows(trade_dates: Iterable[str], count: int = 3) -> list[WalkForwardWindow]:
    dates = sorted(set(trade_dates))
    if count <= 0 or len(dates) < count + 1:
        return []
    size = max(1, len(dates) // (count + 1))
    windows = []
    for index in range(count):
        validation_start_index = size * (index + 1)
        validation_end_index = size * (index + 2) - 1 if index < count - 1 else len(dates) - 1
        if validation_start_index >= len(dates):
            break
        windows.append(
            WalkForwardWindow(
                training_start=dates[0],
                training_end=dates[validation_start_index - 1],
                validation_start=dates[validation_start_index],
                validation_end=dates[min(validation_end_index, len(dates) - 1)],
            )
        )
    return windows


def _result_metrics(result: HistoricalBacktestResult) -> dict[str, object]:
    """Return bounded, JSON-friendly metrics for a research fold."""
    metrics = asdict(compute_metrics(result.equity, result.trades))
    metrics.update(
        {
            "trade_count": len(result.trades),
            "closed_trade_count": sum(
                1 for trade in result.trades if trade.action == "sell" and trade.pnl is not None
            ),
            "equity_points": len(result.equity),
            "blocked_counts": dict(sorted(result.blocked_counts.items())),
        }
    )
    return metrics


def _walk_forward_objective(metrics: Mapping[str, object]) -> float:
    """Select parameters using training data only and penalise drawdown."""
    return float(metrics.get("net_return", 0.0) or 0.0) - float(
        metrics.get("max_drawdown", 0.0) or 0.0
    )


def _aggregate_walk_forward_metrics(folds: Iterable[Mapping[str, object]]) -> dict[str, object]:
    rows = [dict(row) for row in folds]
    if not rows:
        return {
            "fold_count": 0,
            "trade_count": 0,
            "closed_trade_count": 0,
            "compounded_net_return": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "average_holding_days": 0.0,
        }
    returns = [float(row.get("net_return", 0.0) or 0.0) for row in rows]
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
    closed_count = sum(int(row.get("closed_trade_count", 0) or 0) for row in rows)
    gross_profit = sum(
        float(row.get("average_win", 0.0) or 0.0)
        * int(row.get("win_rate", 0.0) * row.get("closed_trade_count", 0) or 0)
        for row in rows
    )
    gross_loss = sum(
        abs(float(row.get("average_loss", 0.0) or 0.0))
        * max(
            0,
            int(row.get("closed_trade_count", 0) or 0)
            - int(row.get("win_rate", 0.0) * row.get("closed_trade_count", 0) or 0),
        )
        for row in rows
    )
    holding_days = sum(
        float(row.get("average_holding_days", 0.0) or 0.0)
        * int(row.get("closed_trade_count", 0) or 0)
        for row in rows
    )
    return {
        "fold_count": len(rows),
        "trade_count": sum(int(row.get("trade_count", 0) or 0) for row in rows),
        "closed_trade_count": closed_count,
        "compounded_net_return": compounded - 1.0,
        "max_drawdown": max(float(row.get("max_drawdown", 0.0) or 0.0) for row in rows),
        "profit_factor": gross_profit / gross_loss if gross_loss else (math.inf if gross_profit else 0.0),
        "average_holding_days": holding_days / closed_count if closed_count else 0.0,
    }


def run_walk_forward(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    base_config: HistoricalBacktestConfig,
    *,
    folds: int = 3,
    holdout_days: int = 20,
    min_score_grid: Iterable[float] | None = None,
    max_positions_grid: Iterable[int] | None = None,
    alpha_profile_grid: Iterable[str] | None = None,
    slippage_model_grid: Iterable[str] | None = None,
) -> dict[str, object]:
    """Run train-only parameter selection, rolling validation and final holdout.

    The function deliberately keeps each fold independent and never writes a
    parameter as approved or active.  The final holdout is evaluated using the
    configuration selected by the last training fold, so it remains unseen
    during parameter selection.
    """
    if folds < 3:
        raise HistoricalDataValidationError("WALK_FORWARD_REQUIRES_THREE_FOLDS")
    if holdout_days < 0:
        raise HistoricalDataValidationError("HOLDOUT_DAYS_MUST_NOT_BE_NEGATIVE")
    dates = store.trade_dates(dataset_id, start, end)
    if holdout_days >= len(dates):
        raise HistoricalDataValidationError("HOLDOUT_EXCEEDS_DATASET_WINDOW")
    holdout = dates[-holdout_days:] if holdout_days else []
    research_dates = dates[:-holdout_days] if holdout_days else dates
    minimum_dates = 21 + folds * 5
    if len(research_dates) < minimum_dates:
        raise HistoricalDataValidationError(
            f"INSUFFICIENT_WALK_FORWARD_DATES:{len(research_dates)}<{minimum_dates}"
        )
    windows = build_walk_forward_windows(research_dates, count=folds)
    if len(windows) != folds:
        raise HistoricalDataValidationError("INSUFFICIENT_WALK_FORWARD_WINDOWS")

    scores = sorted(
        {float(base_config.min_score), *(float(value) for value in (min_score_grid or ())) }
    )
    positions = sorted(
        {int(base_config.max_positions), *(int(value) for value in (max_positions_grid or ())) }
    )
    alpha_profiles = sorted({base_config.alpha_profile, *(str(value) for value in (alpha_profile_grid or ()))})
    slippage_models = sorted({base_config.slippage_model, *(str(value) for value in (slippage_model_grid or ()))})
    if not scores or any(not math.isfinite(value) for value in scores):
        raise HistoricalDataValidationError("INVALID_MIN_SCORE_GRID")
    if not positions or any(value <= 0 for value in positions):
        raise HistoricalDataValidationError("INVALID_MAX_POSITIONS_GRID")
    if any(value not in {"legacy", "relative_v1", "relative_v2"} for value in alpha_profiles):
        raise HistoricalDataValidationError("INVALID_ALPHA_PROFILE_GRID")
    if any(value not in {"fixed", "liquidity_v1"} for value in slippage_models):
        raise HistoricalDataValidationError("INVALID_SLIPPAGE_MODEL_GRID")

    fold_reports: list[dict[str, object]] = []
    selected_config = base_config
    for index, window in enumerate(windows, start=1):
        training_candidates: list[dict[str, object]] = []
        for max_positions in positions:
            for min_score in scores:
                for alpha_profile in alpha_profiles:
                    for slippage_model in slippage_models:
                        candidate_config = replace(
                            base_config,
                            max_positions=max_positions,
                            min_score=min_score,
                            alpha_profile=alpha_profile,
                            slippage_model=slippage_model,
                            parameter_version=f"{base_config.parameter_version}:wf{index}:train",
                        )
                        training_result = run_historical_backtest(
                            store,
                            dataset_id,
                            window.training_start,
                            window.training_end,
                            candidate_config,
                        )
                        training_metrics = _result_metrics(training_result)
                        training_candidates.append(
                            {
                                "max_positions": max_positions,
                                "min_score": min_score,
                                "alpha_profile": alpha_profile,
                                "slippage_model": slippage_model,
                                "objective": _walk_forward_objective(training_metrics),
                                "metrics": training_metrics,
                            }
                        )
        selected = max(
            training_candidates,
            key=lambda row: (
                float(row["objective"]),
                float(row["metrics"].get("net_return", 0.0) or 0.0),
                -float(row["metrics"].get("max_drawdown", 0.0) or 0.0),
                -int(row["max_positions"]),
                -float(row["min_score"]),
            ),
        )
        selected_config = replace(
            base_config,
            max_positions=int(selected["max_positions"]),
            min_score=float(selected["min_score"]),
            alpha_profile=str(selected["alpha_profile"]),
            slippage_model=str(selected["slippage_model"]),
            parameter_version=f"{base_config.parameter_version}:wf{index}:validation",
        )
        validation_result = run_historical_backtest(
            store,
            dataset_id,
            window.validation_start,
            window.validation_end,
            selected_config,
        )
        fold_reports.append(
            {
                "fold": index,
                "window": asdict(window),
                "selected_parameters": {
                    "max_positions": selected_config.max_positions,
                    "min_score": selected_config.min_score,
                    "alpha_profile": selected_config.alpha_profile,
                    "slippage_model": selected_config.slippage_model,
                },
                "training_candidates": training_candidates,
                "validation": _result_metrics(validation_result),
            }
        )

    holdout_report: dict[str, object] | None = None
    if holdout:
        holdout_config = replace(
            selected_config,
            parameter_version=f"{base_config.parameter_version}:holdout",
        )
        holdout_result = run_historical_backtest(
            store, dataset_id, holdout[0], holdout[-1], holdout_config
        )
        holdout_report = {
            "window": {"start": holdout[0], "end": holdout[-1]},
            "selected_parameters": {
                "max_positions": holdout_config.max_positions,
                "min_score": holdout_config.min_score,
                "alpha_profile": holdout_config.alpha_profile,
                "slippage_model": holdout_config.slippage_model,
            },
            "metrics": _result_metrics(holdout_result),
        }

    validation_rows = [dict(row["validation"]) for row in fold_reports]
    evidence_ready = all(
        int(row.get("closed_trade_count", 0) or 0) > 0 for row in validation_rows
    ) and (holdout_report is None or int(holdout_report["metrics"].get("closed_trade_count", 0) or 0) > 0)
    return {
        "status": "complete" if evidence_ready else "insufficient_evidence",
        "dataset_id": str(dataset_id),
        "dataset_hash": store.dataset_hash(dataset_id),
        "mode": base_config.mode,
        "base_parameters": {
            "max_positions": base_config.max_positions,
            "min_score": base_config.min_score,
            "alpha_profile": base_config.alpha_profile,
            "slippage_model": base_config.slippage_model,
            "parameter_version": base_config.parameter_version,
        },
        "folds": fold_reports,
        "validation_aggregate": _aggregate_walk_forward_metrics(validation_rows),
        "holdout": holdout_report,
        "evidence_ready": evidence_ready,
        "rules": {
            "training_selection_objective": "net_return_minus_max_drawdown",
            "holdout_is_unseen": True,
            "parameter_approval": "not_performed",
            "minimum_training_warmup_days": 21,
        },
    }


def compare_results(
    baseline: HistoricalBacktestResult, candidate: HistoricalBacktestResult
) -> dict[str, object]:
    contract_keys = (
        "dataset_hash",
        "window",
        "fees",
        "slippage",
        "capital",
        "strategy_version",
        "parameter_family_count",
        "fee_schedule_version",
        "fee_schedule_sha256",
    )
    mismatches = [key for key in contract_keys if baseline.metadata.get(key) != candidate.metadata.get(key)]
    fee_hashes = (
        baseline.metadata.get("fee_schedule_sha256"),
        candidate.metadata.get("fee_schedule_sha256"),
    )
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
        for value in fee_hashes
    ) and "fee_schedule_sha256" not in mismatches:
        mismatches.append("fee_schedule_sha256")
    if mismatches:
        return {"status": "COMPARISON_CONTRACT_MISMATCH", "mismatches": mismatches}
    left = compute_metrics(baseline.equity, baseline.trades)
    right = compute_metrics(candidate.equity, candidate.trades)
    return {
        "status": "COMPARABLE",
        "net_return_delta": right.net_return - left.net_return,
        "max_drawdown_delta": right.max_drawdown - left.max_drawdown,
        "profit_factor_delta": right.profit_factor - left.profit_factor,
    }


def group_metrics(trades: Iterable[HistoricalTrade], fields: Iterable[str]) -> dict[str, dict]:
    rows = list(trades)
    result: dict[str, dict] = {}
    for field_name in fields:
        buckets: dict[str, list[HistoricalTrade]] = {}
        for trade in rows:
            if field_name == "score_band":
                value = f"{int(trade.score // 10) * 10}-{int(trade.score // 10) * 10 + 9}"
            else:
                value = str(getattr(trade, field_name, "unknown") or "unknown")
            buckets.setdefault(value, []).append(trade)
        ordered = sorted(buckets, key=lambda key: (-len(buckets[key]), key))
        kept = ordered[:20]
        grouped = {
            key: {"count": len(buckets[key]), "net_pnl": sum(float(row.pnl or 0) for row in buckets[key])}
            for key in kept
        }
        overflow = [row for key in ordered[20:] for row in buckets[key]]
        if overflow:
            grouped["other"] = {"count": len(overflow), "net_pnl": sum(float(row.pnl or 0) for row in overflow)}
        result[field_name] = grouped
    return result


def sensitivity_matrix(
    result_factory: Callable[[HistoricalBacktestConfig], HistoricalBacktestResult],
    base_config: HistoricalBacktestConfig,
) -> dict[str, HistoricalBacktestResult]:
    fees = base_config.resolved_fee_schedule()
    variants = {
        "zero_slippage": replace(
            base_config,
            fee_schedule=fees.derive_variant(
                "zero-slippage",
                buy_slippage_rate=Decimal("0"),
                sell_slippage_rate=Decimal("0"),
            ),
        ),
        "base": base_config,
        "double_slippage": replace(
            base_config,
            fee_schedule=fees.derive_variant(
                "double-slippage",
                buy_slippage_rate=fees.buy_slippage_rate * 2,
                sell_slippage_rate=fees.sell_slippage_rate * 2,
            ),
        ),
        "double_fees": replace(
            base_config,
            fee_schedule=fees.derive_variant(
                "double-fees",
                buy_commission_rate=fees.buy_commission_rate * 2,
                sell_commission_rate=fees.sell_commission_rate * 2,
                buy_minimum_commission_yuan=fees.buy_minimum_commission_yuan * 2,
                sell_minimum_commission_yuan=fees.sell_minimum_commission_yuan * 2,
                stamp_tax_rate=fees.stamp_tax_rate * 2,
                transfer_fee_rate=fees.transfer_fee_rate * 2,
                other_fee_rate=fees.other_fee_rate * 2,
            ),
        ),
    }
    return {name: result_factory(config) for name, config in variants.items()}


def _publish_atomic(output_dir: Path, files: dict[str, str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = {
        name: (output_dir / name).read_bytes() if (output_dir / name).exists() else None
        for name in files
    }
    temporary: list[Path] = []
    replaced: list[str] = []
    try:
        for name, content in files.items():
            temp = output_dir / f".{name}.tmp"
            temp.write_text(content, encoding="utf-8", newline="")
            temporary.append(temp)
        for name, temp in zip(files, temporary):
            temp.replace(output_dir / name)
            replaced.append(name)
    except Exception:
        for name in replaced:
            target = output_dir / name
            old = previous[name]
            if old is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(old)
        raise
    finally:
        for temp in temporary:
            temp.unlink(missing_ok=True)


def _json(value: object) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ) + "\n"


def _quality_payload(report) -> dict[str, object]:
    return asdict(report)


def _decision_replay_payload(
    replay: DecisionTimeReplayResult,
) -> dict[str, object]:
    return {
        "status": "complete",
        "replay_mode": "decision_time",
        "dataset_id": replay.dataset_id,
        "dataset_sha256": replay.dataset_sha256,
        "implementation_sha256": _implementation_hash(),
        "start_at": replay.start_at,
        "end_at": replay.end_at,
        "strategy_config_sha256": replay.strategy_config_sha256,
        "batch_count": len(replay.batches),
        "candidate_count": replay.candidate_count,
        "selected_count": replay.selected_count,
        "batches": [
            {
                "decision_at": batch.decision_at,
                "cohort_sha256": batch.cohort_sha256,
                "candidate_count": batch.candidate_count,
                "selected_count": batch.selected_count,
            }
            for batch in replay.batches
        ],
    }


def _implementation_paths() -> tuple[Path, ...]:
    """Return the shared implementation files that affect historical results."""
    root = Path(__file__).parent
    names = (
        # These modules supply execution costs and canonical contract
        # serialization.  They are part of the historical result identity.
        "config.py",
        "execution_contracts.py",
        "candidate_core.py",
        "exit_policy.py",
        "trade_safety.py",
        "historical_data.py",
        "historical_strategy.py",
        "benchmark_data.py",
        "historical_backtest.py",
        "local_entry_policy.py",
        "ml_contracts.py",
    )
    return tuple(root / name for name in names if (root / name).is_file())


def _implementation_hash(paths: Iterable[Path] | None = None) -> str:
    selected = list(paths) if paths is not None else list(_implementation_paths())
    digest = hashlib.sha256()
    for path in sorted(selected, key=lambda item: item.as_posix()):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_id(store: HistoricalStore, args, config: HistoricalBacktestConfig) -> str:
    payload = {
        "dataset_hash": store.dataset_hash(args.dataset),
        "start": args.start,
        "end": args.end,
        "mode": args.mode,
        "strategy_version": args.strategy_version,
        "code_hash": _implementation_hash(),
        "parameter_version": config.parameter_version,
        # Include the complete normalized config.  In particular, max_positions
        # and min_score must change the identity of a run.
        "config": _config_payload(config),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]


def _config_payload(config: HistoricalBacktestConfig) -> dict[str, object]:
    payload = asdict(config)
    payload["fee_schedule"] = config.resolved_fee_schedule().to_dict()
    payload["entry_fee_schedule"] = config.resolved_entry_fee_schedule().to_dict()
    return payload


def _json_safe(value: object) -> object:
    """Convert non-finite floats to JSON null instead of invalid JSON tokens."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _strict_json(value: object) -> str:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, allow_nan=False
    )


def _result_sha256(result: HistoricalBacktestResult) -> str:
    payload = {
        "equity": [asdict(point) for point in result.equity],
        "trades": [asdict(trade) for trade in result.trades],
        "blocked_counts": result.blocked_counts,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _persist_failure(
    store: HistoricalStore,
    run_id: str,
    args,
    config: HistoricalBacktestConfig,
    error: Exception,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    message = " ".join(str(error).split())[:240]
    config_payload = {
        **_config_payload(config),
        "strategy_version": args.strategy_version,
        "code_hash": _implementation_hash(),
    }
    with store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO backtest_runs "
            "(run_id, dataset_id, dataset_hash, start_date, end_date, mode, config_json, status, "
            "error, summary_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, '{}', ?)",
            (
                run_id, args.dataset, store.dataset_hash(args.dataset), args.start, args.end,
                args.mode, json.dumps(config_payload, sort_keys=True), message, now,
            ),
        )


def _persist_result(
    store: HistoricalStore,
    run_id: str,
    dataset_id: str,
    start: str,
    end: str,
    config: HistoricalBacktestConfig,
    strategy_version: str,
    result: HistoricalBacktestResult,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metrics = asdict(compute_metrics(result.equity, result.trades))
    config_payload = {
        **_config_payload(config),
        "strategy_version": strategy_version,
        "code_hash": _implementation_hash(),
    }
    with store.transaction() as connection:
        existing = connection.execute(
            "SELECT status FROM backtest_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing and str(existing[0]) == "complete":
            return
        if existing:
            # A failed attempt is retryable with the same deterministic
            # identity. Remove any partial children before replacing it.
            connection.execute("DELETE FROM backtest_runs WHERE run_id = ?", (run_id,))
        summary = {"metrics": metrics, "result_sha256": _result_sha256(result)}
        connection.execute(
            "INSERT INTO backtest_runs "
            "(run_id, dataset_id, dataset_hash, start_date, end_date, mode, config_json, status, "
            "summary_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?, ?)",
            (
                run_id, dataset_id, store.dataset_hash(dataset_id), start, end,
                config.mode, _strict_json(config_payload), _strict_json(summary), now, now,
            ),
        )
        connection.executemany(
            "INSERT INTO backtest_equity(run_id, trade_date, equity, cash) VALUES (?, ?, ?, ?)",
            [(run_id, point.trade_date, point.equity, point.cash) for point in result.equity],
        )
        connection.executemany(
            "INSERT INTO backtest_trades "
            "(trade_id, run_id, decision_date, trade_date, code, action, quantity, price, fee, reason, pnl, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"{run_id}-{index:06d}", run_id, trade.decision_date, trade.trade_date,
                    trade.code, trade.action, trade.quantity, trade.price, trade.fee, trade.reason,
                    trade.pnl, json.dumps(asdict(trade), ensure_ascii=False, sort_keys=True),
                )
                for index, trade in enumerate(result.trades)
            ],
        )


def _csv_text(rows: list[dict], fields: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _load_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"INVALID_{label.upper().replace(' ', '_')}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _load_json_rows(path: Path) -> list[dict[str, object]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("INVALID_STRICT_IMPORT_FILE") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise ValueError("INVALID_STRICT_IMPORT_JSON") from exc
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        value = value["rows"]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("strict import must contain a JSON row array")
    return [dict(row) for row in value]


def _benchmark_closes_from_args(args) -> dict[str, float] | None:
    """Load only the selected independent benchmark for experimental profiles."""
    path = str(getattr(args, "benchmark_csv", "") or "").strip()
    if not path:
        if getattr(args, "alpha_profile", "legacy") == "relative_v2":
            raise ValueError("BENCHMARK_CSV_REQUIRED_FOR_RELATIVE_V2")
        return None
    values = load_benchmark_csv(path)
    name = str(getattr(args, "benchmark", "") or "").strip()
    if not name:
        name = sorted(values)[0] if values else ""
    if name not in values:
        raise ValueError("BENCHMARK_NOT_FOUND")
    return values[name]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Point-in-time A-share historical backtest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import")
    importer.add_argument("--db", required=True)
    importer.add_argument("--dataset", required=True)
    importer.add_argument(
        "--kind",
        required=True,
        choices=(
            "bars", "status", "universe", "features",
            "decision_candidates", "candidate_prices",
        ),
    )
    importer.add_argument("--file", required=True)
    importer.add_argument("--source", choices=("joinquant", "akshare"))
    importer.add_argument("--adjust", default="raw")
    importer.add_argument("--manifest")
    replay = subparsers.add_parser("replay")
    replay.add_argument("--db", required=True)
    replay.add_argument("--dataset", required=True)
    replay.add_argument("--start-at", required=True)
    replay.add_argument("--end-at", required=True)
    replay.add_argument("--output-dir", required=True)
    replay.add_argument("--expected-dataset-hash", required=True)
    replay.add_argument("--strategy-version", required=True)
    replay.add_argument("--parameter-version", required=True)
    replay.add_argument("--feature-schema-version", required=True)
    replay.add_argument("--market-data-version", required=True)
    replay.add_argument("--code-hash", required=True)
    replay.add_argument("--generator-hash", required=True)
    for command in ("validate", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--db", required=True)
        child.add_argument("--dataset", required=True)
        child.add_argument("--start", required=True)
        child.add_argument("--end", required=True)
        child.add_argument("--mode", required=True, choices=("strict", "price_core"))
        child.add_argument("--output-dir", required=True)
        child.add_argument("--strategy-version", default="historical-v1")
        child.add_argument("--parameter-version", default="v1")
        child.add_argument("--capital", type=float, default=100_000)
        child.add_argument("--max-positions", type=int, default=8)
        child.add_argument("--min-score", type=float, default=75.0)
        child.add_argument("--caution-min-score", type=float, default=85.0)
        child.add_argument("--cooldown-days", type=int, default=3)
        child.add_argument("--max-new-positions-per-day", type=int, default=10)
        child.add_argument("--require-trend-confirmation", action="store_true")
        child.add_argument("--require-breakout-confirmation", action="store_true")
        child.add_argument("--max-chase-atr", type=float, default=0.0)
        child.add_argument("--max-entry-score", type=float, default=100.0)
        child.add_argument("--signal-confirmation-days", type=int, default=1)
        child.add_argument("--max-portfolio-risk-pct", type=float, default=4.0)
        child.add_argument("--max-same-industry-positions", type=int, default=2)
        child.add_argument("--max-pairwise-correlation", type=float, default=0.9)
        child.add_argument("--market-risk-exit", action="store_true")
        child.add_argument("--local-entry-gates", action="store_true")
        child.add_argument("--alpha-profile", choices=("legacy", "relative_v1", "relative_v2"), default="legacy")
        child.add_argument("--benchmark-csv", default="", help="canonical independent benchmark CSV (required by relative_v2)")
        child.add_argument("--benchmark", default="", help="benchmark name in --benchmark-csv")
        child.add_argument("--slippage-model", choices=("fixed", "liquidity_v1"), default="fixed")
        child.add_argument("--max-participation-pct", type=float, default=100.0)
        child.add_argument("--min-holding-days", type=int, default=0)
    walk_forward = subparsers.add_parser(
        "walk-forward",
        help="select parameters on rolling training windows and evaluate unseen windows",
    )
    walk_forward.add_argument("--db", required=True)
    walk_forward.add_argument("--dataset", required=True)
    walk_forward.add_argument("--start", required=True)
    walk_forward.add_argument("--end", required=True)
    walk_forward.add_argument("--mode", required=True, choices=("strict", "price_core"))
    walk_forward.add_argument("--output-dir", required=True)
    walk_forward.add_argument("--strategy-version", default="historical-v1")
    walk_forward.add_argument("--parameter-version", default="v1")
    walk_forward.add_argument("--capital", type=float, default=100_000)
    walk_forward.add_argument("--max-positions", type=int, default=8)
    walk_forward.add_argument("--min-score", type=float, default=75.0)
    walk_forward.add_argument("--caution-min-score", type=float, default=85.0)
    walk_forward.add_argument("--cooldown-days", type=int, default=3)
    walk_forward.add_argument("--max-new-positions-per-day", type=int, default=10)
    walk_forward.add_argument("--require-trend-confirmation", action="store_true")
    walk_forward.add_argument("--require-breakout-confirmation", action="store_true")
    walk_forward.add_argument("--max-chase-atr", type=float, default=0.0)
    walk_forward.add_argument("--max-entry-score", type=float, default=100.0)
    walk_forward.add_argument("--signal-confirmation-days", type=int, default=1)
    walk_forward.add_argument("--max-portfolio-risk-pct", type=float, default=4.0)
    walk_forward.add_argument("--max-same-industry-positions", type=int, default=2)
    walk_forward.add_argument("--max-pairwise-correlation", type=float, default=0.9)
    walk_forward.add_argument("--market-risk-exit", action="store_true")
    walk_forward.add_argument("--local-entry-gates", action="store_true")
    walk_forward.add_argument("--alpha-profile", choices=("legacy", "relative_v1", "relative_v2"), default="legacy")
    walk_forward.add_argument("--benchmark-csv", default="", help="canonical independent benchmark CSV (required by relative_v2)")
    walk_forward.add_argument("--benchmark", default="", help="benchmark name in --benchmark-csv")
    walk_forward.add_argument("--slippage-model", choices=("fixed", "liquidity_v1"), default="fixed")
    walk_forward.add_argument("--max-participation-pct", type=float, default=100.0)
    walk_forward.add_argument("--min-holding-days", type=int, default=0)
    walk_forward.add_argument("--folds", type=int, default=3)
    walk_forward.add_argument("--holdout-days", type=int, default=20)
    walk_forward.add_argument(
        "--min-score-grid",
        default="",
        help="comma-separated training candidates, e.g. 70,75,80",
    )
    walk_forward.add_argument(
        "--max-positions-grid",
        default="",
        help="comma-separated training candidates, e.g. 4,8,12",
    )
    walk_forward.add_argument(
        "--alpha-profile-grid",
        default="",
        help="comma-separated training profiles, e.g. legacy,relative_v1",
    )
    walk_forward.add_argument(
        "--slippage-model-grid",
        default="",
        help="comma-separated execution models, e.g. fixed,liquidity_v1",
    )
    compare = subparsers.add_parser("compare")
    compare.add_argument("--db", required=True)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = HistoricalStore(Path(args.db))
    store.initialize()
    if args.command == "import":
        if args.kind in {"decision_candidates", "candidate_prices"}:
            if not args.manifest:
                raise ValueError("STRICT_MANIFEST_REQUIRED")
            rows = _load_json_rows(Path(args.file))
            manifest = _load_json_object(Path(args.manifest), "strict manifest")
            if str(manifest.get("dataset_id") or "") != str(args.dataset):
                raise ValueError("STRICT_MANIFEST_DATASET_MISMATCH")
            changed = (
                store.import_candidate_cohorts(rows, manifest=manifest)
                if args.kind == "decision_candidates"
                else store.import_candidate_prices(rows, manifest=manifest)
            )
            print(changed)
            return 0
        if args.source is None:
            raise ValueError("IMPORT_SOURCE_REQUIRED")
        print(store.import_csv(
            args.dataset, args.kind, Path(args.file), args.source, args.adjust,
        ))
        return 0
    if args.command == "replay":
        output_file = "historical_decision_replay_latest.json"
        try:
            replay = run_decision_time_replay(
                store,
                args.dataset,
                args.start_at,
                args.end_at,
                strategy_config={
                    "strategy_version": args.strategy_version,
                    "parameter_version": args.parameter_version,
                    "feature_schema_version": args.feature_schema_version,
                    "market_data_version": args.market_data_version,
                    "code_hash": args.code_hash,
                    "generator_hash": args.generator_hash,
                },
                expected_dataset_hash=args.expected_dataset_hash,
            )
        except HistoricalDataValidationError as error:
            _publish_atomic(
                Path(args.output_dir),
                {
                    output_file: _json(
                        {
                            "status": "rejected",
                            "replay_mode": "decision_time",
                            "dataset_id": str(args.dataset),
                            "error": " ".join(str(error).split())[:240],
                        }
                    )
                },
            )
            return 2
        _publish_atomic(
            Path(args.output_dir),
            {output_file: _json(_decision_replay_payload(replay))},
        )
        return 0
    if args.command == "compare":
        with store.connect() as connection:
            rows = {
                row["run_id"]: dict(row)
                for row in connection.execute(
                    "SELECT run_id, dataset_hash, start_date, end_date, config_json, summary_json "
                    "FROM backtest_runs WHERE run_id IN (?, ?)", (args.baseline, args.candidate)
                )
            }
        if set(rows) != {args.baseline, args.candidate}:
            payload = {"status": "RUN_NOT_FOUND"}
            code = 2
        else:
            left, right = rows[args.baseline], rows[args.candidate]
            mismatches = [key for key in ("dataset_hash", "start_date", "end_date") if left[key] != right[key]]
            left_config = json.loads(left["config_json"])
            right_config = json.loads(right["config_json"])
            for key in (
                "initial_cash", "commission_rate", "minimum_commission", "stamp_tax_rate",
                "slippage_bps", "max_positions", "min_score", "mode", "fee_schedule",
                "caution_min_score", "cooldown_days", "max_new_positions_per_day",
                "strategy_version", "code_hash", "parameter_version",
                "require_trend_confirmation", "require_breakout_confirmation",
                "max_chase_atr", "max_entry_score", "signal_confirmation_days",
                "max_portfolio_risk_pct", "max_same_industry_positions",
                "max_pairwise_correlation",
                "market_risk_exit_enabled",
                "local_entry_gates_enabled",
                "alpha_profile",
                "slippage_model",
                "max_participation_pct",
                "min_holding_days",
                "entry_fee_schedule",
            ):
                if left_config.get(key) != right_config.get(key):
                    mismatches.append(f"config:{key}")
            payload = {"status": "COMPARISON_CONTRACT_MISMATCH", "mismatches": mismatches} if mismatches else {"status": "COMPARABLE", "baseline": json.loads(left["summary_json"]), "candidate": json.loads(right["summary_json"])}
            code = 0 if not mismatches else 2
        _publish_atomic(Path(args.output_dir), {"historical_backtest_compare.json": _json(payload)})
        return code

    quality = validate_dataset(store, args.dataset, args.start, args.end, args.mode, STRICT_FEATURES)
    if args.command == "walk-forward":
        report_name = "historical_walk_forward_latest.json"
        if not quality.accepted:
            _publish_atomic(
                Path(args.output_dir),
                {
                    "historical_backtest_quality.json": _json(_quality_payload(quality)),
                    report_name: _json(
                        {
                            "status": "rejected",
                            "reason": "DATASET_QUALITY_REJECTED",
                            "quality": _quality_payload(quality),
                        }
                    ),
                },
            )
            return 2
        try:
            min_score_grid = (
                [float(value.strip()) for value in args.min_score_grid.split(",") if value.strip()]
                if args.min_score_grid
                else None
            )
            max_positions_grid = (
                [int(value.strip()) for value in args.max_positions_grid.split(",") if value.strip()]
                if args.max_positions_grid
                else None
            )
            alpha_profile_grid = (
                [value.strip() for value in args.alpha_profile_grid.split(",") if value.strip()]
                if args.alpha_profile_grid
                else None
            )
            slippage_model_grid = (
                [value.strip() for value in args.slippage_model_grid.split(",") if value.strip()]
                if args.slippage_model_grid
                else None
            )
            report = run_walk_forward(
                store,
                args.dataset,
                args.start,
                args.end,
                HistoricalBacktestConfig(
                    initial_cash=args.capital,
                    mode=args.mode,
                    parameter_version=args.parameter_version,
                    max_positions=args.max_positions,
                    min_score=args.min_score,
                    caution_min_score=args.caution_min_score,
                    cooldown_days=args.cooldown_days,
                    max_new_positions_per_day=args.max_new_positions_per_day,
                    require_trend_confirmation=args.require_trend_confirmation,
                    require_breakout_confirmation=args.require_breakout_confirmation,
                    max_chase_atr=args.max_chase_atr,
                    max_entry_score=args.max_entry_score,
                    signal_confirmation_days=args.signal_confirmation_days,
                    max_portfolio_risk_pct=args.max_portfolio_risk_pct,
                    max_same_industry_positions=args.max_same_industry_positions,
                    max_pairwise_correlation=args.max_pairwise_correlation,
                    market_risk_exit_enabled=args.market_risk_exit,
                    local_entry_gates_enabled=args.local_entry_gates,
                    alpha_profile=args.alpha_profile,
                    benchmark_closes=_benchmark_closes_from_args(args),
                    slippage_model=args.slippage_model,
                    max_participation_pct=args.max_participation_pct,
                    min_holding_days=args.min_holding_days,
                ),
                folds=args.folds,
                holdout_days=args.holdout_days,
                min_score_grid=min_score_grid,
                max_positions_grid=max_positions_grid,
                alpha_profile_grid=alpha_profile_grid,
                slippage_model_grid=slippage_model_grid,
            )
        except (HistoricalDataValidationError, ValueError) as error:
            report = {
                "status": "rejected",
                "reason": " ".join(str(error).split())[:240],
                "dataset_id": args.dataset,
                "mode": args.mode,
                "quality": _quality_payload(quality),
            }
            _publish_atomic(
                Path(args.output_dir),
                {
                    "historical_backtest_quality.json": _json(_quality_payload(quality)),
                    report_name: _json(report),
                },
            )
            return 2
        _publish_atomic(
            Path(args.output_dir),
            {
                "historical_backtest_quality.json": _json(_quality_payload(quality)),
                report_name: _json(report),
            },
        )
        return 0 if report.get("status") == "complete" else 2
    quality_file = {"historical_backtest_quality.json": _json(_quality_payload(quality))}
    if args.command == "validate" or not quality.accepted:
        _publish_atomic(Path(args.output_dir), quality_file)
        return 0 if quality.accepted else 2

    config = HistoricalBacktestConfig(
        initial_cash=args.capital,
        mode=args.mode,
        parameter_version=args.parameter_version,
        max_positions=args.max_positions,
        min_score=args.min_score,
        caution_min_score=args.caution_min_score,
        cooldown_days=args.cooldown_days,
        max_new_positions_per_day=args.max_new_positions_per_day,
        require_trend_confirmation=args.require_trend_confirmation,
        require_breakout_confirmation=args.require_breakout_confirmation,
        max_chase_atr=args.max_chase_atr,
        max_entry_score=args.max_entry_score,
        signal_confirmation_days=args.signal_confirmation_days,
        max_portfolio_risk_pct=args.max_portfolio_risk_pct,
        max_same_industry_positions=args.max_same_industry_positions,
        max_pairwise_correlation=args.max_pairwise_correlation,
        market_risk_exit_enabled=args.market_risk_exit,
        local_entry_gates_enabled=args.local_entry_gates,
        alpha_profile=args.alpha_profile,
        benchmark_closes=_benchmark_closes_from_args(args),
        slippage_model=args.slippage_model,
        max_participation_pct=args.max_participation_pct,
        min_holding_days=args.min_holding_days,
    )
    run_id = _run_id(store, args, config)
    try:
        result = run_historical_backtest(store, args.dataset, args.start, args.end, config)
    except Exception as error:
        _persist_failure(store, run_id, args, config, error)
        _publish_atomic(Path(args.output_dir), quality_file)
        return 2
    result.metadata = {
        **result.metadata,
        "run_id": run_id,
        "dataset_hash": quality.input_hash,
        "window": f"{args.start}:{args.end}",
        "proxy_only": quality.proxy_only,
    }
    _persist_result(store, run_id, args.dataset, args.start, args.end, config, args.strategy_version, result)
    metrics = asdict(compute_metrics(result.equity, result.trades))
    equity_fields = ["trade_date", "equity", "cash"]
    trade_fields = [field.name for field in HistoricalTrade.__dataclass_fields__.values()]
    fee_report = json.dumps(result.metadata["fee_components"], sort_keys=True)
    files = {
        **quality_file,
        "historical_backtest_latest.md": f"# Historical Backtest\n\n- run_id: `{run_id}`\n- mode: `{args.mode}`\n- proxy_only: `{str(quality.proxy_only).lower()}`\n- fee_schedule: `{result.metadata['fee_schedule_version']}`\n- fee_components: `{fee_report}`\n- metrics: `{json.dumps(metrics, sort_keys=True)}`\n",
        "historical_backtest_equity.csv": _csv_text([asdict(row) for row in result.equity], equity_fields),
        "historical_backtest_trades.csv": _csv_text([asdict(row) for row in result.trades], trade_fields),
    }
    _publish_atomic(Path(args.output_dir), files)
    store.prune_runs(20)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
