"""Immutable records shared by the trained-shadow-model pipeline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType


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
    final_action: str = ""
    universe_hash: str = ""
    market_data_version: str = ""
    code_hash: str = ""
    generator_hash: str = ""

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
            "final_action",
            "universe_hash",
            "market_data_version",
            "code_hash",
            "generator_hash",
        ):
            object.__setattr__(self, field_name, str(getattr(self, field_name)))
        object.__setattr__(self, "selected", bool(self.selected))

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
        final_action: str = "",
        universe_hash: str = "",
        market_data_version: str = "",
        code_hash: str = "",
        generator_hash: str = "",
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
            final_action=str(final_action),
            universe_hash=str(universe_hash),
            market_data_version=str(market_data_version),
            code_hash=str(code_hash),
            generator_hash=str(generator_hash),
        )


@dataclass(frozen=True)
class LabelRecord:
    sample_id: str
    label_version: str
    label_source: str
    cost_version: str
    fill_label: int | None = None
    fill_delay_sec: float | None = None
    fill_price: float | None = None
    ret_3d_net: float | None = None
    ret_5d_net: float | None = None
    ret_10d_net: float | None = None
    mfe_10d: float | None = None
    mae_10d: float | None = None
    hit_stop: int | None = None
    hit_take: int | None = None
    actual_net_pnl: float | None = None
    market_data_sha256: str = ""
    matured_at: str = ""


@dataclass(frozen=True)
class PredictionRecord:
    sample_id: str
    model_id: str
    expected_ret_3d: float | None = None
    expected_ret_5d: float | None = None
    expected_ret_10d: float | None = None
    downside_risk: float | None = None
    fill_probability: float | None = None
    ml_score: float | None = None
    ml_filter: bool | None = None
    position_multiplier: float | None = None
    confidence: float | None = None
    created_at: str = ""


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
    split_sha256: str = ""
    search_inputs_hash: str = ""
    holdout_metrics: Mapping[str, object] = MappingProxyType({})
    permission_level: int = 0
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_names", tuple(self.feature_names))
        object.__setattr__(
            self, "dependency_versions", _freeze(self.dependency_versions)
        )
        object.__setattr__(self, "metrics", _freeze(self.metrics))
        object.__setattr__(self, "holdout_metrics", _freeze(self.holdout_metrics))


def canonical_hash(value: object) -> str:
    payload = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _canonical_value(value: object) -> object:
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
