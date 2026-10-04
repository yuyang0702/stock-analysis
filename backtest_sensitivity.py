"""Fee and slippage sensitivity runs for the local decision layer."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from historical_backtest import (
    HistoricalBacktestConfig,
    compute_metrics,
    run_historical_backtest,
)
from historical_data import HistoricalStore
from benchmark_data import load_benchmark_csv


def _numbers(values: Iterable[float]) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if not result or any(value <= 0 for value in result):
        raise ValueError("sensitivity multipliers must be positive")
    return result


def run_cost_sensitivity(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    config: HistoricalBacktestConfig,
    *,
    commission_multipliers: Sequence[float] = (0.8, 1.0, 1.2),
    slippage_multipliers: Sequence[float] = (0.5, 1.0, 2.0),
) -> dict[str, object]:
    """Run the same decision policy across conservative cost scenarios."""
    base = config.resolved_fee_schedule()
    commissions = _numbers(commission_multipliers)
    slippages = _numbers(slippage_multipliers)
    scenarios: list[dict[str, object]] = []
    for commission_multiplier in commissions:
        for slippage_multiplier in slippages:
            schedule = base.derive_variant(
                f"sensitivity-c{commission_multiplier:g}-s{slippage_multiplier:g}",
                buy_commission_rate=base.buy_commission_rate * Decimal(str(commission_multiplier)),
                sell_commission_rate=base.sell_commission_rate * Decimal(str(commission_multiplier)),
                buy_minimum_commission_yuan=base.buy_minimum_commission_yuan * Decimal(str(commission_multiplier)),
                sell_minimum_commission_yuan=base.sell_minimum_commission_yuan * Decimal(str(commission_multiplier)),
                buy_slippage_rate=base.buy_slippage_rate * Decimal(str(slippage_multiplier)),
                sell_slippage_rate=base.sell_slippage_rate * Decimal(str(slippage_multiplier)),
            )
            scenario_config = replace(config, fee_schedule=schedule, entry_fee_schedule=schedule)
            result = run_historical_backtest(store, dataset_id, start, end, scenario_config)
            metrics = asdict(compute_metrics(result.equity, result.trades))
            scenarios.append({
                "commission_multiplier": commission_multiplier,
                "slippage_multiplier": slippage_multiplier,
                "fee_schedule": schedule.to_dict(),
                "metrics": metrics,
                "trade_count": len(result.trades),
                "blocked_counts": dict(sorted(result.blocked_counts.items())),
            })
    return {
        "report_version": "cost-sensitivity-v1",
        "dataset_id": dataset_id,
        "window": {"start": start, "end": end},
        "base_config": asdict(config),
        "scenarios": scenarios,
        "interpretation": "Sensitivity is research evidence; it does not authorize production parameters.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run fee/slippage sensitivity for a historical backtest")
    parser.add_argument("--db", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--commission-multipliers", default="0.8,1,1.2")
    parser.add_argument("--slippage-multipliers", default="0.5,1,2")
    parser.add_argument("--alpha-profile", choices=("legacy", "relative_v1", "relative_v2"), default="relative_v1")
    parser.add_argument("--benchmark-csv", default="")
    parser.add_argument("--benchmark", default="")
    parser.add_argument("--slippage-model", choices=("fixed", "liquidity_v1"), default="liquidity_v1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    benchmark_closes = None
    if args.alpha_profile == "relative_v2" and not args.benchmark_csv:
        raise ValueError("BENCHMARK_CSV_REQUIRED_FOR_RELATIVE_V2")
    if args.benchmark_csv:
        values = load_benchmark_csv(args.benchmark_csv)
        name = args.benchmark or (sorted(values)[0] if values else "")
        if name not in values:
            raise ValueError("BENCHMARK_NOT_FOUND")
        benchmark_closes = values[name]
    config = HistoricalBacktestConfig(
        mode="price_core",
        alpha_profile=args.alpha_profile,
        benchmark_closes=benchmark_closes,
        slippage_model=args.slippage_model,
        max_positions=4,
        min_score=70,
        caution_min_score=75,
        cooldown_days=5,
        max_new_positions_per_day=2,
        require_trend_confirmation=True,
        max_chase_atr=2.5,
        signal_confirmation_days=2,
        max_portfolio_risk_pct=4,
        max_same_industry_positions=2,
        max_pairwise_correlation=0.9,
        local_entry_gates_enabled=True,
        max_participation_pct=2,
        min_holding_days=2,
    )
    report = run_cost_sensitivity(
        HistoricalStore(Path(args.db)),
        args.dataset,
        args.start,
        args.end,
        config,
        commission_multipliers=tuple(float(value) for value in args.commission_multipliers.split(",")),
        slippage_multipliers=tuple(float(value) for value in args.slippage_multipliers.split(",")),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({"scenarios": len(report["scenarios"]), "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
