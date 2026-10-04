"""Point-in-time signal quality research for the local strategy.

The module is deliberately separate from the execution engine.  It evaluates
what happened after a candidate was generated, using the next available open
for entry and a later close for exit, then subtracts the configured A-share
round-trip costs.  It is research evidence only and never approves parameters.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, asdict
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import config as app_config
from benchmark_data import load_benchmark_bars
from decision_profiles import ALPHA_PROFILES
from execution_contracts import FeeSchedule
from historical_data import HistoricalDataValidationError, HistoricalStore
from historical_strategy import Candidate, generate_daily_candidates
from signal_calibration import calibrate_score_buckets


@dataclass(frozen=True)
class SignalObservation:
    trade_date: str
    code: str
    score: float
    mode: str
    market_regime: str
    trend: bool
    breakout: bool
    amount_rank: float
    forward_returns: dict[int, float]
    excess_returns: dict[int, float]
    factor_values: dict[str, float]


def _metric(values: Sequence[float]) -> dict[str, float | int]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return {
            "count": 0,
            "mean": 0.0,
            "median": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
        }
    gains = sum(value for value in clean if value > 0)
    losses = -sum(value for value in clean if value < 0)
    return {
        "count": len(clean),
        "mean": round(sum(clean) / len(clean), 8),
        "median": round(statistics.median(clean), 8),
        "win_rate": round(sum(value > 0 for value in clean) / len(clean), 8),
        "profit_factor": round(gains / losses, 8) if losses else (999.0 if gains else 0.0),
    }


def _bucket(score: float) -> str:
    lower = int(math.floor(float(score) / 5.0) * 5)
    return f"{lower}-{lower + 5}"


def _group_metrics(
    observations: Iterable[SignalObservation],
    key,
    horizon: int,
) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        value = observation.forward_returns.get(horizon)
        if value is not None:
            groups[str(key(observation))].append(float(value))
    return {name: _metric(values) for name, values in sorted(groups.items())}


def _rank(values: Sequence[float]) -> list[float]:
    """Average-rank values without adding a scipy dependency."""
    indexed = sorted(enumerate(float(value) for value in values), key=lambda item: item[1])
    ranks = [0.0] * len(indexed)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average = (cursor + 1 + end) / 2.0
        for index, _ in indexed[cursor:end]:
            ranks[index] = average
        cursor = end
    return ranks


def _spearman(values: Sequence[tuple[float, float]]) -> float | None:
    clean = [(float(left), float(right)) for left, right in values if math.isfinite(float(left)) and math.isfinite(float(right))]
    if len(clean) < 3:
        return None
    left = _rank([item[0] for item in clean])
    right = _rank([item[1] for item in clean])
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_var = sum((a - left_mean) ** 2 for a in left)
    right_var = sum((b - right_mean) ** 2 for b in right)
    if left_var <= 0 or right_var <= 0:
        return None
    return numerator / math.sqrt(left_var * right_var)


def _factor_ic(
    observations: Iterable[SignalObservation],
    factor: str,
    horizon: int,
    *,
    excess: bool = False,
) -> dict[str, float | int | None]:
    pairs: list[tuple[float, float]] = []
    for observation in observations:
        forward = observation.excess_returns if excess else observation.forward_returns
        if factor in observation.factor_values and horizon in forward:
            pairs.append((observation.factor_values[factor], forward[horizon]))
    return {"count": len(pairs), "ic": _spearman(pairs)}


def _factor_groups(
    observations: Iterable[SignalObservation],
    factor: str,
    horizon: int,
    *,
    groups: int = 5,
    excess: bool = False,
) -> dict[str, dict[str, float | int]]:
    rows = [
        observation for observation in observations
        if factor in observation.factor_values
        and horizon in (observation.excess_returns if excess else observation.forward_returns)
    ]
    if not rows:
        return {}
    values = [observation.factor_values[factor] for observation in rows]
    ranks = _rank(values)
    grouped: dict[str, list[float]] = defaultdict(list)
    for observation, rank in zip(rows, ranks):
        bucket = min(groups, max(1, int(math.ceil(rank / len(rows) * groups))))
        forward = observation.excess_returns if excess else observation.forward_returns
        grouped[f"Q{bucket}"] .append(forward[horizon])
    return {name: _metric(values) for name, values in sorted(grouped.items())}


def _next_open(rows: Mapping[str, Mapping[str, object]], code: str) -> float:
    row = rows.get(code)
    if not row or bool(row.get("suspended")):
        return 0.0
    open_price = float(row.get("open") or 0.0)
    limit_up = float(row.get("limit_up") or 0.0)
    if open_price <= 0 or (limit_up > 0 and open_price >= limit_up):
        return 0.0
    return open_price


def _net_forward_return(
    fee_schedule: FeeSchedule,
    entry: float,
    exit_price: float,
    notional: float,
) -> float | None:
    if entry <= 0 or exit_price <= 0:
        return None
    quantity = int(float(notional) / entry / 100) * 100
    if quantity <= 0:
        return None
    buy = fee_schedule.estimate("buy", Decimal(str(entry)), quantity)
    sell = fee_schedule.estimate("sell", Decimal(str(exit_price)), quantity)
    gross = (exit_price - entry) * quantity
    net = gross - float(buy.total_yuan) - float(sell.total_yuan)
    invested = entry * quantity + float(buy.total_yuan)
    return net / invested if invested else None


def research_signals(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    *,
    mode: str = "price_core",
    parameter_version: str = "signal-research-v1",
    min_score: float = 75.0,
    horizons: Sequence[int] = (1, 3, 5, 10),
    notional_per_signal: float = 100_000.0,
    fee_schedule: FeeSchedule | None = None,
    alpha_profile: str = "legacy",
    benchmark_csv: Path | str | None = None,
    benchmark: str | None = None,
    min_expected_net_return_bps: float = 0.0,
) -> dict[str, object]:
    if not horizons or any(int(value) <= 0 for value in horizons):
        raise ValueError("horizons must contain positive integers")
    if notional_per_signal > 0 and math.isfinite(notional_per_signal):
        pass
    else:
        raise ValueError("notional_per_signal must be finite and positive")
    dates = store.trade_dates(dataset_id, start, end)
    if not dates:
        raise HistoricalDataValidationError("NO_TRADE_DATES")
    daily_rows = {
        date: {str(row["code"]): row for row in store.daily_slice(dataset_id, date)}
        for date in dates
    }
    schedule = fee_schedule or app_config.SIMULATION_FEE_SCHEDULE
    benchmark_values = load_benchmark_bars(benchmark_csv) if benchmark_csv else {}
    benchmark_name = str(benchmark or "").strip()
    if benchmark_name and benchmark_name not in benchmark_values:
        raise ValueError(f"benchmark not found: {benchmark_name}")
    if benchmark_values and not benchmark_name:
        benchmark_name = sorted(benchmark_values)[0]
    benchmark_closes = (
        {day: float(row["close"]) for day, row in benchmark_values[benchmark_name].items()}
        if benchmark_name else None
    )
    observations: list[SignalObservation] = []
    for index, trade_date in enumerate(dates):
        candidates = generate_daily_candidates(
            store,
            dataset_id,
            trade_date,
            mode=mode,
            parameter_version=parameter_version,
            min_score=min_score,
            alpha_profile=alpha_profile,
            benchmark_closes=benchmark_closes,
        )
        if not candidates:
            continue
        entry_date_index = index + 1
        if entry_date_index >= len(dates):
            continue
        entry_date = dates[entry_date_index]
        entry_rows = daily_rows[entry_date]
        for candidate in candidates:
            entry = _next_open(entry_rows, candidate.code)
            if entry <= 0:
                continue
            forward: dict[int, float] = {}
            excess: dict[int, float] = {}
            for horizon in sorted({int(value) for value in horizons}):
                # A horizon is a number of completed sessions after the
                # executable next-open entry.  Horizon 1 therefore exits on
                # the following session, never at the same day's close.
                exit_index = entry_date_index + horizon
                if exit_index >= len(dates):
                    continue
                exit_row = daily_rows[dates[exit_index]].get(candidate.code)
                exit_price = float(exit_row.get("close") or 0.0) if exit_row else 0.0
                value = _net_forward_return(schedule, entry, exit_price, notional_per_signal)
                if value is not None:
                    forward[horizon] = value
                    if benchmark_name:
                        benchmark_entry = benchmark_values[benchmark_name].get(entry_date, {}).get("open")
                        benchmark_exit = benchmark_values[benchmark_name].get(dates[exit_index], {}).get("close")
                        if benchmark_entry and benchmark_exit and benchmark_entry > 0:
                            excess[horizon] = value - (benchmark_exit / benchmark_entry - 1.0)
            if not forward:
                continue
            factor_values = {
                "score": float(candidate.score),
                "pct_rank": float(candidate.evidence.get("pct_rank") or 0.0),
                "relative_rank": float(candidate.evidence.get("relative_rank") or 0.0),
                "amount_rank": float(candidate.evidence.get("amount_rank") or 0.0),
                "liquidity_ratio": float(candidate.evidence.get("liquidity_ratio") or 0.0),
                "volatility_20": float(candidate.evidence.get("volatility_20") or 0.0),
                "return_5": float(candidate.evidence.get("return_5") or 0.0),
                "excess_strength_20": float(candidate.evidence.get("excess_strength_20") or 0.0),
            }
            observations.append(
                SignalObservation(
                    trade_date=trade_date,
                    code=candidate.code,
                    score=float(candidate.score),
                    mode=candidate.mode,
                    market_regime=candidate.market_regime,
                    trend=bool(candidate.evidence.get("trend")),
                    breakout=bool(candidate.evidence.get("breakout")),
                    amount_rank=float(candidate.evidence.get("amount_rank") or 0.0),
                    forward_returns=forward,
                    excess_returns=excess,
                    factor_values=factor_values,
                )
            )
    horizon_metrics = {
        str(horizon): _metric(
            [observation.forward_returns[horizon] for observation in observations if horizon in observation.forward_returns]
        )
        for horizon in sorted({int(value) for value in horizons})
    }
    by_horizon: dict[str, object] = {}
    factor_names = (
        "score", "pct_rank", "relative_rank", "amount_rank", "liquidity_ratio",
        "volatility_20", "return_5", "excess_strength_20",
    )
    for horizon in sorted({int(value) for value in horizons}):
        by_horizon[str(horizon)] = {
            "score_bucket": _group_metrics(observations, lambda item: _bucket(item.score), horizon),
            "market_regime": _group_metrics(observations, lambda item: item.market_regime, horizon),
            "mode": _group_metrics(observations, lambda item: item.mode, horizon),
            "trend": _group_metrics(observations, lambda item: item.trend, horizon),
            "breakout": _group_metrics(observations, lambda item: item.breakout, horizon),
            "factor_groups": {
                factor: _factor_groups(observations, factor, horizon)
                for factor in factor_names
            },
            "factor_ic": {
                factor: _factor_ic(observations, factor, horizon)
                for factor in factor_names
            },
            "excess_return": _metric(
                [observation.excess_returns[horizon] for observation in observations if horizon in observation.excess_returns]
            ),
            "excess_factor_ic": {
                factor: _factor_ic(observations, factor, horizon, excess=True)
                for factor in factor_names
            },
        }
    gate_count = sum(
        1 for observation in observations
        for value in observation.forward_returns.values()
        if value * 10000 >= float(min_expected_net_return_bps)
    )
    possible_gate_count = sum(len(observation.forward_returns) for observation in observations)
    calibration = {
        str(horizon): calibrate_score_buckets(
            {"horizon_metrics": horizon_metrics, "by_horizon": by_horizon},
            horizon=int(horizon),
        )
        for horizon in sorted({int(value) for value in horizons})
        if str(horizon) in horizon_metrics
    }
    return {
        "report_version": "signal-research-v2",
        "dataset_id": dataset_id,
        "window": {"start": start, "end": end},
        "mode": mode,
        "parameter_version": parameter_version,
        "alpha_profile": alpha_profile,
        "benchmark": benchmark_name or None,
        "benchmark_csv": str(benchmark_csv) if benchmark_csv else None,
        "fee_schedule": schedule.to_dict(),
        "min_expected_net_return_bps": float(min_expected_net_return_bps),
        "realized_cost_gate_diagnostic": {
            "passed": gate_count,
            "total": possible_gate_count,
            "pass_rate": gate_count / possible_gate_count if possible_gate_count else 0.0,
            "note": "Diagnostic on realized forward returns; decision-time gates must use a training-window calibration.",
        },
        "notional_per_signal": notional_per_signal,
        "horizons": sorted({int(value) for value in horizons}),
        "observation_count": len(observations),
        "horizon_metrics": horizon_metrics,
        "by_horizon": by_horizon,
        "score_calibration": calibration,
        "interpretation": (
            "Forward returns use the next available open for entry and later close for exit; "
            "they subtract the configured simulation fee schedule and do not authorize deployment."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research point-in-time signal quality")
    parser.add_argument("--db", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--mode", choices=("strict", "price_core"), default="price_core")
    parser.add_argument("--parameter-version", default="signal-research-v1")
    parser.add_argument("--min-score", type=float, default=75.0)
    parser.add_argument("--horizons", default="1,3,5,10")
    parser.add_argument("--notional-per-signal", type=float, default=100_000.0)
    parser.add_argument("--alpha-profile", choices=ALPHA_PROFILES, default="legacy")
    parser.add_argument("--benchmark-csv")
    parser.add_argument("--benchmark", default="")
    parser.add_argument("--min-expected-net-return-bps", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = research_signals(
        HistoricalStore(Path(args.db)),
        args.dataset,
        args.start,
        args.end,
        mode=args.mode,
        parameter_version=args.parameter_version,
        min_score=args.min_score,
        horizons=tuple(int(value.strip()) for value in args.horizons.split(",") if value.strip()),
        notional_per_signal=args.notional_per_signal,
        alpha_profile=args.alpha_profile,
        benchmark_csv=args.benchmark_csv,
        benchmark=args.benchmark or None,
        min_expected_net_return_bps=args.min_expected_net_return_bps,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "signal_research_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    print(json.dumps(report["horizon_metrics"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
