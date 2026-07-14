from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import config as app_config
from exit_policy import EXECUTION_PLAN_VERSION, build_buy_execution_plan, market_regime
from pre_trade_check import PortfolioState, RiskLimits, evaluate_observation
from trading_store import SignalConflictError, SignalRecord, StrategyRunRecord, TradingStore, canonical_json
from trade_safety import tradability_reject_reason


UNCATEGORIZED = "__UNCATEGORIZED__"


def clean_code(value: Any) -> str:
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def to_jq_code(code: Any) -> str:
    code = clean_code(code)
    if not code:
        return ""
    if code.startswith("6"):
        return f"{code}.XSHG"
    if code.startswith(("4", "8")):
        return f"{code}.XBJG"
    return f"{code}.XSHE"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _industry(row: pd.Series) -> str:
    return _text(row.get("industry") or row.get("sector"))


def _theme(row: pd.Series) -> str:
    return _text(row.get("theme") or row.get("theme_label") or row.get("concept") or _industry(row))


def _is_sell(row: pd.Series) -> bool:
    action = _text(row.get("signal_action")).lower()
    return action in {
        "sell", "stop_loss", "hard_stop", "take_profit", "take_profit_1",
        "trailing_stop", "time_stop",
    } or "sell" in action


def _has_holding(row: pd.Series) -> bool:
    if bool(row.get("has_holding")):
        return True
    return _text(row.get("hold_status")).lower() in {"holding", "partial_sell"}


def _has_valid_execution_plan(row: pd.Series) -> bool:
    entry = _num(row.get("entry_price"))
    stop = _num(row.get("stop_loss"))
    take = _num(row.get("take_profit"))
    position = _num(row.get("position_pct"))
    return (
        _text(row.get("execution_plan_version")) == EXECUTION_PLAN_VERSION
        and 0 < stop < entry < take
        and position > 0
    )


def _resolved_buy_plan(row: pd.Series) -> dict[str, Any]:
    version = _text(row.get("execution_plan_version"))
    entry = _num(row.get("entry_price"), _num(row.get("price")))
    stop = _num(row.get("stop_loss"))
    take = _num(row.get("take_profit"))
    position = _num(row.get("position_pct"))
    if (
        version == EXECUTION_PLAN_VERSION
        and entry > 0
        and 0 < stop < entry < take
        and position > 0
    ):
        result = {
            "version": version,
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": take,
            "risk_per_share": _num(row.get("risk_per_share"), entry - stop),
            "risk_reward": _num(row.get("risk_reward"), 2.0),
            "position_pct": position,
            "board_type": _text(row.get("board_type")),
            "market_regime": _text(row.get("market_regime")) or market_regime(_text(row.get("market_state"))),
        }
    else:
        plan = build_buy_execution_plan(
            code=clean_code(row.get("code")),
            entry_price=entry,
            support_price=_num(row.get("support_level")),
            atr14=_num(row.get("atr14")),
            position_cap_pct=position,
            market_state=_text(row.get("market_state")),
        )
        result = {
            "version": plan.version,
            "entry_price": plan.entry_price,
            "stop_loss": plan.stop_loss,
            "take_profit": plan.take_profit,
            "risk_per_share": plan.risk_per_share,
            "risk_reward": plan.risk_reward,
            "position_pct": plan.position_pct,
            "board_type": plan.board_type,
            "market_regime": plan.market_regime,
        }
    if not _industry(row) and not _theme(row):
        result["position_pct"] = min(
            float(result["position_pct"]),
            app_config.MAX_UNCATEGORIZED_POSITION_PCT,
        )
    return result


