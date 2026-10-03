"""Compare identical strategy runs across independent historical datasets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

from data_source_registry import load_registry
from historical_data import HistoricalStore


METRIC_KEYS = (
    "net_return",
    "max_drawdown",
    "profit_factor",
    "turnover",
    "win_rate",
    "average_holding_days",
)


def _latest_run(store: HistoricalStore, dataset_id: str) -> dict[str, object]:
    with store.connect() as connection:
        row = connection.execute(
            "SELECT run_id, dataset_id, dataset_hash, start_date, end_date, config_json, summary_json "
            "FROM backtest_runs WHERE dataset_id = ? AND status = 'complete' "
            "ORDER BY created_at DESC LIMIT 1",
            (dataset_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"RUN_NOT_FOUND:{dataset_id}")
    summary = json.loads(str(row["summary_json"]))
    config = json.loads(str(row["config_json"]))
    return {
        "run_id": str(row["run_id"]),
        "dataset_id": str(row["dataset_id"]),
        "dataset_hash": str(row["dataset_hash"]),
        "window": {"start": str(row["start_date"]), "end": str(row["end_date"])},
        "config": config,
        "metrics": dict(summary.get("metrics") or {}),
        "result_sha256": str(summary.get("result_sha256") or ""),
    }


def compare_sources(
    db_path: Path | str,
    dataset_ids: list[str],
    *,
    registry_path: Path | str = "cache/backtest/datasets.json",
) -> dict[str, object]:
    if len(dataset_ids) < 2:
        raise ValueError("AT_LEAST_TWO_DATASETS_REQUIRED")
    store = HistoricalStore(Path(db_path))
    runs = [_latest_run(store, dataset_id) for dataset_id in dataset_ids]
    baseline = runs[0]
    base_metrics = baseline["metrics"]
    comparisons = []
    for candidate in runs[1:]:
        metrics = candidate["metrics"]
        comparisons.append({
            "dataset_id": candidate["dataset_id"],
            "run_id": candidate["run_id"],
            "delta_vs_first": {
                key: float(metrics.get(key, 0.0)) - float(base_metrics.get(key, 0.0))
                for key in METRIC_KEYS
            },
        })
    registry = load_registry(registry_path)
    entries = registry.get("datasets") if isinstance(registry, Mapping) else {}
    return {
        "status": "complete",
        "comparison_type": "cross_source",
        "datasets": [
            {
                **run,
                "provider": (entries.get(run["dataset_id"], {}) if isinstance(entries, Mapping) else {}).get("provider", "unknown"),
                "proxy_only": (entries.get(run["dataset_id"], {}) if isinstance(entries, Mapping) else {}).get("proxy_only"),
            }
            for run in runs
        ],
        "comparisons": comparisons,
        "interpretation": (
            "Runs are comparable only as a source-sensitivity study. A higher return from one provider "
            "does not establish data correctness or production readiness."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare the latest complete run for multiple datasets")
    parser.add_argument("--db", required=True)
    parser.add_argument("--datasets", required=True, help="comma-separated dataset IDs in baseline-first order")
    parser.add_argument("--registry", default="cache/backtest/datasets.json")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    datasets = [value.strip() for value in args.datasets.split(",") if value.strip()]
    report = compare_sources(args.db, datasets, registry_path=args.registry)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
