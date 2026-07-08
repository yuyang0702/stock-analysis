from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_INPUTS = (
    Path("cache/ml/signal_samples.jsonl"),
    Path("cache/joinquant/signals.json"),
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _date_text(row: dict[str, Any]) -> str:
    value = (
        row.get("date")
        or row.get("trade_date")
        or row.get("generated_at")
        or row.get("datetime")
        or row.get("time")
    )
    text = _text(value)
    return text[:10] if text else datetime.now().strftime("%Y-%m-%d")


def _normalize_action(row: dict[str, Any]) -> str:
    action = _text(row.get("action") or row.get("signal_action")).lower()
    if action in {"sell", "stop_loss", "take_profit", "time_stop"} or "sell" in action:
        return "sell"
    if action == "buy":
        return "buy"
    return action or "hold"


def _unwrap_sample(row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("signal"), dict):
        merged = dict(row.get("features") or {})
        merged.update(row["signal"])
        return merged
    return row


def load_signal_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(_unwrap_sample(json.loads(line)))
        return rows
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("signals"), list):
        trade_date = data.get("trade_date")
        rows = []
        for item in data["signals"]:
            if isinstance(item, dict):
                row = dict(item)
                row.setdefault("trade_date", trade_date)
                rows.append(row)
        return rows
    if isinstance(data, list):
        return [_unwrap_sample(row) for row in data if isinstance(row, dict)]
    return []


@dataclass
class BacktestConfig:
    initial_cash: float = 100000.0
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.001
    min_commission: float = 5.0
    max_position_pct: float = 20.0
    max_total_position_pct: float = 80.0
    lot_size: int = 100


@dataclass
class Position:
    code: str
    name: str
    qty: int
    avg_cost: float
    buy_date: str
    stop_loss: float = 0.0
    take_profit: float = 0.0
    last_price: float = 0.0


@dataclass
class BacktestResult:
    initial_cash: float
    final_value: float
    cash: float
    trades: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    open_positions: int
    total_return_pct: float
    max_drawdown_pct: float
    win_trades: int
    loss_trades: int