def _buy_reject_reason(row: pd.Series, min_score: float, allow_buy: bool = True, account_total_value: float = 0.0,
                       current_position_pct: float = 0.0, current_open_risk_pct: float = 0.0,
                       current_position_count: int = 0,
                       sector_exposure_pct: dict[str, float] | None = None,
                       theme_exposure_pct: dict[str, float] | None = None,
                       cooldown_codes: set[str] | None = None, available_cash: float | None = None,
                       new_positions_today: int = 0, orders_today: int = 0,
                       daily_turnover_pct: float = 0.0, daily_pnl_pct: float = 0.0,
                       account_drawdown_pct: float = 0.0, consecutive_losses: int = 0,
                       enforce_execution_contract: bool = False) -> str:
    if not allow_buy:
        return "buy_disabled"
    risk_enabled = app_config.JOINQUANT_PORTFOLIO_RISK_ENABLE_DEFAULT
    if current_position_count >= app_config.JOINQUANT_MAX_POSITIONS_DEFAULT:
        return "buy_max_positions"
    if risk_enabled:
        if new_positions_today >= app_config.MAX_NEW_POSITIONS_PER_DAY:
            return "buy_daily_new_positions_limit"
        if orders_today >= app_config.MAX_ORDERS_PER_DAY:
            return "buy_daily_orders_limit"
        if daily_turnover_pct >= app_config.MAX_DAILY_TURNOVER_PCT:
            return "buy_daily_turnover_limit"
        if daily_pnl_pct <= -app_config.DAILY_LOSS_WARN_PCT:
            return "buy_daily_loss_limit"
        if account_drawdown_pct <= -app_config.ACCOUNT_DRAWDOWN_WARN_PCT:
            return "buy_account_drawdown_limit"
        if consecutive_losses >= app_config.MAX_CONSECUTIVE_LOSSES:
            return "buy_consecutive_loss_limit"
    code = clean_code(row.get("code"))
    if app_config.JOINQUANT_EXIT_COOLDOWN_ENABLE_DEFAULT and code in (cooldown_codes or set()):
        return "buy_cooldown"
    price = _num(row.get("price"))
    entry = _num(row.get("entry_price"), price)
    take = _num(row.get("take_profit"))
    if not code or price <= 0 or entry <= 0:
        return "buy_invalid_price"
    if _is_sell(row):
        return "not_buy_sell_signal"
    has_execution_contract = _text(row.get("execution_plan_version")) == EXECUTION_PLAN_VERSION
    execution_allowed = row.get("execution_allowed")
    if enforce_execution_contract and (
        not has_execution_contract
        or execution_allowed is None
        or (isinstance(execution_allowed, float) and pd.isna(execution_allowed))
    ):
        return "buy_execution_plan_missing"
    if enforce_execution_contract and not _has_valid_execution_plan(row):
        return "buy_execution_plan_invalid"
    if execution_allowed is not None and str(execution_allowed).strip().lower() in {"0", "false", "no", "off"}:
        return "buy_risk_disallowed"
    if take > 0 and take <= entry:
        return "buy_invalid_take_profit"
    tradability_reason = tradability_reject_reason(row) if app_config.JOINQUANT_TRADABILITY_FILTER_ENABLE_DEFAULT else ""
    if tradability_reason:
        return tradability_reason
    regime = market_regime(_text(row.get("market_state")))
    if regime == "RISK_OFF":
        return "buy_disabled"
    required_score = max(min_score, 85.0) if regime == "CAUTION" else min_score
    if _num(row.get("final_score")) < required_score:
        return "buy_low_score"
    if _num(row.get("position_pct")) <= 0:
        return "buy_bad_position"
    if _num(row.get("pct_chg")) >= 9.8:
        return "buy_near_limit_up"
    if price < entry:
        return "buy_not_reached_entry"
    plan = _resolved_buy_plan(row)
    stop = float(plan["stop_loss"])
    adjusted_position_pct = float(plan["position_pct"])
    if stop <= 0 or stop >= entry:
        return "buy_invalid_stop_loss"
    sector = _industry(row)
    theme = _theme(row)
    if not sector and not theme:
        adjusted_position_pct = min(adjusted_position_pct, app_config.MAX_UNCATEGORIZED_POSITION_PCT)
        if risk_enabled and (sector_exposure_pct or {}).get(UNCATEGORIZED, 0) + adjusted_position_pct > app_config.MAX_UNCATEGORIZED_POSITION_PCT:
            return "buy_uncategorized_limit"
    added_risk = adjusted_position_pct * max(entry - stop, 0) / entry if entry > 0 else 0
    open_risk_limit = app_config.MAX_OPEN_RISK_CAUTION_PCT if regime == "CAUTION" else app_config.MAX_OPEN_RISK_NORMAL_PCT
    if risk_enabled and current_open_risk_pct + added_risk > open_risk_limit:
        return "buy_open_risk_limit"
    if risk_enabled and sector and (sector_exposure_pct or {}).get(sector, 0) + adjusted_position_pct > app_config.MAX_INDUSTRY_POSITION_PCT:
        return "buy_sector_limit"
    if risk_enabled and theme and (theme_exposure_pct or {}).get(theme, 0) + adjusted_position_pct > app_config.MAX_THEME_POSITION_PCT:
        return "buy_theme_limit"
    if account_total_value > 0:
        target_value = account_total_value * adjusted_position_pct / 100.0
        if available_cash is not None and target_value > available_cash:
            return "buy_insufficient_available_cash"
        if target_value < entry * 100:
            return "buy_too_small_for_board_lot"
        if current_position_pct + adjusted_position_pct > app_config.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT:
            return "buy_total_position_limit"
    return ""


