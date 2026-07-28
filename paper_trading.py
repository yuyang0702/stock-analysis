from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

import config as app_config
from execution_contracts import FeeBreakdown, FeeSchedule


def new_account(initial_cash: float = 100_000) -> dict[str, Any]:
    cash = round(float(initial_cash), 2)
    return {
        "initial_cash": cash,
        "cash": cash,
        "positions": {},
        "trades": [],
        "cooldown": {},
        "equity_curve": [],
        "realized_pnl": 0.0,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": "",
    }


def load_account(path: Path, initial_cash: float = 100_000) -> dict[str, Any]:
    if not path.exists():
        return new_account(initial_cash)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return new_account(initial_cash)
    if not isinstance(raw, dict):
        return new_account(initial_cash)
    raw.setdefault("initial_cash", float(initial_cash))
    raw.setdefault("cash", float(initial_cash))
    raw.setdefault("positions", {})
    raw.setdefault("trades", [])
    raw.setdefault("cooldown", {})
    raw.setdefault("equity_curve", [])
    raw.setdefault("realized_pnl", 0.0)
    return raw


def save_account(path: Path, account: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    account["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(account, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _code(value: Any) -> str:
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _txt(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _row_map(rows: pd.DataFrame) -> dict[str, pd.Series]:
    if rows.empty or "code" not in rows.columns:
        return {}
    return {_code(row.get("code")): row for _, row in rows.iterrows() if _code(row.get("code"))}


def _fee_schedule(
    fee_schedule: FeeSchedule | None,
    commission_rate: float,
    stamp_tax_rate: float,
    slippage_pct: float,
) -> FeeSchedule:
    if fee_schedule is not None:
        return fee_schedule
    base = app_config.SIMULATION_FEE_SCHEDULE
    if (
        commission_rate == float(base.buy_commission_rate)
        and stamp_tax_rate == float(base.stamp_tax_rate)
        and slippage_pct == float(base.buy_slippage_rate)
    ):
        return base
    no_costs = commission_rate == stamp_tax_rate == slippage_pct == 0
    minimums = (
        (0, 0)
        if no_costs or commission_rate == 0
        else (base.buy_minimum_commission_yuan, base.sell_minimum_commission_yuan)
    )
    return base.derive_variant(
        "paper-compat",
        buy_commission_rate=commission_rate,
        sell_commission_rate=commission_rate,
        buy_minimum_commission_yuan=minimums[0],
        sell_minimum_commission_yuan=minimums[1],
        stamp_tax_rate=stamp_tax_rate,
        buy_slippage_rate=slippage_pct,
        sell_slippage_rate=slippage_pct,
        transfer_fee_rate=0 if no_costs else base.transfer_fee_rate,
        other_fee_rate=0 if no_costs else base.other_fee_rate,
    )


def _fee_fields(value: FeeBreakdown) -> dict[str, float]:
    return {key: float(amount) for key, amount in value.components_dict().items()}


def _equity(account: dict[str, Any]) -> float:
    cash = _num(account.get("cash"))
    market_value = sum(
        _num(pos.get("last_price")) * _num(pos.get("qty"))
        for pos in account.get("positions", {}).values()
    )
    return round(cash + market_value, 2)


def _current_exposure_pct(account: dict[str, Any]) -> float:
    equity = _equity(account)
    if equity <= 0:
        return 0.0
    value = sum(
        _num(pos.get("last_price")) * _num(pos.get("qty"))
        for pos in account.get("positions", {}).values()
    )
    return value / equity * 100.0


def _date_add(value: str, days: int) -> str:
    return (datetime.strptime(value, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _is_limit_up(row: pd.Series) -> bool:
    text = f"{_txt(row.get('limit_quality'))} {_txt(row.get('pressure_label'))}"
    return "一字" in text or "涨停买不进" in text or _num(row.get("pct_chg")) >= 9.8


def _is_limit_down(row: pd.Series) -> bool:
    text = f"{_txt(row.get('limit_quality'))} {_txt(row.get('pressure_label'))}"
    return "跌停" in text or _num(row.get("pct_chg")) <= -9.8


def _signal_type(row: pd.Series) -> str:
    return _txt(row.get("mode")) or _txt(row.get("buy_state")) or "signal"


def _can_buy(row: pd.Series, account: dict[str, Any], trade_date: str, min_score: float) -> bool:
    code = _code(row.get("code"))
    price = _num(row.get("price"))
    entry = _num(row.get("entry_price"), price)
    stop = _num(row.get("stop_loss"))
    if not code or price <= 0 or entry <= 0 or _num(row.get("position_pct")) <= 0:
        return False
    if _txt(account.get("cooldown", {}).get(code)) >= trade_date:
        return False
    if _txt(row.get("signal_action")) in {"stop_loss", "take_profit", "time_stop"}:
        return False
    if _is_limit_up(row):
        return False
    if _num(row.get("final_score")) < min_score:
        return False
    if price > entry * 1.01:
        return False
    if stop > 0 and price <= stop:
        return False
    return True


def _buy_qty(
    account: dict[str, Any],
    price: float,
    position_pct: float,
    fees: FeeSchedule,
    max_position_pct: float,
    max_total_position_pct: float,
) -> int:
    remaining_pct = max(0.0, max_total_position_pct - _current_exposure_pct(account))
    allowed_pct = min(position_pct, max_position_pct, remaining_pct)
    target_value = _equity(account) * allowed_pct / 100.0
    qty = int(min(target_value, _num(account.get("cash"))) // (price * 100)) * 100
    while qty >= 100:
        gross = round(qty * price, 2)
        cost = fees.estimate("buy", Decimal(str(price)), qty).total_yuan
        if gross + float(cost) <= _num(account.get("cash")):
            return qty
        qty -= 100
    return 0


def apply_paper_trades(
    account: dict[str, Any],
    rows: pd.DataFrame,
    trade_date: str | None = None,
    min_score: float = 75.0,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.0005,
    slippage_pct: float = 0.001,
    cooldown_days: int = 3,
    max_positions: int = 5,
    max_position_pct: float = 20.0,
    max_total_position_pct: float = 80.0,
    fee_schedule: FeeSchedule | None = None,
) -> list[dict[str, Any]]:
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    fees = _fee_schedule(fee_schedule, commission_rate, stamp_tax_rate, slippage_pct)
    rows_by_code = _row_map(rows)
    positions = account.setdefault("positions", {})
    events: list[dict[str, Any]] = []

    for code, pos in list(positions.items()):
        row = rows_by_code.get(code)
        if row is None:
            continue
        price = _num(row.get("price"), _num(pos.get("last_price")))
        pos["last_price"] = price
        stop = _num(pos.get("stop_loss")) or _num(row.get("stop_loss"))
        take = _num(pos.get("take_profit")) or _num(row.get("take_profit"))
        reason = "take_profit" if take > 0 and price >= take else "stop_loss" if stop > 0 and price <= stop else ""
        if not reason or _txt(pos.get("buy_date")) >= trade_date or _is_limit_down(row):
            continue

        qty = int(_num(pos.get("qty")))
        deal_price = round(price, 3)
        gross = round(qty * deal_price, 2)
        breakdown = fees.estimate("sell", Decimal(str(deal_price)), qty)
        fee_total = float(breakdown.total_yuan)
        proceeds = round(gross - fee_total, 2)
        pnl = round(proceeds - _num(pos.get("cost_amount")), 2)
        account["cash"] = round(_num(account.get("cash")) + proceeds, 2)
        account["realized_pnl"] = round(_num(account.get("realized_pnl")) + pnl, 2)
        del positions[code]
        if reason == "stop_loss":
            account.setdefault("cooldown", {})[code] = _date_add(trade_date, cooldown_days)
        event = {
            "date": trade_date,
            "action": "sell",
            "code": code,
            "name": _txt(pos.get("name")),
            "price": deal_price,
            "qty": qty,
            "amount": proceeds,
            "fees": fee_total,
            "fee_schedule_version": fees.version,
            "fee_schedule_sha256": fees.contract_sha256,
            "fee_components": _fee_fields(breakdown),
            "pnl": pnl,
            "reason": reason,
            "signal_type": _txt(pos.get("signal_type")),
        }
        account.setdefault("trades", []).append(event)
        events.append(event)

    for code, row in rows_by_code.items():
        if len(positions) >= max_positions:
            break
        if code in positions or not _can_buy(row, account, trade_date, min_score):
            continue
        price = round(_num(row.get("price")), 3)
        qty = _buy_qty(
            account,
            price,
            _num(row.get("position_pct")),
            fees,
            max_position_pct,
            max_total_position_pct,
        )
        if qty < 100:
            continue
        gross = round(qty * price, 2)
        breakdown = fees.estimate("buy", Decimal(str(price)), qty)
        fee_total = float(breakdown.total_yuan)
        cost = round(gross + fee_total, 2)
        signal_type = _signal_type(row)
        account["cash"] = round(_num(account.get("cash")) - cost, 2)
        positions[code] = {
            "code": code,
            "name": _txt(row.get("name")),
            "qty": qty,
            "avg_cost": price,
            "last_price": price,
            "buy_date": trade_date,
            "stop_loss": _num(row.get("stop_loss")),
            "take_profit": _num(row.get("take_profit")),
            "cost_amount": cost,
            "signal_type": signal_type,
        }
        event = {
            "date": trade_date,
            "action": "buy",
            "code": code,
            "name": _txt(row.get("name")),
            "price": price,
            "qty": qty,
            "amount": cost,
            "fees": fee_total,
            "fee_schedule_version": fees.version,
            "fee_schedule_sha256": fees.contract_sha256,
            "fee_components": _fee_fields(breakdown),
            "reason": "signal",
            "signal_type": signal_type,
        }
        account.setdefault("trades", []).append(event)
        events.append(event)

    account.setdefault("equity_curve", []).append({"date": trade_date, "equity": _equity(account)})
    account["fee_schedule_version"] = fees.version
    account["fee_schedule_sha256"] = fees.contract_sha256
    account["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return events


def _max_drawdown_pct(curve: list[dict[str, Any]]) -> float:
    peak = 0.0
    max_dd = 0.0
    for item in curve:
        equity = _num(item.get("equity"))
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, (equity - peak) / peak * 100.0)
    return round(max_dd, 2)


def summarize_account(account: dict[str, Any]) -> dict[str, Any]:
    trades = account.get("trades", [])
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    wins = [trade for trade in sells if _num(trade.get("pnl")) > 0]
    signal_stats: dict[str, dict[str, Any]] = {}
    fee_keys = (
        "commission_yuan",
        "stamp_tax_yuan",
        "transfer_fee_yuan",
        "other_fee_yuan",
        "slippage_yuan",
    )
    fee_groups: dict[str, dict[str, Any]] = {}
    for trade in trades:
        version = _txt(trade.get("fee_schedule_version")) or "not-applicable"
        schedule_hash = _txt(trade.get("fee_schedule_sha256"))
        key = schedule_hash or f"legacy:{version}"
        group = fee_groups.setdefault(
            key,
            {
                "fee_schedule_version": version,
                "fee_schedule_sha256": schedule_hash or "not-applicable",
                "fee_components": {name: 0.0 for name in fee_keys},
            },
        )
        for name in fee_keys:
            group["fee_components"][name] = round(
                group["fee_components"][name]
                + _num(trade.get("fee_components", {}).get(name)),
                2,
            )
    for trade in sells:
        key = _txt(trade.get("signal_type")) or "signal"
        item = signal_stats.setdefault(key, {"closed": 0, "wins": 0, "pnl": 0.0})
        item["closed"] += 1
        item["wins"] += 1 if _num(trade.get("pnl")) > 0 else 0
        item["pnl"] = round(item["pnl"] + _num(trade.get("pnl")), 2)
    ordered_fee_groups = {key: fee_groups[key] for key in sorted(fee_groups)}
    versions = sorted({item["fee_schedule_version"] for item in fee_groups.values()})
    summary_version = (
        "mixed"
        if len(fee_groups) > 1
        else versions[0]
        if versions
        else _txt(account.get("fee_schedule_version")) or "not-applicable"
    )
    summary_hash = (
        "mixed"
        if len(fee_groups) > 1
        else next(iter(fee_groups.values()))["fee_schedule_sha256"]
        if fee_groups
        else _txt(account.get("fee_schedule_sha256")) or "not-applicable"
    )
    return {
        "cash": round(_num(account.get("cash")), 2),
        "equity": _equity(account),
        "position_count": len(account.get("positions", {})),
        "realized_pnl": round(_num(account.get("realized_pnl")), 2),
        "trade_count": len(trades),
        "closed_trades": len(sells),
        "win_rate": round(len(wins) / len(sells) * 100, 2) if sells else 0.0,
        "max_drawdown_pct": _max_drawdown_pct(account.get("equity_curve", [])),
        "signal_stats": signal_stats,
        "fee_schedule_version": summary_version,
        "fee_schedule_versions": versions,
        "fee_schedule_sha256": summary_hash,
        "fee_components_by_schedule": ordered_fee_groups,
        "fee_components": {
            key: round(sum(_num(trade.get("fee_components", {}).get(key)) for trade in trades), 2)
            for key in fee_keys
        },
    }


def build_paper_trade_markdown(account: dict[str, Any], events: list[dict[str, Any]]) -> str:
    summary = summarize_account(account)
    lines = [
        "#### 【本地模拟盘】模拟交易账户",
        "> 来源：本地 JSON 模拟账户，不是 JoinQuant dry-run / 模拟盘。",
        f"> 总资产{summary['equity']:.2f} | 现金{summary['cash']:.2f} | 持仓{summary['position_count']}只 | 已实现{summary['realized_pnl']:+.2f}",
        f"> 胜率{summary['win_rate']:.1f}% | 最大回撤{summary['max_drawdown_pct']:.2f}% | 闭环{summary['closed_trades']}笔",
    ]
    if summary["signal_stats"]:
        stats = []
        for key, item in list(summary["signal_stats"].items())[:3]:
            win_rate = item["wins"] / item["closed"] * 100 if item["closed"] else 0.0
            stats.append(f"{key}:{item['closed']}笔/{win_rate:.0f}%/{item['pnl']:+.0f}")
        lines.append(f"> 信号：{'；'.join(stats)}")
    if events:
        lines.append("#### 本轮模拟成交")
        for event in events[:6]:
            action = "买入" if event["action"] == "buy" else "卖出"
            pnl = f" | 盈亏{event.get('pnl', 0):+.2f}" if event["action"] == "sell" else ""
            lines.append(
                f"- {action} {event['code']} {event.get('name', '')} {event['qty']}股 @ {event['price']:.2f} | 成本{event.get('fees', 0):.2f}{pnl}"
            )
    else:
        lines.append("> 本轮无模拟成交，T+1、冷却或价格条件未满足。")
    lines.insert(2, f"> Fee schedule: {summary['fee_schedule_version']}")
    return "\n".join(lines)
