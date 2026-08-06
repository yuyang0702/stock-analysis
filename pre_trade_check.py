from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    localcontext,
)
from types import MappingProxyType
from typing import Any, Mapping

from execution_contracts import (
    BrokerSnapshot,
    ExecutionIntent,
    FeeSchedule,
    InstrumentRules,
    PreTradeResult,
    QuoteSnapshot,
    StrategyOrderCandidate,
    UNCATEGORIZED,
    canonical_sha256,
)
from position_sizing import CapacityBudget, allocate_buy_quantity


ZERO = Decimal("0")
A_SHARE_TIMEZONE = timezone(timedelta(hours=8))


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite Decimal") from error
    if not result.is_finite() or result < ZERO or (positive and result <= ZERO):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} Decimal")
    return result


def _count(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be {qualifier}")
    return value


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _text_map(value: Mapping[str, object], name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if (
            not isinstance(raw_key, str)
            or not raw_key.strip()
            or not isinstance(raw_value, str)
            or not raw_value.strip()
        ):
            raise ValueError(f"{name} keys and values must be non-empty text")
        result[raw_key.strip()] = raw_value.strip()
    return MappingProxyType(dict(sorted(result.items())))


def _count_map(value: Mapping[str, object], name: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    result: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError(f"{name} keys must be non-empty text")
        key = raw_key.strip()
        if key in result:
            raise ValueError(f"{name} contains duplicate normalized keys")
        result[key] = _count(raw_value, f"{name}[{key}]")
    return MappingProxyType(dict(sorted(result.items())))


def _classification(value: object, name: str) -> str:
    if value is None:
        return UNCATEGORIZED
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value.strip() or UNCATEGORIZED


def _sha256_or_na(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a SHA-256 digest or not-applicable")
    result = value.strip().lower()
    if result == "not-applicable":
        return result
    if len(result) != 64:
        raise ValueError(f"{name} must be a SHA-256 digest")
    try:
        int(result, 16)
    except ValueError as error:
        raise ValueError(f"{name} must be a SHA-256 digest") from error
    return result


@dataclass(frozen=True)
class RiskPolicy:
    checked_at: str
    policy_version: str = "small-capital-live-v1"
    mode: str = "enforce"
    adapter: str = "joinquant"
    market_regime: str = "NORMAL"
    fee_schedule: FeeSchedule | None = None
    signal_max_age_sec: int = 1200
    broker_snapshot_max_age_sec: int = 300
    quote_max_age_sec: int = 120
    future_skew_sec: int = 5
    decision_ttl_sec: int = 30
    per_trade_risk_fraction: Decimal = Decimal("0.01")
    normal_per_trade_risk_fraction: Decimal = Decimal("0.01")
    risk_cap_yuan: Decimal | None = None
    max_cost_edge_ratio: Decimal = Decimal("0.35")
    max_positions: int = 5
    max_single_position_fraction: Decimal = Decimal("0.30")
    max_total_position_fraction: Decimal = Decimal("0.80")
    min_cash_reserve_fraction: Decimal = Decimal("0.05")
    max_industry_fraction: Decimal = Decimal("0.25")
    max_theme_fraction: Decimal = Decimal("0.20")
    max_uncategorized_fraction: Decimal = Decimal("0.10")
    max_open_risk_fraction: Decimal = Decimal("0.04")
    normal_max_open_risk_fraction: Decimal = Decimal("0.04")
    max_new_positions_per_day: int = 10
    max_orders_per_day: int = 50
    max_daily_turnover_fraction: Decimal = Decimal("2")
    max_daily_loss_fraction: Decimal = Decimal("0.05")
    max_account_drawdown_fraction: Decimal = Decimal("0.15")
    max_consecutive_losses: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty text")
        object.__setattr__(self, "policy_version", self.policy_version.strip())
        object.__setattr__(self, "checked_at", _timestamp(self.checked_at, "checked_at"))
        mode = str(self.mode).strip().lower()
        adapter = str(self.adapter).strip().lower()
        if mode not in {"observe", "enforce"}:
            raise ValueError("mode must be observe or enforce")
        if adapter not in {"joinquant", "qmt"}:
            raise ValueError("adapter must be joinquant or qmt")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "adapter", adapter)
        regime = str(self.market_regime).strip().upper()
        if regime not in {"NORMAL", "CAUTION", "RISK_OFF"}:
            raise ValueError(
                "market_regime must be NORMAL, CAUTION or RISK_OFF"
            )
        object.__setattr__(self, "market_regime", regime)
        if self.fee_schedule is not None and not isinstance(self.fee_schedule, FeeSchedule):
            raise ValueError("fee_schedule must be FeeSchedule or None")
        for name in (
            "signal_max_age_sec",
            "broker_snapshot_max_age_sec",
            "quote_max_age_sec",
            "decision_ttl_sec",
        ):
            object.__setattr__(self, name, _count(getattr(self, name), name, positive=True))
        for name in (
            "max_positions",
            "max_new_positions_per_day",
            "max_orders_per_day",
            "max_consecutive_losses",
        ):
            object.__setattr__(self, name, _count(getattr(self, name), name))
        object.__setattr__(
            self, "future_skew_sec", _count(self.future_skew_sec, "future_skew_sec")
        )
        for name in (
            "per_trade_risk_fraction",
            "normal_per_trade_risk_fraction",
        ):
            value = _decimal(getattr(self, name), name, positive=True)
            if value > Decimal("1"):
                raise ValueError(f"{name} must not exceed 1")
            object.__setattr__(self, name, value)
        fraction_names = (
            "max_single_position_fraction",
            "max_total_position_fraction",
            "min_cash_reserve_fraction",
            "max_industry_fraction",
            "max_theme_fraction",
            "max_uncategorized_fraction",
            "max_open_risk_fraction",
            "normal_max_open_risk_fraction",
            "max_daily_loss_fraction",
            "max_account_drawdown_fraction",
        )
        for name in fraction_names:
            value = _decimal(getattr(self, name), name)
            if value > Decimal("1"):
                raise ValueError(f"{name} must not exceed 1")
            object.__setattr__(self, name, value)
        if self.market_regime == "CAUTION" and (
            self.per_trade_risk_fraction
            > self.normal_per_trade_risk_fraction / Decimal("2")
            or self.max_open_risk_fraction
            > self.normal_max_open_risk_fraction / Decimal("2")
        ):
            raise ValueError(
                "CAUTION policy must halve per-trade and open-risk limits"
            )
        object.__setattr__(
            self,
            "max_daily_turnover_fraction",
            _decimal(
                self.max_daily_turnover_fraction,
                "max_daily_turnover_fraction",
            ),
        )
        object.__setattr__(
            self,
            "max_cost_edge_ratio",
            _decimal(self.max_cost_edge_ratio, "max_cost_edge_ratio"),
        )
        if self.risk_cap_yuan is not None:
            object.__setattr__(
                self,
                "risk_cap_yuan",
                _decimal(self.risk_cap_yuan, "risk_cap_yuan", positive=True),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at,
            "policy_version": self.policy_version,
            "mode": self.mode,
            "adapter": self.adapter,
            "market_regime": self.market_regime,
            "fee_schedule": (
                self.fee_schedule.to_dict() if self.fee_schedule is not None else None
            ),
            "signal_max_age_sec": self.signal_max_age_sec,
            "broker_snapshot_max_age_sec": self.broker_snapshot_max_age_sec,
            "quote_max_age_sec": self.quote_max_age_sec,
            "future_skew_sec": self.future_skew_sec,
            "decision_ttl_sec": self.decision_ttl_sec,
            "per_trade_risk_fraction": self.per_trade_risk_fraction,
            "normal_per_trade_risk_fraction": self.normal_per_trade_risk_fraction,
            "risk_cap_yuan": self.risk_cap_yuan,
            "max_cost_edge_ratio": self.max_cost_edge_ratio,
            "max_positions": self.max_positions,
            "max_single_position_fraction": self.max_single_position_fraction,
            "max_total_position_fraction": self.max_total_position_fraction,
            "min_cash_reserve_fraction": self.min_cash_reserve_fraction,
            "max_industry_fraction": self.max_industry_fraction,
            "max_theme_fraction": self.max_theme_fraction,
            "max_uncategorized_fraction": self.max_uncategorized_fraction,
            "max_open_risk_fraction": self.max_open_risk_fraction,
            "normal_max_open_risk_fraction": self.normal_max_open_risk_fraction,
            "max_new_positions_per_day": self.max_new_positions_per_day,
            "max_orders_per_day": self.max_orders_per_day,
            "max_daily_turnover_fraction": self.max_daily_turnover_fraction,
            "max_daily_loss_fraction": self.max_daily_loss_fraction,
            "max_account_drawdown_fraction": self.max_account_drawdown_fraction,
            "max_consecutive_losses": self.max_consecutive_losses,
        }

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True)
class PositionCapacityEvidence:
    position_cycle_id: str
    entry_intent: ExecutionIntent
    effective_stop_price: Decimal

    def __post_init__(self) -> None:
        if (
            not isinstance(self.position_cycle_id, str)
            or not self.position_cycle_id.strip()
        ):
            raise ValueError(
                "position_cycle_id must be non-empty text"
            )
        object.__setattr__(
            self, "position_cycle_id", self.position_cycle_id.strip()
        )
        intent = (
            self.entry_intent
            if isinstance(self.entry_intent, ExecutionIntent)
            else ExecutionIntent.from_dict(self.entry_intent)
        )
        if intent.side != "buy":
            raise ValueError("position entry_intent must be a buy")
        object.__setattr__(self, "entry_intent", intent)
        object.__setattr__(
            self,
            "effective_stop_price",
            _decimal(
                self.effective_stop_price,
                "effective_stop_price",
                positive=True,
            ),
        )
        if (
            self.effective_stop_price
            < intent.pre_trade_result.candidate.stop_price
        ):
            raise ValueError(
                "effective_stop_price cannot loosen the signed initial stop"
            )

    @property
    def code(self) -> str:
        return self.entry_intent.code

    @property
    def account_scope_id(self) -> str:
        return self.entry_intent.account_scope_id

    @property
    def adapter(self) -> str:
        return self.entry_intent.adapter

    @property
    def industry(self) -> str:
        return self.entry_intent.pre_trade_result.candidate.industry

    @property
    def theme(self) -> str:
        return self.entry_intent.pre_trade_result.candidate.theme

    @property
    def uncategorized(self) -> bool:
        return UNCATEGORIZED in {self.industry, self.theme}

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_kind": "execution_intent",
            "position_cycle_id": self.position_cycle_id,
            "entry_intent": self.entry_intent.to_dict(),
            "effective_stop_price": self.effective_stop_price,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PositionCapacityEvidence:
        values = dict(value)
        kind = str(values.pop("evidence_kind", "execution_intent")).strip().lower()
        if kind != "execution_intent":
            raise ValueError("position evidence kind does not match execution intent")
        return cls(**values)


@dataclass(frozen=True)
class AdoptedPositionCapacityEvidence:
    position_cycle_id: str
    account_scope_id: str
    adapter: str
    code: str
    industry: str
    theme: str
    effective_stop_price: Decimal
    gap_price: Decimal
    initial_qty: int
    adopted_at: str
    source_sha256: str

    def __post_init__(self) -> None:
        for name in ("position_cycle_id", "account_scope_id", "code"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
            object.__setattr__(self, name, value.strip())
        adapter = str(self.adapter).strip().lower()
        if adapter not in {"joinquant", "qmt"}:
            raise ValueError("adapter must be joinquant or qmt")
        object.__setattr__(self, "adapter", adapter)
        object.__setattr__(self, "industry", _classification(self.industry, "industry"))
        object.__setattr__(self, "theme", _classification(self.theme, "theme"))
        for name in ("effective_stop_price", "gap_price"):
            object.__setattr__(
                self, name, _decimal(getattr(self, name), name, positive=True)
            )
        object.__setattr__(
            self, "initial_qty", _count(self.initial_qty, "initial_qty", positive=True)
        )
        object.__setattr__(self, "adopted_at", _timestamp(self.adopted_at, "adopted_at"))
        source_sha256 = _sha256_or_na(self.source_sha256, "source_sha256")
        if source_sha256 == "not-applicable":
            raise ValueError("source_sha256 must bind an immutable adoption fact")
        object.__setattr__(self, "source_sha256", source_sha256)

    @property
    def uncategorized(self) -> bool:
        return UNCATEGORIZED in {self.industry, self.theme}

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_kind": "adopted_legacy",
            "position_cycle_id": self.position_cycle_id,
            "account_scope_id": self.account_scope_id,
            "adapter": self.adapter,
            "code": self.code,
            "industry": self.industry,
            "theme": self.theme,
            "effective_stop_price": self.effective_stop_price,
            "gap_price": self.gap_price,
            "initial_qty": self.initial_qty,
            "adopted_at": self.adopted_at,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> AdoptedPositionCapacityEvidence:
        values = dict(value)
        kind = str(values.pop("evidence_kind", "adopted_legacy")).strip().lower()
        if kind != "adopted_legacy":
            raise ValueError("position evidence kind does not match legacy adoption")
        return cls(**values)


def _position_capacity_evidence(
    value: object,
) -> PositionCapacityEvidence | AdoptedPositionCapacityEvidence:
    if isinstance(value, (PositionCapacityEvidence, AdoptedPositionCapacityEvidence)):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("position capacity evidence must be a mapping")
    kind = str(value.get("evidence_kind", "execution_intent")).strip().lower()
    if kind == "execution_intent":
        return PositionCapacityEvidence.from_dict(value)
    if kind == "adopted_legacy":
        return AdoptedPositionCapacityEvidence.from_dict(value)
    raise ValueError("unsupported position evidence kind")


@dataclass(frozen=True)
class CapacityReservationEvidence:
    intent: ExecutionIntent
    status: str
    remaining_qty: int

    def __post_init__(self) -> None:
        intent = (
            self.intent
            if isinstance(self.intent, ExecutionIntent)
            else ExecutionIntent.from_dict(self.intent)
        )
        object.__setattr__(self, "intent", intent)
        status = str(self.status).strip().lower()
        if status not in {
            "ready",
            "submitting",
            "submit_unknown",
            "submitted",
            "partially_filled",
            "pending_cancel",
            "filled",
            "cancelled",
            "rejected",
            "not_submitted",
            "expired",
        }:
            raise ValueError(
                "reservation status is not active or awaiting reconciliation"
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "remaining_qty",
            _count(self.remaining_qty, "remaining_qty", positive=True),
        )
        if self.remaining_qty > intent.order_qty:
            raise ValueError("remaining_qty cannot exceed intent order_qty")

    @property
    def client_order_id(self) -> str:
        return self.intent.client_order_id

    @property
    def code(self) -> str:
        return self.intent.code

    @property
    def side(self) -> str:
        return self.intent.side

    @property
    def industry(self) -> str:
        return self.intent.pre_trade_result.candidate.industry

    @property
    def theme(self) -> str:
        return self.intent.pre_trade_result.candidate.theme

    @property
    def uncategorized(self) -> bool:
        return UNCATEGORIZED in {self.industry, self.theme}

    @property
    def position_value_yuan(self) -> Decimal:
        if self.side == "sell":
            return ZERO
        result = self.intent.pre_trade_result
        return self._remaining_amount(
            result.execution_fee.notional_yuan
        )

    @property
    def cash_yuan(self) -> Decimal:
        if self.side == "sell":
            return ZERO
        fee = self.intent.pre_trade_result.execution_fee
        return self._remaining_amount(
            fee.notional_yuan + fee.total_yuan
        )

    @property
    def open_risk_yuan(self) -> Decimal:
        if self.side == "sell":
            return ZERO
        result = self.intent.pre_trade_result
        return self._remaining_amount(result.per_trade_risk_yuan)

    def _remaining_amount(self, original: Decimal) -> Decimal:
        return _ceil_proportion(
            original, self.remaining_qty, self.intent.order_qty
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.to_dict(),
            "status": self.status,
            "remaining_qty": self.remaining_qty,
        }


@dataclass(frozen=True)
class ReservationView:
    account_scope_id: str
    broker_snapshot_id: str = "not-applicable"
    broker_snapshot_sha256: str = "not-applicable"
    positions: tuple[
        PositionCapacityEvidence | AdoptedPositionCapacityEvidence, ...
    ] = ()
    active_reservations: tuple[CapacityReservationEvidence, ...] = ()
    active_logical_signal_ids: frozenset[str] = frozenset()
    position_exit_owner_ids: Mapping[str, str] = field(default_factory=dict)
    position_exit_target_qtys: Mapping[str, int] = field(default_factory=dict)
    capacity_evidence_complete: bool = False
    daily_evidence_complete: bool = False
    daily_trade_date: str = ""
    daily_source: str = ""
    daily_new_positions: int = 0
    daily_orders: int = 0
    daily_turnover_fraction: Decimal = ZERO
    consecutive_losses: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.account_scope_id, str) or not self.account_scope_id.strip():
            raise ValueError("account_scope_id must be non-empty text")
        object.__setattr__(self, "account_scope_id", self.account_scope_id.strip())
        snapshot_id = str(self.broker_snapshot_id).strip()
        if not snapshot_id:
            raise ValueError("broker_snapshot_id must be non-empty text")
        object.__setattr__(self, "broker_snapshot_id", snapshot_id)
        object.__setattr__(
            self,
            "broker_snapshot_sha256",
            _sha256_or_na(
                self.broker_snapshot_sha256, "broker_snapshot_sha256"
            ),
        )
        positions = tuple(_position_capacity_evidence(item) for item in self.positions)
        if len({item.code for item in positions}) != len(positions):
            raise ValueError("duplicate position capacity evidence")
        if any(
            item.account_scope_id != self.account_scope_id
            for item in positions
        ):
            raise ValueError(
                "position evidence account scope does not match view"
            )
        object.__setattr__(
            self, "positions", tuple(sorted(positions, key=lambda item: item.code))
        )
        reservations = tuple(
            item
            if isinstance(item, CapacityReservationEvidence)
            else CapacityReservationEvidence(**dict(item))
            for item in self.active_reservations
        )
        if len({item.client_order_id for item in reservations}) != len(reservations):
            raise ValueError("duplicate active reservation client_order_id")
        if any(
            item.intent.account_scope_id != self.account_scope_id
            for item in reservations
        ):
            raise ValueError(
                "active reservation account scope does not match view"
            )
        object.__setattr__(
            self,
            "active_reservations",
            tuple(sorted(reservations, key=lambda item: item.client_order_id)),
        )
        object.__setattr__(
            self,
            "daily_turnover_fraction",
            _decimal(self.daily_turnover_fraction, "daily_turnover_fraction"),
        )
        for name in (
            "daily_new_positions",
            "daily_orders",
            "consecutive_losses",
        ):
            object.__setattr__(self, name, _count(getattr(self, name), name))
        for name in ("capacity_evidence_complete", "daily_evidence_complete"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        daily_trade_date = str(self.daily_trade_date).strip()
        daily_source = str(self.daily_source).strip()
        if self.daily_evidence_complete:
            try:
                datetime.strptime(daily_trade_date, "%Y-%m-%d")
            except ValueError as error:
                raise ValueError(
                    "daily_trade_date must be YYYY-MM-DD"
                ) from error
            if not daily_source:
                raise ValueError(
                    "daily_source is required for complete daily evidence"
                )
        object.__setattr__(self, "daily_trade_date", daily_trade_date)
        object.__setattr__(self, "daily_source", daily_source)
        values = self.active_logical_signal_ids
        if not isinstance(values, (set, frozenset)):
            raise ValueError("active_logical_signal_ids must be a set")
        normalized = frozenset(
            value.strip()
            for value in values
            if isinstance(value, str) and value.strip()
        )
        if len(normalized) != len(values):
            raise ValueError(
                "active_logical_signal_ids entries must be non-empty text"
            )
        object.__setattr__(self, "active_logical_signal_ids", normalized)
        object.__setattr__(
            self,
            "position_exit_owner_ids",
            _text_map(self.position_exit_owner_ids, "position_exit_owner_ids"),
        )
        object.__setattr__(
            self,
            "position_exit_target_qtys",
            _count_map(
                self.position_exit_target_qtys,
                "position_exit_target_qtys",
            ),
        )

    @classmethod
    def empty(cls, broker_snapshot: BrokerSnapshot) -> ReservationView:
        if not isinstance(broker_snapshot, BrokerSnapshot):
            raise ValueError("broker_snapshot must be BrokerSnapshot")
        if broker_snapshot.positions or broker_snapshot.open_orders:
            raise ValueError("empty reservation view requires an empty broker snapshot")
        return cls(
            account_scope_id=broker_snapshot.account_scope_id,
            broker_snapshot_id=broker_snapshot.snapshot_id,
            broker_snapshot_sha256=broker_snapshot.snapshot_sha256,
            capacity_evidence_complete=True,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_scope_id": self.account_scope_id,
            "broker_snapshot_id": self.broker_snapshot_id,
            "broker_snapshot_sha256": self.broker_snapshot_sha256,
            "positions": [item.to_dict() for item in self.positions],
            "active_reservations": [
                item.to_dict() for item in self.active_reservations
            ],
            "active_logical_signal_ids": sorted(self.active_logical_signal_ids),
            "position_exit_owner_ids": dict(self.position_exit_owner_ids),
            "position_exit_target_qtys": dict(self.position_exit_target_qtys),
            "capacity_evidence_complete": self.capacity_evidence_complete,
            "daily_evidence_complete": self.daily_evidence_complete,
            "daily_trade_date": self.daily_trade_date,
            "daily_source": self.daily_source,
            "daily_new_positions": self.daily_new_positions,
            "daily_orders": self.daily_orders,
            "daily_turnover_fraction": self.daily_turnover_fraction,
            "consecutive_losses": self.consecutive_losses,
        }

    @property
    def view_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

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

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sector_exposure_pct", MappingProxyType(dict(self.sector_exposure_pct))
        )

    @classmethod
    def empty(cls) -> "PortfolioState":
        return cls()


@dataclass(frozen=True)
class RiskCheckResult:
    allowed: bool
    hard_blocks: tuple[str, ...]
    soft_warnings: tuple[str, ...]
    metrics: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


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
        or (action == "buy" and not _positive_number(position_pct))
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


HARD_BLOCK_ORDER = (
    "BROKER_SNAPSHOT_REQUIRED",
    "ACCOUNT_SCOPE_MISMATCH",
    "ACCOUNT_TRADE_DATE_MISMATCH",
    "BROKER_ADAPTER_MISMATCH",
    "ACCOUNT_SNAPSHOT_FROM_FUTURE",
    "ACCOUNT_SNAPSHOT_STALE",
    "QUOTE_REQUIRED",
    "QUOTE_CODE_MISMATCH",
    "QUOTE_FROM_FUTURE",
    "QUOTE_STALE",
    "SIGNAL_FROM_FUTURE",
    "SIGNAL_EXPIRED",
    "SIGNAL_STALE",
    "RESERVATION_SCOPE_MISMATCH",
    "RESERVATION_EVIDENCE_INCOMPLETE",
    "DAILY_RISK_STATE_INCOMPLETE",
    "INSTRUMENT_RULES_REQUIRED",
    "INSTRUMENT_RULES_MISMATCH",
    "INSTRUMENT_RULES_STALE",
    "INSTRUMENT_RULES_INCOMPLETE",
    "MARKET_RULE_EVIDENCE_CONFLICT",
    "FEE_SCHEDULE_REQUIRED",
    "FEE_SCHEDULE_MISMATCH",
    "FEE_SCHEDULE_NOT_LIVE",
    "SYSTEM_STATE_INCOMPLETE",
    "QMT_ENFORCE_REQUIRED",
    "RISK_CAP_YUAN_REQUIRED",
    "KILL_SWITCH_ACTIVE",
    "BUY_DISABLED",
    "SELL_DISABLED",
    "POLICY_REGIME_MISMATCH",
    "RISK_OFF",
    "DUPLICATE_ORDER",
    "EXIT_OWNER_MISMATCH",
    "EXIT_TARGET_MISMATCH",
    "POSITION_ALREADY_HELD",
    "POSITION_NOT_FOUND",
    "SELL_TARGET_NOT_REDUCING",
    "T_PLUS_ONE_UNSELLABLE",
    "INSTRUMENT_SUSPENDED",
    "INSTRUMENT_SPECIAL_STATUS",
    "BUY_LIMIT_UP",
    "SELL_LIMIT_DOWN",
    "BUY_PRICE_CAP_EXCEEDED",
    "SELL_PRICE_FLOOR_BREACHED",
    "BUY_QTY_BELOW_MIN",
    "BUY_QTY_STEP_INVALID",
    "SELL_QTY_ZERO",
    "SELL_QTY_STEP_INVALID",
    "PRICE_TICK_INVALID",
    "PRICE_ABOVE_LIMIT",
    "PRICE_BELOW_LIMIT",
    "RULE_POSITION_CAP_REQUIRED",
    "MAX_POSITIONS_EXCEEDED",
    "MAX_SINGLE_POSITION_EXCEEDED",
    "MAX_TOTAL_POSITION_EXCEEDED",
    "INDUSTRY_EXPOSURE_EXCEEDED",
    "THEME_EXPOSURE_EXCEEDED",
    "UNCATEGORIZED_EXPOSURE_EXCEEDED",
    "MAX_NEW_POSITIONS_EXCEEDED",
    "MAX_DAILY_ORDERS_EXCEEDED",
    "MAX_DAILY_TURNOVER_EXCEEDED",
    "DAILY_LOSS_LIMIT_EXCEEDED",
    "ACCOUNT_DRAWDOWN_LIMIT_EXCEEDED",
    "CONSECUTIVE_LOSS_LIMIT_EXCEEDED",
    "INVALID_STOP_DISTANCE",
    "NO_BOARD_LOT",
    "PER_TRADE_RISK_EXCEEDED",
    "PORTFOLIO_OPEN_RISK_EXCEEDED",
    "CASH_CAPACITY_EXCEEDED",
    "ECONOMIC_EDGE_INSUFFICIENT",
)
WARNING_ORDER = (
    "SELL_PARTIAL_QUANTITY",
    "SELL_FEE_EVIDENCE_UNAVAILABLE",
    "SELL_RULE_EVIDENCE_UNAVAILABLE",
    "RISK_CAP_YUAN_NOT_APPLIED",
    "ECONOMIC_EDGE_INSUFFICIENT",
)


def _ordered(values: list[str], order: tuple[str, ...]) -> tuple[str, ...]:
    unique = set(values)
    known = [value for value in order if value in unique]
    unknown = [value for value in values if value not in order and value not in known]
    return tuple(known + list(dict.fromkeys(unknown)))


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _control_flag(system_state: Mapping[str, object], name: str) -> bool | None:
    if name not in system_state:
        return None
    value = system_state[name]
    if value in (True, 1, "1", "true", "TRUE", "on", "ON"):
        return True
    if value in (False, 0, "0", "false", "FALSE", "off", "OFF"):
        return False
    return None


def _system_state_payload(
    system_state: Mapping[str, object],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("buy_enabled", "sell_enabled", "kill_switch"):
        value = _control_flag(system_state, name)
        result[name] = (
            "missing"
            if name not in system_state
            else "invalid"
            if value is None
            else "1"
            if value
            else "0"
        )
    regime = system_state.get("market_regime")
    result["market_regime"] = (
        regime.strip().upper()
        if isinstance(regime, str) and regime.strip()
        else "missing"
        if "market_regime" not in system_state
        else "invalid"
    )
    return result


def _max_qty_for_value(value_yuan: Decimal, price: Decimal) -> int:
    if value_yuan <= ZERO:
        return 0
    return int((value_yuan / price).to_integral_value(rounding=ROUND_FLOOR))


def _minimum_valid_qty(rules: InstrumentRules) -> int:
    return (
        (rules.buy_min_qty + rules.buy_qty_step - 1)
        // rules.buy_qty_step
        * rules.buy_qty_step
    )


def _value(mapping: Mapping[str, Decimal], key: str) -> Decimal:
    return mapping.get(key, ZERO)


def _ceil_proportion(
    original: Decimal, remaining_qty: int, original_qty: int
) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_UP
        return (
            original
            * Decimal(remaining_qty)
            / Decimal(original_qty)
        ).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _result_id(
    candidate: StrategyOrderCandidate,
    policy: RiskPolicy,
    reservations: ReservationView,
    broker_snapshot: BrokerSnapshot | None,
    quote: QuoteSnapshot | None,
    rules: InstrumentRules | None,
    system_state_sha256: str,
    hard_blocks: tuple[str, ...],
    warnings: tuple[str, ...],
    approved_qty: int,
    target_position_qty: int,
) -> str:
    digest = canonical_sha256(
        {
            "candidate_sha256": candidate.payload_sha256,
            "policy_sha256": policy.policy_sha256,
            "reservation_view_sha256": reservations.view_sha256,
            "broker_snapshot_sha256": (
                broker_snapshot.snapshot_sha256 if broker_snapshot else None
            ),
            "quote_snapshot_sha256": quote.quote_sha256 if quote else None,
            "instrument_rules_sha256": rules.rules_sha256 if rules else None,
            "system_state_sha256": system_state_sha256,
            "hard_blocks": hard_blocks,
            "warnings": warnings,
            "approved_qty": approved_qty,
            "target_position_qty": target_position_qty,
        }
    )
    return f"pretrade-{digest[:32]}"


def _pre_trade_check(
    candidate: StrategyOrderCandidate,
    broker_snapshot: BrokerSnapshot | None,
    quote: QuoteSnapshot | None,
    instrument_rules: InstrumentRules | None,
    system_state: Mapping[str, object],
    risk_policy: RiskPolicy,
    reservations: ReservationView,
) -> PreTradeResult:
    """Return one deterministic decision without reading time, storage or network."""
    if not isinstance(candidate, StrategyOrderCandidate):
        raise ValueError("candidate must be StrategyOrderCandidate")
    if broker_snapshot is not None and not isinstance(broker_snapshot, BrokerSnapshot):
        raise ValueError("broker_snapshot must be BrokerSnapshot or None")
    if quote is not None and not isinstance(quote, QuoteSnapshot):
        raise ValueError("quote must be QuoteSnapshot or None")
    if instrument_rules is not None and not isinstance(
        instrument_rules, InstrumentRules
    ):
        raise ValueError("instrument_rules must be InstrumentRules or None")
    if not isinstance(system_state, Mapping):
        raise ValueError("system_state must be a mapping")
    if not isinstance(risk_policy, RiskPolicy):
        raise ValueError("risk_policy must be RiskPolicy")
    if not isinstance(reservations, ReservationView):
        raise ValueError("reservations must be ReservationView")

    checked = _instant(risk_policy.checked_at)
    hard: list[str] = []
    warnings: list[str] = []
    system_state_sha256 = canonical_sha256(
        _system_state_payload(system_state)
    )

    signal_time = _instant(candidate.signal_time)
    frozen_until = _instant(candidate.frozen_valid_until)
    if signal_time > checked:
        hard.append("SIGNAL_FROM_FUTURE")
    if checked >= frozen_until:
        hard.append("SIGNAL_EXPIRED")
    if (checked - signal_time).total_seconds() >= risk_policy.signal_max_age_sec:
        hard.append("SIGNAL_STALE")

    if broker_snapshot is None:
        hard.append("BROKER_SNAPSHOT_REQUIRED")
    else:
        if broker_snapshot.account_scope_id != candidate.account_scope_id:
            hard.append("ACCOUNT_SCOPE_MISMATCH")
        if broker_snapshot.adapter != risk_policy.adapter:
            hard.append("BROKER_ADAPTER_MISMATCH")
        if (
            broker_snapshot.trade_date
            != checked.astimezone(A_SHARE_TIMEZONE).date().isoformat()
        ):
            hard.append("ACCOUNT_TRADE_DATE_MISMATCH")
        broker_times = (
            _instant(broker_snapshot.broker_time),
            _instant(broker_snapshot.generated_at),
        )
        if any(
            value > checked + timedelta(seconds=risk_policy.future_skew_sec)
            for value in broker_times
        ):
            hard.append("ACCOUNT_SNAPSHOT_FROM_FUTURE")
        if any(
            (checked - value).total_seconds()
            >= risk_policy.broker_snapshot_max_age_sec
            for value in broker_times
        ):
            hard.append("ACCOUNT_SNAPSHOT_STALE")

    if quote is None:
        hard.append("QUOTE_REQUIRED")
    else:
        if quote.code != candidate.code:
            hard.append("QUOTE_CODE_MISMATCH")
        quote_time = _instant(quote.quote_time)
        if quote_time > checked + timedelta(seconds=risk_policy.future_skew_sec):
            hard.append("QUOTE_FROM_FUTURE")
        if (
            checked - quote_time
        ).total_seconds() >= risk_policy.quote_max_age_sec:
            hard.append("QUOTE_STALE")

    if reservations.account_scope_id != candidate.account_scope_id:
        hard.append("RESERVATION_SCOPE_MISMATCH")
    if candidate.side == "buy" and broker_snapshot is not None and (
        reservations.broker_snapshot_id != broker_snapshot.snapshot_id
        or reservations.broker_snapshot_sha256
        != broker_snapshot.snapshot_sha256
    ):
        hard.append("RESERVATION_EVIDENCE_INCOMPLETE")

    rules = instrument_rules
    rules_available = rules is not None
    if rules is not None and rules.code != candidate.code:
        hard.append("INSTRUMENT_RULES_MISMATCH")
        rules_available = False
    if rules_available and (
        not rules.is_fresh(risk_policy.checked_at)
        or _instant(rules.valid_until) <= checked
    ):
        rules_available = False
        if candidate.side == "buy":
            hard.append("INSTRUMENT_RULES_STALE")
        else:
            warnings.append("SELL_RULE_EVIDENCE_UNAVAILABLE")
    if rules is None:
        if candidate.side == "buy":
            hard.append("INSTRUMENT_RULES_REQUIRED")
        else:
            warnings.append("SELL_RULE_EVIDENCE_UNAVAILABLE")
    if quote is not None and rules_available and rules is not None:
        if any(
            quote_value is not None
            and rule_value is not None
            and quote_value != rule_value
            for quote_value, rule_value in (
                (quote.limit_up_price, rules.limit_up_price),
                (quote.limit_down_price, rules.limit_down_price),
            )
        ):
            hard.append("MARKET_RULE_EVIDENCE_CONFLICT")

    fees = risk_policy.fee_schedule
    fees_available = fees is not None
    if fees is not None and (
        fees.version != candidate.fee_schedule_version
        or (
            broker_snapshot is not None
            and fees.effective_from > broker_snapshot.trade_date
        )
    ):
        fees_available = False
        if candidate.side == "buy":
            hard.append("FEE_SCHEDULE_MISMATCH")
        else:
            warnings.append("SELL_FEE_EVIDENCE_UNAVAILABLE")
    if (
        fees_available
        and fees is not None
        and risk_policy.adapter == "qmt"
        and fees.execution_scope not in {"live", "both"}
    ):
        fees_available = False
        if candidate.side == "buy":
            hard.append("FEE_SCHEDULE_NOT_LIVE")
        else:
            warnings.append("SELL_FEE_EVIDENCE_UNAVAILABLE")
    if fees is None:
        if candidate.side == "buy":
            hard.append("FEE_SCHEDULE_REQUIRED")
        else:
            warnings.append("SELL_FEE_EVIDENCE_UNAVAILABLE")

    kill_switch = _control_flag(system_state, "kill_switch")
    buy_enabled = _control_flag(system_state, "buy_enabled")
    sell_enabled = _control_flag(system_state, "sell_enabled")
    side_enabled = (
        buy_enabled if candidate.side == "buy" else sell_enabled
    )
    if kill_switch is None or side_enabled is None:
        hard.append("SYSTEM_STATE_INCOMPLETE")
    if kill_switch is True:
        hard.append("KILL_SWITCH_ACTIVE")
    if candidate.side == "buy" and buy_enabled is False:
        hard.append("BUY_DISABLED")
    if candidate.side == "sell" and sell_enabled is False:
        hard.append("SELL_DISABLED")
    if risk_policy.adapter == "qmt" and risk_policy.mode != "enforce":
        hard.append("QMT_ENFORCE_REQUIRED")
    if candidate.side == "buy":
        regime = system_state.get("market_regime")
        if not isinstance(regime, str) or regime.strip().upper() not in {
            "NORMAL", "CAUTION", "RISK_OFF",
        }:
            hard.append("SYSTEM_STATE_INCOMPLETE")
        else:
            normalized_regime = regime.strip().upper()
            if normalized_regime != risk_policy.market_regime:
                hard.append("POLICY_REGIME_MISMATCH")
            if normalized_regime == "RISK_OFF":
                hard.append("RISK_OFF")
        if risk_policy.adapter == "qmt" and risk_policy.risk_cap_yuan is None:
            hard.append("RISK_CAP_YUAN_REQUIRED")
        elif risk_policy.risk_cap_yuan is None:
            warnings.append("RISK_CAP_YUAN_NOT_APPLIED")
        if not reservations.capacity_evidence_complete:
            hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
        if not reservations.daily_evidence_complete:
            hard.append("DAILY_RISK_STATE_INCOMPLETE")
        if broker_snapshot is not None and (
            broker_snapshot.daily_risk_evidence_status != "reported"
            or reservations.daily_trade_date != broker_snapshot.trade_date
        ):
            hard.append("DAILY_RISK_STATE_INCOMPLETE")
        if (
            risk_policy.adapter == "qmt"
            and rules_available
            and rules is not None
            and (
                rules.limit_up_price is None
                or rules.limit_down_price is None
            )
        ):
            hard.append("INSTRUMENT_RULES_INCOMPLETE")

    positions = (
        {item.code: item for item in broker_snapshot.positions}
        if broker_snapshot is not None
        else {}
    )
    current_position = positions.get(candidate.code)
    position_evidence = {item.code: item for item in reservations.positions}
    active_reservations = {
        item.client_order_id: item for item in reservations.active_reservations
    }
    broker_open_orders = {
        str(item["client_order_id"]): item
        for item in (
            broker_snapshot.open_orders if broker_snapshot is not None else ()
        )
    }
    broker_active_codes = {
        str(item["stock_code"]) for item in broker_open_orders.values()
    }
    nonterminal_statuses = {
        "ready",
        "submitting",
        "submit_unknown",
        "submitted",
        "partially_filled",
        "pending_cancel",
    }
    if candidate.side == "buy":
        duplicate_order = candidate.code in broker_active_codes or any(
            item.code == candidate.code
            for item in active_reservations.values()
        )
    else:
        duplicate_order = candidate.code in broker_active_codes or any(
            item.code == candidate.code
            and (
                item.side == "sell"
                or item.status in nonterminal_statuses
            )
            for item in active_reservations.values()
        )
    if (
        duplicate_order
        or candidate.logical_signal_id in reservations.active_logical_signal_ids
    ):
        hard.append("DUPLICATE_ORDER")

    if candidate.side == "buy" and broker_snapshot is not None:
        positive_position_codes = {
            code for code, item in positions.items() if item.total_qty > 0
        }
        if set(position_evidence) != positive_position_codes:
            hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
        if any(
            item.adapter != broker_snapshot.adapter
            for item in position_evidence.values()
        ) or any(
            item.intent.adapter != broker_snapshot.adapter
            for item in active_reservations.values()
        ):
            hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
        if any(
            item.total_qty > 0
            and (
                item.last_price <= ZERO
                or item.market_value <= ZERO
                or abs(
                    item.market_value
                    - item.last_price * item.total_qty
                )
                > max(
                    Decimal("0.01"),
                    item.last_price
                    * item.total_qty
                    * Decimal("0.000001"),
                )
            )
            for item in positions.values()
        ):
            hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
        broker_statuses = {
            "submit_unknown",
            "submitted",
            "partially_filled",
            "pending_cancel",
        }
        broker_reservation_ids = {
            item.client_order_id
            for item in active_reservations.values()
            if item.status in broker_statuses
        }
        if broker_reservation_ids != set(broker_open_orders):
            hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
        for client_order_id, broker_order in broker_open_orders.items():
            reservation = active_reservations.get(client_order_id)
            if reservation is None:
                continue
            remaining_qty = (
                int(broker_order["target_qty"])
                - int(broker_order["filled_qty"])
            )
            if (
                reservation.code != broker_order["stock_code"]
                or reservation.side != broker_order["side"]
                or reservation.status != broker_order["status"]
                or reservation.intent.order_qty
                != int(broker_order["target_qty"])
                or reservation.remaining_qty != remaining_qty
                or (
                    reservation.intent.order_qty
                    - reservation.remaining_qty
                    != int(broker_order["filled_qty"])
                )
            ):
                hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
        if any(
            item.status == "submit_unknown"
            for item in active_reservations.values()
        ):
            hard.append("RESERVATION_EVIDENCE_INCOMPLETE")

    current_position_value = (
        current_position.market_value if current_position is not None else ZERO
    )
    current_position_qty = (
        current_position.total_qty if current_position is not None else 0
    )
    position_total_value = sum(
        (item.market_value for item in positions.values()), ZERO
    )

    position_industry_values: dict[str, Decimal] = {}
    position_theme_values: dict[str, Decimal] = {}
    position_uncategorized_value = ZERO
    position_open_risk = ZERO
    position_open_risk_by_code: dict[str, Decimal] = {}
    for code, position_item in positions.items():
        evidence = position_evidence.get(code)
        industry = (
            evidence.industry
            if evidence is not None
            else candidate.industry
            if code == candidate.code
            else UNCATEGORIZED
        )
        theme = (
            evidence.theme
            if evidence is not None
            else candidate.theme
            if code == candidate.code
            else UNCATEGORIZED
        )
        if evidence is None and code != candidate.code:
            position_uncategorized_value += position_item.market_value
            continue
        if evidence is not None:
            if isinstance(evidence, PositionCapacityEvidence):
                entry_result = evidence.entry_intent.pre_trade_result
                if position_item.total_qty > entry_result.approved_qty:
                    if candidate.side == "buy":
                        hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
                    continue
                raw_stop_risk = max(
                    ZERO,
                    (
                        entry_result.execution_fee.price
                        - evidence.effective_stop_price
                    )
                    * position_item.total_qty,
                )
                stop_fee = _ceil_proportion(
                    entry_result.round_trip_cost.total_yuan,
                    position_item.total_qty,
                    entry_result.approved_qty,
                )
                gap_risk = _ceil_proportion(
                    entry_result.gap_loss_yuan,
                    position_item.total_qty,
                    entry_result.approved_qty,
                )
                item_open_risk = max(raw_stop_risk + stop_fee, gap_risk)
            elif (
                position_item.total_qty > evidence.initial_qty
                or position_item.average_cost <= ZERO
                or evidence.gap_price >= position_item.average_cost
                or _instant(evidence.adopted_at) > checked
                or not fees_available
                or fees is None
            ):
                if candidate.side == "buy":
                    hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
                continue
            else:
                stop_cost = fees.estimate_round_trip(
                    position_item.average_cost,
                    evidence.effective_stop_price,
                    position_item.total_qty,
                )
                gap_cost = fees.estimate_round_trip(
                    position_item.average_cost,
                    evidence.gap_price,
                    position_item.total_qty,
                )
                stop_risk = max(
                    ZERO,
                    position_item.average_cost - evidence.effective_stop_price,
                ) * position_item.total_qty + stop_cost.total_yuan
                gap_risk = (
                    position_item.average_cost - evidence.gap_price
                ) * position_item.total_qty + gap_cost.total_yuan
                item_open_risk = max(stop_risk, gap_risk)
            position_open_risk_by_code[code] = item_open_risk
            position_open_risk += item_open_risk
        if industry != UNCATEGORIZED:
            position_industry_values[industry] = (
                position_industry_values.get(industry, ZERO)
                + position_item.market_value
            )
        if theme != UNCATEGORIZED:
            position_theme_values[theme] = (
                position_theme_values.get(theme, ZERO)
                + position_item.market_value
            )
        if UNCATEGORIZED in {industry, theme}:
            position_uncategorized_value += position_item.market_value

    reserved_position_value = ZERO
    reserved_open_risk = ZERO
    reserved_unreflected_cash = ZERO
    reserved_industry_values: dict[str, Decimal] = {}
    reserved_theme_values: dict[str, Decimal] = {}
    reserved_uncategorized_value = ZERO
    for reservation in active_reservations.values():
        if reservation.side != "buy":
            continue
        reserved_position_value += reservation.position_value_yuan
        reserved_open_risk += reservation.open_risk_yuan
        if reservation.client_order_id not in broker_open_orders:
            reserved_unreflected_cash += reservation.cash_yuan
        if reservation.industry != UNCATEGORIZED:
            reserved_industry_values[reservation.industry] = (
                reserved_industry_values.get(reservation.industry, ZERO)
                + reservation.position_value_yuan
            )
        if reservation.theme != UNCATEGORIZED:
            reserved_theme_values[reservation.theme] = (
                reserved_theme_values.get(reservation.theme, ZERO)
                + reservation.position_value_yuan
            )
        if reservation.uncategorized:
            reserved_uncategorized_value += reservation.position_value_yuan

    base_total_value = position_total_value + reserved_position_value
    available_after_unreflected_reservations = max(
        ZERO,
        (
            broker_snapshot.available_cash
            if broker_snapshot is not None else ZERO
        ) - reserved_unreflected_cash,
    )
    projected_available_cash = available_after_unreflected_reservations
    projected_single_value = current_position_value
    projected_total_value = base_total_value
    projected_industry_value = (
        _value(position_industry_values, candidate.industry)
        + _value(reserved_industry_values, candidate.industry)
    )
    projected_theme_value = (
        _value(position_theme_values, candidate.theme)
        + _value(reserved_theme_values, candidate.theme)
    )
    projected_uncategorized_value = (
        position_uncategorized_value + reserved_uncategorized_value
    )
    projected_open_risk = position_open_risk + reserved_open_risk
    if any(
        value > base_total_value
        for value in (
            projected_industry_value,
            projected_theme_value,
            projected_uncategorized_value,
        )
    ):
        if candidate.side == "buy":
            hard.append("RESERVATION_EVIDENCE_INCOMPLETE")
        projected_industry_value = min(
            projected_industry_value, base_total_value
        )
        projected_theme_value = min(projected_theme_value, base_total_value)
        projected_uncategorized_value = min(
            projected_uncategorized_value, base_total_value
        )

    approved_qty = 0
    target_position_qty = current_position_qty
    approved_limit_price: Decimal | None = None
    approved_price_cap: Decimal | None = None
    execution_fee = None
    round_trip_cost = None
    target_round_trip_cost = None
    planned_stop_loss_yuan = None
    gap_price = None
    gap_round_trip_cost = None
    gap_loss_yuan = None
    fee_erosion_ratio = None
    cost_to_expected_edge_ratio = None
    per_trade_risk_yuan = ZERO
    actual_trade_risk_fraction = ZERO

    if candidate.side == "buy":
        if current_position is not None and current_position.total_qty > 0:
            hard.append("POSITION_ALREADY_HELD")
        if candidate.rule_position_cap_fraction is None:
            hard.append("RULE_POSITION_CAP_REQUIRED")

        active_buy_codes = {
            item.code
            for item in active_reservations.values()
            if item.side == "buy"
        } | {
            str(item["stock_code"])
            for item in broker_open_orders.values()
            if item["side"] == "buy"
        }
        occupied_codes = {
            code for code, item in positions.items() if item.total_qty > 0
        } | active_buy_codes
        if candidate.code not in occupied_codes and len(occupied_codes) >= risk_policy.max_positions:
            hard.append("MAX_POSITIONS_EXCEEDED")

        if reservations.daily_evidence_complete:
            if reservations.daily_new_positions >= risk_policy.max_new_positions_per_day:
                hard.append("MAX_NEW_POSITIONS_EXCEEDED")
            if reservations.daily_orders >= risk_policy.max_orders_per_day:
                hard.append("MAX_DAILY_ORDERS_EXCEEDED")
            if (
                reservations.daily_turnover_fraction
                >= risk_policy.max_daily_turnover_fraction
            ):
                hard.append("MAX_DAILY_TURNOVER_EXCEEDED")
            equity = (
                broker_snapshot.total_equity
                if broker_snapshot is not None else ZERO
            )
            if (
                equity > ZERO
                and broker_snapshot is not None
                and broker_snapshot.intraday_pnl
                <= -(equity * risk_policy.max_daily_loss_fraction)
            ):
                hard.append("DAILY_LOSS_LIMIT_EXCEEDED")
            if (
                broker_snapshot is not None
                and abs(broker_snapshot.account_drawdown_pct) / Decimal("100")
                >= risk_policy.max_account_drawdown_fraction
            ):
                hard.append("ACCOUNT_DRAWDOWN_LIMIT_EXCEEDED")
            if reservations.consecutive_losses >= risk_policy.max_consecutive_losses:
                hard.append("CONSECUTIVE_LOSS_LIMIT_EXCEEDED")

        if quote is not None and rules_available:
            if quote.suspended or rules.suspended:
                hard.append("INSTRUMENT_SUSPENDED")
            if rules.special_status.strip().lower() != "normal":
                hard.append("INSTRUMENT_SPECIAL_STATUS")
            limit_up = quote.limit_up_price or rules.limit_up_price
            market_buy_price = quote.ask_price or quote.last_price
            if limit_up is not None and market_buy_price >= limit_up:
                hard.append("BUY_LIMIT_UP")
            if (
                candidate.buy_price_cap is not None
                and market_buy_price > candidate.buy_price_cap
            ):
                hard.append("BUY_PRICE_CAP_EXCEEDED")

        can_size = bool(
            broker_snapshot is not None
            and quote is not None
            and rules_available
            and fees_available
            and candidate.rule_position_cap_fraction is not None
            and candidate.buy_price_cap is not None
        )
        if can_size:
            entry_price = candidate.buy_price_cap
            minimum_qty = _minimum_valid_qty(rules)
            minimum_value = entry_price * minimum_qty
            order_reasons = rules.validate_order("buy", minimum_qty, entry_price)
            hard.extend(order_reasons)
            if candidate.target_price % rules.price_tick:
                hard.append("PRICE_TICK_INVALID")

            equity = broker_snapshot.total_equity
            rule_headroom = equity * candidate.rule_position_cap_fraction
            single_headroom = (
                equity * risk_policy.max_single_position_fraction
                - current_position_value
            )
            total_headroom = (
                equity * risk_policy.max_total_position_fraction
                - base_total_value
            )
            turnover_headroom = (
                equity
                * (
                    risk_policy.max_daily_turnover_fraction
                    - reservations.daily_turnover_fraction
                )
                if reservations.daily_evidence_complete
                else ZERO
            )
            headrooms = [
                rule_headroom,
                single_headroom,
                total_headroom,
                turnover_headroom,
            ]
            if single_headroom < minimum_value:
                hard.append("MAX_SINGLE_POSITION_EXCEEDED")
            if total_headroom < minimum_value:
                hard.append("MAX_TOTAL_POSITION_EXCEEDED")
            if turnover_headroom < minimum_value:
                hard.append("MAX_DAILY_TURNOVER_EXCEEDED")

            if candidate.industry != UNCATEGORIZED:
                industry_headroom = (
                    equity * risk_policy.max_industry_fraction
                    - projected_industry_value
                )
                headrooms.append(industry_headroom)
                if industry_headroom < minimum_value:
                    hard.append("INDUSTRY_EXPOSURE_EXCEEDED")
            if candidate.theme != UNCATEGORIZED:
                theme_headroom = (
                    equity * risk_policy.max_theme_fraction
                    - projected_theme_value
                )
                headrooms.append(theme_headroom)
                if theme_headroom < minimum_value:
                    hard.append("THEME_EXPOSURE_EXCEEDED")
            if candidate.uncategorized:
                uncategorized_headroom = (
                    equity * risk_policy.max_uncategorized_fraction
                    - projected_uncategorized_value
                )
                headrooms.append(uncategorized_headroom)
                if uncategorized_headroom < minimum_value:
                    hard.append("UNCATEGORIZED_EXPOSURE_EXCEEDED")

            max_qty = min(
                _max_qty_for_value(max(ZERO, headroom), entry_price)
                for headroom in headrooms
            )
            if "MAX_POSITIONS_EXCEEDED" in hard:
                max_qty = 0
            remaining_open_risk = max(
                ZERO,
                equity * risk_policy.max_open_risk_fraction
                - projected_open_risk,
            )
            spendable_cash = max(
                ZERO,
                available_after_unreflected_reservations
                - equity * risk_policy.min_cash_reserve_fraction,
            )
            effective_risk_cap = (
                risk_policy.risk_cap_yuan
                if risk_policy.risk_cap_yuan is not None
                else equity * risk_policy.per_trade_risk_fraction
            )
            sizing = None
            if not order_reasons:
                sizing = allocate_buy_quantity(
                    entry_price=entry_price,
                    stop_price=candidate.stop_price,
                    gap_price=candidate.buy_gap_price,
                    rules=rules,
                    fees=fees,
                    equity=equity,
                    available_cash=spendable_cash,
                    risk_pct=risk_policy.per_trade_risk_fraction,
                    risk_cap_yuan=effective_risk_cap,
                    capacity=CapacityBudget(max_qty, remaining_open_risk),
                    expected_gross_return=(
                        (
                            candidate.target_price
                            + rules.price_tick / Decimal("2")
                        )
                        / entry_price
                        - Decimal("1")
                    ),
                    max_cost_edge_ratio=risk_policy.max_cost_edge_ratio,
                    economic_required=risk_policy.mode == "enforce",
                )
                if not sizing.allowed:
                    hard.extend(sizing.reasons)
                elif not sizing.economic_trade_allowed:
                    warnings.append("ECONOMIC_EDGE_INSUFFICIENT")

            preliminary_hard = _ordered(hard, HARD_BLOCK_ORDER)
            if sizing is not None and sizing.allowed and not preliminary_hard:
                approved_qty = sizing.target_qty
                target_position_qty = current_position_qty + approved_qty
                approved_limit_price = entry_price
                approved_price_cap = candidate.buy_price_cap
                execution_fee = sizing.buy_fee
                round_trip_cost = sizing.planned_stop_cost
                target_round_trip_cost = sizing.target_cost
                planned_stop_loss_yuan = sizing.planned_stop_loss_yuan
                gap_price = candidate.buy_gap_price
                gap_round_trip_cost = sizing.gap_cost
                gap_loss_yuan = sizing.gap_loss_yuan
                fee_erosion_ratio = sizing.fee_erosion_ratio
                cost_to_expected_edge_ratio = sizing.cost_to_expected_edge_ratio
                per_trade_risk_yuan = sizing.worst_case_loss_yuan
                actual_trade_risk_fraction = (
                    per_trade_risk_yuan / equity if equity > ZERO else ZERO
                )
                projected_available_cash = max(
                    ZERO,
                    available_after_unreflected_reservations
                    - sizing.buy_cash_required_yuan,
                )
                projected_single_value = (
                    current_position_value + sizing.position_value_yuan
                )
                projected_total_value = (
                    base_total_value + sizing.position_value_yuan
                )
                if candidate.industry != UNCATEGORIZED:
                    projected_industry_value += sizing.position_value_yuan
                if candidate.theme != UNCATEGORIZED:
                    projected_theme_value += sizing.position_value_yuan
                if candidate.uncategorized:
                    projected_uncategorized_value += sizing.position_value_yuan
                projected_open_risk += per_trade_risk_yuan

    else:
        if current_position is None or current_position.total_qty <= 0:
            hard.append("POSITION_NOT_FOUND")
        owner = reservations.position_exit_owner_ids.get(candidate.code)
        if owner != candidate.exit_owner_id:
            hard.append("EXIT_OWNER_MISMATCH")
        exit_target = reservations.position_exit_target_qtys.get(candidate.code)
        if exit_target != candidate.requested_target_position_qty:
            hard.append("EXIT_TARGET_MISMATCH")

        if quote is not None:
            if quote.suspended or (rules_available and rules.suspended):
                hard.append("INSTRUMENT_SUSPENDED")
            limit_down = (
                quote.limit_down_price
                or (rules.limit_down_price if rules_available else None)
            )
            if limit_down is not None and quote.last_price <= limit_down:
                hard.append("SELL_LIMIT_DOWN")
            if (
                candidate.sell_price_floor is not None
                and quote.last_price < candidate.sell_price_floor
            ):
                hard.append("SELL_PRICE_FLOOR_BREACHED")

        if current_position is not None:
            requested_target = candidate.requested_target_position_qty
            if requested_target is None or requested_target >= current_position.total_qty:
                hard.append("SELL_TARGET_NOT_REDUCING")
            else:
                required_qty = current_position.total_qty - requested_target
                approved_qty = min(required_qty, current_position.sellable_qty)
                target_position_qty = current_position.total_qty - approved_qty
                if approved_qty == 0:
                    hard.append("T_PLUS_ONE_UNSELLABLE")
                elif approved_qty < required_qty:
                    warnings.append("SELL_PARTIAL_QUANTITY")
                if (
                    approved_qty > 0
                    and rules_available
                    and candidate.sell_limit_price is not None
                ):
                    hard.extend(
                        rules.validate_order(
                            "sell", approved_qty, candidate.sell_limit_price
                        )
                    )

        preliminary_hard = _ordered(hard, HARD_BLOCK_ORDER)
        if approved_qty > 0 and not preliminary_hard:
            approved_limit_price = candidate.sell_limit_price
            approved_price_cap = candidate.sell_price_floor
            if fees_available:
                execution_fee = fees.estimate(
                    "sell", approved_limit_price, approved_qty
                )
                projected_available_cash = (
                    available_after_unreflected_reservations
                    + execution_fee.notional_yuan
                    - execution_fee.total_yuan
                )
            sold_value = min(
                current_position_value,
                (
                    current_position_value * approved_qty
                    / current_position.total_qty
                    if current_position and current_position.total_qty > 0
                    else ZERO
                ),
            )
            sold_open_risk = (
                position_open_risk_by_code.get(candidate.code, ZERO)
                * approved_qty
                / current_position.total_qty
                if current_position and current_position.total_qty > 0
                else ZERO
            )
            projected_single_value = max(ZERO, current_position_value - sold_value)
            projected_total_value = max(ZERO, base_total_value - sold_value)
            projected_open_risk = max(
                ZERO, projected_open_risk - sold_open_risk
            )
            if candidate.industry != UNCATEGORIZED:
                projected_industry_value = max(
                    ZERO, projected_industry_value - sold_value
                )
            if candidate.theme != UNCATEGORIZED:
                projected_theme_value = max(
                    ZERO, projected_theme_value - sold_value
                )
            if candidate.uncategorized:
                projected_uncategorized_value = max(
                    ZERO, projected_uncategorized_value - sold_value
                )
        elif preliminary_hard:
            approved_qty = 0

    hard_blocks = _ordered(hard, HARD_BLOCK_ORDER)
    ordered_warnings = _ordered(warnings, WARNING_ORDER)
    allowed = not hard_blocks and approved_qty > 0
    if not allowed:
        approved_qty = 0
        target_position_qty = current_position_qty
        approved_limit_price = None
        approved_price_cap = None
        execution_fee = None
        round_trip_cost = None
        target_round_trip_cost = None
        planned_stop_loss_yuan = None
        gap_price = None
        gap_round_trip_cost = None
        gap_loss_yuan = None
        fee_erosion_ratio = None
        cost_to_expected_edge_ratio = None
        per_trade_risk_yuan = ZERO
        actual_trade_risk_fraction = ZERO

    valid_until = checked
    if allowed:
        expiries = [
            frozen_until,
            signal_time + timedelta(seconds=risk_policy.signal_max_age_sec),
            checked + timedelta(seconds=risk_policy.decision_ttl_sec),
        ]
        if broker_snapshot is not None:
            expiries.extend(
                (
                    _instant(broker_snapshot.broker_time)
                    + timedelta(seconds=risk_policy.broker_snapshot_max_age_sec),
                    _instant(broker_snapshot.generated_at)
                    + timedelta(seconds=risk_policy.broker_snapshot_max_age_sec),
                )
            )
        if quote is not None:
            expiries.append(
                _instant(quote.quote_time)
                + timedelta(seconds=risk_policy.quote_max_age_sec)
            )
        if rules_available and rules is not None:
            expiries.append(_instant(rules.valid_until))
        valid_until = min(expiries)

    fee_status = "available" if fees_available else "unavailable"
    rule_status = "available" if rules_available else "unavailable"
    fee_version = fees.version if fees_available and fees is not None else "not-applicable"
    fee_hash = (
        fees.contract_sha256
        if fees_available and fees is not None
        else "not-applicable"
    )
    rule_hash = (
        rules.rules_sha256
        if rules_available and rules is not None
        else "not-applicable"
    )
    result_id = _result_id(
        candidate,
        risk_policy,
        reservations,
        broker_snapshot,
        quote,
        rules if rules_available else None,
        system_state_sha256,
        hard_blocks,
        ordered_warnings,
        approved_qty,
        target_position_qty,
    )
    return PreTradeResult(
        pre_trade_result_id=result_id,
        candidate_id=candidate.candidate_id,
        candidate=candidate,
        allowed=allowed,
        hard_blocks=hard_blocks,
        warnings=ordered_warnings,
        approved_qty=approved_qty,
        target_position_qty=target_position_qty,
        fee_schedule_version=fee_version,
        fee_schedule_sha256=fee_hash,
        fee_evidence_status=fee_status,
        rule_evidence_status=rule_status,
        checked_at=risk_policy.checked_at,
        valid_until=(
            valid_until.astimezone(timezone.utc).isoformat()
            if allowed else risk_policy.checked_at
        ),
        projected_available_cash_yuan=projected_available_cash,
        projected_single_position_value_yuan=projected_single_value,
        projected_total_position_value_yuan=projected_total_value,
        projected_industry_value_yuan=projected_industry_value,
        projected_theme_value_yuan=projected_theme_value,
        projected_uncategorized_value_yuan=projected_uncategorized_value,
        projected_open_risk_yuan=projected_open_risk,
        actual_trade_risk_fraction=actual_trade_risk_fraction,
        per_trade_risk_yuan=per_trade_risk_yuan,
        approved_limit_price=approved_limit_price,
        approved_price_cap=approved_price_cap,
        submission_attempt_id=(
            candidate.candidate_id if allowed else "not-applicable"
        ),
        execution_fee=execution_fee,
        round_trip_cost=round_trip_cost,
        target_round_trip_cost=target_round_trip_cost,
        planned_stop_loss_yuan=planned_stop_loss_yuan,
        gap_price=gap_price,
        gap_round_trip_cost=gap_round_trip_cost,
        gap_loss_yuan=gap_loss_yuan,
        fee_erosion_ratio=fee_erosion_ratio,
        cost_to_expected_edge_ratio=cost_to_expected_edge_ratio,
        broker_snapshot_id=(
            broker_snapshot.snapshot_id
            if broker_snapshot is not None else "not-applicable"
        ),
        broker_snapshot_sha256=(
            broker_snapshot.snapshot_sha256
            if broker_snapshot is not None else "not-applicable"
        ),
        quote_snapshot_id=(
            quote.snapshot_id if quote is not None else "not-applicable"
        ),
        quote_snapshot_sha256=(
            quote.quote_sha256 if quote is not None else "not-applicable"
        ),
        instrument_rules_sha256=rule_hash,
        strategy_version=candidate.strategy_version,
        risk_policy_sha256=risk_policy.policy_sha256,
        reservation_view_sha256=reservations.view_sha256,
        system_state_sha256=system_state_sha256,
    )


def pre_trade_check(
    candidate: StrategyOrderCandidate,
    broker_snapshot: BrokerSnapshot | None,
    quote: QuoteSnapshot | None,
    instrument_rules: InstrumentRules | None,
    system_state: Mapping[str, object],
    risk_policy: RiskPolicy,
    reservations: ReservationView,
) -> PreTradeResult:
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_UP
        return _pre_trade_check(
            candidate,
            broker_snapshot,
            quote,
            instrument_rules,
            system_state,
            risk_policy,
            reservations,
        )
