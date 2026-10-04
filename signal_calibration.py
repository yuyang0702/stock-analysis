"""Training-window signal calibration helpers.

The output is deliberately labelled as research evidence.  It can be used to
populate strict point-in-time features for a later run, but this module never
turns realized forward returns into a live prediction automatically.
"""

from __future__ import annotations

import math
from typing import Mapping


def _metric_mean(metric: Mapping[str, object]) -> float:
    value = float(metric.get("mean", 0.0) or 0.0)
    return value if math.isfinite(value) else 0.0


def calibrate_score_buckets(
    report: Mapping[str, object],
    *,
    horizon: int,
    min_samples: int = 20,
    shrinkage: float = 20.0,
) -> dict[str, object]:
    """Create a conservative score-to-return calibration from one report.

    The report must have been produced on a training window.  Bucket means are
    shrunk toward the training-window mean so small buckets cannot create an
    apparently attractive threshold.  Values are *realized net* basis points;
    callers must not pass them as ``expected_gross_return_bps`` without a
    separate point-in-time forecasting step.
    """
    if int(horizon) <= 0 or int(min_samples) <= 0:
        raise ValueError("horizon and min_samples must be positive")
    if not math.isfinite(float(shrinkage)) or float(shrinkage) < 0:
        raise ValueError("shrinkage must be finite and non-negative")
    horizon_key = str(int(horizon))
    metrics = report.get("horizon_metrics", {})
    by_horizon = report.get("by_horizon", {})
    if not isinstance(metrics, Mapping) or not isinstance(by_horizon, Mapping):
        raise ValueError("invalid signal research report")
    global_metric = metrics.get(horizon_key)
    bucket_metrics = (
        by_horizon.get(horizon_key, {}).get("score_bucket", {})
        if isinstance(by_horizon.get(horizon_key, {}), Mapping)
        else {}
    )
    if not isinstance(global_metric, Mapping) or not isinstance(bucket_metrics, Mapping):
        raise ValueError("horizon metrics missing")
    global_mean = _metric_mean(global_metric)
    rows: dict[str, dict[str, object]] = {}
    for bucket, raw in sorted(bucket_metrics.items()):
        if not isinstance(raw, Mapping):
            continue
        count = int(raw.get("count", 0) or 0)
        mean = _metric_mean(raw)
        weight = count / (count + float(shrinkage)) if count else 0.0
        shrunk = weight * mean + (1.0 - weight) * global_mean
        rows[str(bucket)] = {
            "count": count,
            "realized_net_mean_bps": round(mean * 10000.0, 4),
            "calibrated_net_mean_bps": round(shrunk * 10000.0, 4),
            "eligible": bool(count >= int(min_samples)),
        }
    return {
        "calibration_version": "score-bucket-net-v1",
        "horizon": int(horizon),
        "global_realized_net_mean_bps": round(global_mean * 10000.0, 4),
        "min_samples": int(min_samples),
        "shrinkage": float(shrinkage),
        "buckets": rows,
        "decision_use": "training_evidence_only",
        "warning": (
            "These are realized training-window net returns. Do not use them "
            "as live expected gross returns without a point-in-time model."
        ),
    }
