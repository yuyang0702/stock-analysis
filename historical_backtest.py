"""Deterministic daily A-share historical matching engine."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from exit_policy import PositionExitState, evaluate_exit
from historical_data import STRICT_FEATURES, HistoricalStore, validate_dataset
from historical_strategy import Candidate, generate_daily_candidates


@dataclass(frozen=True)
class HistoricalBacktestConfig:
    initial_cash: float = 100_000.0
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_bps: float = 10.0
    max_positions: int = 8
    mode: str = "price_core"
    parameter_version: str = "v1"
    min_score: float = 75.0


@dataclass
class HistoricalPosition:
    code: str
    quantity: int
    initial_quantity: int
    entry_price: float
    stop_loss: float
    take_profit: float
    atr14: float
    mode: str
    market_regime: str
    industry: str
    theme: str
    buy_date: str
    highest_price: float
    take_profit_stage: int = 0
    last_adjust_factor: float = 1.0
    holding_trade_days: int = 0


@dataclass(frozen=True)
class PendingOrder:
    decision_date: str
    candidate: Candidate


@dataclass(frozen=True)
class PendingSell:
    decision_date: str
    code: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class HistoricalTrade:
    decision_date: str
    trade_date: str
    code: str
    action: str
    quantity: int
    price: float
    fee: float
    reason: str
    pnl: float | None = None
    holding_days: int = 0
    strategy_mode: str = "unknown"
    market_regime: str = "unknown"
    score: float = 0.0
    industry: str = "unknown"
    theme: str = "unknown"


@dataclass(frozen=True)
class EquityPoint:
    trade_date: str
    equity: float
    cash: float


@dataclass
class HistoricalBacktestResult:
    trades: list[HistoricalTrade] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    blocked_counts: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestMetrics:
    net_return: float
    annualized_return: float
    max_drawdown: float
    volatility: float
    calmar: float
    win_rate: float
    average_win: float
    average_loss: float
    profit_factor: float
    tail_loss_5pct: float
    turnover: float
    average_holding_days: float
    net_profit_without_top3: float
    profit_factor_without_top3: float


@dataclass(frozen=True)
class WalkForwardWindow:
    training_start: str
    training_end: str
    validation_start: str
    validation_end: str


def run_historical_backtest(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    config: HistoricalBacktestConfig,
) -> HistoricalBacktestResult:
    dates = store.trade_dates(dataset_id, start, end)
    result = HistoricalBacktestResult()
    cash = float(config.initial_cash)
    positions: dict[str, HistoricalPosition] = {}
    pending: list[PendingOrder] = []
    pending_sells: list[PendingSell] = []
    slip = config.slippage_bps / 10_000

    for trade_date in dates:
        rows = {str(row["code"]): row for row in store.daily_slice(dataset_id, trade_date)}

        for code, position in positions.items():
            row = rows.get(code)
            if row is None:
                continue
            factor = float(row["adjust_factor"])
            if factor <= 0 or factor == position.last_adjust_factor:
                continue
            ratio = factor / position.last_adjust_factor
            position.quantity = int(position.quantity * ratio / 100) * 100
            position.initial_quantity = int(position.initial_quantity * ratio / 100) * 100
            position.entry_price /= ratio
            position.stop_loss /= ratio
            position.take_profit /= ratio
            position.highest_price /= ratio
            position.atr14 /= ratio
            position.last_adjust_factor = factor

        # Exit decisions from the prior close always consume cash/positions before new buys.
        for order in sorted(pending_sells, key=lambda item: item.code):
            position = positions.get(order.code)
            row = rows.get(order.code)
            if position is None:
                continue
            if row is None or bool(row["suspended"]):
                _blocked(result, "SUSPENDED")
                continue
            if float(row["open"]) <= float(row["limit_down"]):
                _blocked(result, "LIMIT_DOWN_SELL_BLOCKED")
                continue
            quantity = min(order.quantity, position.quantity)
            price = round(float(row["open"]) * (1 - slip), 4)
            value = price * quantity
            fee = round(_commission(value, config) + value * config.stamp_tax_rate, 2)
            cash += value - fee
            pnl = round((price - position.entry_price) * quantity - fee, 2)
            result.trades.append(
                HistoricalTrade(
                    order.decision_date, trade_date, order.code, "sell", quantity, price, fee,
                    order.reason, pnl, holding_days=position.holding_trade_days,
                    strategy_mode=position.mode, market_regime=position.market_regime,
                    industry=position.industry, theme=position.theme,
                )
            )
            position.quantity -= quantity
            if position.quantity <= 0:
                del positions[order.code]
            elif order.reason == "TAKE_PROFIT_1":
                position.take_profit_stage = 1
        pending_sells = []

        # Orders decided at the prior close live for one open only.
        for order in sorted(pending, key=lambda item: (-item.candidate.score, item.candidate.code)):
            candidate = order.candidate
            row = rows.get(candidate.code)
            if row is None or bool(row["suspended"]):
                _blocked(result, "SUSPENDED")
                continue
            open_price = float(row["open"])
            if open_price >= float(row["limit_up"]):
                _blocked(result, "LIMIT_UP_BUY_BLOCKED")
                continue
            if candidate.code in positions or len(positions) >= config.max_positions:
                continue
            price = round(open_price * (1 + slip), 4)
            target = _account_value(cash, positions, rows) * candidate.position_pct / 100
            quantity = int(target / price / 100) * 100
            if quantity <= 0:
                _blocked(result, "LOT_TOO_SMALL")
                continue
            fee = _commission(price * quantity, config)
            while quantity > 0 and price * quantity + fee > cash:
                quantity -= 100
                fee = _commission(price * quantity, config) if quantity else 0
            if quantity <= 0:
                _blocked(result, "INSUFFICIENT_CASH")
                continue
            cash -= price * quantity + fee
            positions[candidate.code] = HistoricalPosition(
                code=candidate.code,
                quantity=quantity,
                initial_quantity=quantity,
                entry_price=price,
                stop_loss=candidate.stop_loss,
                take_profit=candidate.take_profit,
                atr14=candidate.atr14,
                mode=candidate.mode,
                market_regime=candidate.market_regime,
                industry=candidate.industry,
                theme=candidate.theme,
                buy_date=trade_date,
                highest_price=float(row["high"]),
                last_adjust_factor=float(row["adjust_factor"]),
            )
            result.trades.append(
                HistoricalTrade(order.decision_date, trade_date, candidate.code, "buy", quantity, price, fee, "SIGNAL")
            )
        pending = []

        # Conservative same-bar ordering: hard stop is evaluated before profit-taking.
        for code in sorted(tuple(positions)):
            position = positions[code]
            row = rows.get(code)
            if row is None or bool(row["suspended"]) or position.buy_date == trade_date:
                continue
            position.highest_price = max(position.highest_price, float(row["high"]))
            reason = ""
            raw_price = 0.0
            if float(row["low"]) <= position.stop_loss:
                reason = "HARD_STOP"
                raw_price = min(float(row["open"]), position.stop_loss)
            elif position.take_profit > 0 and float(row["high"]) >= position.take_profit:
                reason = "TAKE_PROFIT_1"
                raw_price = max(float(row["open"]), position.take_profit)
            if not reason:
                continue
            if float(row["open"]) <= float(row["limit_down"]):
                _blocked(result, "LIMIT_DOWN_SELL_BLOCKED")
                continue
            price = round(raw_price * (1 - slip), 4)
            if reason == "TAKE_PROFIT_1" and position.take_profit_stage == 0:
                target_quantity = position.initial_quantity // 2 // 100 * 100
                quantity = position.quantity - target_quantity
            else:
                quantity = position.quantity
            value = price * quantity
            fee = round(_commission(value, config) + value * config.stamp_tax_rate, 2)
            cash += value - fee
            pnl = round((price - position.entry_price) * quantity - fee, 2)
            result.trades.append(
                HistoricalTrade(
                    trade_date, trade_date, code, "sell", quantity, price, fee, reason, pnl,
                    holding_days=position.holding_trade_days, strategy_mode=position.mode,
                    market_regime=position.market_regime, industry=position.industry, theme=position.theme,
                )
            )
            if quantity >= position.quantity:
                del positions[code]
            else:
                position.quantity -= quantity
                position.take_profit_stage = 1

        for code in sorted(positions):
            position = positions[code]
            row = rows.get(code)
            if row is None or position.buy_date == trade_date:
                continue
            position.holding_trade_days += 1
            decision = evaluate_exit(
                PositionExitState(
                    code=code,
                    mode=position.mode,
                    initial_qty=position.initial_quantity,
                    current_qty=position.quantity,
                    entry_price=position.entry_price,
                    initial_stop_price=position.stop_loss,
                    highest_price=position.highest_price,
                    atr14=position.atr14,
                    take_profit_stage=position.take_profit_stage,
                    holding_trade_days=position.holding_trade_days,
                ),
                float(row["close"]),
                position.market_regime,
            )
            if decision.action != "hold":
                target = decision.target_qty if decision.target_qty is not None else position.quantity
                quantity = position.quantity if target == 0 else max(position.quantity - target, 0)
                if quantity:
                    pending_sells.append(PendingSell(trade_date, code, quantity, decision.action.upper()))

        candidates = generate_daily_candidates(
            store,
            dataset_id,
            trade_date,
            mode=config.mode,
            parameter_version=config.parameter_version,
            min_score=config.min_score,
        )
        pending = [PendingOrder(trade_date, candidate) for candidate in candidates]
        result.equity.append(
            EquityPoint(trade_date, round(_account_value(cash, positions, rows), 2), round(cash, 2))
        )
    return result


def _commission(value: float, config: HistoricalBacktestConfig) -> float:
    if value <= 0:
        return 0.0
    return round(max(config.minimum_commission, value * config.commission_rate), 2)


def _account_value(cash: float, positions: dict[str, HistoricalPosition], rows: dict[str, dict]) -> float:
    return cash + sum(
        position.quantity * float(rows.get(code, {}).get("close", position.entry_price))
        for code, position in positions.items()
    )


def _blocked(result: HistoricalBacktestResult, reason: str) -> None:
    result.blocked_counts[reason] = result.blocked_counts.get(reason, 0) + 1


def compute_metrics(
    equity: Iterable[EquityPoint], trades: Iterable[HistoricalTrade]
) -> BacktestMetrics:
    points = list(equity)
    rows = list(trades)
    values = [point.equity for point in points]
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values)) if values[index - 1]]
    net_return = values[-1] / values[0] - 1 if len(values) >= 2 and values[0] else 0.0
    annualized = (1 + net_return) ** (252 / max(len(returns), 1)) - 1 if 1 + net_return > 0 else -1.0
    peak = values[0] if values else 0.0
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    closed = [trade for trade in rows if trade.action == "sell" and trade.pnl is not None]
    pnls = [float(trade.pnl) for trade in closed]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = gross_win / gross_loss if gross_loss else (math.inf if gross_win else 0.0)
    sorted_returns = sorted(returns)
    tail_count = max(1, math.ceil(len(sorted_returns) * 0.05)) if sorted_returns else 0
    tail_loss = -sum(sorted_returns[:tail_count]) / tail_count if tail_count else 0.0
    average_equity = sum(values) / len(values) if values else 0.0
    turnover = sum(trade.price * trade.quantity for trade in rows) / average_equity if average_equity else 0.0
    remaining = list(pnls)
    for value in sorted((item for item in remaining if item > 0), reverse=True)[:3]:
        remaining.remove(value)
    robust_wins = sum(value for value in remaining if value > 0)
    robust_loss = -sum(value for value in remaining if value < 0)
    robust_pf = robust_wins / robust_loss if robust_loss else (math.inf if robust_wins else 0.0)
    return BacktestMetrics(
        net_return=net_return,
        annualized_return=annualized,
        max_drawdown=max_drawdown,
        volatility=volatility,
        calmar=annualized / max_drawdown if max_drawdown else 0.0,
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        average_win=sum(wins) / len(wins) if wins else 0.0,
        average_loss=sum(losses) / len(losses) if losses else 0.0,
        profit_factor=profit_factor,
        tail_loss_5pct=tail_loss,
        turnover=turnover,
        average_holding_days=sum(trade.holding_days for trade in closed) / len(closed) if closed else 0.0,
        net_profit_without_top3=sum(remaining),
        profit_factor_without_top3=robust_pf,
    )


def build_walk_forward_windows(trade_dates: Iterable[str], count: int = 3) -> list[WalkForwardWindow]:
    dates = sorted(set(trade_dates))
    if count <= 0 or len(dates) < count + 1:
        return []
    size = max(1, len(dates) // (count + 1))
    windows = []
    for index in range(count):
        validation_start_index = size * (index + 1)
        validation_end_index = size * (index + 2) - 1 if index < count - 1 else len(dates) - 1
        if validation_start_index >= len(dates):
            break
        windows.append(
            WalkForwardWindow(
                training_start=dates[0],
                training_end=dates[validation_start_index - 1],
                validation_start=dates[validation_start_index],
                validation_end=dates[min(validation_end_index, len(dates) - 1)],
            )
        )
    return windows


def compare_results(
    baseline: HistoricalBacktestResult, candidate: HistoricalBacktestResult
) -> dict[str, object]:
    contract_keys = (
        "dataset_hash",
        "window",
        "fees",
        "slippage",
        "capital",
        "strategy_version",
        "parameter_family_count",
    )
    mismatches = [key for key in contract_keys if baseline.metadata.get(key) != candidate.metadata.get(key)]
    if mismatches:
        return {"status": "COMPARISON_CONTRACT_MISMATCH", "mismatches": mismatches}
    left = compute_metrics(baseline.equity, baseline.trades)
    right = compute_metrics(candidate.equity, candidate.trades)
    return {
        "status": "COMPARABLE",
        "net_return_delta": right.net_return - left.net_return,
        "max_drawdown_delta": right.max_drawdown - left.max_drawdown,
        "profit_factor_delta": right.profit_factor - left.profit_factor,
    }


def group_metrics(trades: Iterable[HistoricalTrade], fields: Iterable[str]) -> dict[str, dict]:
    rows = list(trades)
    result: dict[str, dict] = {}
    for field_name in fields:
        buckets: dict[str, list[HistoricalTrade]] = {}
        for trade in rows:
            if field_name == "score_band":
                value = f"{int(trade.score // 10) * 10}-{int(trade.score // 10) * 10 + 9}"
            else:
                value = str(getattr(trade, field_name, "unknown") or "unknown")
            buckets.setdefault(value, []).append(trade)
        ordered = sorted(buckets, key=lambda key: (-len(buckets[key]), key))
        kept = ordered[:20]
        grouped = {
            key: {"count": len(buckets[key]), "net_pnl": sum(float(row.pnl or 0) for row in buckets[key])}
            for key in kept
        }
        overflow = [row for key in ordered[20:] for row in buckets[key]]
        if overflow:
            grouped["other"] = {"count": len(overflow), "net_pnl": sum(float(row.pnl or 0) for row in overflow)}
        result[field_name] = grouped
    return result


def sensitivity_matrix(
    result_factory: Callable[[HistoricalBacktestConfig], HistoricalBacktestResult],
    base_config: HistoricalBacktestConfig,
) -> dict[str, HistoricalBacktestResult]:
    variants = {
        "zero_slippage": replace(base_config, slippage_bps=0),
        "base": base_config,
        "double_slippage": replace(base_config, slippage_bps=base_config.slippage_bps * 2),
        "double_fees": replace(
            base_config,
            commission_rate=base_config.commission_rate * 2,
            minimum_commission=base_config.minimum_commission * 2,
            stamp_tax_rate=base_config.stamp_tax_rate * 2,
        ),
    }
    return {name: result_factory(config) for name, config in variants.items()}


def _publish_atomic(output_dir: Path, files: dict[str, str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = {
        name: (output_dir / name).read_bytes() if (output_dir / name).exists() else None
        for name in files
    }
    temporary: list[Path] = []
    replaced: list[str] = []
    try:
        for name, content in files.items():
            temp = output_dir / f".{name}.tmp"
            temp.write_text(content, encoding="utf-8", newline="")
            temporary.append(temp)
        for name, temp in zip(files, temporary):
            temp.replace(output_dir / name)
            replaced.append(name)
    except Exception:
        for name in replaced:
            target = output_dir / name
            old = previous[name]
            if old is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(old)
        raise
    finally:
        for temp in temporary:
            temp.unlink(missing_ok=True)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _quality_payload(report) -> dict[str, object]:
    return asdict(report)


def _implementation_hash(paths: Iterable[Path] | None = None) -> str:
    selected = list(paths) if paths is not None else [
        Path(__file__),
        Path(__file__).with_name("historical_data.py"),
        Path(__file__).with_name("historical_strategy.py"),
    ]
    digest = hashlib.sha256()
    for path in sorted(selected, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_id(store: HistoricalStore, args, config: HistoricalBacktestConfig) -> str:
    payload = {
        "dataset_hash": store.dataset_hash(args.dataset),
        "start": args.start,
        "end": args.end,
        "mode": args.mode,
        "strategy_version": args.strategy_version,
        "code_hash": _implementation_hash(),
        "parameter_version": config.parameter_version,
        "capital": config.initial_cash,
        "commission": config.commission_rate,
        "minimum_commission": config.minimum_commission,
        "stamp_tax": config.stamp_tax_rate,
        "slippage_bps": config.slippage_bps,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _persist_failure(
    store: HistoricalStore,
    run_id: str,
    args,
    config: HistoricalBacktestConfig,
    error: Exception,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    message = " ".join(str(error).split())[:240]
    config_payload = {
        **asdict(config),
        "strategy_version": args.strategy_version,
        "code_hash": _implementation_hash(),
    }
    with store.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO backtest_runs "
            "(run_id, dataset_id, dataset_hash, start_date, end_date, mode, config_json, status, "
            "error, summary_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, '{}', ?)",
            (
                run_id, args.dataset, store.dataset_hash(args.dataset), args.start, args.end,
                args.mode, json.dumps(config_payload, sort_keys=True), message, now,
            ),
        )


def _persist_result(
    store: HistoricalStore,
    run_id: str,
    dataset_id: str,
    start: str,
    end: str,
    config: HistoricalBacktestConfig,
    strategy_version: str,
    result: HistoricalBacktestResult,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    metrics = asdict(compute_metrics(result.equity, result.trades))
    config_payload = {
        **asdict(config),
        "strategy_version": strategy_version,
        "code_hash": _implementation_hash(),
    }
    with store.transaction() as connection:
        exists = connection.execute("SELECT 1 FROM backtest_runs WHERE run_id = ?", (run_id,)).fetchone()
        if exists:
            return
        connection.execute(
            "INSERT INTO backtest_runs "
            "(run_id, dataset_id, dataset_hash, start_date, end_date, mode, config_json, status, "
            "summary_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?, ?)",
            (run_id, dataset_id, store.dataset_hash(dataset_id), start, end, config.mode, json.dumps(config_payload, sort_keys=True), json.dumps(metrics, sort_keys=True), now, now),
        )
        connection.executemany(
            "INSERT INTO backtest_equity(run_id, trade_date, equity, cash) VALUES (?, ?, ?, ?)",
            [(run_id, point.trade_date, point.equity, point.cash) for point in result.equity],
        )
        connection.executemany(
            "INSERT INTO backtest_trades "
            "(trade_id, run_id, decision_date, trade_date, code, action, quantity, price, fee, reason, pnl, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"{run_id}-{index:06d}", run_id, trade.decision_date, trade.trade_date,
                    trade.code, trade.action, trade.quantity, trade.price, trade.fee, trade.reason,
                    trade.pnl, json.dumps(asdict(trade), ensure_ascii=False, sort_keys=True),
                )
                for index, trade in enumerate(result.trades)
            ],
        )


def _csv_text(rows: list[dict], fields: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Point-in-time A-share historical backtest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import")
    importer.add_argument("--db", required=True)
    importer.add_argument("--dataset", required=True)
    importer.add_argument("--kind", required=True, choices=("bars", "status", "universe", "features"))
    importer.add_argument("--file", required=True)
    importer.add_argument("--source", required=True, choices=("joinquant", "akshare"))
    importer.add_argument("--adjust", default="raw")
    for command in ("validate", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--db", required=True)
        child.add_argument("--dataset", required=True)
        child.add_argument("--start", required=True)
        child.add_argument("--end", required=True)
        child.add_argument("--mode", required=True, choices=("strict", "price_core"))
        child.add_argument("--output-dir", required=True)
        child.add_argument("--strategy-version", default="historical-v1")
        child.add_argument("--parameter-version", default="v1")
        child.add_argument("--capital", type=float, default=100_000)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--db", required=True)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = HistoricalStore(Path(args.db))
    store.initialize()
    if args.command == "import":
        print(store.import_csv(args.dataset, args.kind, Path(args.file), args.source, args.adjust))
        return 0
    if args.command == "compare":
        with store.connect() as connection:
            rows = {
                row["run_id"]: dict(row)
                for row in connection.execute(
                    "SELECT run_id, dataset_hash, start_date, end_date, config_json, summary_json "
                    "FROM backtest_runs WHERE run_id IN (?, ?)", (args.baseline, args.candidate)
                )
            }
        if set(rows) != {args.baseline, args.candidate}:
            payload = {"status": "RUN_NOT_FOUND"}
            code = 2
        else:
            left, right = rows[args.baseline], rows[args.candidate]
            mismatches = [key for key in ("dataset_hash", "start_date", "end_date") if left[key] != right[key]]
            left_config = json.loads(left["config_json"])
            right_config = json.loads(right["config_json"])
            for key in (
                "initial_cash", "commission_rate", "minimum_commission", "stamp_tax_rate",
                "slippage_bps", "strategy_version", "code_hash", "parameter_version",
            ):
                if left_config.get(key) != right_config.get(key):
                    mismatches.append(f"config:{key}")
            payload = {"status": "COMPARISON_CONTRACT_MISMATCH", "mismatches": mismatches} if mismatches else {"status": "COMPARABLE", "baseline": json.loads(left["summary_json"]), "candidate": json.loads(right["summary_json"])}
            code = 0 if not mismatches else 2
        _publish_atomic(Path(args.output_dir), {"historical_backtest_compare.json": _json(payload)})
        return code

    quality = validate_dataset(store, args.dataset, args.start, args.end, args.mode, STRICT_FEATURES)
    quality_file = {"historical_backtest_quality.json": _json(_quality_payload(quality))}
    if args.command == "validate" or not quality.accepted:
        _publish_atomic(Path(args.output_dir), quality_file)
        return 0 if quality.accepted else 2

    config = HistoricalBacktestConfig(
        initial_cash=args.capital,
        mode=args.mode,
        parameter_version=args.parameter_version,
    )
    run_id = _run_id(store, args, config)
    try:
        result = run_historical_backtest(store, args.dataset, args.start, args.end, config)
    except Exception as error:
        _persist_failure(store, run_id, args, config, error)
        _publish_atomic(Path(args.output_dir), quality_file)
        return 2
    result.metadata = {
        "run_id": run_id,
        "dataset_hash": quality.input_hash,
        "window": f"{args.start}:{args.end}",
        "proxy_only": quality.proxy_only,
    }
    _persist_result(store, run_id, args.dataset, args.start, args.end, config, args.strategy_version, result)
    metrics = asdict(compute_metrics(result.equity, result.trades))
    equity_fields = ["trade_date", "equity", "cash"]
    trade_fields = [field.name for field in HistoricalTrade.__dataclass_fields__.values()]
    files = {
        **quality_file,
        "historical_backtest_latest.md": f"# Historical Backtest\n\n- run_id: `{run_id}`\n- mode: `{args.mode}`\n- proxy_only: `{str(quality.proxy_only).lower()}`\n- metrics: `{json.dumps(metrics, sort_keys=True)}`\n",
        "historical_backtest_equity.csv": _csv_text([asdict(row) for row in result.equity], equity_fields),
        "historical_backtest_trades.csv": _csv_text([asdict(row) for row in result.trades], trade_fields),
    }
    _publish_atomic(Path(args.output_dir), files)
    store.prune_runs(20)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
