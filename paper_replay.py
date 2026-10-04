"""Replay the local strategy through the bounded JSON paper account.

This is a local shadow validation path.  It deliberately uses the same
candidate generator and A-share fee contract as the historical engine, while
keeping the account state separate from the formal trading ledger and any
broker adapter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from historical_backtest import HistoricalBacktestConfig
from benchmark_data import load_benchmark_csv
from decision_profiles import ALPHA_PROFILES
from historical_data import HistoricalStore
from historical_strategy import generate_daily_candidates
from paper_trading import apply_paper_trades, new_account, summarize_account


def replay_paper_account(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    config: HistoricalBacktestConfig,
) -> dict[str, object]:
    dates = store.trade_dates(dataset_id, start, end)
    account = new_account(config.initial_cash)
    pending: dict[str, list[object]] = {}
    daily_events: list[dict[str, object]] = []
    for index, trade_date in enumerate(dates):
        rows = {str(row["code"]): row for row in store.daily_slice(dataset_id, trade_date)}
        payload: list[dict[str, object]] = []
        for code, position in account.get("positions", {}).items():
            row = rows.get(str(code))
            if not row:
                continue
            payload.append({
                "code": str(code),
                "price": float(row["close"]),
                "position_pct": 0.0,
                "stop_loss": position.get("stop_loss", 0.0),
                "take_profit": position.get("take_profit", 0.0),
                "final_score": 0.0,
                "mode": position.get("signal_type", "paper"),
                "pct_chg": 0.0,
            })
        for candidate in pending.pop(trade_date, []):
            row = rows.get(candidate.code)
            if not row or bool(row["suspended"]):
                continue
            payload.append({
                "code": candidate.code,
                "price": float(row["open"]),
                "entry_price": float(candidate.entry_price),
                "position_pct": float(candidate.position_pct),
                "stop_loss": float(candidate.stop_loss),
                "take_profit": float(candidate.take_profit),
                "final_score": float(candidate.score),
                "mode": candidate.mode,
                "pct_chg": 0.0,
            })
        frame = pd.DataFrame(payload)
        events = apply_paper_trades(
            account,
            frame,
            trade_date=trade_date,
            min_score=config.min_score,
            max_positions=config.max_positions,
            max_total_position_pct=80.0,
            fee_schedule=config.resolved_fee_schedule(),
        )
        daily_events.extend(events)
        if index + 1 < len(dates):
            candidates = generate_daily_candidates(
                store,
                dataset_id,
                trade_date,
                mode=config.mode,
                parameter_version=config.parameter_version,
                min_score=config.min_score,
                caution_min_score=config.caution_min_score,
                require_trend_confirmation=config.require_trend_confirmation,
                require_breakout_confirmation=config.require_breakout_confirmation,
                max_chase_atr=config.max_chase_atr,
                max_entry_score=config.max_entry_score,
                alpha_profile=config.alpha_profile,
                benchmark_closes=config.benchmark_closes,
            )
            next_date = dates[index + 1]
            pending[next_date] = candidates[: config.max_new_positions_per_day]
    summary = summarize_account(account)
    return {
        "status": "complete",
        "dataset_id": dataset_id,
        "window": {"start": start, "end": end},
        "strategy": {
            "parameter_version": config.parameter_version,
            "alpha_profile": config.alpha_profile,
            "fee_schedule_version": config.resolved_fee_schedule().version,
        },
        "summary": summary,
        "event_count": len(daily_events),
        "events": daily_events[-100:],
        "interpretation": (
            "Local paper account only. Candidate decisions use T close and enter at the next available open; "
            "this is not broker execution evidence and does not grant production authority."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay the strategy in the bounded local paper account")
    parser.add_argument("--db", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("strict", "price_core"), default="price_core")
    parser.add_argument("--parameter-version", default="paper-replay-v1")
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--max-positions", type=int, default=4)
    parser.add_argument("--min-score", type=float, default=75.0)
    parser.add_argument("--caution-min-score", type=float, default=85.0)
    parser.add_argument("--max-new-positions-per-day", type=int, default=2)
    parser.add_argument("--require-trend-confirmation", action="store_true")
    parser.add_argument("--max-chase-atr", type=float, default=2.5)
    parser.add_argument("--max-entry-score", type=float, default=94.999)
    parser.add_argument("--alpha-profile", choices=ALPHA_PROFILES, default="relative_v1")
    parser.add_argument("--benchmark-csv", default="")
    parser.add_argument("--benchmark", default="000300.XSHG")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = HistoricalStore(Path(args.db))
    store.initialize()
    benchmark_closes = None
    if args.benchmark_csv:
        benchmark_values = load_benchmark_csv(Path(args.benchmark_csv))
        if args.benchmark not in benchmark_values:
            raise ValueError("BENCHMARK_NOT_FOUND")
        benchmark_closes = benchmark_values[args.benchmark]
    config = HistoricalBacktestConfig(
        initial_cash=args.capital,
        mode=args.mode,
        parameter_version=args.parameter_version,
        max_positions=args.max_positions,
        min_score=args.min_score,
        caution_min_score=args.caution_min_score,
        max_new_positions_per_day=args.max_new_positions_per_day,
        require_trend_confirmation=args.require_trend_confirmation,
        max_chase_atr=args.max_chase_atr,
        max_entry_score=args.max_entry_score,
        alpha_profile=args.alpha_profile,
        benchmark_closes=benchmark_closes,
    )
    report = replay_paper_account(store, args.dataset, args.start, args.end, config)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
