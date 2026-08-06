"""Verified trained-model inference with fail-open-to-rules semantics."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn

from ml_contracts import CandidateSample, PredictionRecord


class ModelVerificationError(RuntimeError):
    """Raised when a registered model bundle cannot be trusted."""


@dataclass(frozen=True)
class LoadedBundle:
    model_id: str
    manifest: Mapping[str, object]
    models: Mapping[str, object]
    artifact_dir: Path
    artifact_sha256: str


@dataclass(frozen=True)
class ConfidenceResult:
    value: float
    coverage: float
    disagreement: float
    drift: float
    drift_sufficient: bool


@dataclass(frozen=True)
class RuntimeObservation:
    status: str
    model_id: str | None
    permission_level: int
    prediction_count: int
    recorded_count: int
    elapsed_ms: float
    reasons: tuple[str, ...]
    predictions: pd.DataFrame


@dataclass(frozen=True)
class CounterfactualEconomics:
    conditional_net_pnl_yuan: float
    unconditional_expected_net_pnl_yuan: float
    conservative_edge_yuan: float
    conservative_price_downside_rate: float
    filled_downside_yuan: float
    hard_risk_yuan: float


def frozen_midrank_pct(
    value: float | int | None,
    reference: Sequence[float | int],
) -> float | None:
    try:
        number = float(value) if value is not None else math.nan
        ordered = sorted(float(item) for item in reference)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not ordered or any(
        not math.isfinite(item) for item in ordered
    ):
        return None
    return _frozen_midrank_pct_sorted(number, ordered)


def compute_feature_psi(
    frame: pd.DataFrame,
    reference: Mapping[str, Mapping[str, object]],
    *,
    pseudocount: float = 0.5,
) -> dict[str, float]:
    if pseudocount <= 0 or not math.isfinite(float(pseudocount)):
        raise ValueError("PSI pseudocount must be finite and positive")
    result: dict[str, float] = {}
    for name, spec in reference.items():
        kind = str(spec.get("kind") or "")
        train_counts = np.asarray(spec.get("counts") or (), dtype=float)
        if train_counts.size == 0 or np.any(~np.isfinite(train_counts)) or np.any(train_counts < 0):
            raise ValueError(f"invalid PSI training counts for {name}")
        series = frame[name] if name in frame.columns else pd.Series([None] * len(frame))
        if kind == "numeric":
            boundaries = [float(value) for value in spec.get("bins") or ()]
            if boundaries != sorted(set(boundaries)) or any(
                not math.isfinite(value) for value in boundaries
            ):
                raise ValueError(f"invalid PSI numeric bins for {name}")
            current_counts = np.zeros(len(boundaries) + 2, dtype=float)
            for raw in series.tolist():
                try:
                    value = float(raw)
                except (TypeError, ValueError, OverflowError):
                    current_counts[-1] += 1
                    continue
                if not math.isfinite(value):
                    current_counts[-1] += 1
                else:
                    current_counts[bisect_right(boundaries, value)] += 1
        elif kind == "categorical":
            categories = [str(value) for value in spec.get("categories") or ()]
            if "OTHER" not in categories or "MISSING" not in categories:
                raise ValueError(f"categorical PSI buckets incomplete for {name}")
            current_counts = np.zeros(len(categories), dtype=float)
            indexes = {value: index for index, value in enumerate(categories)}
            for raw in series.tolist():
                if raw is None or (isinstance(raw, float) and math.isnan(raw)):
                    bucket = "MISSING"
                else:
                    bucket = str(raw)
                    if bucket not in indexes:
                        bucket = "OTHER"
                current_counts[indexes[bucket]] += 1
        else:
            raise ValueError(f"unknown PSI feature kind for {name}: {kind}")
        if train_counts.size != current_counts.size:
            raise ValueError(f"PSI bucket count mismatch for {name}")
        train = (train_counts + pseudocount) / (
            train_counts.sum() + pseudocount * train_counts.size
        )
        current = (current_counts + pseudocount) / (
            current_counts.sum() + pseudocount * current_counts.size
        )
        result[str(name)] = float(np.sum((current - train) * np.log(current / train)))
    return result


def compute_confidence(
    *,
    required_features: int,
    provided_features: int,
    challenger_d5: float,
    ridge_d5: float,
    d5_difference_p90: float,
    max_feature_psi: float,
    drift_sufficient: bool,
) -> ConfidenceResult:
    if required_features <= 0:
        coverage = 0.0
    else:
        coverage = _clip(provided_features / required_features)
    try:
        difference = abs(float(challenger_d5) - float(ridge_d5))
        denominator = max(float(d5_difference_p90), 1e-6)
        disagreement = _clip(1.0 - difference / denominator)
    except (TypeError, ValueError, OverflowError):
        disagreement = 0.0
    try:
        drift = _clip(1.0 - float(max_feature_psi) / 0.25)
    except (TypeError, ValueError, OverflowError):
        drift = 0.0
    if not drift_sufficient:
        drift = 0.0
    return ConfidenceResult(
        value=_clip(min(coverage, disagreement, drift)),
        coverage=coverage,
        disagreement=disagreement,
        drift=drift,
        drift_sufficient=bool(drift_sufficient),
    )


def predict_candidate_frame(
    frame: pd.DataFrame,
    bundle: LoadedBundle,
    *,
    drift_frame: pd.DataFrame | None = None,
    drift_min_rows: int = 200,
    drift_batch_count: int = 0,
    drift_min_batches: int = 20,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    required = [str(value) for value in bundle.manifest.get("required_features", ())]
    missing = [name for name in required if name not in frame.columns]
    features = frame.reindex(columns=required).copy()
    predictions = {
        "pred_ret_3d": _predict_regressor(bundle, "ret_3d", features),
        "pred_ret_5d": _predict_regressor(bundle, "ret_5d", features),
        "pred_ret_10d": _predict_regressor(bundle, "ret_10d", features),
        "pred_downside": _predict_regressor(bundle, "downside", features),
        "ridge_ret_5d": _predict_regressor(bundle, "ridge_ret_5d", features),
        "fill_probability": _predict_probability(bundle, "fill", features),
    }
    drift_reference = bundle.manifest.get("drift_reference")
    drift_source = frame if drift_frame is None else drift_frame
    drift_features = drift_source.reindex(columns=required).copy()
    psi = compute_feature_psi(
        drift_features,
        drift_reference if isinstance(drift_reference, Mapping) else {},
    )
    max_psi = max(psi.values(), default=0.0)
    drift_sufficient = (
        len(drift_features) >= int(drift_min_rows)
        and int(drift_batch_count) >= int(drift_min_batches)
    )
    refs = bundle.manifest.get("oof_references")
    refs = refs if isinstance(refs, Mapping) else {}
    residual = bundle.manifest.get("residual_q80")
    residual = residual if isinstance(residual, Mapping) else {}
    d5_difference_p90 = _finite_or(
        bundle.manifest.get("d5_difference_p90"), 0.0
    )
    downside_p80 = _finite_or(
        bundle.manifest.get("downside_oof_prediction_p80"), math.inf
    )

    sorted_references = {
        key: tuple(sorted(float(value) for value in raw))
        for key, raw in refs.items()
        if isinstance(raw, (tuple, list))
    }
    rows: list[dict[str, object]] = []
    for index in range(len(frame)):
        values = {name: float(array[index]) for name, array in predictions.items()}
        provided = sum(
            1 for name in required
            if name in frame.columns and not pd.isna(frame.iloc[index][name])
        )
        confidence = compute_confidence(
            required_features=len(required),
            provided_features=provided,
            challenger_d5=values["pred_ret_5d"],
            ridge_d5=values["ridge_ret_5d"],
            d5_difference_p90=d5_difference_p90,
            max_feature_psi=max_psi,
            drift_sufficient=drift_sufficient,
        )
        return_pct = _frozen_midrank_pct_sorted(
            values["pred_ret_5d"], sorted_references.get("ret_5d", ())
        )
        downside_pct = _frozen_midrank_pct_sorted(
            values["pred_downside"], sorted_references.get("downside", ())
        )
        reasons: list[str] = []
        finite = all(math.isfinite(value) for value in values.values())
        if missing or confidence.coverage < 0.95:
            reasons.append("ML_FEATURE_COVERAGE_LOW")
        if not drift_sufficient:
            reasons.append("ML_DRIFT_INSUFFICIENT")
        if not finite or return_pct is None or downside_pct is None:
            reasons.append("ML_OUTPUT_NONFINITE")
        if confidence.value < 0.60:
            reasons.append("ML_CONFIDENCE_LOW")
        usable = not reasons
        score = None
        ml_filter = False
        multiplier = 1.0
        if finite and return_pct is not None and downside_pct is not None:
            score = (
                0.60 * return_pct
                + 0.30 * (100.0 - downside_pct)
                + 0.10 * (100.0 * _clip(values["fill_probability"]))
            )
        if usable and score is not None:
            ml_filter = bool(
                values["pred_ret_5d"] + _finite_or(residual.get("ret_5d"), 0.0) <= 0
                or max(
                    0.0,
                    values["pred_downside"]
                    - _finite_or(residual.get("downside"), 0.0),
                ) >= downside_p80
                or min(
                    1.0,
                    values["fill_probability"]
                    + _finite_or(residual.get("fill"), 0.0),
                ) < 0.60
            )
            multiplier = (
                0.8 if score < 40 else
                0.9 if score < 60 else
                1.0 if score < 80 else
                1.1
            )
        row = {
            "model_id": bundle.model_id,
            **values,
            "ml_score": score,
            "ml_filter": int(ml_filter),
            "position_multiplier": multiplier,
            "confidence": confidence.value,
            "feature_coverage": confidence.coverage,
            "max_feature_psi": max_psi,
            "drift_status": "ready" if drift_sufficient else "insufficient",
            "ml_reasons": tuple(reasons),
        }
        if "sample_id" in frame.columns:
            row["sample_id"] = frame.iloc[index]["sample_id"]
        if "code" in frame.columns:
            row["code"] = frame.iloc[index]["code"]
        rows.append(row)
    return pd.DataFrame(rows)


def apply_model_policy(
    rule_frame: pd.DataFrame,
    predictions: pd.DataFrame,
    *,
    level: int,
) -> pd.DataFrame:
    if level not in {0, 1, 2, 3}:
        raise ValueError("permission level must be between 0 and 3")
    original = rule_frame.copy(deep=True)
    if level == 0 or original.empty:
        return original
    key = "sample_id" if "sample_id" in original.columns and "sample_id" in predictions.columns else "code"
    if key not in original.columns or key not in predictions.columns:
        return original
    fields = [key, "ml_score", "ml_filter", "position_multiplier"]
    available = [name for name in fields if name in predictions.columns]
    joined = original.reset_index(drop=False).rename(columns={"index": "__rule_order"}).merge(
        predictions[available].drop_duplicates(key, keep="last"),
        on=key,
        how="left",
        sort=False,
    )
    joined["ml_score"] = pd.to_numeric(joined.get("ml_score"), errors="coerce").fillna(-math.inf)
    if "rule_eligible" in joined.columns:
        eligible = joined["rule_eligible"].fillna(False).astype(bool)
    elif "rule_selected" in joined.columns:
        eligible = joined["rule_selected"].fillna(False).astype(bool)
    else:
        eligible = pd.Series(True, index=joined.index, dtype=bool)
    if "action" in joined.columns:
        buys = joined["action"].astype("string").str.lower().eq("buy")
    elif "is_sell" in joined.columns:
        buys = ~joined["is_sell"].fillna(False).astype(bool)
    else:
        buys = pd.Series(True, index=joined.index, dtype=bool)
    mutable = eligible & buys
    if level >= 1:
        eligible_rows = joined.loc[mutable].sort_values(
            ["ml_score", "__rule_order"], ascending=[False, True], kind="mergesort"
        )
        mutable_slots = list(joined.index[mutable])
        for slot, (_, ordered_row) in zip(mutable_slots, eligible_rows.iterrows()):
            joined.loc[slot] = ordered_row
    if level >= 2:
        filters = pd.to_numeric(joined.get("ml_filter"), errors="coerce").fillna(0)
        joined = joined[~(pd.Series(mutable, index=joined.index) & filters.ne(0))]
    if level >= 3 and "target_qty" in joined.columns:
        multipliers = pd.to_numeric(
            joined.get("position_multiplier"), errors="coerce"
        ).fillna(1.0).clip(lower=0.8, upper=1.1)
        quantities = pd.to_numeric(joined["target_qty"], errors="coerce").fillna(0)
        adjusted: list[int] = []
        if "rule_eligible" in joined.columns:
            eligible = joined["rule_eligible"].fillna(False).astype(bool)
        elif "rule_selected" in joined.columns:
            eligible = joined["rule_selected"].fillna(False).astype(bool)
        else:
            eligible = pd.Series(True, index=joined.index, dtype=bool)
        if "action" in joined.columns:
            buys = joined["action"].astype("string").str.lower().eq("buy")
        elif "is_sell" in joined.columns:
            buys = ~joined["is_sell"].fillna(False).astype(bool)
        else:
            buys = pd.Series(True, index=joined.index, dtype=bool)
        for index, (quantity, multiplier) in enumerate(zip(quantities, multipliers)):
            if not bool(eligible.iloc[index] and buys.iloc[index]):
                adjusted.append(int(quantity))
                continue
            raw = int(math.floor(float(quantity) * float(multiplier)))
            if raw >= 100:
                raw = (raw // 100) * 100
            adjusted.append(min(int(quantity), max(raw, 0)))
        joined["target_qty"] = adjusted
    return joined[original.columns].reset_index(drop=True)


def load_active_bundle(
    store: Any,
    model_dir: Path,
    expected_versions: Mapping[str, str],
) -> LoadedBundle | None:
    state = store.runtime_state()
    model_id = state.get("active_model_id")
    if not model_id:
        return None
    record = store.model_record(str(model_id))
    if record is None:
        raise ModelVerificationError("ACTIVE_MODEL_NOT_REGISTERED")
    root = Path(model_dir).resolve()
    artifact = Path(str(_row(record, "artifact_path") or ""))
    if not artifact.is_absolute():
        artifact = root / artifact
    artifact = artifact.resolve()
    if artifact != root and root not in artifact.parents:
        raise ModelVerificationError("MODEL_PATH_ESCAPE")
    manifest_path = artifact / "manifest.json"
    bundle_path = artifact / "bundle.joblib"
    if not manifest_path.is_file() or not bundle_path.is_file():
        raise ModelVerificationError("MODEL_BUNDLE_INCOMPLETE")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or str(manifest.get("model_id")) != str(model_id):
        raise ModelVerificationError("MODEL_MANIFEST_ID_MISMATCH")
    if str(manifest.get("generated_by") or "") != "stock-analysis":
        raise ModelVerificationError("EXTERNAL_MODEL_BUNDLE_FORBIDDEN")
    if str(manifest.get("status") or "") not in {
        "approvable_l0", "approved", "active"
    }:
        raise ModelVerificationError("MODEL_NOT_APPROVABLE")
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise ModelVerificationError("MODEL_FILE_MANIFEST_MISSING")
    if str(files.get("bundle.joblib") or "") != _file_sha256(bundle_path):
        raise ModelVerificationError("MODEL_FILE_HASH_MISMATCH:bundle.joblib")
    dependencies = manifest.get("dependency_versions")
    dependencies = dependencies if isinstance(dependencies, Mapping) else {}
    for name, expected in expected_versions.items():
        actual = manifest.get(name)
        if actual is None:
            actual = dependencies.get(name)
        if str(actual or "") != str(expected):
            raise ModelVerificationError(f"MODEL_VERSION_MISMATCH:{name}")
    artifact_sha = _bundle_sha256(artifact)
    if artifact_sha != str(_row(record, "artifact_sha256") or ""):
        raise ModelVerificationError("MODEL_ARTIFACT_HASH_MISMATCH")
    approval_reader = getattr(store, "approved_model_event", None)
    if callable(approval_reader):
        approval = approval_reader(str(model_id), artifact_sha)
        if approval is None:
            raise ModelVerificationError("ACTIVE_MODEL_NOT_APPROVED")
        runtime_level = int(state.get("permission_level") or 0)
        approved_level = _row(approval, "new_level")
        if int(-1 if approved_level is None else approved_level) < runtime_level:
            raise ModelVerificationError("ACTIVE_PERMISSION_EXCEEDS_APPROVAL")
    loaded = joblib.load(bundle_path)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("models"), dict):
        raise ModelVerificationError("MODEL_BUNDLE_FORMAT_INVALID")
    if str(loaded.get("generated_by") or "") != "stock-analysis":
        raise ModelVerificationError("MODEL_BUNDLE_GENERATOR_MISMATCH")
    if str(loaded.get("model_id") or "") != str(model_id):
        raise ModelVerificationError("MODEL_BUNDLE_ID_MISMATCH")
    if str(loaded.get("bundle_schema_version") or "") != str(
        manifest.get("manifest_schema_version") or ""
    ):
        raise ModelVerificationError("MODEL_BUNDLE_SCHEMA_MISMATCH")
    return LoadedBundle(
        model_id=str(model_id),
        manifest=manifest,
        models=loaded["models"],
        artifact_dir=artifact,
        artifact_sha256=artifact_sha,
    )


def runtime_dependency_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scikit-learn": sklearn.__version__,
        "joblib": _package_version("joblib", joblib.__version__),
    }


def candidate_samples_to_frame(samples: Sequence[CandidateSample]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for sample in samples:
        if not isinstance(sample, CandidateSample):
            raise TypeError("samples must contain CandidateSample records")
        row: dict[str, object] = {
            "sample_id": sample.sample_id,
            "code": sample.code,
            "decision_at": sample.decision_at,
        }
        row.update({name: feature.value for name, feature in sample.features.items()})
        rows.append(row)
    return pd.DataFrame.from_records(rows)


def observe_candidate_frame(
    store: Any,
    model_dir: Path,
    frame: pd.DataFrame,
    *,
    expected_versions: Mapping[str, str],
    created_at: str,
    timeout_sec: float,
    max_permission_level: int = 0,
    drift_frame: pd.DataFrame | None = None,
    drift_batch_count: int = 0,
) -> RuntimeObservation:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    if int(max_permission_level) not in {0, 1, 2, 3}:
        raise ValueError("maximum permission level must be between 0 and 3")
    if not math.isfinite(float(timeout_sec)) or float(timeout_sec) <= 0:
        raise ValueError("timeout_sec must be finite and positive")
    if frame.empty:
        _record_runtime_health(
            store,
            status="disabled",
            reason="ML_NO_CANDIDATES",
            attempted_at=created_at,
            prediction_count=0,
            successful=False,
            trading_equivalent=True,
        )
        return RuntimeObservation(
            status="no_candidates", model_id=None, permission_level=0,
            prediction_count=0, recorded_count=0, elapsed_ms=0.0,
            reasons=(), predictions=pd.DataFrame(),
        )
    state = store.runtime_state()
    active_model_id = state.get("active_model_id")
    if not active_model_id:
        _record_runtime_health(
            store,
            status="no_model",
            reason="ML_ACTIVE_MODEL_MISSING",
            attempted_at=created_at,
            prediction_count=0,
            successful=False,
            trading_equivalent=True,
        )
        return RuntimeObservation(
            status="no_active_model", model_id=None, permission_level=0,
            prediction_count=0, recorded_count=0, elapsed_ms=0.0,
            reasons=("ML_ACTIVE_MODEL_MISSING",), predictions=pd.DataFrame(),
        )
    permission_level = min(
        int(state.get("permission_level") or 0), int(max_permission_level)
    )
    started = time.perf_counter()
    try:
        bundle = load_active_bundle(store, Path(model_dir), expected_versions)
        if bundle is None:
            raise ModelVerificationError("ML_ACTIVE_MODEL_MISSING")
        if drift_frame is None:
            recent_reader = getattr(store, "recent_prediction_candidate_rows", None)
            if callable(recent_reader):
                recent_rows = recent_reader(
                    bundle.model_id, batch_limit=20, row_limit=1_000
                )
                drift_frame = pd.DataFrame.from_records(recent_rows)
                drift_batch_count = (
                    int(drift_frame["decision_at"].nunique())
                    if not drift_frame.empty and "decision_at" in drift_frame
                    else 0
                )
        predictions = predict_candidate_frame(
            frame,
            bundle,
            drift_frame=drift_frame,
            drift_batch_count=drift_batch_count,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms > float(timeout_sec) * 1000.0:
            _record_runtime_health(
                store,
                status="fallback",
                reason="ML_INFERENCE_TIMEOUT",
                attempted_at=created_at,
                prediction_count=0,
                successful=False,
                trading_equivalent=True,
            )
            return RuntimeObservation(
                status="fallback_rules",
                model_id=str(active_model_id),
                permission_level=0,
                prediction_count=0,
                recorded_count=0,
                elapsed_ms=elapsed_ms,
                reasons=("ML_INFERENCE_TIMEOUT",),
                predictions=pd.DataFrame(),
            )
        records = _prediction_records(predictions, created_at=created_at)
        recorded = int(store.record_predictions(records)) if records else 0
        _record_runtime_health(
            store,
            status="ok",
            reason="",
            attempted_at=created_at,
            prediction_count=len(records),
            successful=True,
            trading_equivalent=True if permission_level == 0 else None,
        )
        return RuntimeObservation(
            status="observed_l0" if permission_level == 0 else "counterfactual_ready",
            model_id=bundle.model_id,
            permission_level=permission_level,
            prediction_count=len(records),
            recorded_count=recorded,
            elapsed_ms=elapsed_ms,
            reasons=tuple(sorted({
                str(reason)
                for values in predictions.get("ml_reasons", pd.Series(dtype=object))
                for reason in (values if isinstance(values, (tuple, list)) else ())
            })),
            predictions=predictions,
        )
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        _record_runtime_health(
            store,
            status="fallback",
            reason=f"{type(exc).__name__}:{str(exc)[:440]}",
            attempted_at=created_at,
            prediction_count=0,
            successful=False,
            trading_equivalent=True,
        )
        return RuntimeObservation(
            status="fallback_rules",
            model_id=str(active_model_id),
            permission_level=0,
            prediction_count=0,
            recorded_count=0,
            elapsed_ms=elapsed_ms,
            reasons=(f"{type(exc).__name__}:{str(exc)[:160]}",),
            predictions=pd.DataFrame(),
        )


def observe_candidate_samples(
    store: Any,
    model_dir: Path,
    samples: Sequence[CandidateSample],
    **kwargs: object,
) -> RuntimeObservation:
    return observe_candidate_frame(
        store,
        model_dir,
        candidate_samples_to_frame(samples),
        **kwargs,
    )


def compute_counterfactual_economics(
    *,
    order_notional_yuan: float,
    predicted_gross_return: float,
    fill_probability: float,
    confidence: float,
    actual_round_trip_cost_yuan: float,
    predicted_downside_loss: float,
    downside_residual_q80: float,
    reference_round_trip_cost_rate: float,
    rule_stop_loss_yuan: float,
    no_fill_cost_yuan: float = 0.0,
) -> CounterfactualEconomics:
    values = {
        "order_notional_yuan": order_notional_yuan,
        "predicted_gross_return": predicted_gross_return,
        "fill_probability": fill_probability,
        "confidence": confidence,
        "actual_round_trip_cost_yuan": actual_round_trip_cost_yuan,
        "predicted_downside_loss": predicted_downside_loss,
        "downside_residual_q80": downside_residual_q80,
        "reference_round_trip_cost_rate": reference_round_trip_cost_rate,
        "rule_stop_loss_yuan": rule_stop_loss_yuan,
        "no_fill_cost_yuan": no_fill_cost_yuan,
    }
    if any(not math.isfinite(float(value)) for value in values.values()):
        raise ValueError("counterfactual economics values must be finite")
    if order_notional_yuan < 0 or actual_round_trip_cost_yuan < 0:
        raise ValueError("notional and costs must be non-negative")
    fill = _clip(fill_probability)
    conf = _clip(confidence)
    conditional = (
        float(order_notional_yuan) * float(predicted_gross_return)
        - float(actual_round_trip_cost_yuan)
    )
    unconditional = (
        fill * conditional - (1.0 - fill) * float(no_fill_cost_yuan)
    )
    conservative_edge = (
        conf * max(unconditional, 0.0) + min(unconditional, 0.0)
    )
    downside_rate = max(
        0.0,
        float(predicted_downside_loss)
        + float(downside_residual_q80)
        - float(reference_round_trip_cost_rate),
    )
    filled_downside = (
        float(order_notional_yuan) * downside_rate
        + float(actual_round_trip_cost_yuan)
    )
    return CounterfactualEconomics(
        conditional_net_pnl_yuan=conditional,
        unconditional_expected_net_pnl_yuan=unconditional,
        conservative_edge_yuan=conservative_edge,
        conservative_price_downside_rate=downside_rate,
        filled_downside_yuan=filled_downside,
        hard_risk_yuan=max(float(rule_stop_loss_yuan), filled_downside),
    )


def _predict_regressor(
    bundle: LoadedBundle, name: str, features: pd.DataFrame
) -> np.ndarray:
    model = bundle.models.get(name)
    if model is None or not hasattr(model, "predict"):
        raise ModelVerificationError(f"MODEL_HEAD_MISSING:{name}")
    values = np.asarray(model.predict(features), dtype=float)
    if values.shape != (len(features),):
        raise ModelVerificationError(f"MODEL_HEAD_SHAPE_INVALID:{name}")
    return values


def _predict_probability(
    bundle: LoadedBundle, name: str, features: pd.DataFrame
) -> np.ndarray:
    model = bundle.models.get(name)
    if model is None or not hasattr(model, "predict_proba"):
        raise ModelVerificationError(f"MODEL_HEAD_MISSING:{name}")
    values = np.asarray(model.predict_proba(features), dtype=float)
    if values.ndim != 2 or values.shape[0] != len(features) or values.shape[1] < 2:
        raise ModelVerificationError(f"MODEL_HEAD_SHAPE_INVALID:{name}")
    return values[:, -1]


def _prediction_records(
    predictions: pd.DataFrame,
    *,
    created_at: str,
) -> list[PredictionRecord]:
    records: list[PredictionRecord] = []
    for _, row in predictions.iterrows():
        sample_id = str(row.get("sample_id") or "").strip()
        model_id = str(row.get("model_id") or "").strip()
        if not sample_id or not model_id:
            raise ModelVerificationError("PREDICTION_IDENTITY_MISSING")
        records.append(
            PredictionRecord(
                sample_id=sample_id,
                model_id=model_id,
                created_at=created_at,
                expected_ret_3d=_optional_finite(row.get("pred_ret_3d")),
                expected_ret_5d=_optional_finite(row.get("pred_ret_5d")),
                expected_ret_10d=_optional_finite(row.get("pred_ret_10d")),
                downside_risk=_optional_finite(row.get("pred_downside")),
                fill_probability=_optional_finite(row.get("fill_probability")),
                ml_score=_optional_finite(row.get("ml_score")),
                ml_filter=bool(row.get("ml_filter")),
                position_multiplier=_optional_finite(row.get("position_multiplier")),
                confidence=_optional_finite(row.get("confidence")),
                feature_coverage=_optional_finite(row.get("feature_coverage")),
                max_feature_psi=_optional_finite(row.get("max_feature_psi")),
                drift_status=str(row.get("drift_status") or "unknown"),
                reasons=tuple(
                    str(value)
                    for value in (
                        row.get("ml_reasons")
                        if isinstance(row.get("ml_reasons"), (tuple, list))
                        else ()
                    )
                ),
            )
        )
    return records


def _record_runtime_health(
    store: object,
    *,
    status: str,
    reason: str,
    attempted_at: str,
    prediction_count: int,
    successful: bool,
    trading_equivalent: bool | None,
) -> None:
    writer = getattr(store, "record_runtime_health", None)
    if not callable(writer):
        return
    try:
        writer(
            status=status,
            reason=reason,
            attempted_at=attempted_at,
            prediction_count=prediction_count,
            successful=successful,
            trading_equivalent=trading_equivalent,
        )
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception:
        # Runtime health is audit evidence.  Failure to update it must not
        # change the pure-rule trading path or create a new trading control.
        return


def _frozen_midrank_pct_sorted(
    value: float | int | None,
    ordered: Sequence[float | int],
) -> float | None:
    try:
        number = float(value) if value is not None else math.nan
        reference = tuple(float(item) for item in ordered)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not reference or any(
        not math.isfinite(item) for item in reference
    ):
        return None
    below = bisect_left(reference, number)
    at_or_below = bisect_right(reference, number)
    return 100.0 * (below + 0.5 * (at_or_below - below)) / len(reference)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_version(name: str, fallback: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return str(fallback)


def _bundle_sha256(directory: Path) -> str:
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


def _row(record: object, name: str) -> object:
    if isinstance(record, Mapping):
        return record.get(name)
    try:
        return record[name]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return getattr(record, name, None)


def _finite_or(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _optional_finite(value: object) -> float | None:
    if value is None or value is pd.NA:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    number = float(value)
    if not math.isfinite(number):
        raise ModelVerificationError("MODEL_OUTPUT_NONFINITE")
    return number


def _clip(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(1.0, max(0.0, number))