def _can_buy(row: pd.Series, min_score: float, allow_buy: bool = True) -> bool:
    return _buy_reject_reason(row, min_score, allow_buy=allow_buy) == ""


def _base_payload(run_id: str | None, trade_date: str | None, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trade_date": trade_date or datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": run_id,
        "source": "a_share_strategy",
        "dry_run": dry_run,
        "signals": [],
    }


def _signal_id(run_id: str | None, code: str, action: str, index: int) -> str:
    prefix = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{code}-{action}-{index:04d}"


def _buy_signal(row: pd.Series, run_id: str | None, index: int) -> dict[str, Any]:
    code = clean_code(row.get("code"))
    price = _num(row.get("price"))
    plan = _resolved_buy_plan(row)
    entry = float(plan["entry_price"])
    atr14 = _num(row.get("atr14"))
    stop = float(plan["stop_loss"])
    take = float(plan["take_profit"])
    position_pct = float(plan["position_pct"])
    return {
        "id": _signal_id(run_id, code, "buy", index),
        "code": code,
        "jq_code": to_jq_code(code),
        "name": _text(row.get("name")),
        "action": "buy",
        "price": round(price, 2),
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2) if stop > 0 else None,
        "take_profit": round(take, 2) if take > 0 else None,
        "position_pct": position_pct,
        "execution_plan_version": str(plan["version"]),
        "final_score": round(_num(row.get("final_score")), 1),
        "enhanced_score": round(_num(row.get("enhanced_score")), 1) if _num(row.get("enhanced_score")) else None,
        "signal_type": _text(row.get("mode")) or _text(row.get("buy_state")) or "signal",
        "max_age_min": 5 if (_text(row.get("mode")) or "").lower() == "short" else 20,
        "reason": _text(row.get("risk_reason") or row.get("buy_reason") or row.get("entry_reason")),
        "atr14": round(atr14, 4) if atr14 > 0 else None,
        "board_type": str(plan["board_type"]),
        "market_regime": str(plan["market_regime"]),
        "industry": _industry(row),
        "theme": _theme(row),
    }


def _sell_signal(row: pd.Series, run_id: str | None, index: int) -> dict[str, Any] | None:
    code = clean_code(row.get("code"))
    price = _num(row.get("price"))
    if not code or price <= 0:
        return None
    signal = {
        "id": _text(row.get("exit_signal_id")) or _signal_id(run_id, code, "sell", index),
        "code": code,
        "jq_code": to_jq_code(code),
        "name": _text(row.get("name")),
        "action": "sell",
        "price": round(price, 2),
        "reason": _text(row.get("risk_reason") or row.get("signal_note") or row.get("buy_reason")),
    }
    if row.get("target_qty") is not None and not pd.isna(row.get("target_qty")):
        signal["target_qty"] = max(0, int(_num(row.get("target_qty"))))
    return signal


