"""Long-cycle research and simulated-production qualification report.

This module deliberately separates research evidence from production authority.
It can run on ``price_core`` proxy data, but a production-ready result requires
strict point-in-time data, a live fee contract, and observed execution evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from historical_backtest import (
    BacktestMetrics,
    HistoricalBacktestConfig,
    HistoricalBacktestResult,
    compute_metrics,
    run_historical_backtest,
)
from historical_data import STRICT_FEATURES, HistoricalStore, validate_dataset


REPORT_VERSION = "long-cycle-validation-v1"
TRADING_DAYS_PER_YEAR = 252


def _round(value: float, digits: int = 8) -> float:
    return round(float(value), digits)


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _drawdown_stats(values: Sequence[float]) -> tuple[float, int]:
    if not values:
        return 0.0, 0
    peak = values[0]
    max_drawdown = 0.0
    max_duration = 0
    duration = 0
    for value in values:
        if value >= peak:
            peak = value
            duration = 0
        elif peak:
            duration += 1
            max_duration = max(max_duration, duration)
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    return max_drawdown, max_duration


def series_metrics(values: Sequence[float]) -> dict[str, float | int]:
    """Return risk metrics for a daily equity series."""

    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return {
            "observations": 0,
            "net_return": 0.0,
            "annualized_return": 0.0,
            "volatility": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_days": 0,
        }
    returns = [clean[index] / clean[index - 1] - 1 for index in range(1, len(clean)) if clean[index - 1]]
    net_return = clean[-1] / clean[0] - 1 if clean[0] else 0.0
    years = max(len(returns) / TRADING_DAYS_PER_YEAR, 1 / TRADING_DAYS_PER_YEAR)
    annualized = (1 + net_return) ** (1 / years) - 1 if 1 + net_return > 0 else -1.0
    volatility = statistics.pstdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR) if len(returns) > 1 else 0.0
    downside = [min(value, 0.0) for value in returns]
    downside_deviation = math.sqrt(sum(value * value for value in downside) / len(downside)) * math.sqrt(TRADING_DAYS_PER_YEAR) if downside else 0.0
    sharpe = _safe_ratio(statistics.mean(returns) * TRADING_DAYS_PER_YEAR, volatility)
    sortino = _safe_ratio(statistics.mean(returns) * TRADING_DAYS_PER_YEAR, downside_deviation)
    drawdown, duration = _drawdown_stats(clean)
    return {
        "observations": len(clean),
        "net_return": _round(net_return),
        "annualized_return": _round(annualized),
        "volatility": _round(volatility),
        "sharpe": _round(sharpe),
        "sortino": _round(sortino),
        "max_drawdown": _round(drawdown),
        "max_drawdown_days": duration,
    }


def _load_rows(store: HistoricalStore, dataset_id: str, start: str, end: str) -> list[dict[str, object]]:
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT b.trade_date, b.code, b.close, b.amount, s.suspended, "
            "s.st, s.limit_up, s.limit_down "
            "FROM daily_bars b "
            "JOIN daily_universe u ON u.dataset_id=b.dataset_id AND u.trade_date=b.trade_date AND u.code=b.code "
            "JOIN daily_status s ON s.dataset_id=b.dataset_id AND s.trade_date=b.trade_date AND s.code=b.code "
            "WHERE b.dataset_id=? AND b.trade_date BETWEEN ? AND ? "
            "ORDER BY b.trade_date, b.code",
            (dataset_id, start, end),
        ).fetchall()
    return [dict(row) for row in rows]


def _equal_weight_benchmark(rows: Sequence[Mapping[str, object]], initial_cash: float) -> dict[str, object]:
    """Construct an equal-weight buy-and-hold proxy from the imported universe.

    This is intentionally labelled a proxy: it is not a CSI index and does not
    include index corporate actions or constituent changes that are absent from
    the source dataset.
    """

    by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        by_date[str(row["trade_date"])].append(row)
    dates = sorted(by_date)
    if not dates:
        return {"name": "equal_weight_buy_hold_proxy", "dates": [], "equity": [], "metrics": series_metrics([])}
    first_rows = [row for row in by_date[dates[0]] if float(row["close"]) > 0]
    codes = {str(row["code"]) for row in first_rows}
    allocation = initial_cash / len(codes) if codes else 0.0
    quantities = {
        str(row["code"]): allocation / float(row["close"])
        for row in first_rows
        if float(row["close"]) > 0
    }
    last_close: dict[str, float] = {}
    equity: list[float] = []
    for date in dates:
        for row in by_date[date]:
            code = str(row["code"])
            if code in quantities and float(row["close"]) > 0:
                last_close[code] = float(row["close"])
        equity.append(sum(quantities[code] * last_close.get(code, 0.0) for code in quantities))
    return {
        "name": "equal_weight_buy_hold_proxy",
        "dates": dates,
        "equity": [round(value, 2) for value in equity],
        "metrics": series_metrics(equity),
        "code_count": len(codes),
        "proxy_only": True,
    }


def _strategy_equity(result: HistoricalBacktestResult) -> list[float]:
    return [float(point.equity) for point in result.equity]


def _trade_attribution(result: HistoricalBacktestResult) -> dict[str, dict[str, dict[str, float | int]]]:
    groups: dict[str, dict[str, dict[str, float | int]]] = {
        "code": {},
        "reason": {},
        "strategy_mode": {},
        "market_regime": {},
        "industry": {},
    }
    for trade in result.trades:
        if trade.action != "sell" or trade.pnl is None:
            continue
        for field, value in (
            ("code", trade.code),
            ("reason", trade.reason),
            ("strategy_mode", trade.strategy_mode),
            ("market_regime", trade.market_regime),
            ("industry", trade.industry),
        ):
            bucket = groups[field].setdefault(
                str(value), {"closed_trades": 0, "wins": 0, "net_pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0}
            )
            pnl = float(trade.pnl)
            bucket["closed_trades"] += 1
            bucket["wins"] += int(pnl > 0)
            bucket["net_pnl"] = round(float(bucket["net_pnl"]) + pnl, 2)
            if pnl > 0:
                bucket["gross_win"] = round(float(bucket["gross_win"]) + pnl, 2)
            else:
                bucket["gross_loss"] = round(float(bucket["gross_loss"]) - pnl, 2)
    for field, values in groups.items():
        for bucket in values.values():
            bucket["win_rate"] = _round(_safe_ratio(float(bucket["wins"]), float(bucket["closed_trades"])))
            bucket["profit_factor"] = _round(_safe_ratio(float(bucket["gross_win"]), float(bucket["gross_loss"])))
    return groups


def _capacity_report(result: HistoricalBacktestResult, rows: Sequence[Mapping[str, object]], initial_cash: float) -> dict[str, object]:
    amounts = {(str(row["trade_date"]), str(row["code"])): float(row["amount"] or 0.0) for row in rows}
    participation: list[float] = []
    for trade in result.trades:
        if trade.action != "buy":
            continue
        amount = amounts.get((trade.trade_date, trade.code), 0.0)
        if amount > 0:
            participation.append(trade.price * trade.quantity / amount)
    participation.sort()
    p95 = participation[min(len(participation) - 1, int(len(participation) * 0.95))] if participation else 0.0
    scenarios = {}
    for capital in (initial_cash, initial_cash * 5, initial_cash * 10):
        multiplier = capital / initial_cash if initial_cash else 0.0
        scenarios[str(int(capital))] = {
            "p95_participation": _round(p95 * multiplier),
            "max_participation": _round((max(participation) if participation else 0.0) * multiplier),
        }
    return {
        "initial_cash": initial_cash,
        "buy_observations": len(participation),
        "median_participation": _round(statistics.median(participation)) if participation else 0.0,
        "p95_participation": _round(p95),
        "max_participation": _round(max(participation)) if participation else 0.0,
        "capital_scenarios": scenarios,
        "unit": "fraction_of_daily_amount",
        "proxy_only": True,
        "interpretation": "Uses daily amount and ignores queue position, hidden liquidity and market impact.",
    }


def _period_returns(result: HistoricalBacktestResult) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for point in result.equity:
        grouped[str(point.trade_date)[:7]].append(float(point.equity))
    output: dict[str, dict[str, float | int]] = {}
    for period, values in sorted(grouped.items()):
        output[period] = {
            "net_return": _round(values[-1] / values[0] - 1 if values and values[0] else 0.0),
            "start_equity": round(values[0], 2) if values else 0.0,
            "end_equity": round(values[-1], 2) if values else 0.0,
            "observations": len(values),
        }
    return output


def _annual_returns(result: HistoricalBacktestResult) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for point in result.equity:
        grouped[str(point.trade_date)[:4]].append(float(point.equity))
    output: dict[str, dict[str, float | int]] = {}
    for period, values in sorted(grouped.items()):
        output[period] = {
            "net_return": _round(values[-1] / values[0] - 1 if values and values[0] else 0.0),
            "start_equity": round(values[0], 2) if values else 0.0,
            "end_equity": round(values[-1], 2) if values else 0.0,
            "observations": len(values),
        }
    return output


def _market_stage_attribution(result: HistoricalBacktestResult, benchmark: Mapping[str, object]) -> dict[str, dict[str, float | int]]:
    dates = list(benchmark.get("dates", []))
    values = [float(value) for value in benchmark.get("equity", [])]
    stage_by_date: dict[str, str] = {}
    for index, date in enumerate(dates):
        lookback = values[max(0, index - 19): index + 1]
        long_window = values[max(0, index - 59): index + 1]
        short_return = values[index] / lookback[0] - 1 if lookback and lookback[0] else 0.0
        long_return = values[index] / long_window[0] - 1 if long_window and long_window[0] else 0.0
        if len(long_window) >= 20 and long_return > 0.0 and short_return >= 0.0:
            stage = "risk_on"
        elif len(long_window) >= 20 and long_return < 0.0 and short_return < 0.0:
            stage = "risk_off"
        else:
            stage = "transition"
        stage_by_date[str(date)] = stage
    result_by_stage: dict[str, dict[str, float | int]] = {}
    for trade in result.trades:
        if trade.action != "sell" or trade.pnl is None:
            continue
        stage = stage_by_date.get(trade.trade_date, "unknown")
        bucket = result_by_stage.setdefault(stage, {"closed_trades": 0, "wins": 0, "net_pnl": 0.0})
        pnl = float(trade.pnl)
        bucket["closed_trades"] += 1
        bucket["wins"] += int(pnl > 0)
        bucket["net_pnl"] = round(float(bucket["net_pnl"]) + pnl, 2)
    for bucket in result_by_stage.values():
        bucket["win_rate"] = _round(_safe_ratio(float(bucket["wins"]), float(bucket["closed_trades"])))
    return result_by_stage


def _bootstrap_robustness(result: HistoricalBacktestResult, *, seed: int = 7, iterations: int = 1000) -> dict[str, float | int]:
    pnls = [float(trade.pnl) for trade in result.trades if trade.action == "sell" and trade.pnl is not None]
    if not pnls:
        return {"closed_trades": 0, "iterations": 0, "positive_total_probability": 0.0}
    rng = random.Random(seed)
    totals = [sum(rng.choice(pnls) for _ in pnls) for _ in range(iterations)]
    totals.sort()
    return {
        "closed_trades": len(pnls),
        "iterations": iterations,
        "seed": seed,
        "positive_total_probability": _round(sum(value > 0 for value in totals) / len(totals)),
        "p05_total_pnl": round(totals[max(0, int(len(totals) * 0.05))], 2),
        "median_total_pnl": round(statistics.median(totals), 2),
        "p95_total_pnl": round(totals[min(len(totals) - 1, int(len(totals) * 0.95))], 2),
        "interpretation": "Bootstrap resamples observed closed trades; it does not create new market paths or remove data bias.",
    }


def _stress_run(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    base_config: HistoricalBacktestConfig,
    label: str,
) -> dict[str, object]:
    fees = base_config.resolved_fee_schedule()
    if label == "zero_slippage":
        schedule = fees.derive_variant("long-zero-slippage", buy_slippage_rate=Decimal("0"), sell_slippage_rate=Decimal("0"))
    elif label == "double_slippage":
        schedule = fees.derive_variant("long-double-slippage", buy_slippage_rate=fees.buy_slippage_rate * 2, sell_slippage_rate=fees.sell_slippage_rate * 2)
    elif label == "double_fees":
        schedule = fees.derive_variant(
            "long-double-fees",
            buy_commission_rate=fees.buy_commission_rate * 2,
            sell_commission_rate=fees.sell_commission_rate * 2,
            buy_minimum_commission_yuan=fees.buy_minimum_commission_yuan * 2,
            sell_minimum_commission_yuan=fees.sell_minimum_commission_yuan * 2,
            stamp_tax_rate=fees.stamp_tax_rate * 2,
            transfer_fee_rate=fees.transfer_fee_rate * 2,
        )
    else:
        raise ValueError(f"unknown stress label: {label}")
    result = run_historical_backtest(store, dataset_id, start, end, replace(base_config, fee_schedule=schedule))
    metrics = compute_metrics(result.equity, result.trades)
    return {"label": label, "metrics": asdict(metrics), "fee_components": result.metadata.get("fee_components", {})}


def _read_walk_forward(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists():
        return {"available": False, "parameter_stability": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    selected = [fold.get("selected_parameters", {}) for fold in payload.get("folds", [])]
    return {
        "available": True,
        "status": payload.get("status", "unknown"),
        "evidence_ready": bool(payload.get("evidence_ready")),
        "selected_parameters": selected,
        "parameter_stability": {
            "min_score_values": sorted({item.get("min_score") for item in selected}),
            "max_positions_values": sorted({item.get("max_positions") for item in selected}),
            "stable_selection": len({json.dumps(item, sort_keys=True) for item in selected}) <= 1,
        },
        "validation_aggregate": payload.get("validation_aggregate", {}),
        "holdout": payload.get("holdout", {}),
    }


def _production_gates(
    *,
    quality: Mapping[str, object],
    result_metrics: Mapping[str, object],
    benchmark_metrics: Mapping[str, object],
    stress: Sequence[Mapping[str, object]],
    walk_forward: Mapping[str, object],
) -> dict[str, object]:
    stress_by_label = {str(item.get("label")): item for item in stress}
    gates = {
        "strict_point_in_time_data": bool(quality.get("accepted")) and not bool(quality.get("proxy_only")),
        "positive_long_cycle_return": float(result_metrics.get("net_return", 0.0)) > 0,
        "positive_holdout": float(walk_forward.get("holdout", {}).get("metrics", {}).get("net_return", 0.0)) > 0,
        "outperforms_equal_weight_proxy": float(result_metrics.get("net_return", 0.0)) > float(benchmark_metrics.get("net_return", 0.0)),
        "double_costs_remain_positive": all(
            label in stress_by_label
            and float(stress_by_label[label].get("metrics", {}).get("net_return", 0.0)) > 0
            for label in ("double_fees", "double_slippage")
        ),
        "live_fee_contract": False,
        "broker_execution_observed": False,
        "fault_recovery_observed": False,
    }
    return {
        "gates": gates,
        "production_ready": all(gates.values()),
        "reason": "Production readiness requires every gate; proxy backtests cannot grant broker execution authority.",
    }


def build_long_cycle_report(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    config: HistoricalBacktestConfig,
    *,
    walk_forward_json: Path | None = None,
    run_stress: bool = True,
) -> dict[str, object]:
    quality = validate_dataset(store, dataset_id, start, end, config.mode, STRICT_FEATURES)
    result = run_historical_backtest(store, dataset_id, start, end, config)
    metrics = compute_metrics(result.equity, result.trades)
    strategy_metrics = asdict(metrics)
    strategy_metrics.update({
        key: value
        for key, value in series_metrics(_strategy_equity(result)).items()
        if key not in {"observations", "net_return", "annualized_return", "max_drawdown"}
    })
    rows = _load_rows(store, dataset_id, start, end)
    benchmark = _equal_weight_benchmark(rows, config.initial_cash)
    stress = [_stress_run(store, dataset_id, start, end, config, label) for label in ("zero_slippage", "double_slippage", "double_fees")] if run_stress else []
    walk_forward = _read_walk_forward(walk_forward_json)
    production = _production_gates(
        quality=asdict(quality),
        result_metrics=strategy_metrics,
        benchmark_metrics=benchmark["metrics"],
        stress=stress,
        walk_forward=walk_forward,
    )
    return {
        "report_version": REPORT_VERSION,
        "dataset_id": dataset_id,
        "dataset_hash": store.dataset_hash(dataset_id),
        "window": {"start": start, "end": end},
        "proxy_only": bool(quality.proxy_only),
        "quality": asdict(quality),
        "strategy": {"config": asdict(config), "metrics": strategy_metrics, "blocked_counts": result.blocked_counts, "fee_components": result.metadata.get("fee_components", {})},
        "benchmark": benchmark,
        "active_return_vs_equal_weight": _round(float(metrics.net_return) - float(benchmark["metrics"]["net_return"])),
        "attribution": _trade_attribution(result),
        "monthly_returns": _period_returns(result),
        "annual_returns": _annual_returns(result),
        "market_stage_attribution": _market_stage_attribution(result, benchmark),
        "robustness": _bootstrap_robustness(result),
        "capacity": _capacity_report(result, rows, config.initial_cash),
        "stress": stress,
        "walk_forward": walk_forward,
        "production_qualification": production,
    }


def _markdown(report: Mapping[str, object]) -> str:
    strategy = report["strategy"]
    metrics = strategy["metrics"]
    benchmark = report["benchmark"]["metrics"]
    qualification = report["production_qualification"]
    lines = [
        "# Long Cycle Validation",
        "",
        f"- report_version: `{report['report_version']}`",
        f"- dataset: `{report['dataset_id']}`",
        f"- window: `{report['window']['start']}..{report['window']['end']}`",
        f"- proxy_only: `{report['proxy_only']}`",
        "",
        "## Strategy and benchmark",
        "",
        f"- strategy net return: `{float(metrics['net_return']):.2%}`",
        f"- strategy max drawdown: `{float(metrics['max_drawdown']):.2%}`",
        f"- strategy Sharpe / Sortino: `{float(metrics['sharpe']):.2f}` / `{float(metrics['sortino']):.2f}`",
        f"- equal-weight buy-and-hold proxy: `{float(benchmark['net_return']):.2%}`",
        f"- active return: `{float(report['active_return_vs_equal_weight']):.2%}`",
        "",
        "## Stress and robustness",
        "",
        f"- bootstrap positive-total probability: `{float(report['robustness']['positive_total_probability']):.2%}`",
        f"- p05 / median / p95 bootstrap PnL: `{report['robustness']['p05_total_pnl']}` / `{report['robustness']['median_total_pnl']}` / `{report['robustness']['p95_total_pnl']}` yuan",
        f"- cost stress variants: `{', '.join(str(item['label']) + '=' + format(float(item['metrics']['net_return']), '.2%') for item in report['stress']) or 'not-run'}`",
        "",
        "## Production qualification",
        "",
        f"- production_ready: `{qualification['production_ready']}`",
    ]
    for name, value in qualification["gates"].items():
        lines.append(f"- {name}: `{value}`")
    lines.extend([
        "",
        "A proxy backtest can validate research mechanics and expose weaknesses; it cannot authorize broker execution.",
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run long-cycle research and simulated-production qualification")
    parser.add_argument("--db", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--walk-forward-json")
    parser.add_argument("--mode", choices=("strict", "price_core"), default="price_core")
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--max-positions", type=int, default=8)
    parser.add_argument("--min-score", type=float, default=75.0)
    parser.add_argument("--caution-min-score", type=float, default=85.0)
    parser.add_argument("--cooldown-days", type=int, default=3)
    parser.add_argument("--max-new-positions-per-day", type=int, default=3)
    parser.add_argument("--no-stress", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = HistoricalStore(Path(args.db))
    config = HistoricalBacktestConfig(
        initial_cash=args.capital,
        max_positions=args.max_positions,
        min_score=args.min_score,
        caution_min_score=args.caution_min_score,
        cooldown_days=args.cooldown_days,
        max_new_positions_per_day=args.max_new_positions_per_day,
        mode=args.mode,
        parameter_version="long-cycle-validation-v1",
    )
    report = build_long_cycle_report(
        store,
        args.dataset,
        args.start,
        args.end,
        config,
        walk_forward_json=Path(args.walk_forward_json) if args.walk_forward_json else None,
        run_stress=not args.no_stress,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "long_cycle_validation_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    (output_dir / "long_cycle_validation_latest.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["production_qualification"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