class BacktestEngine:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()
        self.cash = float(self.config.initial_cash)
        self.positions: dict[str, Position] = {}
        self.trades: list[dict[str, Any]] = []
        self.equity_curve: list[dict[str, Any]] = []
        self.realized_returns: list[float] = []

    def run(self, rows: Iterable[dict[str, Any]]) -> BacktestResult:
        normalized = sorted(
            [dict(row) for row in rows],
            key=lambda row: (_date_text(row), _text(row.get("code"))),
        )
        for row in normalized:
            self._mark_price(row)
            self._check_stops(row)
            action = _normalize_action(row)
            if action == "buy":
                self._buy(row)
            elif action == "sell":
                self._sell(row, "sell_signal")
            self._record_equity(_date_text(row))
        if not normalized:
            self._record_equity(datetime.now().strftime("%Y-%m-%d"))
        final_value = self._portfolio_value()
        return BacktestResult(
            initial_cash=self.config.initial_cash,
            final_value=final_value,
            cash=self.cash,
            trades=self.trades,
            equity_curve=self.equity_curve,
            open_positions=len(self.positions),
            total_return_pct=(final_value / self.config.initial_cash - 1.0) * 100.0,
            max_drawdown_pct=self._max_drawdown_pct(),
            win_trades=sum(1 for value in self.realized_returns if value > 0),
            loss_trades=sum(1 for value in self.realized_returns if value < 0),
        )

    def _mark_price(self, row: dict[str, Any]) -> None:
        code = _text(row.get("code"))
        price = _num(row.get("price"))
        if code in self.positions and price > 0:
            self.positions[code].last_price = price

    def _check_stops(self, row: dict[str, Any]) -> None:
        code = _text(row.get("code"))
        pos = self.positions.get(code)
        if not pos:
            return
        price = _num(row.get("price"))
        if price <= 0 or _date_text(row) <= pos.buy_date:
            return
        stop = _num(row.get("stop_loss"), pos.stop_loss)
        take = _num(row.get("take_profit"), pos.take_profit)
        if stop > 0 and price <= stop:
            self._sell(row, "stop_loss")
        elif take > 0 and price >= take:
            self._sell(row, "take_profit")

    def _buy(self, row: dict[str, Any]) -> None:
        code = _text(row.get("code"))
        if not code or code in self.positions:
            return
        price = _num(row.get("entry_price"), _num(row.get("price")))
        if price <= 0 or _num(row.get("pct_chg")) >= 9.8:
            return
        target_pct = min(
            max(_num(row.get("position_pct")), 0.0),
            self.config.max_position_pct,
            self.config.max_total_position_pct,
        )
        budget = self.config.initial_cash * target_pct / 100.0
        max_total_value = self.config.initial_cash * self.config.max_total_position_pct / 100.0
        invested = sum(pos.qty * pos.last_price for pos in self.positions.values())
        budget = min(budget, max(max_total_value - invested, 0.0), self.cash)
        qty = int(budget // (price * self.config.lot_size)) * self.config.lot_size
        if qty <= 0:
            return
        fee = self._buy_fee(qty * price)
        if qty * price + fee > self.cash:
            qty = int((self.cash - fee) // (price * self.config.lot_size)) * self.config.lot_size
        if qty <= 0:
            return
        gross = qty * price
        fee = self._buy_fee(gross)
        self.cash -= gross + fee
        self.positions[code] = Position(
            code=code,
            name=_text(row.get("name")),
            qty=qty,
            avg_cost=price,
            buy_date=_date_text(row),
            stop_loss=_num(row.get("stop_loss")),
            take_profit=_num(row.get("take_profit")),
            last_price=price,
        )
        self.trades.append(self._trade_row(row, "buy", qty, price, fee, "buy_signal", 0.0))

    def _sell(self, row: dict[str, Any], reason: str) -> None:
        code = _text(row.get("code"))
        pos = self.positions.get(code)
        if not pos or _date_text(row) <= pos.buy_date:
            return
        price = _num(row.get("price"), pos.last_price)
        if price <= 0 or _num(row.get("pct_chg")) <= -9.8:
            return
        gross = pos.qty * price
        fee = self._sell_fee(gross)
        self.cash += gross - fee
        pnl = (price - pos.avg_cost) * pos.qty - fee
        self.realized_returns.append(pnl)
        self.trades.append(self._trade_row(row, "sell", pos.qty, price, fee, reason, pnl))
        del self.positions[code]

    def _trade_row(
        self,
        row: dict[str, Any],
        action: str,
        qty: int,
        price: float,
        fee: float,
        reason: str,
        pnl: float,
    ) -> dict[str, Any]:
        return {
            "date": _date_text(row),
            "code": _text(row.get("code")),
            "name": _text(row.get("name")),
            "action": action,
            "qty": qty,
            "price": round(price, 4),
            "fee": round(fee, 4),
            "reason": reason,
            "pnl": round(pnl, 4),
            "final_score": _num(row.get("final_score")),
        }

    def _buy_fee(self, gross: float) -> float:
        if gross <= 0:
            return 0.0
        return max(gross * self.config.commission_rate, self.config.min_commission)

    def _sell_fee(self, gross: float) -> float:
        if gross <= 0:
            return 0.0
        commission = max(gross * self.config.commission_rate, self.config.min_commission)
        return commission + gross * self.config.stamp_tax_rate

    def _record_equity(self, date: str) -> None:
        value = self._portfolio_value()
        if self.equity_curve and self.equity_curve[-1]["date"] == date:
            self.equity_curve[-1] = {"date": date, "value": round(value, 4)}
        else:
            self.equity_curve.append({"date": date, "value": round(value, 4)})

    def _portfolio_value(self) -> float:
        return self.cash + sum(pos.qty * pos.last_price for pos in self.positions.values())

    def _max_drawdown_pct(self) -> float:
        peak = 0.0
        max_drawdown = 0.0
        for row in self.equity_curve:
            value = _num(row.get("value"))
            peak = max(peak, value)
            if peak > 0:
                max_drawdown = min(max_drawdown, (value / peak - 1.0) * 100.0)
        return max_drawdown


def build_report(result: BacktestResult) -> str:
    win_rate = (
        result.win_trades / max(result.win_trades + result.loss_trades, 1) * 100.0
    )
    lines = [
        "# 本地信号回测报告",
        "",
        f"- 初始资金：{result.initial_cash:.2f}",
        f"- 期末权益：{result.final_value:.2f}",
        f"- 总收益：{result.total_return_pct:.2f}%",
        f"- 最大回撤：{result.max_drawdown_pct:.2f}%",
        f"- 交易次数：{len(result.trades)}",
        f"- 胜率：{win_rate:.2f}%",
        f"- 未平仓数量：{result.open_positions}",
        "",
        "## 最近交易",
        "",
    ]
    for row in result.trades[-20:]:
        lines.append(
            f"- {row['date']} {row['action']} {row['code']} {row.get('name', '')} "
            f"数量{row['qty']} 价格{row['price']} 原因{row['reason']} 盈亏{row['pnl']}"
        )
    if not result.trades:
        lines.append("- 暂无交易。")
    lines.append("")
    lines.append("> 第一版为信号级轻量回测，用于评估已生成信号的执行效果；完整历史重跑策略会在后续阶段补充。")
    return "\n".join(lines) + "\n"


def write_trades_csv(path: Path, trades: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["date", "code", "name", "action", "qty", "price", "fee", "reason", "pnl", "final_score"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in trades:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def resolve_default_input() -> Path:
    for path in DEFAULT_INPUTS:
        if path.exists():
            return path
    return DEFAULT_INPUTS[0]


def run_backtest(
    input_file: Path | None = None,
    report_file: Path | None = None,
    trades_file: Path | None = None,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    input_file = input_file or resolve_default_input()
    report_file = report_file or Path("output/backtest_report.md")
    trades_file = trades_file or Path("output/backtest_trades.csv")
    result = BacktestEngine(config).run(load_signal_rows(input_file))
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(build_report(result), encoding="utf-8")
    write_trades_csv(trades_file, result.trades)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local signal-level backtest")
    parser.add_argument("--input", type=Path, default=None, help="Signal CSV/JSON/JSONL file")
    parser.add_argument("--report", type=Path, default=Path("output/backtest_report.md"))
    parser.add_argument("--trades", type=Path, default=Path("output/backtest_trades.csv"))
    parser.add_argument("--cash", type=float, default=100000.0)
    parser.add_argument("--commission-rate", type=float, default=0.0003)
    parser.add_argument("--stamp-tax-rate", type=float, default=0.001)
    args = parser.parse_args()
    result = run_backtest(
        args.input,
        args.report,
        args.trades,
        BacktestConfig(
            initial_cash=args.cash,
            commission_rate=args.commission_rate,
            stamp_tax_rate=args.stamp_tax_rate,
        ),
    )
    print(f"Backtest report: {args.report}")
    print(f"Trades CSV: {args.trades}")
    print(f"Total return: {result.total_return_pct:.2f}%")


if __name__ == "__main__":
    main()
