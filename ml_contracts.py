"""Immutable records shared by the trained-shadow-model pipeline."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from types import MappingProxyType


CANDIDATE_FINAL_ACTIONS = frozenset({
    "selected", "score_rejected", "risk_rejected", "tradability_rejected",
    "execution_rejected", "buy_published", "rule_rejected", "sell_published",
    "sell_rejected_no_holding", "sell_blocked_disabled",
    "sell_blocked_kill_switch", "buy_blocked_disabled",
    "buy_blocked_kill_switch",
})


@dataclass(frozen=True)
class TimedFeature:
    value: object
    available_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze(self.value))
        object.__setattr__(
            self,
            "available_at",
            _aware_datetime(self.available_at, "available_at").isoformat(),
        )

    def __hash__(self) -> int:
        return _stable_hash(self)


@dataclass(frozen=True)
class CandidateSample:
    sample_id: str
    source: str
    dataset_id: str
    trade_date: str
    decision_at: str
    code: str
    strategy_version: str
    parameter_version: str
    feature_schema_version: str
    features: Mapping[str, TimedFeature]
    selected: bool
    rejection_stage: str
    rejection_code: str
    final_action: str
    universe_hash: str
    market_data_version: str
    code_hash: str
    generator_hash: str

    def __post_init__(self) -> None:
        supplied_id = str(self.sample_id)
        for field_name in (
            "source",
            "dataset_id",
            "strategy_version",
            "parameter_version",
            "feature_schema_version",
            "rejection_stage",
            "rejection_code",
        ):
            object.__setattr__(self, field_name, str(getattr(self, field_name)))
        for field_name in (
            "final_action",
            "universe_hash",
            "market_data_version",
            "code_hash",
            "generator_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if self.final_action not in CANDIDATE_FINAL_ACTIONS:
            raise ValueError(f"UNKNOWN_FINAL_ACTION: {self.final_action}")
        object.__setattr__(self, "selected", bool(self.selected))
        rejection_stage = self.rejection_stage.strip()
        rejection_code = self.rejection_code.strip()
        object.__setattr__(self, "rejection_stage", rejection_stage)
        object.__setattr__(self, "rejection_code", rejection_code)
        decision_is_valid = (
            self.selected
            and rejection_stage == "selected"
            and not rejection_code
        ) or (
            not self.selected
            and bool(rejection_stage)
            and rejection_stage != "selected"
            and bool(rejection_code)
        )
        if not decision_is_valid:
            raise ValueError("CANDIDATE_DECISION_MISMATCH")

        decision_time = _aware_datetime(self.decision_at, "decision_at")
        decision_at = decision_time.isoformat()
        trade_date = decision_time.date().isoformat()
        if str(self.trade_date) != trade_date:
            raise ValueError("TRADE_DATE_MISMATCH")

        normalized_features: dict[str, TimedFeature] = {}
        for name, feature in self.features.items():
            if not isinstance(feature, TimedFeature):
                raise TypeError(f"feature {name!r} must be TimedFeature")
            available_time = _aware_datetime(
                feature.available_at, f"features.{name}.available_at"
            )
            if available_time > decision_time:
                raise ValueError(f"FEATURE_FROM_FUTURE: {name}")
            normalized_features[str(name)] = feature

        object.__setattr__(self, "trade_date", trade_date)
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(self, "code", _normalize_code(self.code))
        object.__setattr__(self, "features", _freeze(normalized_features))
        expected_id = candidate_sample_id(self)
        if supplied_id and supplied_id != expected_id:
            raise ValueError("SAMPLE_ID_MISMATCH")
        object.__setattr__(self, "sample_id", expected_id)

    def __hash__(self) -> int:
        return _stable_hash(self)

    @classmethod
    def from_values(
        cls,
        *,
        source: str,
        dataset_id: str,
        decision_at: str,
        code: str,
        strategy_version: str,
        parameter_version: str,
        feature_schema_version: str,
        features: Mapping[str, TimedFeature],
        selected: bool,
        rejection_stage: str,
        rejection_code: str,
        final_action: str,
        universe_hash: str,
        market_data_version: str,
        code_hash: str,
        generator_hash: str,
    ) -> "CandidateSample":
        decision_time = _aware_datetime(decision_at, "decision_at")
        return cls(
            sample_id="",
            source=str(source),
            dataset_id=str(dataset_id),
            trade_date=decision_time.date().isoformat(),
            decision_at=decision_time.isoformat(),
            code=str(code),
            strategy_version=str(strategy_version),
            parameter_version=str(parameter_version),
            feature_schema_version=str(feature_schema_version),
            features=features,
            selected=bool(selected),
            rejection_stage=str(rejection_stage),
            rejection_code=str(rejection_code),
            final_action=final_action,
            universe_hash=universe_hash,
            market_data_version=market_data_version,
            code_hash=code_hash,
            generator_hash=generator_hash,
        )


@dataclass(frozen=True)
class HorizonLabel:
    horizon_days: int
    gross_return: float
    net_return: float
    exit_price: float
    buy_commission_yuan: float
    buy_transfer_fee_yuan: float
    buy_other_fee_yuan: float
    buy_slippage_yuan: float
    sell_commission_yuan: float
    sell_stamp_tax_yuan: float
    sell_transfer_fee_yuan: float
    sell_other_fee_yuan: float
    sell_slippage_yuan: float
    total_cost_yuan: float
    cost_rate: float
    matured_at: str
    market_data_sha256: str

    def __post_init__(self) -> None:
        if self.horizon_days not in {3, 5, 10}:
            raise ValueError("horizon_days must be 3, 5, or 10")
        for name in (
            "gross_return",
            "net_return",
            "exit_price",
            "buy_commission_yuan",
            "buy_transfer_fee_yuan",
            "buy_other_fee_yuan",
            "buy_slippage_yuan",
            "sell_commission_yuan",
            "sell_stamp_tax_yuan",
            "sell_transfer_fee_yuan",
            "sell_other_fee_yuan",
            "sell_slippage_yuan",
            "total_cost_yuan",
            "cost_rate",
        ):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.exit_price <= 0 or self.total_cost_yuan < 0 or self.cost_rate < 0:
            raise ValueError("horizon price and costs are invalid")
        object.__setattr__(
            self,
            "matured_at",
            _aware_datetime(self.matured_at, "matured_at").isoformat(),
        )
        object.__setattr__(
            self,
            "market_data_sha256",
            _required_text(self.market_data_sha256, "market_data_sha256"),
        )

    def __hash__(self) -> int:
        return _stable_hash(self)


@dataclass(frozen=True)
class DownsideLabel:
    status: str
    mfe_10d_net: float | None
    mae_10d_net: float | None
    downside_loss: float | None
    paused_path: int
    exit_blocked: int
    hit_stop: int | None
    hit_take: int | None
    failure_reason: str
    matured_at: str | None
    evidence_sha256: str

    def __post_init__(self) -> None:
        status = str(self.status).strip().lower()
        if status not in {"pending", "complete", "failed"}:
            raise ValueError("UNKNOWN_DOWNSIDE_STATUS")
        object.__setattr__(self, "status", status)
        for name in ("mfe_10d_net", "mae_10d_net", "downside_loss"):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in {int, float} or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite or None")
        if self.mae_10d_net is not None and self.mae_10d_net > 0:
            raise ValueError("mae_10d_net must be non-positive")
        if self.downside_loss is not None and self.downside_loss < 0:
            raise ValueError("downside_loss must be non-negative")
        if (
            self.mae_10d_net is not None
            and self.downside_loss is not None
            and abs(self.downside_loss + self.mae_10d_net) > 1e-12
        ):
            raise ValueError("downside_loss must equal negative mae_10d_net")
        for name in ("paused_path", "exit_blocked", "hit_stop", "hit_take"):
            value = getattr(self, name)
            if value not in {None, 0, 1}:
                raise ValueError(f"{name} must be 0, 1, or None")
        object.__setattr__(self, "failure_reason", str(self.failure_reason).strip())
        if self.matured_at is not None:
            object.__setattr__(
                self,
                "matured_at",
                _aware_datetime(self.matured_at, "matured_at").isoformat(),
            )
        object.__setattr__(
            self,
            "evidence_sha256",
            _required_text(self.evidence_sha256, "evidence_sha256"),
        )

    def __hash__(self) -> int:
        return _stable_hash(self)


@dataclass(frozen=True)
class LabelRecord:
    sample_id: str
    label_version: str
    label_source: str
    cost_version: str
    label_id: str = ""
    cost_sha256: str = ""
    policy_version: str = ""
    policy_sha256: str = ""
    candidate_source: str = ""
    dataset_id: str = ""
    trade_date: str = ""
    decision_at: str | None = None
    code: str = ""
    candidate_content_sha256: str = ""
    candidate_origin: str = ""
    fill_label: int | None = None
    fill_status: str = "pending"
    fill_reason: str = ""
    fill_evidence_sha256: str = ""
    fill_delay_sec: float | None = None
    fill_price: float | None = None
    fill_at: str | None = None
    fill_matured_at: str | None = None
    reference_qty: int = 100
    reference_notional_yuan: float = 10_000.0
    reference_trade_notional_yuan: float | None = None
    ret_3d_gross: float | None = None
    ret_3d_net: float | None = None
    ret_5d_gross: float | None = None
    ret_5d_net: float | None = None
    ret_10d_gross: float | None = None
    ret_10d_net: float | None = None
    mfe_10d: float | None = None
    mae_10d: float | None = None
    downside_loss: float | None = None
    hit_stop: int | None = None
    hit_take: int | None = None
    exit_blocked: int = 0
    paused_path: int = 0
    buy_cost: float | None = None
    sell_cost: float | None = None
    slippage_cost: float | None = None
    commission_cost: float | None = None
    stamp_tax_cost: float | None = None
    transfer_fee_cost: float | None = None
    other_fee_cost: float | None = None
    net_cost: float | None = None
    actual_net_pnl: float | None = None
    quality_status: str = "pending"
    quality_reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ()
    market_data_sha256: str = ""
    matured_3d_at: str | None = None
    matured_5d_at: str | None = None
    matured_10d_at: str | None = None
    downside_matured_at: str | None = None
    matured_at: str | None = None
    horizons: tuple[HorizonLabel, ...] = ()
    downside: DownsideLabel | None = None

    def __post_init__(self) -> None:
        supplied_label_id = str(self.label_id)
        for name in ("sample_id", "label_version", "label_source", "cost_version"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        fill_status = str(self.fill_status).strip().lower()
        if fill_status not in {
            "pending",
            "filled",
            "not_filled",
            "failed",
            "invalid",
        }:
            raise ValueError("UNKNOWN_FILL_STATUS")
        object.__setattr__(self, "fill_status", fill_status)
        object.__setattr__(self, "fill_reason", str(self.fill_reason).strip())
        if self.fill_label not in {None, 0, 1}:
            raise ValueError("fill_label must be 0, 1, or None")
        if type(self.reference_qty) is not int or self.reference_qty <= 0:
            raise ValueError("reference_qty must be a positive integer")
        if (
            type(self.reference_notional_yuan) not in {int, float}
            or not math.isfinite(float(self.reference_notional_yuan))
            or self.reference_notional_yuan <= 0
        ):
            raise ValueError("reference_notional_yuan must be positive and finite")
        if self.reference_trade_notional_yuan is not None and (
            type(self.reference_trade_notional_yuan) not in {int, float}
            or not math.isfinite(float(self.reference_trade_notional_yuan))
            or self.reference_trade_notional_yuan <= 0
        ):
            raise ValueError(
                "reference_trade_notional_yuan must be positive and finite or None"
            )
        for name in ("hit_stop", "hit_take", "exit_blocked", "paused_path"):
            value = getattr(self, name)
            if value not in {None, 0, 1}:
                raise ValueError(f"{name} must be 0, 1, or None")
        for name in (
            "fill_delay_sec",
            "fill_price",
            "ret_3d_gross",
            "ret_3d_net",
            "ret_5d_gross",
            "ret_5d_net",
            "ret_10d_gross",
            "ret_10d_net",
            "mfe_10d",
            "mae_10d",
            "downside_loss",
            "buy_cost",
            "sell_cost",
            "slippage_cost",
            "commission_cost",
            "stamp_tax_cost",
            "transfer_fee_cost",
            "other_fee_cost",
            "net_cost",
            "actual_net_pnl",
        ):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in {int, float} or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite or None")
        quality_status = str(self.quality_status).strip().lower()
        if quality_status not in {"pending", "partial", "complete", "failed"}:
            raise ValueError("UNKNOWN_LABEL_QUALITY_STATUS")
        object.__setattr__(self, "quality_status", quality_status)
        object.__setattr__(
            self,
            "quality_reasons",
            tuple(sorted({str(reason).strip() for reason in self.quality_reasons if str(reason).strip()})),
        )
        for name in ("flags", "failure_reasons"):
            object.__setattr__(
                self,
                name,
                tuple(
                    sorted(
                        {
                            str(reason).strip()
                            for reason in getattr(self, name)
                            if str(reason).strip()
                        }
                    )
                ),
            )
        normalized_horizons = tuple(sorted(self.horizons, key=lambda item: item.horizon_days))
        if any(not isinstance(item, HorizonLabel) for item in normalized_horizons):
            raise TypeError("horizons must contain HorizonLabel records")
        if len({item.horizon_days for item in normalized_horizons}) != len(normalized_horizons):
            raise ValueError("duplicate horizon label")
        object.__setattr__(self, "horizons", normalized_horizons)
        if self.downside is not None and not isinstance(self.downside, DownsideLabel):
            raise TypeError("downside must be DownsideLabel or None")
        if self.decision_at is not None:
            object.__setattr__(
                self,
                "decision_at",
                _aware_datetime(self.decision_at, "decision_at").isoformat(),
            )
        if self.code:
            object.__setattr__(self, "code", _normalize_code(self.code))
        for name in (
            "fill_at",
            "fill_matured_at",
            "matured_3d_at",
            "matured_5d_at",
            "matured_10d_at",
            "downside_matured_at",
            "matured_at",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _aware_datetime(value, name).isoformat(),
                )
        expected_label_id = label_record_id(self)
        if supplied_label_id and supplied_label_id != expected_label_id:
            raise ValueError("LABEL_ID_MISMATCH")
        object.__setattr__(self, "label_id", expected_label_id)

    def __hash__(self) -> int:
        return _stable_hash(self)


@dataclass(frozen=True)
class PredictionRecord:
    sample_id: str
    model_id: str
    created_at: str
    expected_ret_3d: float | None = None
    expected_ret_5d: float | None = None
    expected_ret_10d: float | None = None
    downside_risk: float | None = None
    fill_probability: float | None = None
    ml_score: float | None = None
    ml_filter: bool | None = None
    position_multiplier: float | None = None
    confidence: float | None = None
    feature_coverage: float | None = None
    max_feature_psi: float | None = None
    drift_status: str = "unknown"
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "created_at",
            _aware_datetime(self.created_at, "created_at").isoformat(),
        )
        for name in ("feature_coverage", "max_feature_psi"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool):
                raise ValueError(f"{name} must be finite")
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{name} must be finite") from exc
            if not math.isfinite(number):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, number)
        if self.feature_coverage is not None and not 0 <= self.feature_coverage <= 1:
            raise ValueError("feature_coverage must be between 0 and 1")
        if self.max_feature_psi is not None and self.max_feature_psi < 0:
            raise ValueError("max_feature_psi must be non-negative")
        drift_status = str(self.drift_status or "unknown").strip().lower()
        if drift_status not in {"unknown", "ready", "insufficient"}:
            raise ValueError("drift_status is invalid")
        object.__setattr__(self, "drift_status", drift_status)
        reasons = tuple(str(value).strip() for value in self.reasons)
        if any(not value for value in reasons):
            raise ValueError("prediction reasons must be non-empty text")
        object.__setattr__(self, "reasons", reasons)

    def __hash__(self) -> int:
        return _stable_hash(self)


@dataclass(frozen=True)
class ModelManifest:
    model_id: str
    parent_model_id: str | None
    feature_names: tuple[str, ...]
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    holdout_start: str
    holdout_end: str
    dataset_sha256: str
    code_sha256: str
    config_sha256: str
    artifact_sha256: str
    parameter_version: str
    cost_version: str
    dependency_versions: Mapping[str, str]
    metrics: Mapping[str, object]
    created_at: str
    split_sha256: str = ""
    search_inputs_hash: str = ""
    holdout_metrics: Mapping[str, object] = MappingProxyType({})
    permission_level: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "created_at",
            _aware_datetime(self.created_at, "created_at").isoformat(),
        )
        object.__setattr__(self, "feature_names", tuple(self.feature_names))
        object.__setattr__(
            self, "dependency_versions", _freeze(self.dependency_versions)
        )
        object.__setattr__(self, "metrics", _freeze(self.metrics))
        object.__setattr__(self, "holdout_metrics", _freeze(self.holdout_metrics))

    def __hash__(self) -> int:
        return _stable_hash(self)


def canonical_hash(value: object) -> str:
    payload = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_hash(value: object) -> int:
    return int(canonical_hash(value)[:15], 16)


def candidate_sample_id(sample: CandidateSample) -> str:
    return canonical_hash(
        {
            "source": sample.source,
            "dataset_id": sample.dataset_id,
            "trade_date": sample.trade_date,
            "decision_at": sample.decision_at,
            "code": sample.code,
            "strategy_version": sample.strategy_version,
            "parameter_version": sample.parameter_version,
            "feature_schema_version": sample.feature_schema_version,
        }
    )


def label_record_id(label: LabelRecord) -> str:
    version = str(label.label_version).casefold()
    if version in {"l1", "label-v1"} or version.endswith("-v1"):
        return str(label.sample_id)
    return canonical_hash(
        {
            "sample_id": label.sample_id,
            "label_source": label.label_source,
            "label_version": label.label_version,
            "cost_identity": label.cost_sha256 or label.cost_version,
            "policy_identity": label.policy_sha256 or label.policy_version,
        }
    )


def _aware_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field}")
    return parsed


def _normalize_code(value: str) -> str:
    code = "".join(filter(str.isdigit, str(value))).zfill(6)
    if len(code) != 6:
        raise ValueError("INVALID_STOCK_CODE")
    return code


def _required_text(value: object, field: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"REQUIRED_FIELD: {field}")
    return text


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _canonical_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    return value
