"""Leakage-safe training frames, temporal splits, and ML data gates.

This module intentionally stops before preprocessing or model fitting.  It keeps
the caller-provided feature allowlist exact, validates point-in-time evidence,
weights repeated intraday samples as one stock-day, and exposes date-only
walk-forward splits with a sealed holdout.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

import pandas as pd

from ml_contracts import TimedFeature, canonical_hash


FORBIDDEN_MODEL_FEATURES = frozenset(
    {
        "code",
        "name",
        "stock_code",
        "stock_name",
        "enhanced_score",
        "shadow_adjust_score",
        "shadow_rank",
        "shadow_rank_change",
        "shadow_reason",
        "future_result",
        "future_return",
        "future_price",
        "order_status",
        "order_reason",
        "order_id",
        "filled",
        "fill_label",
        "fill_price",
        "fill_delay_sec",
        "ret_3d_net",
        "ret_5d_net",
        "ret_10d_net",
        "downside_loss",
        "actual_net_pnl",
        "hit_stop",
        "hit_take",
    }
)

REGIMES = ("NORMAL", "CAUTION", "RISK_OFF")

CORE_LABEL_COLUMNS = (
    "label_id",
    "label_version",
    "label_source",
    "cost_version",
    "cost_sha256",
    "policy_version",
    "policy_sha256",
    "fill_label",
    "fill_status",
    "fill_delay_sec",
    "fill_price",
    "fill_matured_at",
    "entry_ref",
    "ret_3d_gross",
    "ret_3d_net",
    "ret_3d_matured_at",
    "matured_3d_at",
    "ret_5d_gross",
    "ret_5d_net",
    "ret_5d_matured_at",
    "matured_5d_at",
    "ret_10d_gross",
    "ret_10d_net",
    "ret_10d_matured_at",
    "matured_10d_at",
    "downside_loss",
    "downside_matured_at",
    "mfe_10d",
    "mae_10d",
    "buy_cost",
    "sell_cost",
    "slippage_cost",
    "other_cost",
    "net_cost",
    "paused_path",
    "exit_blocked",
    "hit_stop",
    "hit_take",
    "actual_net_pnl",
    "quality_reason",
    "quality_failure_reason",
    "market_data_sha256",
    "content_sha256",
    "matured_at",
)

RULE_AUDIT_COLUMNS = (
    "rule_selected",
    "rule_rejection_stage",
    "rule_rejection_code",
    "rule_final_action",
    "rule_score",
    "rule_order",
    "rule_target_qty",
    "rule_slot_count",
)

STRICT_LABEL_SOURCES = frozenset({"strict", "strict_counterfactual_v2"})


@dataclass(frozen=True)
class TrainingFrame:
    """Raw, unprocessed model rows and their frozen feature schema."""

    rows: pd.DataFrame
    feature_names: tuple[str, ...]
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.rows, pd.DataFrame):
            raise TypeError("rows must be a pandas DataFrame")
        normalized = self.rows.copy(deep=True).reset_index(drop=True)
        names = tuple(str(name) for name in self.feature_names)
        if len(names) != len(set(names)):
            raise ValueError("DUPLICATE_FEATURE_ALLOWLIST")
        missing = [name for name in names if name not in normalized.columns]
        if missing:
            raise ValueError(
                "TRAINING_FRAME_MISSING_FEATURES: " + ",".join(sorted(missing))
            )
        metadata = dict(self.metadata)
        metadata.setdefault("feature_names", names)
        metadata.setdefault("dataset_sha256", _dataframe_sha256(normalized, names))
        object.__setattr__(self, "rows", normalized)
        object.__setattr__(self, "feature_names", names)
        object.__setattr__(self, "metadata", _freeze(metadata))

    @property
    def features(self) -> pd.DataFrame:
        return self.rows.loc[:, list(self.feature_names)].copy(deep=True)

    @property
    def labels(self) -> pd.DataFrame:
        columns = [
            column
            for column in self.rows.columns
            if column in CORE_LABEL_COLUMNS
            or column.endswith("_matured_at")
            or column.startswith("quality_")
        ]
        return self.rows.loc[:, columns].copy(deep=True)

    @property
    def weights(self) -> pd.Series:
        if "sample_weight" not in self.rows.columns:
            return pd.Series(dtype=float, name="sample_weight")
        return self.rows["sample_weight"].copy(deep=True)

    @property
    def trade_dates(self) -> tuple[str, ...]:
        return tuple(_ordered_trade_dates(self))

    @property
    def dataset_sha256(self) -> str:
        return str(self.metadata["dataset_sha256"])


@dataclass(frozen=True)
class WalkForwardFold:
    fold_index: int
    train_dates: tuple[str, ...]
    test_dates: tuple[str, ...]
    embargo_dates: tuple[str, ...]
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]

    @property
    def validation_dates(self) -> tuple[str, ...]:
        return self.test_dates

    @property
    def validation_indices(self) -> tuple[int, ...]:
        return self.test_indices

    @property
    def train_start(self) -> str:
        return self.train_dates[0]

    @property
    def train_end(self) -> str:
        return self.train_dates[-1]

    @property
    def test_start(self) -> str:
        return self.test_dates[0]

    @property
    def test_end(self) -> str:
        return self.test_dates[-1]

    @property
    def validation_start(self) -> str:
        return self.test_start

    @property
    def validation_end(self) -> str:
        return self.test_end


@dataclass(frozen=True)
class TrainingSplits:
    walk_forward: tuple[WalkForwardFold, ...]
    holdout_dates: tuple[str, ...]
    all_development_dates: tuple[str, ...]
    holdout_indices: tuple[int, ...]
    embargo_days: int
    split_sha256: str

    @property
    def development_dates(self) -> tuple[str, ...]:
        return self.all_development_dates

    @property
    def folds(self) -> tuple[WalkForwardFold, ...]:
        return self.walk_forward

    @property
    def holdout_start(self) -> str:
        return self.holdout_dates[0]

    @property
    def holdout_end(self) -> str:
        return self.holdout_dates[-1]


@dataclass(frozen=True)
class DataReadiness:
    linear_diagnostic_ready: bool
    gradient_diagnostic_ready: bool
    diagnostic_ready: bool
    l0_ready: bool
    require_l0: bool
    eligible: bool
    approvable: bool
    diagnostic_reasons: tuple[str, ...]
    l0_reasons: tuple[str, ...]
    reasons: tuple[str, ...]
    metrics: Mapping[str, object]
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostic_reasons", tuple(self.diagnostic_reasons))
        object.__setattr__(self, "l0_reasons", tuple(self.l0_reasons))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "metrics", _freeze(self.metrics))


def assign_stock_day_weights(frame: pd.DataFrame) -> pd.Series:
    """Return reciprocal weights so each code/trade-date totals exactly one."""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    required = {"trade_date", "code"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError("WEIGHT_COLUMNS_MISSING: " + ",".join(missing))
    if frame.empty:
        return pd.Series(index=frame.index, dtype=float, name="sample_weight")
    counts = frame.groupby(["trade_date", "code"], dropna=False)["code"].transform(
        "size"
    )
    if (counts <= 0).any():
        raise ValueError("INVALID_STOCK_DAY_COUNT")
    weights = 1.0 / counts.astype(float)
    weights.name = "sample_weight"
    return weights


def build_training_frame(
    candidates: object,
    labels: object,
    feature_allowlist: Iterable[str],
) -> TrainingFrame:
    """Join candidate facts to labels after strict point-in-time validation."""

    feature_names = tuple(str(name).strip() for name in feature_allowlist)
    if not feature_names or any(not name for name in feature_names):
        raise ValueError("EMPTY_FEATURE_ALLOWLIST")
    if len(feature_names) != len(set(feature_names)):
        raise ValueError("DUPLICATE_FEATURE_ALLOWLIST")
    for name in feature_names:
        if _is_forbidden_feature(name):
            raise ValueError(f"FORBIDDEN_MODEL_FEATURE: {name}")

    normalized_candidates: dict[str, dict[str, object]] = {}
    for raw_candidate in _iter_records(candidates):
        candidate = _normalize_candidate(raw_candidate, feature_names)
        sample_id = str(candidate["sample_id"])
        previous = normalized_candidates.get(sample_id)
        if previous is not None:
            if canonical_hash(previous) != canonical_hash(candidate):
                raise ValueError(f"DUPLICATE_CANDIDATE_CONFLICT: {sample_id}")
            continue
        normalized_candidates[sample_id] = candidate
    if not normalized_candidates:
        raise ValueError("NO_CANDIDATES")

    normalized_labels: dict[str, dict[str, object]] = {}
    label_columns: set[str] = set(CORE_LABEL_COLUMNS)
    for raw_label in _iter_records(labels):
        label = _normalize_label(raw_label)
        sample_id = str(label["sample_id"])
        if sample_id not in normalized_candidates:
            raise ValueError(f"LABEL_WITHOUT_CANDIDATE: {sample_id}")
        previous = normalized_labels.get(sample_id)
        if previous is not None:
            if canonical_hash(previous) != canonical_hash(label):
                raise ValueError(f"DUPLICATE_LABEL_CONFLICT: {sample_id}")
            continue
        normalized_labels[sample_id] = label
        label_columns.update(key for key in label if key != "sample_id")

    candidate_rows = list(normalized_candidates.values())
    _require_single_version(candidate_rows, "source", "MIXED_CANDIDATE_SOURCE")
    _require_single_version(candidate_rows, "dataset_id", "MIXED_DATASET_ID")
    _require_single_version(
        candidate_rows, "strategy_version", "MIXED_STRATEGY_VERSION"
    )
    _require_single_version(
        candidate_rows, "parameter_version", "MIXED_PARAMETER_VERSION"
    )
    _require_single_version(
        candidate_rows,
        "feature_schema_version",
        "MIXED_FEATURE_SCHEMA_VERSION",
    )
    label_rows = list(normalized_labels.values())
    if label_rows:
        _require_single_version(label_rows, "label_version", "MIXED_LABEL_VERSION")
        _require_single_version(label_rows, "label_source", "MIXED_LABEL_SOURCE")
        _require_single_version(label_rows, "cost_version", "MIXED_COST_VERSION")
        _require_single_optional_identity(
            label_rows, "cost_sha256", "MIXED_COST_SHA256"
        )
        _require_single_optional_identity(
            label_rows, "policy_sha256", "MIXED_POLICY_SHA256"
        )

    ordered_label_columns = [
        *CORE_LABEL_COLUMNS,
        *sorted(label_columns.difference(CORE_LABEL_COLUMNS)),
    ]
    rows: list[dict[str, object]] = []
    for candidate in sorted(
        candidate_rows,
        key=lambda row: (
            str(row["trade_date"]),
            str(row["decision_at"]),
            str(row["code"]),
            str(row["sample_id"]),
        ),
    ):
        sample_id = str(candidate["sample_id"])
        label = normalized_labels.get(sample_id, {})
        row = {
            key: value
            for key, value in candidate.items()
            if key != "feature_times"
        }
        for column in ordered_label_columns:
            row[column] = label.get(column)
        rows.append(row)

    dataframe = pd.DataFrame.from_records(rows)
    dataframe["sample_weight"] = assign_stock_day_weights(dataframe)
    metadata = {
        "candidate_count": len(dataframe),
        "labeled_count": len(normalized_labels),
        "feature_names": feature_names,
        "label_names": tuple(ordered_label_columns),
        "source": _only_value(candidate_rows, "source"),
        "dataset_id": _only_value(candidate_rows, "dataset_id"),
        "strategy_version": _only_value(candidate_rows, "strategy_version"),
        "parameter_version": _only_value(candidate_rows, "parameter_version"),
        "feature_schema_version": _only_value(
            candidate_rows, "feature_schema_version"
        ),
        "label_version": _only_value(label_rows, "label_version"),
        "label_source": _only_value(label_rows, "label_source"),
        "cost_version": _only_value(label_rows, "cost_version"),
        "cost_sha256": _only_value(label_rows, "cost_sha256"),
        "policy_version": _only_value(label_rows, "policy_version"),
        "policy_sha256": _only_value(label_rows, "policy_sha256"),
        "construction_source": "build_training_frame-v2",
    }
    metadata["strict_provenance_sha256"] = _strict_provenance_sha256(
        dataframe, feature_names
    )
    return TrainingFrame(
        rows=dataframe,
        feature_names=feature_names,
        metadata=metadata,
    )


def build_ml_splits(
    frame: TrainingFrame | pd.DataFrame | Iterable[object],
    holdout_days: int = 40,
    folds: int = 3,
    embargo_days: int = 10,
) -> TrainingSplits:
    """Build expanding date-only folds after sealing the final holdout dates."""

    holdout_days = int(holdout_days)
    folds = int(folds)
    embargo_days = int(embargo_days)
    if folds < 3:
        raise ValueError("AT_LEAST_THREE_FOLDS_REQUIRED")
    if holdout_days <= 0:
        raise ValueError("HOLDOUT_DAYS_MUST_BE_POSITIVE")
    if embargo_days < 0:
        raise ValueError("EMBARGO_DAYS_MUST_BE_NONNEGATIVE")

    trade_dates = _ordered_trade_dates(frame)
    if len(trade_dates) <= holdout_days:
        raise ValueError("INSUFFICIENT_DATES_FOR_SPLITS")
    development_dates = trade_dates[:-holdout_days]
    holdout_dates = trade_dates[-holdout_days:]
    validation_size = len(development_dates) // (folds + 1)
    first_validation_start = len(development_dates) - folds * validation_size
    if validation_size <= 0 or first_validation_start - embargo_days <= 0:
        raise ValueError("INSUFFICIENT_DATES_FOR_SPLITS")

    row_dates = _row_trade_dates(frame)
    walk_forward: list[WalkForwardFold] = []
    for fold_index in range(folds):
        test_start = first_validation_start + fold_index * validation_size
        test_end = (
            len(development_dates)
            if fold_index == folds - 1
            else test_start + validation_size
        )
        train_end = test_start - embargo_days
        train_dates = tuple(development_dates[:train_end])
        embargo = tuple(development_dates[train_end:test_start])
        test_dates = tuple(development_dates[test_start:test_end])
        if not train_dates or not test_dates or len(embargo) != embargo_days:
            raise ValueError("INSUFFICIENT_DATES_FOR_SPLITS")
        walk_forward.append(
            WalkForwardFold(
                fold_index=fold_index,
                train_dates=train_dates,
                test_dates=test_dates,
                embargo_dates=embargo,
                train_indices=_indices_for_dates(row_dates, train_dates),
                test_indices=_indices_for_dates(row_dates, test_dates),
            )
        )

    hash_payload = {
        "development_dates": development_dates,
        "holdout_dates": holdout_dates,
        "embargo_days": embargo_days,
        "walk_forward": [
            {
                "fold_index": fold.fold_index,
                "train_dates": fold.train_dates,
                "test_dates": fold.test_dates,
                "embargo_dates": fold.embargo_dates,
            }
            for fold in walk_forward
        ],
    }
    return TrainingSplits(
        walk_forward=tuple(walk_forward),
        holdout_dates=tuple(holdout_dates),
        all_development_dates=tuple(development_dates),
        holdout_indices=_indices_for_dates(row_dates, holdout_dates),
        embargo_days=embargo_days,
        split_sha256=canonical_hash(hash_payload),
    )


def validate_training_data(
    frame: TrainingFrame,
    splits: TrainingSplits | None = None,
    require_l0: bool = False,
) -> DataReadiness:
    """Return diagnostic and L0 data readiness without approving any model."""

    if not isinstance(frame, TrainingFrame):
        raise TypeError("frame must be TrainingFrame")
    splits = splits or build_ml_splits(frame)
    if not isinstance(splits, TrainingSplits):
        raise TypeError("splits must be TrainingSplits")
    rows = frame.rows
    if rows.empty:
        raise ValueError("EMPTY_TRAINING_FRAME")

    trade_dates = _ordered_trade_dates(frame)
    stock_days = int(rows[["trade_date", "code"]].drop_duplicates().shape[0])
    trading_days = len(trade_dates)
    first_date = date.fromisoformat(trade_dates[0])
    last_date = date.fromisoformat(trade_dates[-1])
    calendar_span_days = (last_date - first_date).days + 1

    integrity_reasons = _training_integrity_reasons(frame)
    split_reasons = _split_reasons(frame, splits)
    diagnostic_reasons: list[str] = [*integrity_reasons, *split_reasons]
    if stock_days < 5_000:
        diagnostic_reasons.append("LINEAR_STOCK_DAYS_LT_5000")
    if trading_days < 120:
        diagnostic_reasons.append("LINEAR_TRADING_DAYS_LT_120")

    fill_mature_mask = _maturity_mask(rows, "fill")
    fill_values = _numeric_series(rows, "fill_label")
    mature_fill_values = fill_values[fill_mature_mask & fill_values.notna()]
    fill_classes = tuple(sorted({int(value) for value in mature_fill_values.tolist()}))
    if len(fill_classes) < 2:
        diagnostic_reasons.append("FILL_LABEL_SINGLE_CLASS")

    linear_ready = not diagnostic_reasons
    gradient_reasons: list[str] = [*integrity_reasons, *split_reasons]
    if stock_days < 15_000:
        gradient_reasons.append("GRADIENT_STOCK_DAYS_LT_15000")
    if trading_days < 180:
        gradient_reasons.append("GRADIENT_TRADING_DAYS_LT_180")
    if len(fill_classes) < 2:
        gradient_reasons.append("FILL_LABEL_SINGLE_CLASS")
    gradient_ready = not gradient_reasons
    for reason in gradient_reasons:
        if reason not in diagnostic_reasons:
            diagnostic_reasons.append(reason)

    coverage_denominators: dict[str, int] = {}
    coverage_numerators: dict[str, int] = {}
    quality_failures: dict[str, int] = {}
    coverage: dict[str, float] = {}

    fill_denominator = int(fill_mature_mask.sum())
    fill_numerator = int((fill_mature_mask & fill_values.isin([0, 1])).sum())
    _record_coverage(
        "fill",
        fill_denominator,
        fill_numerator,
        coverage_denominators,
        coverage_numerators,
        quality_failures,
        coverage,
    )

    for key, column, maturity_key in (
        ("ret_3d", "ret_3d_net", "ret_3d"),
        ("ret_5d", "ret_5d_net", "ret_5d"),
        ("ret_10d", "ret_10d_net", "ret_10d"),
        ("downside", "downside_loss", "downside"),
    ):
        maturity = _maturity_mask(rows, maturity_key)
        denominator_mask = maturity & fill_values.eq(1)
        values = _numeric_series(rows, column)
        valid = denominator_mask & values.notna() & values.map(_finite_or_false)
        _record_coverage(
            key,
            int(denominator_mask.sum()),
            int(valid.sum()),
            coverage_denominators,
            coverage_numerators,
            quality_failures,
            coverage,
        )

    stock_day_rows = rows.drop_duplicates(["trade_date", "code"])
    regime_series = (
        stock_day_rows["market_regime"].astype("string").str.upper()
        if "market_regime" in stock_day_rows.columns
        else pd.Series("", index=stock_day_rows.index, dtype="string")
    )
    regimes: dict[str, dict[str, int]] = {}
    for regime in REGIMES:
        regime_rows = stock_day_rows[regime_series == regime]
        regimes[regime] = {
            "trading_days": int(regime_rows["trade_date"].nunique()),
            "stock_days": int(len(regime_rows)),
        }

    l0_reasons: list[str] = [*integrity_reasons, *split_reasons]
    if not gradient_ready:
        l0_reasons.append("L0_GRADIENT_DIAGNOSTIC_NOT_READY")
    if calendar_span_days < 365:
        l0_reasons.append("L0_CALENDAR_SPAN_LT_365")
    if trading_days < 240:
        l0_reasons.append("L0_TRADING_DAYS_LT_240")
    source_values = _column_values(rows, "source")
    if source_values != {"strict"}:
        l0_reasons.append("L0_CANDIDATE_SOURCE_NOT_STRICT")
    label_source_values = _column_values(rows, "label_source")
    if len(label_source_values) != 1 or not label_source_values.issubset(
        STRICT_LABEL_SOURCES
    ):
        l0_reasons.append("L0_LABEL_SOURCE_NOT_STRICT")
    if (
        str(frame.metadata.get("construction_source") or "")
        != "build_training_frame-v2"
        or str(frame.metadata.get("strict_provenance_sha256") or "")
        != _strict_provenance_sha256(rows, frame.feature_names)
    ):
        l0_reasons.append("L0_STRICT_BUILDER_PROVENANCE_REQUIRED")
    for column, reason in (
        ("cost_sha256", "L0_COST_SHA256_REQUIRED"),
        ("policy_sha256", "L0_POLICY_SHA256_REQUIRED"),
    ):
        values = _column_values(rows, column)
        if len(values) != 1:
            l0_reasons.append(reason)
    if not _rule_baseline_evidence_complete(rows):
        l0_reasons.append("L0_RULE_BASELINE_EVIDENCE_REQUIRED")
    if coverage["fill"] < 0.99:
        l0_reasons.append("FILL_COVERAGE_LT_0_99")
    for key, reason in (
        ("ret_3d", "RET_3D_COVERAGE_LT_0_90"),
        ("ret_5d", "RET_5D_COVERAGE_LT_0_90"),
        ("ret_10d", "RET_10D_COVERAGE_LT_0_90"),
        ("downside", "DOWNSIDE_COVERAGE_LT_0_90"),
    ):
        if coverage[key] < 0.90:
            l0_reasons.append(reason)
    for regime in REGIMES:
        if regimes[regime]["trading_days"] < 10:
            l0_reasons.append(f"REGIME_{regime}_TRADING_DAYS_LT_10")
        if regimes[regime]["stock_days"] < 500:
            l0_reasons.append(f"REGIME_{regime}_STOCK_DAYS_LT_500")

    diagnostic_reasons_tuple = tuple(_unique(diagnostic_reasons))
    l0_reasons_tuple = tuple(_unique(l0_reasons))
    l0_ready = not l0_reasons_tuple
    diagnostic_ready = linear_ready
    eligible = l0_ready if require_l0 else diagnostic_ready
    reasons = l0_reasons_tuple if require_l0 else diagnostic_reasons_tuple
    if l0_ready:
        status = "l0_data_ready"
    elif diagnostic_ready:
        status = "diagnostic_only"
    else:
        status = "not_ready"

    metrics = {
        "candidate_rows": int(len(rows)),
        "stock_days": stock_days,
        "trading_days": trading_days,
        "calendar_span_days": calendar_span_days,
        "first_trade_date": trade_dates[0],
        "last_trade_date": trade_dates[-1],
        "fill_classes": fill_classes,
        "fill_coverage": coverage["fill"],
        "ret_3d_coverage": coverage["ret_3d"],
        "ret_5d_coverage": coverage["ret_5d"],
        "ret_10d_coverage": coverage["ret_10d"],
        "downside_coverage": coverage["downside"],
        "coverage_denominators": coverage_denominators,
        "coverage_numerators": coverage_numerators,
        "quality_failures": quality_failures,
        "regimes": regimes,
        "dataset_sha256": frame.dataset_sha256,
        "split_sha256": splits.split_sha256,
        "holdout_days": len(splits.holdout_dates),
        "walk_forward_folds": len(splits.walk_forward),
        "embargo_days": splits.embargo_days,
    }
    return DataReadiness(
        linear_diagnostic_ready=linear_ready,
        gradient_diagnostic_ready=gradient_ready,
        diagnostic_ready=diagnostic_ready,
        l0_ready=l0_ready,
        require_l0=bool(require_l0),
        eligible=eligible,
        approvable=False,
        diagnostic_reasons=diagnostic_reasons_tuple,
        l0_reasons=l0_reasons_tuple,
        reasons=reasons,
        metrics=metrics,
        status=status,
    )


def _normalize_candidate(
    raw: object,
    feature_names: tuple[str, ...],
) -> dict[str, object]:
    record = _object_record(raw)
    sample_id = _required(record, "sample_id")
    decision_at = _aware_timestamp(_required(record, "decision_at"), "decision_at")
    decision_time = datetime.fromisoformat(decision_at)
    trade_date = _trade_date(_required(record, "trade_date"))
    if trade_date != decision_time.date().isoformat():
        raise ValueError("TRADE_DATE_MISMATCH")

    raw_features = record.get("features")
    if raw_features is None and record.get("features_json") is not None:
        values = _json_mapping(record["features_json"], "features_json")
        times = _json_mapping(record.get("feature_times_json"), "feature_times_json")
        if set(values) != set(times):
            raise ValueError("FEATURE_TIME_SET_MISMATCH")
        raw_features = {
            name: {"value": value, "available_at": times[name]}
            for name, value in values.items()
        }
    if raw_features is None:
        raw_features = {
            name: {
                "value": record[name],
                "available_at": record.get(f"{name}_available_at"),
            }
            for name in feature_names
            if name in record
        }
    if not isinstance(raw_features, Mapping):
        raise ValueError("FEATURES_NOT_MAPPING")

    feature_values: dict[str, object] = {}
    feature_times: dict[str, str] = {}
    audit_feature_values: dict[str, object] = {}
    for raw_name, raw_feature in raw_features.items():
        name = str(raw_name)
        if _is_forbidden_feature(name):
            raise ValueError(f"FORBIDDEN_MODEL_FEATURE: {name}")
        value, available_at = _timed_feature(raw_feature, name)
        available = _aware_timestamp(available_at, f"features.{name}.available_at")
        if datetime.fromisoformat(available) > decision_time:
            raise ValueError(f"FEATURE_FROM_FUTURE: {name}")
        if name in feature_names:
            feature_values[name] = _feature_value(value, name)
            feature_times[name] = available
        if name in {"final_score", "rule_score"}:
            audit_feature_values["rule_score"] = _feature_value(value, name)

    missing = [name for name in feature_names if name not in feature_values]
    if missing:
        raise ValueError(
            "FEATURE_NOT_IN_FROZEN_ALLOWLIST: " + ",".join(sorted(missing))
        )
    selected_features = {name: feature_values[name] for name in feature_names}
    regime = selected_features.get("market_regime")
    if regime is not None:
        normalized_regime = str(regime).upper()
        if normalized_regime not in REGIMES:
            raise ValueError(f"INVALID_MARKET_REGIME: {normalized_regime}")
        selected_features["market_regime"] = normalized_regime

    selected_value = record.get("selected")
    if selected_value is None:
        selected = None
    elif selected_value in {True, 1, "1", "true", "True"}:
        selected = True
    elif selected_value in {False, 0, "0", "false", "False"}:
        selected = False
    else:
        raise ValueError("INVALID_RULE_SELECTED")
    rule_score = record.get("rule_score", audit_feature_values.get("rule_score"))
    rule_order = record.get("rule_order")
    rule_target_qty = record.get("rule_target_qty", record.get("target_qty"))
    rule_slot_count = record.get("rule_slot_count", record.get("slot_count"))
    candidate_identity = {
        "sample_id": sample_id,
        "source": _required(record, "source"),
        "dataset_id": _required(record, "dataset_id"),
        "trade_date": trade_date,
        "decision_at": decision_at,
        "code": _code(_required(record, "code")),
        "strategy_version": _required(record, "strategy_version"),
        "parameter_version": _required(record, "parameter_version"),
        "feature_schema_version": _required(record, "feature_schema_version"),
        "rule_selected": selected,
        "rule_rejection_stage": str(record.get("rejection_stage") or ""),
        "rule_rejection_code": str(record.get("rejection_code") or ""),
        "rule_final_action": str(record.get("final_action") or ""),
        "rule_score": _optional_finite_number(rule_score, "rule_score"),
        "rule_order": _optional_nonnegative_integer(rule_order, "rule_order"),
        "rule_target_qty": _optional_nonnegative_integer(
            rule_target_qty, "rule_target_qty"
        ),
        "rule_slot_count": _optional_nonnegative_integer(
            rule_slot_count, "rule_slot_count"
        ),
        **selected_features,
        "feature_times": {name: feature_times[name] for name in feature_names},
    }
    candidate_identity["feature_times_sha256"] = canonical_hash(
        candidate_identity["feature_times"]
    )
    supplied_content_hash = str(
        record.get("candidate_content_sha256")
        or record.get("content_sha256")
        or ""
    ).strip()
    candidate_identity["candidate_content_sha256"] = (
        supplied_content_hash or canonical_hash(candidate_identity)
    )
    return candidate_identity


def _normalize_label(raw: object) -> dict[str, object]:
    record = _object_record(raw)
    normalized = {
        str(key): _label_value(value)
        for key, value in record.items()
        if str(key) != "sample_id"
    }
    normalized["sample_id"] = _required(record, "sample_id")
    for field, reason in (
        ("label_version", "LABEL_VERSION_REQUIRED"),
        ("label_source", "LABEL_SOURCE_REQUIRED"),
        ("cost_version", "COST_VERSION_REQUIRED"),
    ):
        if not str(normalized.get(field) or "").strip():
            raise ValueError(reason)
        normalized[field] = str(normalized[field]).strip()
    if normalized.get("matured_at") is not None:
        normalized["matured_at"] = _aware_timestamp(
            normalized["matured_at"], "matured_at"
        )
    for key in tuple(normalized):
        if key.endswith("_matured_at") and normalized[key] is not None:
            normalized[key] = _aware_timestamp(normalized[key], key)
    fill_label = normalized.get("fill_label")
    if fill_label is not None:
        try:
            fill_value = int(fill_label)
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_FILL_LABEL") from exc
        if fill_value not in (0, 1) or float(fill_label) != fill_value:
            raise ValueError("INVALID_FILL_LABEL")
        normalized["fill_label"] = fill_value
    for key, value in normalized.items():
        if key == "sample_id" or value is None:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise ValueError(f"NON_FINITE_LABEL: {key}")
    supplied_hash = str(normalized.get("content_sha256") or "").strip()
    normalized["label_content_sha256"] = supplied_hash or canonical_hash(
        {key: value for key, value in normalized.items() if key != "content_sha256"}
    )
    return normalized


def _training_integrity_reasons(frame: TrainingFrame) -> list[str]:
    rows = frame.rows
    reasons: list[str] = []
    for column, reason in (
        ("source", "MIXED_CANDIDATE_SOURCE"),
        ("dataset_id", "MIXED_DATASET_ID"),
        ("strategy_version", "MIXED_STRATEGY_VERSION"),
        ("parameter_version", "MIXED_PARAMETER_VERSION"),
        ("feature_schema_version", "MIXED_FEATURE_SCHEMA_VERSION"),
        ("label_version", "MIXED_LABEL_VERSION"),
        ("label_source", "MIXED_LABEL_SOURCE"),
        ("cost_version", "MIXED_COST_VERSION"),
    ):
        values = _column_values(rows, column)
        if len(values) > 1:
            reasons.append(reason)
        if not values:
            reasons.append(f"MISSING_{column.upper()}")
    for column, reason in (
        ("cost_sha256", "MIXED_COST_SHA256"),
        ("policy_sha256", "MIXED_POLICY_SHA256"),
    ):
        if len(_column_values(rows, column)) > 1:
            reasons.append(reason)
    if "sample_id" not in rows or rows["sample_id"].astype(str).duplicated().any():
        reasons.append("DUPLICATE_SAMPLE_ID")
    for feature in frame.feature_names:
        if _is_forbidden_feature(feature):
            reasons.append(f"FORBIDDEN_MODEL_FEATURE: {feature}")
        if feature not in rows:
            reasons.append(f"MISSING_MODEL_FEATURE: {feature}")
            continue
        values = rows[feature]
        for value in values:
            if value is None or value is pd.NA:
                continue
            if isinstance(value, float) and math.isnan(value):
                continue
            if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                reasons.append(f"NON_FINITE_FEATURE: {feature}")
                break
    if "fill_label" in rows:
        invalid_fill = pd.to_numeric(rows["fill_label"], errors="coerce")
        supplied = rows["fill_label"].notna()
        if (~invalid_fill[supplied].isin([0, 1])).any():
            reasons.append("INVALID_FILL_LABEL")
    return _unique(reasons)


def _split_reasons(frame: TrainingFrame, splits: TrainingSplits) -> list[str]:
    reasons: list[str] = []
    dates = _ordered_trade_dates(frame)
    date_positions = {value: index for index, value in enumerate(dates)}
    development = set(splits.all_development_dates)
    holdout = set(splits.holdout_dates)
    if len(splits.walk_forward) < 3:
        reasons.append("WALK_FORWARD_FOLDS_LT_3")
    if len(splits.holdout_dates) != 40:
        reasons.append("HOLDOUT_DAYS_NE_40")
    if development.intersection(holdout):
        reasons.append("HOLDOUT_OVERLAPS_DEVELOPMENT")
    if tuple(dates[-len(splits.holdout_dates):]) != splits.holdout_dates:
        reasons.append("HOLDOUT_NOT_FINAL_DATES")
    for fold in splits.walk_forward:
        if not fold.train_dates or not fold.test_dates:
            reasons.append(f"FOLD_{fold.fold_index}_EMPTY")
            continue
        if set(fold.train_dates).intersection(fold.test_dates):
            reasons.append(f"FOLD_{fold.fold_index}_TRAIN_TEST_OVERLAP")
        if set(fold.test_dates).intersection(holdout):
            reasons.append(f"FOLD_{fold.fold_index}_USES_HOLDOUT")
        unknown = (
            set(fold.train_dates)
            | set(fold.test_dates)
            | set(fold.embargo_dates)
        ).difference(date_positions)
        if unknown:
            reasons.append(f"FOLD_{fold.fold_index}_UNKNOWN_DATE")
            continue
        gap = (
            date_positions[fold.test_dates[0]]
            - date_positions[fold.train_dates[-1]]
            - 1
        )
        if gap < 10 or len(fold.embargo_dates) < 10:
            reasons.append(f"FOLD_{fold.fold_index}_EMBARGO_LT_10")
    return _unique(reasons)


def _record_coverage(
    key: str,
    denominator: int,
    numerator: int,
    denominators: dict[str, int],
    numerators: dict[str, int],
    failures: dict[str, int],
    coverage: dict[str, float],
) -> None:
    denominators[key] = denominator
    numerators[key] = numerator
    failures[key] = max(0, denominator - numerator)
    coverage[key] = numerator / denominator if denominator else 0.0


def _maturity_mask(rows: pd.DataFrame, key: str) -> pd.Series:
    aliases = {
        "fill": ("fill_matured_at", "fill_matured"),
        "ret_3d": ("ret_3d_matured_at", "matured_3d_at", "ret_3d_matured"),
        "ret_5d": ("ret_5d_matured_at", "matured_5d_at", "ret_5d_matured"),
        "ret_10d": (
            "ret_10d_matured_at",
            "matured_10d_at",
            "ret_10d_matured",
        ),
        "downside": ("downside_matured_at", "downside_matured"),
    }
    candidates = aliases.get(key, ())
    for column in candidates:
        if column not in rows:
            continue
        values = rows[column]
        if column.endswith("_at"):
            mask = values.notna() & values.astype("string").str.strip().ne("")
            if mask.any():
                return mask
            continue
        return values.fillna(False).astype(bool)
    return pd.Series(False, index=rows.index, dtype=bool)


def _iter_records(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if isinstance(value, Mapping):
        if "sample_id" in value:
            return [value]
        return list(value.values())
    if isinstance(value, (str, bytes)):
        raise TypeError("records must not be text")
    try:
        return list(value)  # type: ignore[arg-type]
    except TypeError:
        return [value]


def _object_record(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if hasattr(value, "keys") and hasattr(value, "__getitem__"):
        return {str(key): value[key] for key in value.keys()}  # type: ignore[index]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError("record must be a mapping or data record")


def _timed_feature(raw: object, name: str) -> tuple[object, object]:
    if isinstance(raw, TimedFeature):
        return raw.value, raw.available_at
    if isinstance(raw, Mapping):
        if "value" not in raw or "available_at" not in raw:
            raise ValueError(f"FEATURE_TIME_REQUIRED: {name}")
        return raw["value"], raw["available_at"]
    if hasattr(raw, "value") and hasattr(raw, "available_at"):
        return getattr(raw, "value"), getattr(raw, "available_at")
    raise ValueError(f"FEATURE_TIME_REQUIRED: {name}")


def _feature_value(value: object, name: str) -> object:
    value = _plain_value(value)
    if value is None or value is pd.NA:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"NON_FINITE_FEATURE: {name}")
        return value
    if isinstance(value, str):
        return value
    raise ValueError(f"UNSUPPORTED_FEATURE_VALUE: {name}")


def _plain_value(value: object) -> object:
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _label_value(value: object) -> object:
    value = _plain_value(value)
    if value is None or value is pd.NA:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _is_forbidden_feature(name: str) -> bool:
    normalized = str(name).strip().lower()
    return normalized in FORBIDDEN_MODEL_FEATURES or normalized.startswith("future_")


def _required(record: Mapping[str, object], field: str) -> str:
    value = record.get(field)
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"REQUIRED_FIELD: {field}")
    return text


def _aware_timestamp(value: object, field: str) -> str:
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field}")
    return parsed.isoformat()


def _trade_date(value: object) -> str:
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("INVALID_TRADE_DATE") from exc


def _code(value: object) -> str:
    digits = "".join(filter(str.isdigit, str(value)))
    if not digits or len(digits) > 6:
        raise ValueError("INVALID_STOCK_CODE")
    return digits.zfill(6)


def _json_mapping(value: object, field: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if value is None:
        raise ValueError(f"{field.upper()}_REQUIRED")
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"INVALID_{field.upper()}") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError(f"INVALID_{field.upper()}")
    return parsed


def _require_single_version(
    rows: Sequence[Mapping[str, object]],
    field: str,
    reason: str,
) -> None:
    values = {str(row.get(field) or "").strip() for row in rows}
    if "" in values:
        raise ValueError(f"REQUIRED_FIELD: {field}")
    if len(values) > 1:
        raise ValueError(reason)


def _require_single_optional_identity(
    rows: Sequence[Mapping[str, object]],
    field: str,
    reason: str,
) -> None:
    values = [str(row.get(field) or "").strip() for row in rows]
    supplied = {value for value in values if value}
    if len(supplied) > 1 or (supplied and any(not value for value in values)):
        raise ValueError(reason)


def _optional_finite_number(value: object, field: str) -> float | None:
    if value is None or value is pd.NA or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"INVALID_{field.upper()}") from exc
    if not math.isfinite(number):
        raise ValueError(f"NON_FINITE_{field.upper()}")
    return number


def _optional_nonnegative_integer(value: object, field: str) -> int | None:
    if value is None or value is pd.NA or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"INVALID_{field.upper()}")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"INVALID_{field.upper()}") from exc
    try:
        if float(value) != number:
            raise ValueError(f"INVALID_{field.upper()}")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"INVALID_{field.upper()}") from exc
    if number < 0:
        raise ValueError(f"INVALID_{field.upper()}")
    return number


def _rule_baseline_evidence_complete(rows: pd.DataFrame) -> bool:
    if rows.empty or any(column not in rows for column in RULE_AUDIT_COLUMNS):
        return False
    required_all = (
        "rule_selected",
        "rule_final_action",
        "rule_score",
        "rule_order",
        "rule_slot_count",
    )
    for column in required_all:
        values = rows[column]
        if values.isna().any() or values.astype("string").str.strip().eq("").any():
            return False
    selected = rows["rule_selected"].fillna(False).astype(bool)
    if selected.any() and rows.loc[selected, "rule_target_qty"].isna().any():
        return False
    return True


def _strict_provenance_sha256(
    rows: pd.DataFrame,
    feature_names: Sequence[str],
) -> str:
    columns = (
        "sample_id",
        "candidate_content_sha256",
        "feature_times_sha256",
        "label_id",
        "label_content_sha256",
        "cost_sha256",
        "policy_sha256",
    )
    payload_rows: list[dict[str, object]] = []
    for _, row in rows.iterrows():
        item: dict[str, object] = {}
        for column in columns:
            value = row[column] if column in rows else None
            item[column] = _plain_value(value)
        payload_rows.append(item)
    return canonical_hash(
        {
            "builder": "build_training_frame-v2",
            "feature_names": tuple(feature_names),
            "rows": payload_rows,
        }
    )


def _only_value(rows: Sequence[Mapping[str, object]], field: str) -> str:
    values = {str(row.get(field) or "").strip() for row in rows}
    values.discard("")
    return next(iter(values)) if len(values) == 1 else ""


def _ordered_trade_dates(
    frame: TrainingFrame | pd.DataFrame | Iterable[object],
) -> list[str]:
    if isinstance(frame, TrainingFrame):
        values: Iterable[object] = frame.rows["trade_date"].tolist()
    elif isinstance(frame, pd.DataFrame):
        if "trade_date" not in frame:
            raise ValueError("TRADE_DATE_COLUMN_REQUIRED")
        values = frame["trade_date"].tolist()
    else:
        values = frame
    normalized = {_date_value(value) for value in values}
    if not normalized:
        raise ValueError("NO_TRADE_DATES")
    return sorted(normalized)


def _row_trade_dates(
    frame: TrainingFrame | pd.DataFrame | Iterable[object],
) -> tuple[str, ...]:
    if isinstance(frame, TrainingFrame):
        return tuple(_date_value(value) for value in frame.rows["trade_date"].tolist())
    if isinstance(frame, pd.DataFrame):
        if "trade_date" not in frame:
            raise ValueError("TRADE_DATE_COLUMN_REQUIRED")
        return tuple(_date_value(value) for value in frame["trade_date"].tolist())
    return tuple(_ordered_trade_dates(frame))


def _date_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if "T" in text or " " in text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError as exc:
            raise ValueError(f"INVALID_TRADE_DATE: {text}") from exc
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"INVALID_TRADE_DATE: {text}") from exc


def _indices_for_dates(
    row_dates: Sequence[str],
    selected_dates: Sequence[str],
) -> tuple[int, ...]:
    selected = set(selected_dates)
    return tuple(index for index, value in enumerate(row_dates) if value in selected)


def _column_values(rows: pd.DataFrame, column: str) -> set[str]:
    if column not in rows:
        return set()
    return {
        str(value).strip()
        for value in rows[column].tolist()
        if value is not None
        and value is not pd.NA
        and str(value).strip()
        and str(value).lower() != "nan"
    }


def _finite_or_false(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _numeric_series(rows: pd.DataFrame, column: str) -> pd.Series:
    if column not in rows:
        return pd.Series(float("nan"), index=rows.index, dtype=float)
    return pd.to_numeric(rows[column], errors="coerce")


def _dataframe_sha256(frame: pd.DataFrame, feature_names: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {"columns": list(frame.columns), "feature_names": list(feature_names)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if not frame.empty:
        hashed = pd.util.hash_pandas_object(frame, index=True, categorize=True)
        digest.update(hashed.to_numpy(dtype="uint64", copy=False).tobytes())
    return digest.hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
