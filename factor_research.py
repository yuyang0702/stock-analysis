"""Walk-forward-aware factor evidence and release gates."""

import math


FACTOR_RESEARCH_VERSION = "2026-08-10.1"


def _research_metrics(values):
    returns = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not returns:
        return {"count": 0, "expectancy": 0.0, "profit_factor": 0.0, "win_rate": 0.0}
    gains = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    return {
        "count": len(returns),
        "expectancy": sum(returns) / len(returns),
        "profit_factor": gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0),
        "win_rate": sum(value > 0 for value in returns) / len(returns),
    }

def evaluate_factor_release_gate(
    factor_path,
    cycle_net_returns,
    walk_forward_fold_expectancies,
    baseline_max_drawdown,
    candidate_max_drawdown,
):
    metrics = _research_metrics(cycle_net_returns)
    minimum = 50 if str(factor_path) == "wave3_v1" else 30
    folds = [float(value) for value in walk_forward_fold_expectancies]
    same_direction = sum(value > 0 for value in folds)
    top3_removed = sorted(
        [float(value) for value in cycle_net_returns], reverse=True
    )[3:]
    robust = _research_metrics(top3_removed)
    drawdown_limit = min(
        float(baseline_max_drawdown) + 0.005,
        float(baseline_max_drawdown) * 1.25,
    )
    reasons = []
    if metrics["count"] < minimum:
        reasons.append("INSUFFICIENT_COMPLETED_CYCLES")
    if metrics["expectancy"] <= 0:
        reasons.append("NET_EXPECTANCY_NOT_POSITIVE")
    if metrics["profit_factor"] < 1.2:
        reasons.append("PROFIT_FACTOR_BELOW_1_2")
    if robust["count"] and robust["expectancy"] <= 0:
        reasons.append("TOP3_REMOVAL_UNSTABLE")
    if len(folds) < 3 or same_direction < 2:
        reasons.append("WALK_FORWARD_DIRECTION_UNSTABLE")
    if float(candidate_max_drawdown) > drawdown_limit:
        reasons.append("DRAWDOWN_ABOVE_BASELINE_LIMIT")
    return {
        "version": FACTOR_RESEARCH_VERSION,
        "factor_path": str(factor_path),
        "passed": not bool(reasons),
        "reasons": reasons,
        "metrics": metrics,
        "top3_removed_metrics": robust,
        "walk_forward_positive_folds": same_direction,
        "walk_forward_fold_count": len(folds),
        "drawdown_limit": drawdown_limit,
        "candidate_max_drawdown": float(candidate_max_drawdown),
    }