def export_signals(
    df: pd.DataFrame,
    run_id: str | None = None,
    trade_date: str | None = None,
    dry_run: bool | None = None,
    min_score: float | None = None,
    output_path: Path | None = None,
    ml_sample_path: Path | None = None,
    allow_buy: bool = True,
    allow_sell: bool = True,
    account_total_value: float = 0.0,
    current_position_pct: float = 0.0,
    current_position_count: int = 0,
    current_open_risk_pct: float = 0.0,
    sector_exposure_pct: dict[str, float] | None = None,
    theme_exposure_pct: dict[str, float] | None = None,
    cooldown_codes: set[str] | None = None,
    available_cash: float | None = None,
    new_positions_today: int = 0,
    orders_today: int = 0,
    daily_turnover_pct: float = 0.0,
    daily_pnl_pct: float = 0.0,
    account_drawdown_pct: float = 0.0,
    consecutive_losses: int = 0,
    enforce_execution_contract: bool = False,
    store: TradingStore | None = None,
) -> Path:
    dry_run = app_config.JOINQUANT_DRY_RUN_DEFAULT if dry_run is None else dry_run
    min_score = app_config.JOINQUANT_MIN_SCORE_DEFAULT if min_score is None else min_score
    output_path = output_path or app_config.JOINQUANT_SIGNAL_FILE
    payload = _base_payload(run_id, trade_date, dry_run)

    sample_rows: list[tuple[pd.Series, dict[str, Any]]] = []
    reject_reasons: Counter[str] = Counter()
    if df is not None and not df.empty:
        signals: list[dict[str, Any]] = []
        ordered_rows = [row for _, row in df.iterrows()]
        if not any(_is_sell(row) for row in ordered_rows):
            ordered_rows.sort(key=lambda row: -_num(row.get("final_score")))
        for index, row in enumerate(ordered_rows):
            buy_reject_reason = _buy_reject_reason(
                row, min_score, allow_buy=allow_buy, account_total_value=account_total_value,
                current_position_pct=current_position_pct, current_open_risk_pct=current_open_risk_pct,
                current_position_count=current_position_count,
                sector_exposure_pct=sector_exposure_pct,
                theme_exposure_pct=theme_exposure_pct,
                cooldown_codes=cooldown_codes,
                available_cash=available_cash,
                new_positions_today=new_positions_today, orders_today=orders_today,
                daily_turnover_pct=daily_turnover_pct, daily_pnl_pct=daily_pnl_pct,
                account_drawdown_pct=account_drawdown_pct,
                consecutive_losses=consecutive_losses,
                enforce_execution_contract=enforce_execution_contract,
            )
            if not buy_reject_reason:
                signal = _buy_signal(row, run_id, index)
                signals.append(signal)
                current_position_count += 1
                current_position_pct += float(signal.get("position_pct") or 0)
                entry = float(signal.get("entry_price") or 0)
                current_open_risk_pct += float(signal.get("position_pct") or 0) * max(
                    entry - float(signal.get("stop_loss") or entry), 0,
                ) / entry if entry > 0 else 0
                sector = _industry(row)
                if sector:
                    sector_exposure_pct = dict(sector_exposure_pct or {})
                    sector_exposure_pct[sector] = sector_exposure_pct.get(sector, 0) + float(signal.get("position_pct") or 0)
                theme = _theme(row)
                if theme:
                    theme_exposure_pct = dict(theme_exposure_pct or {})
                    theme_exposure_pct[theme] = theme_exposure_pct.get(theme, 0) + float(signal.get("position_pct") or 0)
                if not sector and not theme:
                    sector_exposure_pct = dict(sector_exposure_pct or {})
                    sector_exposure_pct[UNCATEGORIZED] = sector_exposure_pct.get(UNCATEGORIZED, 0) + float(signal.get("position_pct") or 0)
                if available_cash is not None:
                    available_cash -= account_total_value * float(signal.get("position_pct") or 0) / 100.0
                sample_rows.append((row, signal))
            elif _is_sell(row) and _has_holding(row) and allow_sell:
                sell = _sell_signal(row, run_id, index)
                if sell:
                    signals.append(sell)
                    sample_rows.append((row, sell))
            elif _is_sell(row) and _has_holding(row):
                reject_reasons["sell_disabled"] += 1
            elif _is_sell(row):
                reject_reasons["sell_without_holding"] += 1
            else:
                reject_reasons[buy_reject_reason] += 1
        payload["signals"] = signals
    payload["diagnostics"] = {
        "candidate_count": int(len(df)) if df is not None else 0,
        "allow_buy": bool(allow_buy),
        "allow_sell": bool(allow_sell),
        "max_positions": int(app_config.JOINQUANT_MAX_POSITIONS_DEFAULT),
        "max_total_position_pct": float(app_config.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT),
        "min_score": float(min_score),
        "account_total_value": float(account_total_value or 0.0),
        "reject_reasons": dict(reject_reasons),
        "ledger_ok": False,
        "ledger_signal_count": 0,
        "ledger_error": "",
        "buy_publication_blocked": False,
        "buy_enabled": "1",
        "kill_switch": "0",
    }

    store = store or TradingStore(app_config.TRADING_DB_FILE)
    limits = RiskLimits(
        max_single_position_pct=app_config.MAX_SINGLE_POSITION_PCT,
        max_total_position_pct=app_config.MAX_TOTAL_POSITION_PCT,
        min_cash_reserve_pct=app_config.MIN_CASH_RESERVE_PCT,
        max_sector_exposure_pct=app_config.MAX_SECTOR_EXPOSURE_PCT,
        max_new_positions_per_day=app_config.MAX_NEW_POSITIONS_PER_DAY,
        max_orders_per_day=app_config.MAX_ORDERS_PER_DAY,
        max_daily_turnover_pct=app_config.MAX_DAILY_TURNOVER_PCT,
        daily_loss_warn_pct=app_config.DAILY_LOSS_WARN_PCT,
        account_drawdown_warn_pct=app_config.ACCOUNT_DRAWDOWN_WARN_PCT,
    )
    decisions = [(signal, evaluate_observation(signal, PortfolioState.empty(), limits)) for signal in payload["signals"]]
    ledger_run_id = run_id or f"export-{payload['generated_at'].replace(' ', 'T')}"
    try:
        store.initialize()
        inserted_signal_count = 0
        with store.transaction() as conn:
            buy_row = conn.execute("SELECT value FROM system_state WHERE key='buy_enabled'").fetchone()
            kill_row = conn.execute("SELECT value FROM system_state WHERE key='kill_switch'").fetchone()
            buy_enabled = str(buy_row[0]) if buy_row else "1"
            kill_switch = str(kill_row[0]) if kill_row else "0"
            store.record_strategy_run(conn, StrategyRunRecord(
                run_id=ledger_run_id,
                trade_date=payload["trade_date"],
                started_at=payload["generated_at"],
                strategy_version="a_share_strategy",
                parameter_version="risk-observe-v1",
            ))
            for signal, decision in decisions:
                inserted_signal_count += int(store.record_signal(conn, SignalRecord(
                    signal_id=signal["id"], run_id=ledger_run_id,
                    trade_date=payload["trade_date"], code=signal["code"],
                    jq_code=signal["jq_code"], action=signal["action"],
                    position_pct=float(signal.get("position_pct") or 0),
                    generated_at=payload["generated_at"], expires_at="",
                    raw_json=canonical_json(signal),
                )))
                if signal["action"] == "sell":
                    store.upsert_exit_intent(
                        conn, signal["id"], signal["code"], int(signal.get("target_qty") or 0),
                        str(signal.get("reason") or "sell"), payload["generated_at"],
                    )
                metrics = decision.metrics
                conn.execute(
                    """INSERT INTO risk_decisions(
                    signal_id, risk_mode, allowed, hard_block_code, shadow_codes,
                    current_single_exposure, projected_single_exposure,
                    current_portfolio_exposure, projected_portfolio_exposure,
                    current_industry_exposure, projected_industry_exposure,
                    daily_profit_loss, account_drawdown, turnover_rate, snapshot_at,
                    raw_json, decided_at) VALUES (?, 'observe', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (signal["id"], int(decision.allowed), ",".join(decision.hard_blocks) or None,
                     json.dumps(decision.soft_warnings), metrics.get("position_pct") or 0,
                     metrics.get("position_pct") or 0, 0, metrics.get("total_position_pct") or 0,
                     0, metrics.get("sector_exposure_pct") or 0, metrics.get("daily_pnl_pct") or 0,
                     metrics.get("account_drawdown_pct") or 0, metrics.get("daily_turnover_pct") or 0,
                     payload["generated_at"], json.dumps({"hard_blocks": decision.hard_blocks,
                     "soft_warnings": decision.soft_warnings, "metrics": dict(metrics)}, default=str),
                     payload["generated_at"]),
                )
        payload["diagnostics"]["ledger_ok"] = True
        payload["diagnostics"]["ledger_signal_count"] = inserted_signal_count
        payload["diagnostics"]["buy_enabled"] = buy_enabled
        payload["diagnostics"]["kill_switch"] = kill_switch
        if kill_switch == "1":
            payload["diagnostics"]["buy_publication_blocked"] = any(
                signal["action"] == "buy" for signal in payload["signals"]
            )
            payload["signals"] = []
        elif buy_enabled == "0":
            had_buys = any(signal["action"] == "buy" for signal in payload["signals"])
            payload["signals"] = [signal for signal in payload["signals"] if signal["action"] == "sell"]
            payload["diagnostics"]["buy_publication_blocked"] = had_buys
    except (sqlite3.Error, OSError, SignalConflictError) as exc:
        had_buys = any(signal["action"] == "buy" for signal in payload["signals"])
        payload["signals"] = [signal for signal in payload["signals"] if signal["action"] == "sell"]
        payload["diagnostics"]["ledger_error"] = str(exc)
        payload["diagnostics"]["buy_publication_blocked"] = had_buys

    try:
        from ml_dataset import append_signal_samples

        published_ids = {signal["id"] for signal in payload["signals"]}
        append_signal_samples(
            [(row, signal) for row, signal in sample_rows if signal["id"] in published_ids],
            payload, ml_sample_path,
        )
    except Exception as exc:
        print(f"ML sample append skipped: {exc}", flush=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(output_path)
    return output_path


if __name__ == "__main__":
    demo = pd.DataFrame(
        [
            {
                "code": "600000",
                "name": "PF Bank",
                "price": 10.0,
                "entry_price": 10.0,
                "position_pct": 10,
                "final_score": 90,
                "signal_action": "continue",
            }
        ]
    )
    print(export_signals(demo))
