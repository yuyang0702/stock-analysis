"""Deterministic five-head training and immutable challenger bundles.

The trainer deliberately has no permission-changing or runtime-state side
effects.  It consumes a frozen :class:`ml_training_data.TrainingFrame`, tunes
at most three fixed configurations on expanding walk-forward folds, opens the
sealed holdout only after configuration freeze, and publishes one immutable
``bundle.joblib`` plus canonical ``manifest.json`` directory.

Rejected challengers are still published as reproducible evidence.  A caller
may register the returned manifest/status in ``MlStore`` once the store's
expanded model-manifest contract is available; this module never approves or
activates a model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, mean_absolute_error, mean_pinball_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ml_contracts import canonical_hash
from ml_training_data import (
    DataReadiness,
    TrainingFrame,
    TrainingSplits,
    validate_training_data,
)


MODEL_STRATEGY_VERSION = "five-head-tabular-v1"
MANIFEST_SCHEMA_VERSION = "five-head-bundle-v1"
GENERATED_BY = "stock-analysis"
TRAINING_SEED = 7
PSI_PSEUDOCOUNT = 0.5
RETURN_HEADS = ("ret_3d", "ret_5d", "ret_10d")
REGRESSION_HEADS = (*RETURN_HEADS, "downside")
TARGET_COLUMNS = {
    "ret_3d": "ret_3d_net",
    "ret_5d": "ret_5d_net",
    "ret_10d": "ret_10d_net",
    "downside": "downside_loss",
    "fill": "fill_label",
}
RULE_COUNTERFACTUAL_COLUMNS = frozenset(
    {
        "rule_score",
        "rule_order",
        "rule_selected",
        "rule_rejection_stage",
        "rule_rejection_code",
        "rule_final_action",
        "rule_target_qty",
        "rule_slot_count",
    }
)
_ALLOWED_HGB_PARAMETERS = frozenset(
    {
        "learning_rate",
        "max_iter",
        "max_leaf_nodes",
        "min_samples_leaf",
        "l2_regularization",
        "max_bins",
        "early_stopping",
        "validation_fraction",
        "n_iter_no_change",
        "tol",
    }
)
_DEFAULT_PARAMETER_CONFIG = MappingProxyType(
    {
        "learning_rate": 0.05,
        "max_iter": 80,
        "max_leaf_nodes": 15,
        "min_samples_leaf": 20,
        "l2_regularization": 1.0,
        "early_stopping": False,
    }
)


class TrainingError(RuntimeError):
    """Raised when deterministic training cannot be completed safely."""


@dataclass(frozen=True)
class TrainingConfig:
    """Frozen first-generation search and bundle configuration."""

    seed: int = TRAINING_SEED
    parameter_configs: tuple[Mapping[str, object], ...] = field(
        default_factory=lambda: (_DEFAULT_PARAMETER_CONFIG,)
    )
    ridge_alpha: float = 1.0
    calibration_fraction: float = 0.20
    parent_model_id: str | None = None
    model_strategy_version: str = MODEL_STRATEGY_VERSION

    def __post_init__(self) -> None:
        if int(self.seed) != TRAINING_SEED:
            raise ValueError("TRAINING_SEED_MUST_BE_7")
        configs = tuple(dict(item) for item in self.parameter_configs)
        if not configs:
            raise ValueError("PARAMETER_CONFIG_REQUIRED")
        if len(configs) > 3:
            raise ValueError("PARAMETER_CONFIG_LIMIT_EXCEEDED")
        normalized: list[Mapping[str, object]] = []
        for index, config in enumerate(configs):
            unknown = sorted(set(config).difference(_ALLOWED_HGB_PARAMETERS))
            if unknown:
                raise ValueError(
                    f"UNSUPPORTED_PARAMETER_CONFIG_{index}:" + ",".join(unknown)
                )
            for forbidden in ("random_state", "loss", "quantile"):
                if forbidden in config:
                    raise ValueError(f"FIXED_PARAMETER_OVERRIDE_FORBIDDEN:{forbidden}")
            normalized.append(_freeze_mapping(config))
        if not math.isfinite(float(self.ridge_alpha)) or float(self.ridge_alpha) <= 0:
            raise ValueError("RIDGE_ALPHA_MUST_BE_POSITIVE")
        fraction = float(self.calibration_fraction)
        if not math.isfinite(fraction) or not 0.10 <= fraction <= 0.40:
            raise ValueError("CALIBRATION_FRACTION_OUT_OF_RANGE")
        if not str(self.model_strategy_version).strip():
            raise ValueError("MODEL_STRATEGY_VERSION_REQUIRED")
        object.__setattr__(self, "seed", TRAINING_SEED)
        object.__setattr__(self, "parameter_configs", tuple(normalized))
        object.__setattr__(self, "ridge_alpha", float(self.ridge_alpha))
        object.__setattr__(self, "calibration_fraction", fraction)
        object.__setattr__(
            self,
            "parent_model_id",
            str(self.parent_model_id).strip() if self.parent_model_id else None,
        )
        object.__setattr__(
            self, "model_strategy_version", str(self.model_strategy_version).strip()
        )

    @property
    def config_sha256(self) -> str:
        return canonical_hash(
            {
                "seed": self.seed,
                "parameter_configs": [dict(item) for item in self.parameter_configs],
                "ridge_alpha": self.ridge_alpha,
                "calibration_fraction": self.calibration_fraction,
                "parent_model_id": self.parent_model_id,
                "model_strategy_version": self.model_strategy_version,
            }
        )


@dataclass(frozen=True)
class HeadMetrics:
    head: str
    challenger_name: str
    baseline_name: str
    oof_metrics: Mapping[str, object]
    holdout_metrics: Mapping[str, object]
    fold_metrics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "oof_metrics", _freeze(self.oof_metrics))
        object.__setattr__(self, "holdout_metrics", _freeze(self.holdout_metrics))
        object.__setattr__(
            self,
            "fold_metrics",
            tuple(_freeze(item) for item in self.fold_metrics),
        )


@dataclass(frozen=True)
class ModelEvaluation:
    head_metrics: Mapping[str, HeadMetrics]
    performance_metrics: Mapping[str, object]
    failed_gates: tuple[str, ...]
    data_readiness: Mapping[str, object]
    passed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "head_metrics", _freeze(self.head_metrics))
        object.__setattr__(
            self, "performance_metrics", _freeze(self.performance_metrics)
        )
        object.__setattr__(self, "failed_gates", tuple(self.failed_gates))
        object.__setattr__(self, "data_readiness", _freeze(self.data_readiness))


@dataclass(frozen=True)
class ModelBundleManifest:
    model_id: str
    parent_model_id: str | None
    generated_by: str
    manifest_schema_version: str
    model_strategy_version: str
    status: str
    permission_level: int
    required_features: tuple[str, ...]
    numeric_features: tuple[str, ...]
    categorical_features: tuple[str, ...]
    dropped_constant_features: tuple[str, ...]
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    holdout_start: str
    holdout_end: str
    dataset_sha256: str
    strict_provenance_sha256: str
    code_sha256: str
    feature_sha256: str
    label_sha256: str
    label_policy_sha256: str
    cost_sha256: str
    split_sha256: str
    config_sha256: str
    dependency_sha256: str
    dependency_versions: Mapping[str, str]
    strategy_version: str
    parameter_version: str
    feature_schema_version: str
    label_version: str
    policy_version: str
    cost_version: str
    selected_parameters: Mapping[str, object]
    search_inputs_hash: str
    search_indices: tuple[int, ...]
    holdout_indices: tuple[int, ...]
    holdout_evaluated_after_freeze: bool
    oof_references: Mapping[str, tuple[float, ...]]
    residual_q80: Mapping[str, float]
    d5_difference_p90: float
    downside_oof_prediction_p80: float
    drift_reference: Mapping[str, Mapping[str, object]]
    psi_pseudocount: float
    metrics: Mapping[str, object]
    holdout_metrics: Mapping[str, object]
    failed_gates: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        for name in (
            "required_features",
            "numeric_features",
            "categorical_features",
            "dropped_constant_features",
            "search_indices",
            "holdout_indices",
            "failed_gates",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "dependency_versions", _freeze(self.dependency_versions))
        object.__setattr__(self, "selected_parameters", _freeze(self.selected_parameters))
        object.__setattr__(self, "oof_references", _freeze(self.oof_references))
        object.__setattr__(self, "residual_q80", _freeze(self.residual_q80))
        object.__setattr__(self, "drift_reference", _freeze(self.drift_reference))
        object.__setattr__(self, "metrics", _freeze(self.metrics))
        object.__setattr__(self, "holdout_metrics", _freeze(self.holdout_metrics))

    def as_dict(self) -> dict[str, object]:
        """Return the canonical runtime manifest (artifact hash stays external)."""

        return _json_safe(
            {
                "model_id": self.model_id,
                "parent_model_id": self.parent_model_id,
                "generated_by": self.generated_by,
                "bundle_schema_version": self.manifest_schema_version,
                "manifest_schema_version": self.manifest_schema_version,
                "model_strategy_version": self.model_strategy_version,
                "status": self.status,
                "permission_level": self.permission_level,
                "required_features": self.required_features,
                "numeric_features": self.numeric_features,
                "categorical_features": self.categorical_features,
                "dropped_constant_features": self.dropped_constant_features,
                "train_start": self.train_start,
                "train_end": self.train_end,
                "validation_start": self.validation_start,
                "validation_end": self.validation_end,
                "holdout_start": self.holdout_start,
                "holdout_end": self.holdout_end,
                "dataset_sha256": self.dataset_sha256,
                "strict_provenance_sha256": self.strict_provenance_sha256,
                "code_sha256": self.code_sha256,
                "feature_sha256": self.feature_sha256,
                "label_sha256": self.label_sha256,
                "label_policy_sha256": self.label_policy_sha256,
                "cost_sha256": self.cost_sha256,
                "split_sha256": self.split_sha256,
                "config_sha256": self.config_sha256,
                "dependency_sha256": self.dependency_sha256,
                "dependency_versions": self.dependency_versions,
                "strategy_version": self.strategy_version,
                "parameter_version": self.parameter_version,
                "feature_schema_version": self.feature_schema_version,
                "label_version": self.label_version,
                "policy_version": self.policy_version,
                "cost_version": self.cost_version,
                "selected_parameters": self.selected_parameters,
                "search_inputs_hash": self.search_inputs_hash,
                "search_indices": self.search_indices,
                "holdout_indices": self.holdout_indices,
                "holdout_evaluated_after_freeze": self.holdout_evaluated_after_freeze,
                "oof_references": self.oof_references,
                "residual_q80": self.residual_q80,
                "d5_difference_p90": self.d5_difference_p90,
                "downside_oof_prediction_p80": self.downside_oof_prediction_p80,
                "drift_reference": self.drift_reference,
                "psi_pseudocount": self.psi_pseudocount,
                "metrics": self.metrics,
                "holdout_metrics": self.holdout_metrics,
                "failed_gates": self.failed_gates,
                "created_at": self.created_at,
            }
        )


@dataclass(frozen=True)
class TrainingResult:
    model_id: str
    status: str
    approvable_l0: bool
    failed_gates: tuple[str, ...]
    manifest: ModelBundleManifest
    manifest_sha256: str
    artifact_sha256: str
    artifact_dir: Path
    reused_existing: bool
    evaluation: ModelEvaluation


@dataclass
class _ModelSet:
    models: dict[str, object]
    downside_constant: float


class CalibratedHGBClassifier:
    """Small deterministic HGB classifier with fold-local sigmoid calibration."""

    def __init__(
        self,
        *,
        preprocessor: ColumnTransformer,
        estimator_parameters: Mapping[str, object],
        seed: int = TRAINING_SEED,
        calibration_fraction: float = 0.20,
    ) -> None:
        self.preprocessor = preprocessor
        self.estimator_parameters = dict(estimator_parameters)
        self.seed = int(seed)
        self.calibration_fraction = float(calibration_fraction)

    def fit(
        self,
        features: pd.DataFrame,
        target: Sequence[int],
        sample_weight: Sequence[float] | None = None,
    ) -> "CalibratedHGBClassifier":
        rows = features.reset_index(drop=True)
        values = np.asarray(target, dtype=int)
        weights = (
            np.ones(len(values), dtype=float)
            if sample_weight is None
            else np.asarray(sample_weight, dtype=float)
        )
        if len(values) != len(rows) or len(weights) != len(values):
            raise ValueError("FILL_TRAINING_SHAPE_MISMATCH")
        if set(np.unique(values)) != {0, 1}:
            raise ValueError("FILL_TRAIN_SINGLE_CLASS")
        base_indices, calibration_indices = _calibration_split(
            values, self.calibration_fraction
        )
        if not calibration_indices.size:
            raise ValueError("FILL_CALIBRATION_SPLIT_UNAVAILABLE")
        self.base_fit_indices_ = base_indices.copy()
        self.calibration_indices_ = calibration_indices.copy()
        self.preprocessor_ = clone(self.preprocessor)
        base_matrix = self.preprocessor_.fit_transform(rows.iloc[base_indices])
        parameters = dict(self.estimator_parameters)
        parameters.update(random_state=self.seed, early_stopping=False)
        self.estimator_ = HistGradientBoostingClassifier(**parameters)
        self.estimator_.fit(
            base_matrix,
            values[base_indices],
            sample_weight=weights[base_indices],
        )
        calibration_matrix = self.preprocessor_.transform(rows.iloc[calibration_indices])
        raw = self.estimator_.predict_proba(calibration_matrix)[:, 1]
        calibration_target = values[calibration_indices]
        if set(np.unique(calibration_target)) != {0, 1}:
            raise ValueError("FILL_CALIBRATION_SINGLE_CLASS")
        calibrator = LogisticRegression(
            max_iter=1000,
            random_state=self.seed,
            solver="lbfgs",
        )
        calibrator.fit(
            raw.reshape(-1, 1),
            calibration_target,
            sample_weight=weights[calibration_indices],
        )
        self.calibrator_ = calibrator
        self.classes_ = np.asarray([0, 1], dtype=int)
        return self

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        matrix = self.preprocessor_.transform(features)
        raw = np.asarray(self.estimator_.predict_proba(matrix)[:, 1], dtype=float)
        probability = self.calibrator_.predict_proba(raw.reshape(-1, 1))[:, 1]
        probability = np.clip(probability, 0.0, 1.0)
        return np.column_stack((1.0 - probability, probability))

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(features)[:, 1] >= 0.5).astype(int)


def train_challenger(
    frame: TrainingFrame,
    splits: TrainingSplits,
    config: TrainingConfig,
    output_dir: Path | str,
) -> TrainingResult:
    """Train, evaluate and atomically publish one immutable challenger.

    No holdout row is supplied to configuration search or any fold-local fit.
    The final bundle is fitted on development rows only; the sealed holdout is
    opened once for evaluation and is never folded back into the artifact.
    """

    if not isinstance(frame, TrainingFrame):
        raise TypeError("frame must be TrainingFrame")
    if not isinstance(splits, TrainingSplits):
        raise TypeError("splits must be TrainingSplits")
    if not isinstance(config, TrainingConfig):
        raise TypeError("config must be TrainingConfig")
    original_dataset_sha = _dataframe_sha256(frame.rows, frame.feature_names)
    if original_dataset_sha != frame.dataset_sha256:
        raise TrainingError("TRAINING_FRAME_MUTATED")
    rows = frame.rows.copy(deep=True).reset_index(drop=True)
    if not rows.index.is_unique:
        raise TrainingError("TRAINING_INDEX_NOT_UNIQUE")
    development_indices = _indices_for_dates(rows, splits.all_development_dates)
    holdout_indices = tuple(int(value) for value in splits.holdout_indices)
    if not development_indices or not holdout_indices:
        raise TrainingError("TRAINING_SPLIT_EMPTY")
    if set(development_indices).intersection(holdout_indices):
        raise TrainingError("HOLDOUT_OVERLAPS_DEVELOPMENT")

    schema_fit_indices = tuple(splits.walk_forward[0].train_indices)
    numeric, categorical, dropped = _feature_schema(
        rows.iloc[list(schema_fit_indices)], frame.feature_names
    )
    required_features = (*numeric, *categorical)
    if not required_features:
        raise TrainingError("NO_NON_CONSTANT_MODEL_FEATURES")

    search_inputs_hash = canonical_hash(
        rows.iloc[list(development_indices)].to_dict(orient="records")
    )
    search_results: list[tuple[float, str, Mapping[str, object], pd.DataFrame]] = []
    for candidate_config in config.parameter_configs:
        oof = _build_oof_predictions(
            rows,
            splits,
            required_features,
            numeric,
            categorical,
            candidate_config,
            config,
        )
        d5_score = _oof_rank_for_head(rows, oof, "ret_5d")
        comparable = d5_score if math.isfinite(d5_score) else -math.inf
        config_hash = canonical_hash(candidate_config)
        search_results.append((comparable, config_hash, candidate_config, oof))
    search_results.sort(key=lambda item: (-item[0], item[1]))
    _, _, selected_parameters, oof = search_results[0]

    final_models = _fit_model_set(
        rows.iloc[list(development_indices)],
        required_features,
        numeric,
        categorical,
        selected_parameters,
        config,
        label_cutoff=splits.holdout_start,
    )
    holdout_rows = rows.iloc[list(holdout_indices)]
    holdout_predictions = _predict_model_set(
        final_models, holdout_rows, required_features
    )

    performance_metrics, residual_q80 = _evaluate_predictions(
        rows,
        splits,
        oof,
        holdout_indices,
        holdout_predictions,
    )
    performance_failures = list(evaluate_performance_gates(performance_metrics))
    readiness = validate_training_data(frame, splits, require_l0=True)
    failed_gates = [*readiness.l0_reasons, *performance_failures]
    provenance_failures = _provenance_failures(frame)
    failed_gates.extend(provenance_failures)
    failed_gates = list(_unique(failed_gates))
    approvable_l0 = not failed_gates
    status = "approvable_l0" if approvable_l0 else "rejected"

    dependency_versions = _dependency_versions()
    dependency_sha256 = canonical_hash(dependency_versions)
    code_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    feature_sha256 = canonical_hash(
        {
            "feature_schema_version": _metadata_text(
                frame, "feature_schema_version"
            ),
            "required_features": required_features,
            "numeric_features": numeric,
            "categorical_features": categorical,
            "dropped_constant_features": dropped,
        }
    )
    label_sha256 = str(frame.metadata.get("label_sha256") or "") or canonical_hash(
        {
            "label_version": _metadata_text(frame, "label_version"),
            "label_source": _metadata_text(frame, "label_source"),
            "target_columns": TARGET_COLUMNS,
        }
    )
    label_policy_sha256 = str(frame.metadata.get("policy_sha256") or "")
    cost_sha256 = str(frame.metadata.get("cost_sha256") or "")
    strict_provenance_sha256 = str(
        frame.metadata.get("strict_provenance_sha256") or ""
    )
    config_sha256 = config.config_sha256
    identity = {
        "generated_by": GENERATED_BY,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "model_strategy_version": config.model_strategy_version,
        "dataset_sha256": frame.dataset_sha256,
        "strict_provenance_sha256": strict_provenance_sha256,
        "split_sha256": splits.split_sha256,
        "code_sha256": code_sha256,
        "feature_sha256": feature_sha256,
        "label_sha256": label_sha256,
        "label_policy_sha256": label_policy_sha256,
        "cost_sha256": cost_sha256,
        "config_sha256": config_sha256,
        "dependency_sha256": dependency_sha256,
        "selected_parameters": dict(selected_parameters),
    }
    model_id = "ml-" + canonical_hash(identity)[:32]

    references = _oof_references(oof)
    d5_difference_p90 = _finite_quantile(
        np.abs(
            pd.to_numeric(oof["pred_ret_5d"], errors="coerce").to_numpy()
            - pd.to_numeric(oof["ridge_ret_5d"], errors="coerce").to_numpy()
        ),
        0.90,
    )
    downside_oof_prediction_p80 = _finite_quantile(
        pd.to_numeric(oof["pred_downside"], errors="coerce").to_numpy(),
        0.80,
    )
    drift_reference = _build_drift_reference(
        rows.iloc[list(development_indices)], numeric, categorical
    )
    head_metrics = _head_metrics(performance_metrics)
    evaluation = ModelEvaluation(
        head_metrics=head_metrics,
        performance_metrics=performance_metrics,
        failed_gates=tuple(failed_gates),
        data_readiness=_readiness_dict(readiness),
        passed=approvable_l0,
    )
    created_at = _deterministic_created_at(rows, development_indices)
    manifest = ModelBundleManifest(
        model_id=model_id,
        parent_model_id=config.parent_model_id,
        generated_by=GENERATED_BY,
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        model_strategy_version=config.model_strategy_version,
        status=status,
        permission_level=0,
        required_features=tuple(required_features),
        numeric_features=tuple(numeric),
        categorical_features=tuple(categorical),
        dropped_constant_features=tuple(dropped),
        train_start=splits.walk_forward[0].train_start,
        train_end=splits.all_development_dates[-1],
        validation_start=splits.walk_forward[0].validation_start,
        validation_end=splits.walk_forward[-1].validation_end,
        holdout_start=splits.holdout_start,
        holdout_end=splits.holdout_end,
        dataset_sha256=frame.dataset_sha256,
        strict_provenance_sha256=strict_provenance_sha256,
        code_sha256=code_sha256,
        feature_sha256=feature_sha256,
        label_sha256=label_sha256,
        label_policy_sha256=label_policy_sha256,
        cost_sha256=cost_sha256,
        split_sha256=splits.split_sha256,
        config_sha256=config_sha256,
        dependency_sha256=dependency_sha256,
        dependency_versions=dependency_versions,
        strategy_version=_metadata_text(frame, "strategy_version"),
        parameter_version=_metadata_text(frame, "parameter_version"),
        feature_schema_version=_metadata_text(frame, "feature_schema_version"),
        label_version=_metadata_text(frame, "label_version"),
        policy_version=_metadata_text(frame, "policy_version"),
        cost_version=_metadata_text(frame, "cost_version"),
        selected_parameters=dict(selected_parameters),
        search_inputs_hash=search_inputs_hash,
        search_indices=tuple(development_indices),
        holdout_indices=holdout_indices,
        holdout_evaluated_after_freeze=True,
        oof_references=references,
        residual_q80=residual_q80,
        d5_difference_p90=d5_difference_p90,
        downside_oof_prediction_p80=downside_oof_prediction_p80,
        drift_reference=drift_reference,
        psi_pseudocount=PSI_PSEUDOCOUNT,
        metrics=performance_metrics,
        holdout_metrics=_holdout_metric_summary(performance_metrics),
        failed_gates=tuple(failed_gates),
        created_at=created_at,
    )
    bundle_payload = {
        "generated_by": GENERATED_BY,
        "bundle_schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "model_id": model_id,
        "models": final_models.models,
    }
    if _dataframe_sha256(frame.rows, frame.feature_names) != original_dataset_sha:
        raise TrainingError("TRAINING_FRAME_CHANGED_DURING_TRAINING")
    artifact_dir, manifest_sha256, artifact_sha256, reused = _publish_bundle(
        Path(output_dir), model_id, bundle_payload, manifest.as_dict()
    )
    return TrainingResult(
        model_id=model_id,
        status=status,
        approvable_l0=approvable_l0,
        failed_gates=tuple(failed_gates),
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        artifact_sha256=artifact_sha256,
        artifact_dir=artifact_dir,
        reused_existing=reused,
        evaluation=evaluation,
    )


def evaluate_performance_gates(metrics: Mapping[str, object]) -> tuple[str, ...]:
    """Apply the frozen sample-out performance gates with stable reason codes."""

    failures: list[str] = []
    rank = _mapping(metrics.get("rank"))
    d5 = _mapping(rank.get("ret_5d"))
    d5_folds = _float_list(d5.get("folds"))
    if not _positive(d5.get("oof")) or sum(value > 0 for value in d5_folds) < 2:
        failures.append("D5_OOF_RANK_GATE")
    if not _positive(d5.get("holdout")):
        failures.append("D5_HOLDOUT_RANK_GATE")
    for head, reason in (
        ("ret_3d", "D3_OOF_RANK_GATE"),
        ("ret_10d", "D10_OOF_RANK_GATE"),
    ):
        values = _mapping(rank.get(head))
        folds = _float_list(values.get("folds"))
        overall = _finite_or_nan(values.get("oof"))
        if not math.isfinite(overall) or overall < 0 or sum(v < 0 for v in folds) > 1:
            failures.append(reason)

    top20 = _mapping(metrics.get("top20"))
    challenger_mean = _finite_or_nan(top20.get("challenger_mean"))
    pool_mean = _finite_or_nan(top20.get("pool_mean"))
    ridge_mean = _finite_or_nan(top20.get("ridge_mean"))
    if not (
        math.isfinite(challenger_mean)
        and math.isfinite(pool_mean)
        and math.isfinite(ridge_mean)
        and challenger_mean > pool_mean
        and challenger_mean > ridge_mean
    ):
        failures.append("D5_TOP20_GATE")

    monotonicity = _mapping(metrics.get("monotonicity"))
    if (
        monotonicity.get("downside") is not True
        or monotonicity.get("downside_holdout") is not True
        or monotonicity.get("downside_stop_rate") is not True
        or monotonicity.get("downside_stop_rate_holdout") is not True
    ):
        failures.append("DOWNSIDE_MONOTONICITY_GATE")
    if (
        monotonicity.get("fill") is not True
        or monotonicity.get("fill_holdout") is not True
    ):
        failures.append("FILL_MONOTONICITY_GATE")

    counterfactual = _mapping(metrics.get("counterfactual"))
    if counterfactual.get("evaluable") is not True:
        failures.append("COUNTERFACTUAL_RULE_BASELINE_NOT_EVALUABLE")
    else:
        if not _positive(counterfactual.get("return_improvement")):
            failures.append("COUNTERFACTUAL_RETURN_GATE")
        drawdown_delta = _finite_or_nan(counterfactual.get("max_drawdown_delta"))
        if not math.isfinite(drawdown_delta) or drawdown_delta > 1e-12:
            failures.append("COUNTERFACTUAL_DRAWDOWN_GATE")

    errors = _mapping(metrics.get("errors"))
    for head, reason in (
        ("ret_3d", "RET_3D_MAE_GATE"),
        ("ret_5d", "RET_5D_MAE_GATE"),
        ("ret_10d", "RET_10D_MAE_GATE"),
    ):
        values = _mapping(errors.get(head))
        challenger = _finite_or_nan(values.get("challenger_mae"))
        baseline = _finite_or_nan(values.get("baseline_mae"))
        holdout_challenger = _finite_or_nan(
            values.get("holdout_challenger_mae")
        )
        holdout_baseline = _finite_or_nan(values.get("holdout_baseline_mae"))
        if not (
            math.isfinite(challenger)
            and math.isfinite(baseline)
            and challenger <= baseline + 1e-12
            and math.isfinite(holdout_challenger)
            and math.isfinite(holdout_baseline)
            and holdout_challenger <= holdout_baseline + 1e-12
        ):
            failures.append(reason)
    downside = _mapping(errors.get("downside"))
    challenger_pinball = _finite_or_nan(downside.get("challenger_pinball"))
    baseline_pinball = _finite_or_nan(downside.get("baseline_pinball"))
    holdout_challenger_pinball = _finite_or_nan(
        downside.get("holdout_challenger_pinball")
    )
    holdout_baseline_pinball = _finite_or_nan(
        downside.get("holdout_baseline_pinball")
    )
    if not (
        math.isfinite(challenger_pinball)
        and math.isfinite(baseline_pinball)
        and challenger_pinball <= baseline_pinball + 1e-12
        and math.isfinite(holdout_challenger_pinball)
        and math.isfinite(holdout_baseline_pinball)
        and holdout_challenger_pinball <= holdout_baseline_pinball + 1e-12
    ):
        failures.append("DOWNSIDE_PINBALL_GATE")
    fill = _mapping(errors.get("fill"))
    challenger_brier = _finite_or_nan(fill.get("challenger_brier"))
    baseline_brier = _finite_or_nan(fill.get("baseline_brier"))
    holdout_challenger_brier = _finite_or_nan(
        fill.get("holdout_challenger_brier")
    )
    holdout_baseline_brier = _finite_or_nan(fill.get("holdout_baseline_brier"))
    if not (
        math.isfinite(challenger_brier)
        and math.isfinite(baseline_brier)
        and challenger_brier <= baseline_brier + 1e-12
        and math.isfinite(holdout_challenger_brier)
        and math.isfinite(holdout_baseline_brier)
        and holdout_challenger_brier <= holdout_baseline_brier + 1e-12
    ):
        failures.append("FILL_BRIER_GATE")
    calibration_error = _finite_or_nan(fill.get("max_calibration_error"))
    holdout_calibration_error = _finite_or_nan(
        fill.get("holdout_max_calibration_error")
    )
    if (
        not math.isfinite(calibration_error)
        or calibration_error > 0.10 + 1e-12
        or not math.isfinite(holdout_calibration_error)
        or holdout_calibration_error > 0.10 + 1e-12
    ):
        failures.append("FILL_CALIBRATION_GATE")

    interval = _mapping(metrics.get("interval_coverage"))
    for head, reason in (
        ("ret_3d", "RET_3D_INTERVAL_COVERAGE_GATE"),
        ("ret_5d", "RET_5D_INTERVAL_COVERAGE_GATE"),
        ("ret_10d", "RET_10D_INTERVAL_COVERAGE_GATE"),
        ("downside", "DOWNSIDE_INTERVAL_COVERAGE_GATE"),
    ):
        values = _mapping(interval.get(head))
        coverages = [*_float_list(values.get("folds"))]
        holdout = _finite_or_nan(values.get("holdout"))
        coverages.append(holdout)
        if not coverages or any(
            not math.isfinite(value) or value < 0.75 or value > 0.85
            for value in coverages
        ):
            failures.append(reason)
    return tuple(_unique(failures))


def _build_oof_predictions(
    rows: pd.DataFrame,
    splits: TrainingSplits,
    required_features: Sequence[str],
    numeric_features: Sequence[str],
    categorical_features: Sequence[str],
    parameters: Mapping[str, object],
    config: TrainingConfig,
) -> pd.DataFrame:
    records: list[pd.DataFrame] = []
    for fold in splits.walk_forward:
        train_rows = rows.iloc[list(fold.train_indices)]
        test_rows = rows.iloc[list(fold.test_indices)]
        fitted = _fit_model_set(
            train_rows,
            required_features,
            numeric_features,
            categorical_features,
            parameters,
            config,
            label_cutoff=fold.test_start,
        )
        predictions = _predict_model_set(fitted, test_rows, required_features)
        predictions["row_index"] = list(fold.test_indices)
        predictions["fold_index"] = fold.fold_index
        records.append(predictions)
    if not records:
        raise TrainingError("NO_WALK_FORWARD_PREDICTIONS")
    result = pd.concat(records, ignore_index=True).set_index("row_index")
    if result.index.duplicated().any():
        raise TrainingError("OOF_INDEX_OVERLAP")
    return result.sort_index()


def _fit_model_set(
    rows: pd.DataFrame,
    required_features: Sequence[str],
    numeric_features: Sequence[str],
    categorical_features: Sequence[str],
    parameters: Mapping[str, object],
    config: TrainingConfig,
    *,
    label_cutoff: str,
) -> _ModelSet:
    features = rows.loc[:, list(required_features)]
    weights = _sample_weights(rows)
    models: dict[str, object] = {}
    for head in RETURN_HEADS:
        mask = _target_mask(rows, head, label_cutoff=label_cutoff, require_fill=True)
        if int(mask.sum()) < 8:
            raise TrainingError(f"INSUFFICIENT_TRAINING_LABELS:{head}")
        target = pd.to_numeric(rows.loc[mask, TARGET_COLUMNS[head]], errors="coerce")
        challenger = Pipeline(
            [
                (
                    "preprocess",
                    _make_preprocessor(numeric_features, categorical_features),
                ),
                (
                    "model",
                    HistGradientBoostingRegressor(
                        **dict(parameters), random_state=TRAINING_SEED
                    ),
                ),
            ]
        )
        challenger.fit(
            features.loc[mask],
            target,
            model__sample_weight=weights.loc[mask].to_numpy(),
        )
        ridge = Pipeline(
            [
                (
                    "preprocess",
                    _make_preprocessor(
                        numeric_features,
                        categorical_features,
                        scale_numeric=True,
                    ),
                ),
                ("model", Ridge(alpha=config.ridge_alpha)),
            ]
        )
        ridge.fit(
            features.loc[mask],
            target,
            model__sample_weight=weights.loc[mask].to_numpy(),
        )
        models[head] = challenger
        models[f"ridge_{head}"] = ridge

    downside_mask = _target_mask(
        rows, "downside", label_cutoff=label_cutoff, require_fill=True
    )
    if int(downside_mask.sum()) < 8:
        raise TrainingError("INSUFFICIENT_TRAINING_LABELS:downside")
    downside_target = pd.to_numeric(
        rows.loc[downside_mask, TARGET_COLUMNS["downside"]], errors="coerce"
    )
    downside_weights = weights.loc[downside_mask].to_numpy()
    downside = Pipeline(
        [
            (
                "preprocess",
                _make_preprocessor(numeric_features, categorical_features),
            ),
            (
                "model",
                HistGradientBoostingRegressor(
                    **dict(parameters),
                    loss="quantile",
                    quantile=0.8,
                    random_state=TRAINING_SEED,
                ),
            ),
        ]
    )
    downside.fit(
        features.loc[downside_mask],
        downside_target,
        model__sample_weight=downside_weights,
    )
    models["downside"] = downside
    downside_constant = _weighted_quantile(
        downside_target.to_numpy(dtype=float), downside_weights, 0.8
    )

    fill_mask = _target_mask(
        rows, "fill", label_cutoff=label_cutoff, require_fill=False
    )
    fill_target = pd.to_numeric(
        rows.loc[fill_mask, TARGET_COLUMNS["fill"]], errors="coerce"
    ).astype(int)
    if int(fill_mask.sum()) < 12 or set(fill_target.unique()) != {0, 1}:
        raise TrainingError("FILL_TRAIN_SINGLE_CLASS")
    fill_preprocessor = _make_preprocessor(numeric_features, categorical_features)
    fill_model = CalibratedHGBClassifier(
        preprocessor=fill_preprocessor,
        estimator_parameters=parameters,
        seed=TRAINING_SEED,
        calibration_fraction=config.calibration_fraction,
    )
    fill_model.fit(
        features.loc[fill_mask],
        fill_target.to_numpy(),
        sample_weight=weights.loc[fill_mask].to_numpy(),
    )
    logistic = Pipeline(
        [
            (
                "preprocess",
                _make_preprocessor(
                    numeric_features,
                    categorical_features,
                    scale_numeric=True,
                ),
            ),
            (
                "model",
                LogisticRegression(
                    max_iter=1000,
                    random_state=TRAINING_SEED,
                    solver="lbfgs",
                ),
            ),
        ]
    )
    logistic.fit(
        features.loc[fill_mask],
        fill_target,
        model__sample_weight=weights.loc[fill_mask].to_numpy(),
    )
    models["fill"] = fill_model
    models["logistic_fill"] = logistic
    return _ModelSet(models=models, downside_constant=downside_constant)


def _predict_model_set(
    fitted: _ModelSet,
    rows: pd.DataFrame,
    required_features: Sequence[str],
) -> pd.DataFrame:
    features = rows.reindex(columns=list(required_features))
    result: dict[str, object] = {}
    for head in RETURN_HEADS:
        result[f"pred_{head}"] = np.asarray(
            fitted.models[head].predict(features), dtype=float
        )
        result[f"ridge_{head}"] = np.asarray(
            fitted.models[f"ridge_{head}"].predict(features), dtype=float
        )
    result["pred_downside"] = np.asarray(
        fitted.models["downside"].predict(features), dtype=float
    )
    result["constant_downside"] = np.full(
        len(rows), fitted.downside_constant, dtype=float
    )
    result["pred_fill"] = np.asarray(
        fitted.models["fill"].predict_proba(features)[:, 1], dtype=float
    )
    result["logistic_fill"] = np.asarray(
        fitted.models["logistic_fill"].predict_proba(features)[:, 1], dtype=float
    )
    return pd.DataFrame(result, index=rows.index).reset_index(drop=True)


def _evaluate_predictions(
    rows: pd.DataFrame,
    splits: TrainingSplits,
    oof: pd.DataFrame,
    holdout_indices: Sequence[int],
    holdout_predictions: pd.DataFrame,
) -> tuple[dict[str, object], dict[str, float]]:
    holdout = holdout_predictions.copy(deep=True)
    holdout["row_index"] = list(holdout_indices)
    holdout = holdout.set_index("row_index")
    rank: dict[str, object] = {}
    errors: dict[str, object] = {}
    interval_coverage: dict[str, object] = {}
    residual_q80: dict[str, float] = {}
    weights = _sample_weights(rows)

    for head in RETURN_HEADS:
        target_column = TARGET_COLUMNS[head]
        oof_mask = _evaluation_mask(rows, oof.index, head, require_fill=True)
        holdout_mask = _evaluation_mask(
            rows, holdout.index, head, require_fill=True
        )
        oof_actual = pd.to_numeric(
            rows.loc[oof.index, target_column], errors="coerce"
        )
        holdout_actual = pd.to_numeric(
            rows.loc[holdout.index, target_column], errors="coerce"
        )
        folds: list[float] = []
        for fold in splits.walk_forward:
            indices = [index for index in fold.test_indices if index in oof.index]
            fold_mask = _evaluation_mask(rows, indices, head, require_fill=True)
            selected = [index for index in indices if bool(fold_mask.loc[index])]
            folds.append(
                _spearman(
                    rows.loc[selected, target_column],
                    oof.loc[selected, f"pred_{head}"],
                )
            )
        rank[head] = {
            "oof": _spearman(
                oof_actual[oof_mask], oof.loc[oof_mask, f"pred_{head}"]
            ),
            "folds": folds,
            "holdout": _spearman(
                holdout_actual[holdout_mask],
                holdout.loc[holdout_mask, f"pred_{head}"],
            ),
        }
        challenger_mae = _weighted_mae(
            oof_actual[oof_mask],
            oof.loc[oof_mask, f"pred_{head}"],
            weights.loc[oof.index][oof_mask],
        )
        baseline_mae = _weighted_mae(
            oof_actual[oof_mask],
            oof.loc[oof_mask, f"ridge_{head}"],
            weights.loc[oof.index][oof_mask],
        )
        errors[head] = {
            "challenger_mae": challenger_mae,
            "baseline_mae": baseline_mae,
            "holdout_challenger_mae": _weighted_mae(
                holdout_actual[holdout_mask],
                holdout.loc[holdout_mask, f"pred_{head}"],
                weights.loc[holdout.index][holdout_mask],
            ),
            "holdout_baseline_mae": _weighted_mae(
                holdout_actual[holdout_mask],
                holdout.loc[holdout_mask, f"ridge_{head}"],
                weights.loc[holdout.index][holdout_mask],
            ),
        }
        residuals = np.abs(
            oof_actual[oof_mask].to_numpy(dtype=float)
            - oof.loc[oof_mask, f"pred_{head}"].to_numpy(dtype=float)
        )
        residual_weights = weights.loc[oof.index][oof_mask].to_numpy(dtype=float)
        q80 = _weighted_quantile(residuals, residual_weights, 0.8)
        residual_q80[head] = q80
        fold_coverages: list[float] = []
        for fold in splits.walk_forward:
            indices = [index for index in fold.test_indices if index in oof.index]
            fold_mask = _evaluation_mask(rows, indices, head, require_fill=True)
            selected = [index for index in indices if bool(fold_mask.loc[index])]
            fold_coverages.append(
                _interval_coverage(
                    rows.loc[selected, target_column],
                    oof.loc[selected, f"pred_{head}"],
                    q80,
                )
            )
        interval_coverage[head] = {
            "folds": fold_coverages,
            "holdout": _interval_coverage(
                holdout_actual[holdout_mask],
                holdout.loc[holdout_mask, f"pred_{head}"],
                q80,
            ),
        }

    downside_mask = _evaluation_mask(rows, oof.index, "downside", require_fill=True)
    holdout_downside_mask = _evaluation_mask(
        rows, holdout.index, "downside", require_fill=True
    )
    downside_actual = pd.to_numeric(
        rows.loc[oof.index, TARGET_COLUMNS["downside"]], errors="coerce"
    )
    holdout_downside_actual = pd.to_numeric(
        rows.loc[holdout.index, TARGET_COLUMNS["downside"]], errors="coerce"
    )
    errors["downside"] = {
        "challenger_pinball": _weighted_pinball(
            downside_actual[downside_mask],
            oof.loc[downside_mask, "pred_downside"],
            weights.loc[oof.index][downside_mask],
        ),
        "baseline_pinball": _weighted_pinball(
            downside_actual[downside_mask],
            oof.loc[downside_mask, "constant_downside"],
            weights.loc[oof.index][downside_mask],
        ),
        "holdout_challenger_pinball": _weighted_pinball(
            holdout_downside_actual[holdout_downside_mask],
            holdout.loc[holdout_downside_mask, "pred_downside"],
            weights.loc[holdout.index][holdout_downside_mask],
        ),
        "holdout_baseline_pinball": _weighted_pinball(
            holdout_downside_actual[holdout_downside_mask],
            holdout.loc[holdout_downside_mask, "constant_downside"],
            weights.loc[holdout.index][holdout_downside_mask],
        ),
    }
    downside_residuals = np.abs(
        downside_actual[downside_mask].to_numpy(dtype=float)
        - oof.loc[downside_mask, "pred_downside"].to_numpy(dtype=float)
    )
    downside_residual_weights = weights.loc[oof.index][downside_mask].to_numpy(
        dtype=float
    )
    downside_q80 = _weighted_quantile(
        downside_residuals, downside_residual_weights, 0.8
    )
    residual_q80["downside"] = downside_q80
    downside_fold_coverages: list[float] = []
    for fold in splits.walk_forward:
        indices = [index for index in fold.test_indices if index in oof.index]
        fold_mask = _evaluation_mask(rows, indices, "downside", require_fill=True)
        selected = [index for index in indices if bool(fold_mask.loc[index])]
        downside_fold_coverages.append(
            _interval_coverage(
                rows.loc[selected, TARGET_COLUMNS["downside"]],
                oof.loc[selected, "pred_downside"],
                downside_q80,
            )
        )
    interval_coverage["downside"] = {
        "folds": downside_fold_coverages,
        "holdout": _interval_coverage(
            holdout_downside_actual[holdout_downside_mask],
            holdout.loc[holdout_downside_mask, "pred_downside"],
            downside_q80,
        ),
    }

    fill_mask = _evaluation_mask(rows, oof.index, "fill", require_fill=False)
    holdout_fill_mask = _evaluation_mask(
        rows, holdout.index, "fill", require_fill=False
    )
    fill_actual = pd.to_numeric(
        rows.loc[oof.index, TARGET_COLUMNS["fill"]], errors="coerce"
    )
    holdout_fill_actual = pd.to_numeric(
        rows.loc[holdout.index, TARGET_COLUMNS["fill"]], errors="coerce"
    )
    errors["fill"] = {
        "challenger_brier": _weighted_brier(
            fill_actual[fill_mask],
            oof.loc[fill_mask, "pred_fill"],
            weights.loc[oof.index][fill_mask],
        ),
        "baseline_brier": _weighted_brier(
            fill_actual[fill_mask],
            oof.loc[fill_mask, "logistic_fill"],
            weights.loc[oof.index][fill_mask],
        ),
        "holdout_challenger_brier": _weighted_brier(
            holdout_fill_actual[holdout_fill_mask],
            holdout.loc[holdout_fill_mask, "pred_fill"],
            weights.loc[holdout.index][holdout_fill_mask],
        ),
        "holdout_baseline_brier": _weighted_brier(
            holdout_fill_actual[holdout_fill_mask],
            holdout.loc[holdout_fill_mask, "logistic_fill"],
            weights.loc[holdout.index][holdout_fill_mask],
        ),
        "max_calibration_error": _max_calibration_error(
            fill_actual[fill_mask], oof.loc[fill_mask, "pred_fill"]
        ),
        "holdout_max_calibration_error": _max_calibration_error(
            holdout_fill_actual[holdout_fill_mask],
            holdout.loc[holdout_fill_mask, "pred_fill"],
        ),
    }
    fill_residual = np.abs(
        fill_actual[fill_mask].to_numpy(dtype=float)
        - oof.loc[fill_mask, "pred_fill"].to_numpy(dtype=float)
    )
    residual_q80["fill"] = _weighted_quantile(
        fill_residual,
        weights.loc[oof.index][fill_mask].to_numpy(dtype=float),
        0.8,
    )

    top20 = _top20_metrics(rows, oof)
    monotonicity = {
        "downside": _directionally_monotonic(
            oof.loc[downside_mask, "pred_downside"],
            downside_actual[downside_mask],
        ),
        "downside_holdout": _directionally_monotonic(
            holdout.loc[holdout_downside_mask, "pred_downside"],
            holdout_downside_actual[holdout_downside_mask],
        ),
        "downside_stop_rate": _optional_monotonicity(
            rows,
            oof.index,
            oof["pred_downside"],
            "hit_stop",
            downside_mask,
        ),
        "downside_stop_rate_holdout": _optional_monotonicity(
            rows,
            holdout.index,
            holdout["pred_downside"],
            "hit_stop",
            holdout_downside_mask,
        ),
        "fill": _directionally_monotonic(
            oof.loc[fill_mask, "pred_fill"], fill_actual[fill_mask]
        ),
        "fill_holdout": _directionally_monotonic(
            holdout.loc[holdout_fill_mask, "pred_fill"],
            holdout_fill_actual[holdout_fill_mask],
        ),
    }
    counterfactual = _counterfactual_metrics(rows, oof)
    return (
        {
            "rank": rank,
            "top20": top20,
            "monotonicity": monotonicity,
            "counterfactual": counterfactual,
            "errors": errors,
            "interval_coverage": interval_coverage,
        },
        residual_q80,
    )


def _top20_metrics(rows: pd.DataFrame, oof: pd.DataFrame) -> dict[str, float]:
    mask = _evaluation_mask(rows, oof.index, "ret_5d", require_fill=True)
    selected_indices = [index for index in oof.index if bool(mask.loc[index])]
    if not selected_indices:
        return {
            "challenger_mean": math.nan,
            "pool_mean": math.nan,
            "ridge_mean": math.nan,
        }
    actual = pd.to_numeric(rows.loc[selected_indices, "ret_5d_net"], errors="coerce")
    count = max(1, int(math.ceil(len(selected_indices) * 0.20)))
    challenger_indices = (
        oof.loc[selected_indices, "pred_ret_5d"].sort_values(ascending=False).index[:count]
    )
    ridge_indices = (
        oof.loc[selected_indices, "ridge_ret_5d"].sort_values(ascending=False).index[:count]
    )
    weights = _sample_weights(rows)
    return {
        "challenger_mean": _weighted_mean(
            rows.loc[challenger_indices, "ret_5d_net"], weights.loc[challenger_indices]
        ),
        "pool_mean": _weighted_mean(actual, weights.loc[selected_indices]),
        "ridge_mean": _weighted_mean(
            rows.loc[ridge_indices, "ret_5d_net"], weights.loc[ridge_indices]
        ),
    }


def _counterfactual_metrics(
    rows: pd.DataFrame, oof: pd.DataFrame
) -> dict[str, object]:
    if not RULE_COUNTERFACTUAL_COLUMNS.issubset(rows.columns):
        return {
            "evaluable": False,
            "missing_columns": sorted(RULE_COUNTERFACTUAL_COLUMNS.difference(rows.columns)),
            "return_improvement": math.nan,
            "max_drawdown_delta": math.nan,
        }
    joined = rows.loc[oof.index].copy(deep=True)
    joined["pred_ret_5d"] = oof["pred_ret_5d"]
    joined["pred_downside"] = oof["pred_downside"]
    joined["pred_fill"] = oof["pred_fill"]
    joined["actual"] = pd.to_numeric(joined["ret_5d_net"], errors="coerce")
    rejection_stage = joined["rule_rejection_stage"].fillna("").astype(str)
    rank_eligible = joined["rule_selected"].fillna(False).astype(bool) | rejection_stage.isin(
        {"", "score"}
    )
    joined = joined[
        rank_eligible
        & joined["actual"].map(math.isfinite)
        & pd.to_numeric(joined["fill_label"], errors="coerce").eq(1)
    ]
    if joined.empty:
        return {
            "evaluable": False,
            "missing_columns": [],
            "return_improvement": math.nan,
            "max_drawdown_delta": math.nan,
        }
    daily_rule: list[float] = []
    daily_model: list[float] = []
    for _, group in joined.groupby("trade_date", sort=True):
        baseline_candidates = group[
            group["rule_selected"].fillna(0).astype(bool)
        ]
        if baseline_candidates.empty:
            continue
        slot_values = pd.to_numeric(group["rule_slot_count"], errors="coerce")
        if slot_values.isna().any() or slot_values.nunique() != 1:
            return {
                "evaluable": False,
                "missing_columns": [],
                "return_improvement": math.nan,
                "max_drawdown_delta": math.nan,
                "reason": "RULE_SLOT_COUNT_INVALID",
            }
        count = min(len(baseline_candidates), int(slot_values.iloc[0]))
        if count <= 0:
            continue
        baseline = baseline_candidates.sort_values(
            ["rule_order", "rule_score"],
            ascending=[True, False],
            kind="mergesort",
        ).head(count)
        model = group.sort_values(
            ["pred_ret_5d", "rule_order"],
            ascending=[False, True],
            kind="mergesort",
        ).head(count)
        if (
            baseline["rule_target_qty"].isna().any()
            or model["rule_target_qty"].isna().any()
        ):
            return {
                "evaluable": False,
                "missing_columns": [],
                "return_improvement": math.nan,
                "max_drawdown_delta": math.nan,
                "reason": "RULE_TARGET_QTY_MISSING",
            }
        daily_rule.append(
            _weighted_mean(baseline["actual"], baseline["rule_target_qty"])
        )
        daily_model.append(_weighted_mean(model["actual"], model["rule_target_qty"]))
    if not daily_rule:
        return {
            "evaluable": False,
            "missing_columns": [],
            "return_improvement": math.nan,
            "max_drawdown_delta": math.nan,
        }
    rule_drawdown = _max_drawdown(daily_rule)
    model_drawdown = _max_drawdown(daily_model)
    return {
        "evaluable": True,
        "days": len(daily_rule),
        "rule_mean": float(np.mean(daily_rule)),
        "model_mean": float(np.mean(daily_model)),
        "return_improvement": float(np.mean(daily_model) - np.mean(daily_rule)),
        "rule_max_drawdown": rule_drawdown,
        "model_max_drawdown": model_drawdown,
        "max_drawdown_delta": model_drawdown - rule_drawdown,
    }


def _publish_bundle(
    output_dir: Path,
    model_id: str,
    bundle_payload: Mapping[str, object],
    manifest_payload: Mapping[str, object],
) -> tuple[Path, str, str, bool]:
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    final_dir = root / model_id
    temp_dir = Path(tempfile.mkdtemp(prefix=f".tmp-{model_id}-", dir=root))
    published_new = False
    try:
        bundle_path = temp_dir / "bundle.joblib"
        joblib.dump(dict(bundle_payload), bundle_path, compress=0, protocol=5)
        bundle_sha256 = _file_sha256(bundle_path)
        payload = dict(manifest_payload)
        if "artifact_sha256" in payload:
            raise TrainingError("SELF_REFERENTIAL_ARTIFACT_HASH_FORBIDDEN")
        payload["files"] = {"bundle.joblib": bundle_sha256}
        manifest_bytes = _canonical_json_bytes(payload)
        manifest_path = temp_dir / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        _verify_staged_bundle(temp_dir, model_id)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        artifact_sha256 = _directory_sha256(temp_dir)
        if final_dir.exists():
            existing_manifest = _file_sha256(final_dir / "manifest.json")
            existing_artifact = _directory_sha256(final_dir)
            if (
                existing_manifest != manifest_sha256
                or existing_artifact != artifact_sha256
            ):
                raise TrainingError(f"IMMUTABLE_MODEL_ID_CONFLICT:{model_id}")
            shutil.rmtree(temp_dir)
            return final_dir, manifest_sha256, artifact_sha256, True
        os.replace(temp_dir, final_dir)
        published_new = True
        _verify_staged_bundle(final_dir, model_id)
        if _directory_sha256(final_dir) != artifact_sha256:
            raise TrainingError("PUBLISHED_ARTIFACT_HASH_MISMATCH")
        return final_dir, manifest_sha256, artifact_sha256, False
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        if published_new and final_dir.exists():
            shutil.rmtree(final_dir, ignore_errors=True)
        raise


def _verify_staged_bundle(directory: Path, model_id: str) -> None:
    manifest_path = directory / "manifest.json"
    bundle_path = directory / "bundle.joblib"
    if not manifest_path.is_file() or not bundle_path.is_file():
        raise TrainingError("MODEL_BUNDLE_INCOMPLETE")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TrainingError("MODEL_MANIFEST_INVALID")
    if str(manifest.get("model_id")) != model_id:
        raise TrainingError("MODEL_MANIFEST_ID_MISMATCH")
    if str(manifest.get("generated_by")) != GENERATED_BY:
        raise TrainingError("MODEL_MANIFEST_GENERATOR_MISMATCH")
    if "artifact_sha256" in manifest:
        raise TrainingError("SELF_REFERENTIAL_ARTIFACT_HASH_FORBIDDEN")
    files = _mapping(manifest.get("files"))
    if files.get("bundle.joblib") != _file_sha256(bundle_path):
        raise TrainingError("MODEL_FILE_HASH_MISMATCH:bundle.joblib")
    loaded = joblib.load(bundle_path)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("models"), dict):
        raise TrainingError("MODEL_BUNDLE_FORMAT_INVALID")
    if str(loaded.get("model_id")) != model_id:
        raise TrainingError("MODEL_BUNDLE_ID_MISMATCH")


def _make_preprocessor(
    numeric_features: Sequence[str],
    categorical_features: Sequence[str],
    *,
    scale_numeric: bool = False,
) -> ColumnTransformer:
    transformers: list[tuple[str, object, Sequence[str]]] = []
    if numeric_features:
        numeric_steps: list[tuple[str, object]] = [
            ("imputer", SimpleImputer(strategy="median"))
        ]
        if scale_numeric:
            numeric_steps.append(("scaler", StandardScaler()))
        transformers.append(
            ("num", Pipeline(numeric_steps), list(numeric_features))
        )
    if categorical_features:
        transformers.append(
            (
                "cat",
                Pipeline(
                    [
                        (
                            "imputer",
                            SimpleImputer(
                                strategy="constant", fill_value="__MISSING__"
                            ),
                        ),
                        (
                            "onehot",
                            OneHotEncoder(
                                handle_unknown="ignore", sparse_output=False
                            ),
                        ),
                    ]
                ),
                list(categorical_features),
            )
        )
    return ColumnTransformer(
        transformers=transformers,
        remainder="drop",
        sparse_threshold=0.0,
        verbose_feature_names_out=False,
    )


def _feature_schema(
    rows: pd.DataFrame, feature_names: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    numeric: list[str] = []
    categorical: list[str] = []
    dropped: list[str] = []
    for name in feature_names:
        if name not in rows:
            raise TrainingError(f"TRAINING_FEATURE_MISSING:{name}")
        series = rows[name]
        non_missing = series[~series.isna()]
        if non_missing.nunique(dropna=True) <= 1:
            dropped.append(str(name))
            continue
        converted = pd.to_numeric(non_missing, errors="coerce")
        if len(non_missing) and converted.notna().all() and not all(
            isinstance(value, (bool, np.bool_)) for value in non_missing.tolist()
        ):
            numeric.append(str(name))
        else:
            categorical.append(str(name))
    return tuple(numeric), tuple(categorical), tuple(dropped)


def _build_drift_reference(
    rows: pd.DataFrame,
    numeric_features: Sequence[str],
    categorical_features: Sequence[str],
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for name in numeric_features:
        values = pd.to_numeric(rows[name], errors="coerce")
        finite = values[np.isfinite(values)]
        bins = sorted(
            {
                float(value)
                for value in finite.quantile(
                    [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
                ).tolist()
                if math.isfinite(float(value))
            }
        )
        counts = np.zeros(len(bins) + 2, dtype=int)
        for raw in values.tolist():
            if raw is None or not math.isfinite(float(raw)):
                counts[-1] += 1
            else:
                counts[int(np.searchsorted(bins, float(raw), side="right"))] += 1
        result[name] = {
            "kind": "numeric",
            "bins": tuple(bins),
            "counts": tuple(int(value) for value in counts),
            "frequencies": _smoothed_frequencies(counts),
        }
    for name in categorical_features:
        observed = sorted(
            {
                str(value)
                for value in rows[name].tolist()
                if not _is_missing(value)
            }
        )
        categories = [
            value for value in observed if value not in {"OTHER", "MISSING"}
        ]
        categories.extend(["OTHER", "MISSING"])
        indexes = {value: index for index, value in enumerate(categories)}
        counts = np.zeros(len(categories), dtype=int)
        for raw in rows[name].tolist():
            if _is_missing(raw):
                bucket = "MISSING"
            else:
                bucket = str(raw)
                if bucket not in indexes:
                    bucket = "OTHER"
            counts[indexes[bucket]] += 1
        result[name] = {
            "kind": "categorical",
            "categories": tuple(categories),
            "counts": tuple(int(value) for value in counts),
            "frequencies": _smoothed_frequencies(counts),
        }
    return result


def _smoothed_frequencies(counts: np.ndarray) -> tuple[float, ...]:
    values = np.asarray(counts, dtype=float)
    denominator = float(values.sum() + PSI_PSEUDOCOUNT * len(values))
    if denominator <= 0:
        return tuple()
    return tuple(float(value) for value in (values + PSI_PSEUDOCOUNT) / denominator)


def _target_mask(
    rows: pd.DataFrame,
    head: str,
    *,
    label_cutoff: str,
    require_fill: bool,
) -> pd.Series:
    target = pd.to_numeric(rows[TARGET_COLUMNS[head]], errors="coerce")
    mask = target.notna() & target.map(math.isfinite)
    maturity = _maturity_before(rows, head, label_cutoff)
    mask &= maturity
    if require_fill:
        fill = pd.to_numeric(rows.get("fill_label"), errors="coerce")
        mask &= fill.eq(1)
        mask &= _maturity_before(rows, "fill", label_cutoff)
    elif head == "fill":
        mask &= target.isin([0, 1])
    return mask.fillna(False)


def _maturity_before(rows: pd.DataFrame, head: str, cutoff_date: str) -> pd.Series:
    candidates = {
        "ret_3d": ("ret_3d_matured_at", "matured_3d_at"),
        "ret_5d": ("ret_5d_matured_at", "matured_5d_at"),
        "ret_10d": ("ret_10d_matured_at", "matured_10d_at"),
        "downside": (
            "downside_matured_at",
            "matured_downside_at",
        ),
        "fill": ("fill_matured_at", "matured_fill_at"),
    }[head]
    column = next(
        (name for name in candidates if name in rows and rows[name].notna().any()),
        None,
    )
    if column is None:
        return pd.Series(False, index=rows.index, dtype=bool)
    parsed = pd.to_datetime(rows[column], errors="coerce", utc=True)
    cutoff = pd.Timestamp(f"{cutoff_date}T00:00:00+08:00").tz_convert("UTC")
    return parsed.notna() & parsed.lt(cutoff)


def _evaluation_mask(
    rows: pd.DataFrame,
    indices: Iterable[int],
    head: str,
    *,
    require_fill: bool,
) -> pd.Series:
    selected = list(indices)
    target = pd.to_numeric(
        rows.loc[selected, TARGET_COLUMNS[head]], errors="coerce"
    )
    mask = target.notna() & target.map(math.isfinite)
    if require_fill:
        mask &= pd.to_numeric(
            rows.loc[selected, "fill_label"], errors="coerce"
        ).eq(1)
    elif head == "fill":
        mask &= target.isin([0, 1])
    return mask.fillna(False)


def _sample_weights(rows: pd.DataFrame) -> pd.Series:
    if "sample_weight" not in rows:
        return pd.Series(1.0, index=rows.index, dtype=float)
    result = pd.to_numeric(rows["sample_weight"], errors="coerce")
    if result.isna().any() or (~result.map(math.isfinite)).any() or (result <= 0).any():
        raise TrainingError("INVALID_SAMPLE_WEIGHT")
    return result.astype(float)


def _oof_rank_for_head(rows: pd.DataFrame, oof: pd.DataFrame, head: str) -> float:
    mask = _evaluation_mask(rows, oof.index, head, require_fill=True)
    return _spearman(
        rows.loc[oof.index, TARGET_COLUMNS[head]][mask],
        oof.loc[mask, f"pred_{head}"],
    )


def _oof_references(oof: pd.DataFrame) -> dict[str, tuple[float, ...]]:
    mapping = {
        "ret_3d": "pred_ret_3d",
        "ret_5d": "pred_ret_5d",
        "ret_10d": "pred_ret_10d",
        "downside": "pred_downside",
        "fill": "pred_fill",
    }
    result: dict[str, tuple[float, ...]] = {}
    for head, column in mapping.items():
        values = sorted(
            float(value)
            for value in pd.to_numeric(oof[column], errors="coerce").tolist()
            if math.isfinite(float(value))
        )
        result[head] = tuple(values)
    return result


def _head_metrics(metrics: Mapping[str, object]) -> dict[str, HeadMetrics]:
    rank = _mapping(metrics.get("rank"))
    errors = _mapping(metrics.get("errors"))
    interval = _mapping(metrics.get("interval_coverage"))
    result: dict[str, HeadMetrics] = {}
    for head in RETURN_HEADS:
        head_rank = _mapping(rank.get(head))
        head_errors = _mapping(errors.get(head))
        result[head] = HeadMetrics(
            head=head,
            challenger_name="HistGradientBoostingRegressor",
            baseline_name="Ridge",
            oof_metrics={
                "rank": head_rank.get("oof"),
                "mae": head_errors.get("challenger_mae"),
                "baseline_mae": head_errors.get("baseline_mae"),
                "interval": _mapping(interval.get(head)).get("folds"),
            },
            holdout_metrics={
                "rank": head_rank.get("holdout"),
                "mae": head_errors.get("holdout_challenger_mae"),
                "baseline_mae": head_errors.get("holdout_baseline_mae"),
                "interval": _mapping(interval.get(head)).get("holdout"),
            },
        )
    downside = _mapping(errors.get("downside"))
    result["downside"] = HeadMetrics(
        head="downside",
        challenger_name="HistGradientBoostingRegressor(loss=quantile,quantile=0.8)",
        baseline_name="constant_train_q80",
        oof_metrics={
            "pinball": downside.get("challenger_pinball"),
            "baseline_pinball": downside.get("baseline_pinball"),
            "interval": _mapping(interval.get("downside")).get("folds"),
        },
        holdout_metrics={
            "pinball": downside.get("holdout_challenger_pinball"),
            "baseline_pinball": downside.get("holdout_baseline_pinball"),
            "interval": _mapping(interval.get("downside")).get("holdout"),
        },
    )
    fill = _mapping(errors.get("fill"))
    result["fill"] = HeadMetrics(
        head="fill",
        challenger_name="calibrated_HistGradientBoostingClassifier",
        baseline_name="LogisticRegression",
        oof_metrics={
            "brier": fill.get("challenger_brier"),
            "baseline_brier": fill.get("baseline_brier"),
            "max_calibration_error": fill.get("max_calibration_error"),
        },
        holdout_metrics={
            "brier": fill.get("holdout_challenger_brier"),
            "baseline_brier": fill.get("holdout_baseline_brier"),
            "max_calibration_error": fill.get("holdout_max_calibration_error"),
        },
    )
    return result


def _holdout_metric_summary(metrics: Mapping[str, object]) -> dict[str, object]:
    rank = _mapping(metrics.get("rank"))
    errors = _mapping(metrics.get("errors"))
    interval = _mapping(metrics.get("interval_coverage"))
    return {
        "rank": {
            head: _mapping(rank.get(head)).get("holdout") for head in RETURN_HEADS
        },
        "errors": {
            head: {
                key: value
                for key, value in _mapping(errors.get(head)).items()
                if str(key).startswith("holdout_")
            }
            for head in (*RETURN_HEADS, "downside", "fill")
        },
        "interval_coverage": {
            head: _mapping(interval.get(head)).get("holdout")
            for head in REGRESSION_HEADS
        },
        "evaluated_after_freeze": True,
    }


def _readiness_dict(readiness: DataReadiness) -> dict[str, object]:
    return {
        "linear_diagnostic_ready": readiness.linear_diagnostic_ready,
        "gradient_diagnostic_ready": readiness.gradient_diagnostic_ready,
        "diagnostic_ready": readiness.diagnostic_ready,
        "l0_ready": readiness.l0_ready,
        "status": readiness.status,
        "diagnostic_reasons": readiness.diagnostic_reasons,
        "l0_reasons": readiness.l0_reasons,
        "metrics": readiness.metrics,
    }


def _provenance_failures(frame: TrainingFrame) -> tuple[str, ...]:
    reasons: list[str] = []
    if (
        str(frame.metadata.get("construction_source") or "")
        != "build_training_frame-v2"
        or not str(frame.metadata.get("strict_provenance_sha256") or "")
    ):
        reasons.append("L0_STRICT_BUILDER_PROVENANCE_REQUIRED")
    if not str(frame.metadata.get("cost_sha256") or ""):
        reasons.append("COST_HASH_MISSING")
    if not str(frame.metadata.get("policy_sha256") or ""):
        reasons.append("LABEL_POLICY_HASH_MISSING")
    for head, columns in {
        "ret_3d": ("ret_3d_matured_at", "matured_3d_at"),
        "ret_5d": ("ret_5d_matured_at", "matured_5d_at"),
        "ret_10d": ("ret_10d_matured_at", "matured_10d_at"),
        "downside": (
            "downside_matured_at",
            "matured_downside_at",
        ),
        "fill": ("fill_matured_at", "matured_fill_at"),
    }.items():
        if not any(column in frame.rows for column in columns):
            reasons.append(f"{head.upper()}_MATURITY_EVIDENCE_MISSING")
    return tuple(reasons)


def _dependency_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scikit-learn": sklearn.__version__,
        "joblib": _package_version("joblib", joblib.__version__),
    }


def _package_version(name: str, fallback: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return str(fallback)


def _calibration_split(
    target: np.ndarray, fraction: float
) -> tuple[np.ndarray, np.ndarray]:
    if len(target) < 12:
        return np.arange(len(target), dtype=int), np.asarray([], dtype=int)
    preferred = max(2, int(math.floor(len(target) * (1.0 - fraction))))
    for split_at in range(preferred, 1, -1):
        base = np.arange(split_at, dtype=int)
        calibration = np.arange(split_at, len(target), dtype=int)
        if len(calibration) < 2:
            continue
        if set(np.unique(target[base])) == {0, 1} and set(
            np.unique(target[calibration])
        ) == {0, 1}:
            return base, calibration
    return np.arange(len(target), dtype=int), np.asarray([], dtype=int)


def _directionally_monotonic(
    predictions: Sequence[object], actual: Sequence[object]
) -> bool:
    frame = pd.DataFrame(
        {
            "prediction": pd.to_numeric(pd.Series(predictions), errors="coerce"),
            "actual": pd.to_numeric(pd.Series(actual), errors="coerce"),
        }
    ).dropna()
    if len(frame) < 10 or frame["prediction"].nunique() < 2:
        return False
    bins = min(10, frame["prediction"].nunique())
    try:
        frame["bucket"] = pd.qcut(
            frame["prediction"], q=bins, duplicates="drop", labels=False
        )
    except ValueError:
        return False
    means = frame.groupby("bucket", observed=True)["actual"].mean().to_numpy()
    if len(means) < 2:
        return False
    return bool(np.all(np.diff(means) >= -1e-12))


def _optional_monotonicity(
    rows: pd.DataFrame,
    indices: Sequence[int] | pd.Index,
    predictions: Sequence[object],
    target_column: str,
    base_mask: pd.Series,
) -> bool:
    if target_column not in rows:
        return False
    selected_indices = list(indices)
    actual = pd.to_numeric(
        rows.loc[selected_indices, target_column], errors="coerce"
    )
    mask = base_mask.reindex(selected_indices).fillna(False) & actual.notna()
    return _directionally_monotonic(
        pd.Series(predictions, index=selected_indices)[mask], actual[mask]
    )


def _max_calibration_error(
    actual: Sequence[object], probability: Sequence[object]
) -> float:
    frame = pd.DataFrame(
        {
            "actual": pd.to_numeric(pd.Series(actual), errors="coerce"),
            "probability": pd.to_numeric(pd.Series(probability), errors="coerce"),
        }
    ).dropna()
    if len(frame) < 10 or frame["probability"].nunique() < 2:
        return math.nan
    try:
        frame["bucket"] = pd.qcut(
            frame["probability"],
            q=min(10, frame["probability"].nunique()),
            duplicates="drop",
            labels=False,
        )
    except ValueError:
        return math.nan
    grouped = frame.groupby("bucket", observed=True).agg(
        actual=("actual", "mean"), probability=("probability", "mean")
    )
    if grouped.empty:
        return math.nan
    return float((grouped["actual"] - grouped["probability"]).abs().max())


def _weighted_mae(
    actual: Sequence[object], predicted: Sequence[object], weights: Sequence[object]
) -> float:
    a, p, w = _finite_triplet(actual, predicted, weights)
    if not len(a):
        return math.nan
    return float(mean_absolute_error(a, p, sample_weight=w))


def _weighted_pinball(
    actual: Sequence[object], predicted: Sequence[object], weights: Sequence[object]
) -> float:
    a, p, w = _finite_triplet(actual, predicted, weights)
    if not len(a):
        return math.nan
    return float(mean_pinball_loss(a, p, alpha=0.8, sample_weight=w))


def _weighted_brier(
    actual: Sequence[object], predicted: Sequence[object], weights: Sequence[object]
) -> float:
    a, p, w = _finite_triplet(actual, predicted, weights)
    if not len(a):
        return math.nan
    return float(brier_score_loss(a.astype(int), np.clip(p, 0, 1), sample_weight=w))


def _finite_triplet(
    actual: Sequence[object], predicted: Sequence[object], weights: Sequence[object]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a = pd.to_numeric(pd.Series(actual), errors="coerce").to_numpy(dtype=float)
    p = pd.to_numeric(pd.Series(predicted), errors="coerce").to_numpy(dtype=float)
    w = pd.to_numeric(pd.Series(weights), errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(a) & np.isfinite(p) & np.isfinite(w) & (w > 0)
    return a[mask], p[mask], w[mask]


def _weighted_mean(values: Sequence[object], weights: Sequence[object]) -> float:
    v = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)
    w = pd.to_numeric(pd.Series(weights), errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(v) & np.isfinite(w) & (w > 0)
    if not mask.any():
        return math.nan
    return float(np.average(v[mask], weights=w[mask]))


def _weighted_quantile(
    values: Sequence[float], weights: Sequence[float], quantile: float
) -> float:
    value_array = np.asarray(values, dtype=float)
    weight_array = np.asarray(weights, dtype=float)
    mask = (
        np.isfinite(value_array)
        & np.isfinite(weight_array)
        & (weight_array > 0)
    )
    value_array = value_array[mask]
    weight_array = weight_array[mask]
    if not len(value_array):
        return math.nan
    order = np.argsort(value_array, kind="mergesort")
    values_sorted = value_array[order]
    weights_sorted = weight_array[order]
    cumulative = np.cumsum(weights_sorted)
    cutoff = float(quantile) * cumulative[-1]
    index = min(int(np.searchsorted(cumulative, cutoff, side="left")), len(values_sorted) - 1)
    return float(values_sorted[index])


def _finite_quantile(values: Sequence[object], quantile: float) -> float:
    array = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)
    array = array[np.isfinite(array)]
    if not len(array):
        return math.nan
    return float(np.quantile(array, quantile))


def _interval_coverage(
    actual: Sequence[object], predicted: Sequence[object], residual_q80: float
) -> float:
    a = pd.to_numeric(pd.Series(actual), errors="coerce").to_numpy(dtype=float)
    p = pd.to_numeric(pd.Series(predicted), errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(a) & np.isfinite(p)
    if not mask.any() or not math.isfinite(float(residual_q80)):
        return math.nan
    return float(np.mean(np.abs(a[mask] - p[mask]) <= float(residual_q80)))


def _spearman(actual: Sequence[object], predicted: Sequence[object]) -> float:
    frame = pd.DataFrame(
        {
            "actual": pd.to_numeric(pd.Series(actual), errors="coerce"),
            "predicted": pd.to_numeric(pd.Series(predicted), errors="coerce"),
        }
    ).dropna()
    if len(frame) < 2 or frame["actual"].nunique() < 2 or frame["predicted"].nunique() < 2:
        return math.nan
    value = frame["actual"].corr(frame["predicted"], method="spearman")
    return float(value) if value is not None and math.isfinite(float(value)) else math.nan


def _max_drawdown(daily_returns: Sequence[float]) -> float:
    values = np.asarray(daily_returns, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return math.nan
    wealth = np.cumprod(1.0 + np.clip(values, -0.999999, None))
    peaks = np.maximum.accumulate(wealth)
    drawdowns = wealth / peaks - 1.0
    return float(abs(np.min(drawdowns)))


def _indices_for_dates(rows: pd.DataFrame, dates: Sequence[str]) -> tuple[int, ...]:
    selected = set(str(value) for value in dates)
    return tuple(
        int(index)
        for index, value in rows["trade_date"].items()
        if str(value) in selected
    )


def _metadata_text(frame: TrainingFrame, field: str) -> str:
    return str(frame.metadata.get(field) or "").strip()


def _deterministic_created_at(
    rows: pd.DataFrame, development_indices: Sequence[int]
) -> str:
    if "decision_at" in rows:
        parsed = pd.to_datetime(
            rows.loc[list(development_indices), "decision_at"], errors="coerce", utc=True
        )
        if parsed.notna().any():
            value = parsed.max().to_pydatetime()
            return value.astimezone(timezone.utc).isoformat()
    last_date = date.fromisoformat(
        str(rows.loc[list(development_indices), "trade_date"].max())
    )
    return datetime.combine(last_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _directory_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in directory.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(directory).as_posix(),
    ):
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_safe(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {str(key): _freeze(item) for key, item in sorted(value.items())}
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _float_list(value: object) -> list[float]:
    if not isinstance(value, (tuple, list)):
        return []
    return [_finite_or_nan(item) for item in value]


def _finite_or_nan(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _is_missing(value: object) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(result) if isinstance(result, (bool, np.bool_)) else False


def _positive(value: object) -> bool:
    number = _finite_or_nan(value)
    return math.isfinite(number) and number > 0


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


__all__ = [
    "TrainingConfig",
    "HeadMetrics",
    "ModelEvaluation",
    "ModelBundleManifest",
    "TrainingResult",
    "TrainingError",
    "CalibratedHGBClassifier",
    "evaluate_performance_gates",
    "train_challenger",
]
