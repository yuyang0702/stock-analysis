"""Bounded opportunity and trade attribution helpers."""

from collections import Counter, defaultdict


ATTRIBUTION_VERSION = "2026-08-10.1"


def build_strategy_attribution(
    trade_date,
    code,
    decision_at,
    factor_path,
    setup_id,
    selected,
    rejection_code,
    factor_score,
    target_qty=0,
    round_trip_cost_yuan=None,
    simulation_only=True,
):
    return {
        "attribution_version": ATTRIBUTION_VERSION,
        "trade_date": str(trade_date),
        "code": str(code),
        "decision_at": str(decision_at),
        "factor_path": str(factor_path),
        "setup_id": str(setup_id or ""),
        "selected": bool(selected),
        "rejection_code": str(rejection_code or ""),
        "factor_score": float(factor_score or 0.0),
        "target_qty": int(target_qty or 0),
        "round_trip_cost_yuan": (
            None if round_trip_cost_yuan is None else float(round_trip_cost_yuan)
        ),
        "simulation_only": bool(simulation_only),
    }


def aggregate_strategy_attribution(rows):
    """Aggregate in memory; callers persist only bounded latest reports."""
    by_path = defaultdict(lambda: {
        "opportunities": 0,
        "selected": 0,
        "rejections": Counter(),
        "target_qty": 0,
        "estimated_cost_yuan": 0.0,
    })
    for row in rows:
        path = str(row.get("factor_path") or "unknown")
        bucket = by_path[path]
        bucket["opportunities"] += 1
        bucket["selected"] += int(bool(row.get("selected")))
        if row.get("rejection_code"):
            bucket["rejections"][str(row["rejection_code"])] += 1
        bucket["target_qty"] += int(row.get("target_qty") or 0)
        bucket["estimated_cost_yuan"] += float(row.get("round_trip_cost_yuan") or 0.0)
    result = {}
    for path, bucket in sorted(by_path.items()):
        result[path] = {
            "opportunities": bucket["opportunities"],
            "selected": bucket["selected"],
            "selection_rate": (
                bucket["selected"] / bucket["opportunities"]
                if bucket["opportunities"] else 0.0
            ),
            "rejections": dict(bucket["rejections"].most_common(20)),
            "target_qty": bucket["target_qty"],
            "estimated_cost_yuan": round(bucket["estimated_cost_yuan"], 2),
        }
    return {"version": ATTRIBUTION_VERSION, "paths": result}
