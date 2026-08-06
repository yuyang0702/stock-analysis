from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

import config as app_config
from execution_admission import AdmissionRequest, admit_candidate
from execution_contracts import (
    ExecutionIntent,
    InstrumentRules,
    QuoteSnapshot,
    StrategyOrderCandidate,
    UNCATEGORIZED as CONTRACT_UNCATEGORIZED,
    canonical_sha256,
    logical_signal_id,
)
from exit_policy import (
    EXECUTION_PLAN_VERSION,
    build_buy_execution_plan,
    market_regime,
    strict_market_regime,
    trade_risk_budget_pct,
    normalize_exit_action,
)
from notification_outbox import (
    NotificationEvent,
    notification_event_key,
    plan_version,
    redact_secret_text,
)
from pre_trade_check import PortfolioState, RiskLimits, RiskPolicy, evaluate_observation
from ml_contracts import canonical_hash
from ml_dataset import (
    FEATURE_COLUMNS,
    append_signal_samples,
    build_candidate_samples,
)
from ml_runtime import observe_candidate_samples, runtime_dependency_versions
from ml_store import MlCapacityError, MlDataConflict, MlStore
from trading_store import SignalConflictError, SignalRecord, StrategyRunRecord, TradingStore, canonical_json
from trade_safety import tradability_reject_reason
from gap_reentry import (
    GapReentryDecision, GapReentryInput, estimated_limit_up_price, evaluate_gap_reentry,
    minimum_lot_position,
)


UNCATEGORIZED = "__UNCATEGORIZED__"
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
PRICE_TICK = Decimal("0.01")
EXACT_STRATEGY_ID = "a_share_strategy"
EXACT_STRATEGY_VERSION = f"a_share_strategy:{EXECUTION_PLAN_VERSION}"
IMPLEMENTATION_HASH_FILES = (
    "a_share_strategy.py",
    "candidate_core.py",
    "joinquant_exporter.py",
    "ml_dataset.py",
    "trade_safety.py",
    "exit_policy.py",
    "trading_store.py",
    "gap_reentry.py",
    "config.py",
)

_REJECTION_STAGES = {
    "score": {"buy_low_score"},
    "tradability": {
        "buy_suspended", "buy_st", "buy_delisting", "buy_special_listing_stage",
        "buy_quote_stale", "buy_chasing", "buy_illiquid", "buy_invalid_price",
        "buy_near_limit_up",
        "gap_reentry_locked_limit", "gap_reentry_open_observing",
        "gap_reentry_resealed", "gap_reentry_too_far", "gap_reentry_falling",
        "gap_reentry_attempts_exhausted", "gap_reentry_too_late",
        "gap_reentry_parent_invalid", "gap_reentry_current_score_low",
        "gap_reentry_quote_stale",
    },
    "risk": {
        "buy_disabled", "buy_max_positions", "buy_daily_new_positions_limit",
        "buy_daily_orders_limit", "buy_daily_turnover_limit", "buy_daily_loss_limit",
        "buy_account_drawdown_limit", "buy_consecutive_loss_limit", "buy_cooldown",
        "buy_risk_disallowed", "buy_bad_position", "buy_open_risk_limit",
        "buy_sector_limit", "buy_theme_limit", "buy_uncategorized_limit",
        "buy_insufficient_available_cash", "buy_total_position_limit",
        "buy_too_small_for_board_lot", "buy_single_position_limit",
        "gap_reentry_min_lot_risk_exceeded", "gap_reentry_insufficient_cash",
        "gap_reentry_per_trade_risk_exceeded",
        "gap_reentry_portfolio_open_risk_exceeded",
        "gap_reentry_per_trade_and_portfolio_risk_exceeded",
        "gap_reentry_fee_schedule_required",
        "gap_reentry_pending_order", "gap_reentry_current_risk_disallowed",
        "gap_reentry_state_unavailable",
    },
    "execution": {
        "not_buy_sell_signal", "buy_execution_plan_missing", "buy_execution_plan_invalid",
        "buy_invalid_take_profit", "buy_invalid_stop_loss", "buy_not_reached_entry",
        "gap_reentry_rr_invalid",
    },
}


def _confirmed_gap_reentry(row: pd.Series) -> bool:
    return (
        app_config.GAP_REENTRY_ENABLE_DEFAULT
        and _text(row.get("entry_path")) == "gap_reentry"
        and _text(row.get("gap_reentry_state")) == "OPEN_CONFIRMED"
        and bool(_text(row.get("parent_signal_id")))
    )


def rejection_stage(reason: str) -> str:
    if not reason:
        return "selected"
    for stage, reasons in _REJECTION_STAGES.items():
        if reason in reasons:
            return stage
    raise ValueError(f"UNKNOWN_REJECTION_CODE: {reason}")


def _ml_decision_at(generated_at: str) -> str:
    value = datetime.fromisoformat(generated_at)
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI_TIMEZONE)
    return value.astimezone(SHANGHAI_TIMEZONE).isoformat()


def _prepare_gap_reentry_row(
    row: pd.Series,
    *,
    store: TradingStore,
    run_id: str | None,
    trade_date: str,
    generated_at: str,
    min_score: float,
    allow_buy: bool,
) -> pd.Series:
    if not app_config.GAP_REENTRY_ENABLE_DEFAULT or not allow_buy or _is_sell(row):
        return row
    code = clean_code(row.get("code"))
    existing = store.get_gap_reentry_for_stock_date(trade_date, code)
    if existing and _text(existing.get("new_signal_id")):
        prepared = row.copy()
        prepared["entry_path"] = "gap_reentry"
        prepared["gap_reentry_state"] = "ALREADY_PUBLISHED"
        prepared["gap_reentry_reason"] = "gap_reentry_pending_order"
        prepared["parent_signal_id"] = existing["parent_signal_id"]
        prepared["gap_reentry_opportunity_id"] = existing["opportunity_id"]
        prepared["reentry_cap_price"] = existing["reentry_cap_price"]
        prepared["gap_reentry_transitioned"] = False
        return prepared
    parent = store.latest_prior_buy_signal(code, generated_at)
    if not parent:
        return row
    try:
        parent_age = (
            datetime.fromisoformat(generated_at).date()
            - datetime.fromisoformat(_text(parent.get("_ledger_generated_at"))).date()
        ).days
    except ValueError:
        return row
    if parent_age < 1 or parent_age > 14 or bool(row.get("corporate_action")):
        return row
    original_entry = _num(parent.get("entry_price"))
    original_stop = _num(parent.get("stop_loss"))
    price = _num(row.get("price"))
    if min(original_entry, original_stop, price) <= 0 or price <= original_entry:
        return row
    limit_up = _num(row.get("limit_up_price"))
    if limit_up <= 0:
        limit_up = estimated_limit_up_price(code, _num(row.get("prev_close")))
    if limit_up <= 0:
        return row
    locked = price >= limit_up - 0.005
    prior_state = _text((existing or {}).get("state"))
    resealed = locked and prior_state == "OPEN_OBSERVING"
    attempts = int((existing or {}).get("attempt_count") or 0)
    first_open_at = _text((existing or {}).get("first_open_at"))
    first_open_price = _num((existing or {}).get("first_open_price"))
    if not locked and prior_state in {"", "LOCKED_LIMIT", "RESEALED"}:
        attempts += 1
        first_open_at = ""
        first_open_price = 0.0
    normalized_market_state = strict_market_regime(_text(row.get("market_state")))
    try:
        decision = evaluate_gap_reentry(GapReentryInput(
            trade_date=trade_date,
            code=code,
            parent_signal_id=_text(parent.get("id")),
            batch_id=run_id or generated_at,
            now=generated_at,
            price=price,
            limit_up_price=limit_up,
            original_entry_price=original_entry,
            original_stop_price=original_stop,
            market_state=normalized_market_state,
            current_score=_num(row.get("final_score")),
            required_score=max(min_score, 85.0) if normalized_market_state == "CAUTION" else min_score,
            quote_age_sec=_num(row.get("quote_age_sec")),
            first_open_at=first_open_at,
            first_open_price=first_open_price,
            first_batch_id=_text((existing or {}).get("first_batch_id")),
            confirmation_count=int((existing or {}).get("confirmation_count") or 0),
            attempt_count=attempts,
            at_limit=locked,
            resealed=resealed,
            buy_enabled=allow_buy,
        ))
    except (TypeError, ValueError):
        decision = GapReentryDecision("INELIGIBLE", "gap_reentry_parent_invalid")
    opportunity_id = _text((existing or {}).get("opportunity_id")) or (
        f"gap-{trade_date.replace('-', '')}-{code}-{_text(parent.get('id'))[:12]}"
    )
    if decision.state == "OPEN_OBSERVING" and not first_open_at:
        first_open_at, first_open_price = generated_at, price
    event = {
        "opportunity_id": opportunity_id, "trade_date": trade_date,
        "stock_code": code, "parent_signal_id": _text(parent.get("id")),
        "state": decision.state, "reason": decision.reason,
        "original_entry_price": original_entry, "original_stop_price": original_stop,
        "original_risk_r": original_entry - original_stop,
        "reentry_cap_price": decision.cap_price, "first_open_at": first_open_at or None,
        "first_open_price": first_open_price or None,
        "first_batch_id": (
            (run_id or generated_at) if decision.state == "OPEN_OBSERVING"
            and not _text((existing or {}).get("first_batch_id"))
            else _text((existing or {}).get("first_batch_id")) or None
        ),
        "confirmation_count": decision.confirmation_count,
        "attempt_count": decision.attempt_count,
    }
    prepared = row.copy()
    prepared["entry_path"] = "gap_reentry"
    prepared["gap_reentry_state"] = decision.state
    prepared["gap_reentry_reason"] = decision.reason
    prepared["parent_signal_id"] = parent["id"]
    prepared["gap_reentry_opportunity_id"] = opportunity_id
    prepared["original_entry_price"] = original_entry
    prepared["original_stop_price"] = original_stop
    prepared["reentry_cap_price"] = decision.cap_price
    prepared["gap_reentry_transitioned"] = prior_state != decision.state
    if decision.allowed:
        stop = max(original_stop, _num(row.get("stop_loss")))
        risk = price - stop
        if risk <= 0:
            event.update({"state": "RISK_REJECTED", "reason": "gap_reentry_rr_invalid"})
            prepared["gap_reentry_state"] = "RISK_REJECTED"
            prepared["gap_reentry_reason"] = "gap_reentry_rr_invalid"
        else:
            prepared["gap_reentry_state"] = "OPEN_CONFIRMED"
            prepared["gap_reentry_reason"] = ""
            prepared["entry_price"] = price
            prepared["stop_loss"] = stop
            prepared["take_profit"] = price + 2 * risk
            event.update({
                "planned_entry_price": price, "planned_stop_price": stop,
                "planned_take_profit": price + 2 * risk,
            })
    with store.transaction() as conn:
        store.upsert_gap_reentry_opportunity(conn, event)
    return prepared


@lru_cache(maxsize=1)
def _ml_code_hash() -> str:
    digest = hashlib.sha256()
    for name in IMPLEMENTATION_HASH_FILES:
        path = Path(__file__).with_name(name)
        digest.update(name.encode("utf-8"))
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            content = b"<missing>"
        digest.update(content)
    return digest.hexdigest()


def _ml_parameter_snapshot(
    min_score: float,
    enforce_execution_contract: bool,
) -> dict[str, float | int | bool]:
    return {
        "min_score": float(min_score),
        "caution_min_score": 85.0,
        "near_limit_up_pct": 9.8,
        "board_lot_size": 100,
        "special_listing_days": 5,
        "quote_stale_sec": 120,
        "chasing_max_pct": 0.02,
        "chasing_atr_multiplier": 0.5,
        "min_tradable_amount": 20_000_000,
        "enforce_execution_contract": bool(enforce_execution_contract),
        "portfolio_risk_enabled": bool(app_config.JOINQUANT_PORTFOLIO_RISK_ENABLE_DEFAULT),
        "max_positions": int(app_config.JOINQUANT_MAX_POSITIONS_DEFAULT),
        "max_new_positions_per_day": int(app_config.MAX_NEW_POSITIONS_PER_DAY),
        "max_orders_per_day": int(app_config.MAX_ORDERS_PER_DAY),
        "max_daily_turnover_pct": float(app_config.MAX_DAILY_TURNOVER_PCT),
        "daily_loss_warn_pct": float(app_config.DAILY_LOSS_WARN_PCT),
        "account_drawdown_warn_pct": float(app_config.ACCOUNT_DRAWDOWN_WARN_PCT),
        "max_consecutive_losses": int(app_config.MAX_CONSECUTIVE_LOSSES),
        "exit_cooldown_enabled": bool(app_config.JOINQUANT_EXIT_COOLDOWN_ENABLE_DEFAULT),
        "tradability_filter_enabled": bool(app_config.JOINQUANT_TRADABILITY_FILTER_ENABLE_DEFAULT),
        "max_uncategorized_position_pct": float(app_config.MAX_UNCATEGORIZED_POSITION_PCT),
        "max_open_risk_caution_pct": float(app_config.MAX_OPEN_RISK_CAUTION_PCT),
        "max_open_risk_normal_pct": float(app_config.MAX_OPEN_RISK_NORMAL_PCT),
        "max_industry_position_pct": float(app_config.MAX_INDUSTRY_POSITION_PCT),
        "max_theme_position_pct": float(app_config.MAX_THEME_POSITION_PCT),
        "max_total_position_pct": float(app_config.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT),
    }


def _finalize_candidate_decisions(
    decisions: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    allow_buy: bool,
    allow_sell: bool,
) -> None:
    diagnostics = payload["diagnostics"]
    published_ids = {signal["id"] for signal in payload["signals"]}
    kill_switch = diagnostics["kill_switch"] == "1"
    buy_disabled = diagnostics["buy_enabled"] == "0" or not allow_buy
    controlled_batch = kill_switch or buy_disabled
    for decision in decisions:
        signal_id = decision.get("signal_id")
        if decision["is_sell"]:
            if not decision["has_holding"]:
                action = "sell_rejected_no_holding"
            elif not allow_sell:
                action = "sell_blocked_disabled"
            elif signal_id in published_ids:
                action = "sell_published"
            elif kill_switch:
                action = "sell_blocked_kill_switch"
            else:
                action = "rule_rejected"
            eligible = False
        else:
            reason = decision["rejection_code"]
            if reason:
                action = (
                    "buy_blocked_disabled"
                    if reason == "buy_disabled" and not allow_buy
                    else "rule_rejected"
                )
            elif signal_id in published_ids:
                action = "buy_published"
            elif kill_switch:
                action = "buy_blocked_kill_switch"
            elif buy_disabled:
                action = "buy_blocked_disabled"
            else:
                action = "rule_rejected"
            eligible = not controlled_batch and action in {"buy_published", "rule_rejected"}
        decision["final_action"] = action
        decision["training_eligible"] = eligible


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
        "trailing_stop", "time_stop", "market_risk_exit",
    } or "sell" in action


_EXIT_NOTIFICATION_STAGES = {
    "hard_stop", "market_risk_exit", "trailing_stop",
    "take_profit_1", "time_stop",
}


def _exit_notification_stage(*values: object) -> str:
    for value in values:
        text = _text(value).lower().replace("-", "_")
        if text == "take_profit":
            text = "take_profit_1"
        stage = normalize_exit_action(text)
        if stage in _EXIT_NOTIFICATION_STAGES:
            return stage
    return "sell"


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
    if _confirmed_gap_reentry(row) and int(_num(row.get("target_qty"))) != 100:
        result["position_pct"] = float(result["position_pct"]) / 3.0
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
    try:
        current_open_risk_pct = float(current_open_risk_pct)
    except (TypeError, ValueError):
        return "buy_risk_disallowed"
    if not math.isfinite(current_open_risk_pct) or current_open_risk_pct < 0:
        return "buy_risk_disallowed"
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
    if (
        app_config.GAP_REENTRY_ENABLE_DEFAULT
        and _text(row.get("entry_path")) == "gap_reentry"
        and _text(row.get("gap_reentry_state")) != "OPEN_CONFIRMED"
    ):
        return _text(row.get("gap_reentry_reason")) or "gap_reentry_parent_invalid"
    gap_reentry = _confirmed_gap_reentry(row)
    strict_regime = strict_market_regime(_text(row.get("market_state")))
    if gap_reentry and not strict_regime:
        return "gap_reentry_current_risk_disallowed"
    regime = strict_regime if gap_reentry else market_regime(_text(row.get("market_state")))
    if regime == "RISK_OFF":
        return "buy_disabled"
    if gap_reentry and price > _num(row.get("reentry_cap_price")):
        return "gap_reentry_too_far"
    tradability_reason = tradability_reject_reason(row) if app_config.JOINQUANT_TRADABILITY_FILTER_ENABLE_DEFAULT else ""
    if gap_reentry and tradability_reason == "buy_chasing":
        tradability_reason = ""
    if tradability_reason:
        return tradability_reason
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
    open_risk_limit = app_config.MAX_OPEN_RISK_CAUTION_PCT if regime == "CAUTION" else app_config.MAX_OPEN_RISK_NORMAL_PCT
    if account_total_value > 0 and account_total_value * adjusted_position_pct / 100.0 <= entry * 100:
        if not gap_reentry:
            return "buy_too_small_for_board_lot"
        plan_board = _text(plan.get("board_type"))
        if plan_board not in {"main_low", "main_active", "growth"}:
            return "buy_execution_plan_invalid"
        lot = minimum_lot_position(
            entry_price=entry, stop_price=stop, account_value=account_total_value,
            available_cash=float(available_cash or 0),
            per_trade_risk_yuan=Decimal(str(
                account_total_value
                * trade_risk_budget_pct(plan_board, regime)
                / 100.0
            )),
            remaining_open_risk_yuan=Decimal(str(
                account_total_value
                * max(0.0, open_risk_limit - current_open_risk_pct)
                / 100.0
            )),
            current_position_pct=current_position_pct,
            max_total_position_pct=app_config.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT,
            rules=InstrumentRules.a_share(clean_code(row.get("code"))),
            max_single_position_pct=app_config.MAX_SINGLE_POSITION_PCT,
            fees=app_config.SIMULATION_FEE_SCHEDULE,
        )
        if not lot.allowed:
            return lot.reason
        row["gap_reentry_open_risk_pct"] = lot.risk_pct
        row["gap_reentry_cash_required_yuan"] = lot.cash_required_yuan
        adjusted_position_pct = float(lot.position_pct)
        row["position_pct"] = adjusted_position_pct
        row["target_qty"] = lot.qty
    sector = _industry(row)
    theme = _theme(row)
    if not sector and not theme:
        adjusted_position_pct = min(adjusted_position_pct, app_config.MAX_UNCATEGORIZED_POSITION_PCT)
        if risk_enabled and (sector_exposure_pct or {}).get(UNCATEGORIZED, 0) + adjusted_position_pct > app_config.MAX_UNCATEGORIZED_POSITION_PCT:
            return "buy_uncategorized_limit"
    added_risk = adjusted_position_pct * max(entry - stop, 0) / entry if entry > 0 else 0
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


def _execution_now() -> str:
    return datetime.now(SHANGHAI_TIMEZONE).isoformat(timespec="seconds")


def _decimal_value(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite Decimal")
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError(f"{name} must be a finite positive Decimal")
    return result


def _fraction(value: object) -> Decimal:
    return _decimal_value(value, "percentage") / Decimal("100")


def _aware_time(value: object, name: str) -> datetime:
    text = _text(value).replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result.astimezone(SHANGHAI_TIMEZONE)


def _limit_fraction(code: str, name: str) -> Decimal:
    upper_name = name.upper()
    if "ST" in upper_name:
        return Decimal("0.05")
    if code.startswith(("300", "301", "688")):
        return Decimal("0.20")
    if code.startswith(("4", "8")):
        return Decimal("0.30")
    return Decimal("0.10")


def _price_limits(row: pd.Series, code: str) -> tuple[Decimal | None, Decimal | None]:
    up = _num(row.get("limit_up_price") or row.get("high_limit"))
    down = _num(row.get("limit_down_price") or row.get("low_limit"))
    previous_close = _num(row.get("prev_close"))
    if previous_close > 0:
        base = _decimal_value(previous_close, "prev_close", positive=True)
        fraction = _limit_fraction(code, _text(row.get("name")))
        up = up or float((base * (Decimal("1") + fraction)).quantize(
            PRICE_TICK, rounding=ROUND_HALF_UP,
        ))
        down = down or float((base * (Decimal("1") - fraction)).quantize(
            PRICE_TICK, rounding=ROUND_HALF_UP,
        ))
    return (
        _decimal_value(up, "limit_up_price", positive=True) if up > 0 else None,
        _decimal_value(down, "limit_down_price", positive=True) if down > 0 else None,
    )


def _buy_price_cap(row: pd.Series, signal: dict[str, Any]) -> Decimal:
    if signal.get("entry_path") == "gap_reentry":
        raw_cap = signal.get("reentry_cap_price")
    else:
        entry = _decimal_value(signal.get("entry_price"), "entry_price", positive=True)
        atr = _decimal_value(signal.get("atr14") or 0, "atr14")
        max_move = min(
            Decimal("0.02"),
            atr / entry / Decimal("2") if atr > 0 else Decimal("0.02"),
        )
        raw_cap = entry * (Decimal("1") + max_move)
    return _decimal_value(raw_cap, "buy_price_cap", positive=True).quantize(
        PRICE_TICK, rounding=ROUND_DOWN,
    )


def _primary_execution_context(store: TradingStore) -> tuple[str, str, Decimal | None]:
    with store.connect() as conn:
        scope = conn.execute(
            """SELECT account_scope_id FROM account_scopes
               WHERE adapter='joinquant' AND scope_alias='primary'"""
        ).fetchone()
        regime = conn.execute(
            "SELECT value FROM system_state WHERE key='market_regime'"
        ).fetchone()
        snapshot = (
            store.load_current_broker_snapshot(conn, str(scope[0]))
            if scope is not None else None
        )
    if scope is None:
        raise ValueError("JoinQuant primary account scope is unavailable")
    normalized_regime = str(regime[0]).strip().upper() if regime else ""
    if normalized_regime not in {"NORMAL", "CAUTION", "RISK_OFF"}:
        raise ValueError("confirmed market regime is unavailable")
    return (
        str(scope[0]), normalized_regime,
        snapshot.total_equity if snapshot is not None else None,
    )


def _exact_candidate(
    row: pd.Series,
    signal: dict[str, Any],
    *,
    account_scope_id: str,
    trade_date: str,
    run_id: str,
    parameter_version: str,
    account_equity: Decimal | None,
) -> StrategyOrderCandidate:
    code = str(signal["code"])
    entry = _decimal_value(signal.get("entry_price"), "entry_price", positive=True)
    stop = _decimal_value(signal.get("stop_loss"), "stop_loss", positive=True)
    target = _decimal_value(signal.get("take_profit"), "take_profit", positive=True)
    price_cap = _buy_price_cap(row, signal)
    _, limit_down = _price_limits(row, code)
    if limit_down is None:
        fallback = entry * (Decimal("1") - _limit_fraction(code, str(signal.get("name") or "")))
        limit_down = min(stop, fallback).quantize(PRICE_TICK, rounding=ROUND_DOWN)
    signal_time = _aware_time(row.get("quote_time"), "quote_time")
    max_age = max(1, int(signal.get("max_age_min") or app_config.JOINQUANT_MAX_SIGNAL_AGE_MIN_DEFAULT))
    session_close = signal_time.replace(hour=15, minute=0, second=0, microsecond=0)
    frozen_valid_until = min(
        signal_time + timedelta(minutes=max_age), session_close,
    )
    setup_type = str(
        signal.get("entry_path") or signal.get("signal_type") or "rule_buy"
    )
    source_signal_id = str(signal["id"])
    industry = _industry(row) or CONTRACT_UNCATEGORIZED
    theme = _theme(row) or CONTRACT_UNCATEGORIZED
    rule_position_cap_fraction = _fraction(signal.get("position_pct"))
    if _confirmed_gap_reentry(row) and int(_num(signal.get("target_qty"))) == 100:
        if account_equity is not None and account_equity > 0:
            rule_position_cap_fraction = min(
                Decimal("1"),
                (price_cap * Decimal("100") / account_equity).quantize(
                    Decimal("0.000000000000000001"), rounding=ROUND_CEILING,
                ),
            )
        else:
            rule_position_cap_fraction = min(
                Decimal("1"), rule_position_cap_fraction * price_cap / entry,
            )
    return StrategyOrderCandidate(
        candidate_id="jqc-" + canonical_sha256({
            "account_scope_id": account_scope_id,
            "source_signal_id": source_signal_id,
        })[:28],
        logical_signal_id=logical_signal_id(
            account_scope_id, trade_date, EXACT_STRATEGY_ID,
            EXACT_STRATEGY_VERSION, code, "buy", setup_type,
        ),
        account_scope_id=account_scope_id,
        source_signal_id=source_signal_id,
        source_run_id=run_id,
        strategy_id=EXACT_STRATEGY_ID,
        strategy_version=EXACT_STRATEGY_VERSION,
        parameter_version=parameter_version,
        model_version="rule-baseline",
        fee_schedule_version=app_config.SIMULATION_FEE_SCHEDULE.version,
        code=code,
        side="buy",
        setup_type=setup_type,
        suggested_entry_price=entry,
        stop_price=stop,
        target_price=target,
        signal_time=signal_time.isoformat(timespec="seconds"),
        frozen_valid_until=frozen_valid_until.isoformat(timespec="seconds"),
        industry=industry,
        theme=theme,
        uncategorized=CONTRACT_UNCATEGORIZED in {industry, theme},
        buy_gap_price=limit_down,
        buy_price_cap=price_cap,
        requested_target_position_qty=None,
        exit_owner_id=None,
        exit_action=None,
        exit_priority=None,
        sell_limit_price=None,
        sell_price_floor=None,
        rule_position_cap_fraction=rule_position_cap_fraction,
    )


def _freeze_candidate_valid_until(
    store: TradingStore,
    candidate: StrategyOrderCandidate,
    trade_date: str,
) -> StrategyOrderCandidate:
    existing = store.get_logical_signal_plan(
        candidate.account_scope_id,
        trade_date,
        candidate.logical_signal_id,
    )
    if existing is None:
        return candidate
    return replace(
        candidate,
        frozen_valid_until=str(existing["frozen_valid_until"]),
        payload_sha256="",
    )


def _exact_quote_and_rules(
    row: pd.Series, candidate: StrategyOrderCandidate,
) -> tuple[QuoteSnapshot, InstrumentRules]:
    quote_time = _aware_time(row.get("quote_time"), "quote_time")
    last_price = _decimal_value(row.get("price"), "price", positive=True).quantize(
        PRICE_TICK, rounding=ROUND_HALF_UP,
    )
    limit_up, limit_down = _price_limits(row, candidate.code)
    optional_prices = {}
    for field, aliases in {
        "bid_price": ("bid_price", "bid"),
        "ask_price": ("ask_price", "ask"),
    }.items():
        raw = next((_num(row.get(alias)) for alias in aliases if _num(row.get(alias)) > 0), 0)
        optional_prices[field] = (
            _decimal_value(raw, field, positive=True).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
            if raw > 0 else None
        )
    suspended = any(
        str(row.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}
        for key in ("paused", "suspended", "is_suspended")
    )
    quote = QuoteSnapshot.from_values(
        code=candidate.code,
        quote_time=quote_time.isoformat(timespec="seconds"),
        last_price=last_price,
        bid_price=optional_prices["bid_price"],
        ask_price=optional_prices["ask_price"],
        limit_up_price=limit_up,
        limit_down_price=limit_down,
        suspended=suspended,
    )
    session_close = quote_time.replace(hour=15, minute=0, second=0, microsecond=0)
    name = str(row.get("name") or "")
    exchange = "XSHG" if candidate.code.startswith("6") else (
        "XBJG" if candidate.code.startswith(("4", "8")) else "XSHE"
    )
    rules = InstrumentRules.a_share(
        candidate.code,
        exchange=exchange,
        board=str(row.get("board_type") or "a_share"),
        source="live-spot-a-share-v1",
        as_of=quote_time.isoformat(timespec="seconds"),
        valid_until=session_close.isoformat(timespec="seconds"),
        limit_up_price=limit_up,
        limit_down_price=limit_down,
        suspended=suspended,
        special_status="st" if "ST" in name.upper() else "normal",
    )
    return quote, rules


def _exact_policy(
    signal: dict[str, Any], *, checked_at: str, market_regime: str,
    parameter_version: str,
) -> RiskPolicy:
    board = str(signal.get("board_type") or "main_active")
    return RiskPolicy(
        checked_at=checked_at,
        policy_version="joinquant-exact-v1:" + parameter_version[-12:],
        mode=app_config.RISK_MODE,
        adapter="joinquant",
        market_regime=market_regime,
        fee_schedule=app_config.SIMULATION_FEE_SCHEDULE,
        signal_max_age_sec=max(60, int(signal.get("max_age_min") or 20) * 60),
        broker_snapshot_max_age_sec=app_config.ACCOUNT_SNAPSHOT_MAX_AGE_SEC,
        quote_max_age_sec=120,
        decision_ttl_sec=app_config.JOINQUANT_EXECUTION_INTENT_TTL_SEC_DEFAULT,
        per_trade_risk_fraction=_fraction(trade_risk_budget_pct(board, market_regime)),
        normal_per_trade_risk_fraction=_fraction(trade_risk_budget_pct(board, "NORMAL")),
        risk_cap_yuan=None,
        max_positions=app_config.JOINQUANT_MAX_POSITIONS_DEFAULT,
        max_single_position_fraction=_fraction(app_config.MAX_SINGLE_POSITION_PCT),
        max_total_position_fraction=_fraction(app_config.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT),
        min_cash_reserve_fraction=_fraction(app_config.MIN_CASH_RESERVE_PCT),
        max_industry_fraction=_fraction(app_config.MAX_INDUSTRY_POSITION_PCT),
        max_theme_fraction=_fraction(app_config.MAX_THEME_POSITION_PCT),
        max_uncategorized_fraction=_fraction(app_config.MAX_UNCATEGORIZED_POSITION_PCT),
        max_open_risk_fraction=_fraction(
            app_config.MAX_OPEN_RISK_CAUTION_PCT
            if market_regime == "CAUTION" else app_config.MAX_OPEN_RISK_NORMAL_PCT
        ),
        normal_max_open_risk_fraction=_fraction(app_config.MAX_OPEN_RISK_NORMAL_PCT),
        max_new_positions_per_day=app_config.MAX_NEW_POSITIONS_PER_DAY,
        max_orders_per_day=app_config.MAX_ORDERS_PER_DAY,
        max_daily_turnover_fraction=_fraction(app_config.MAX_DAILY_TURNOVER_PCT),
        max_daily_loss_fraction=_fraction(app_config.DAILY_LOSS_WARN_PCT),
        max_account_drawdown_fraction=_fraction(app_config.ACCOUNT_DRAWDOWN_WARN_PCT),
        max_consecutive_losses=app_config.MAX_CONSECUTIVE_LOSSES,
    )


def _publish_exact_intent(signal: dict[str, Any], intent: ExecutionIntent) -> None:
    result = intent.pre_trade_result
    candidate = result.candidate
    fee = result.execution_fee
    if fee is None:
        raise ValueError("allowed buy intent is missing execution fee evidence")
    required_cash = fee.notional_yuan + fee.total_yuan
    signal.update({
        "account_scope_id": intent.account_scope_id,
        "target_qty": intent.target_position_qty,
        "target_position": intent.target_position_qty,
        "order_qty": intent.order_qty,
        "expected_current_qty": intent.expected_current_qty,
        "client_order_id": intent.client_order_id,
        "logical_signal_id": intent.logical_signal_id,
        "execution_intent_sha256": intent.intent_sha256,
        "pre_trade_result_id": intent.pre_trade_result_id,
        "pre_trade_result_sha256": intent.pre_trade_result_sha256,
        "broker_snapshot_id": intent.broker_snapshot_id,
        "broker_snapshot_sha256": intent.broker_snapshot_sha256,
        "quote_snapshot_id": intent.quote_snapshot_id,
        "quote_snapshot_sha256": intent.quote_snapshot_sha256,
        "instrument_rules_sha256": intent.instrument_rules_sha256,
        "fee_schedule_version": intent.fee_schedule_version,
        "fee_schedule_sha256": intent.fee_schedule_sha256,
        "parameter_version": intent.parameter_version,
        "model_version": intent.model_version,
        "strategy_version": intent.strategy_version,
        "signal_time": intent.signal_time,
        "frozen_valid_until": candidate.frozen_valid_until,
        "price_cap": float(intent.price_cap or intent.limit_price),
        "limit_price": float(intent.limit_price) if intent.limit_price is not None else None,
        "expires_at": intent.expires_at,
        "required_cash_yuan": float(required_cash),
    })


def _enqueue_buy_plan_notification(
    store: TradingStore,
    conn: object,
    account_scope_id: str,
    trade_date: str,
    signal: dict[str, Any],
) -> str | None:
    if signal.get("action") != "buy":
        return None
    logical_id = str(signal["logical_signal_id"])
    existing = conn.execute(
        """SELECT frozen_valid_until, current_plan_version
           FROM logical_signal_plans
           WHERE account_scope_id=? AND trade_date=? AND logical_signal_id=?""",
        (account_scope_id, trade_date, logical_id),
    ).fetchone()
    frozen = store._shanghai_timestamp(
        existing["frozen_valid_until"] if existing else signal["frozen_valid_until"],
        "frozen_valid_until",
    )
    entry_tick = _decimal_value(
        signal.get("limit_price") or signal.get("price_cap"),
        "entry_tick",
        positive=True,
    ).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    stop_tick = _decimal_value(
        signal.get("stop_price") or signal.get("stop_loss"),
        "stop_tick",
        positive=True,
    ).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    version = plan_version(
        str(signal["code"]),
        "buy",
        int(signal["order_qty"]),
        int(signal["target_position"]),
        entry_tick,
        stop_tick,
        frozen,
        str(signal["strategy_version"]),
        str(signal["parameter_version"]),
    )
    event_key = notification_event_key(
        "joinquant",
        account_scope_id,
        "buy-plan",
        trade_date=trade_date,
        logical_signal_id=logical_id,
        plan_version=version,
    )
    signal.update({
        "account_scope_id": account_scope_id,
        "trade_date": trade_date,
        "logical_signal_id": logical_id,
        "frozen_valid_until": frozen,
        "plan_version": version,
        "buy_plan_event_key": event_key,
    })
    if existing is not None and str(existing["current_plan_version"]) == version:
        return event_key

    occurred_at = store._shanghai_timestamp(
        signal["signal_time"], "signal_time",
    )
    close = datetime.strptime(trade_date, "%Y-%m-%d").replace(
        hour=15, tzinfo=SHANGHAI_TIMEZONE,
    )
    expires_at = min(datetime.fromisoformat(frozen), close).isoformat()
    if existing is not None:
        old_version = str(existing["current_plan_version"])
        old_event_key = notification_event_key(
            "joinquant",
            account_scope_id,
            "buy-plan",
            trade_date=trade_date,
            logical_signal_id=logical_id,
            plan_version=old_version,
        )
        store.resolve_notification_gap(
            conn,
            old_event_key,
            occurred_at,
            "superseded_by_buy_plan_version",
        )
        old_rows = conn.execute(
            """SELECT event_key FROM notification_outbox
               WHERE account_scope_id=? AND event_type='buy-plan'
                 AND object_type='logical_signal_plan' AND object_id=?
                 AND state IN ('pending','leased') AND event_key<>?""",
            (account_scope_id, logical_id, event_key),
        ).fetchall()
        for old in old_rows:
            store.request_notification_cancel(
                conn,
                str(old["event_key"]),
                occurred_at,
                "buy plan replaced",
            )
    store.upsert_logical_signal_plan(
        conn,
        account_scope_id,
        trade_date,
        logical_id,
        frozen,
        version,
        occurred_at,
    )
    source_fact_id = f"{logical_id}:{version}"
    event = NotificationEvent(
        event_key=event_key,
        account_scope_id=account_scope_id,
        adapter="joinquant",
        event_type="buy-plan",
        object_type="logical_signal_plan",
        object_id=logical_id,
        source_fact_id=source_fact_id,
        priority="normal",
        payload_version=1,
        occurred_at=occurred_at,
        expires_at=expires_at,
        title=f"JoinQuant 买入计划 {signal['code']}",
        body=(
            f"> {signal['code']} {signal.get('name') or ''} | 买入 {int(signal['order_qty'])}股"
            f" | 目标持仓 {int(signal['target_position'])}股\n"
            f"> 入场上限 {entry_tick} | 止损 {stop_tick}\n"
            f"> 业务时间：{occurred_at}"
        ),
        payload={
            "trade_date": trade_date,
            "logical_signal_id": logical_id,
            "plan_version": version,
            "code": str(signal["code"]),
            "side": "buy",
            "order_qty": int(signal["order_qty"]),
            "target_position": int(signal["target_position"]),
            "entry_tick": str(entry_tick),
            "stop_tick": str(stop_tick),
            "frozen_valid_until": frozen,
            "strategy_version": str(signal["strategy_version"]),
            "parameter_version": str(signal["parameter_version"]),
        },
        metadata={"renderer": "buy-plan-v1"},
    )
    store.enqueue_notification_or_gap(conn, event, occurred_at)
    return event_key


def _buy_signal(row: pd.Series, run_id: str | None, index: int) -> dict[str, Any]:
    code = clean_code(row.get("code"))
    price = _num(row.get("price"))
    plan = _resolved_buy_plan(row)
    entry = float(plan["entry_price"])
    atr14 = _num(row.get("atr14"))
    stop = float(plan["stop_loss"])
    take = float(plan["take_profit"])
    position_pct = float(plan["position_pct"])
    signal = {
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
        "signal_type": _text(row.get("mode")) or _text(row.get("buy_state")) or "signal",
        "max_age_min": 5 if (_text(row.get("mode")) or "").lower() == "short" else 20,
        "reason": _text(row.get("risk_reason") or row.get("buy_reason") or row.get("entry_reason")),
        "atr14": round(atr14, 4) if atr14 > 0 else None,
        "board_type": str(plan["board_type"]),
        "market_regime": str(plan["market_regime"]),
        "industry": _industry(row),
        "theme": _theme(row),
    }
    if _confirmed_gap_reentry(row):
        signal.update({
            "entry_path": "gap_reentry",
            "parent_signal_id": _text(row.get("parent_signal_id")),
            "original_entry_price": _num(row.get("original_entry_price")),
            "original_stop_price": _num(row.get("original_stop_price")),
            "reentry_cap_price": _num(row.get("reentry_cap_price")),
            "gap_reentry_state": "OPEN_CONFIRMED",
        })
    if _num(row.get("target_qty")) > 0:
        signal["target_qty"] = int(_num(row.get("target_qty")))
    return signal


def _sell_signal(row: pd.Series, run_id: str | None, index: int) -> dict[str, Any] | None:
    code = clean_code(row.get("code"))
    price = _num(row.get("price"))
    if not code or price <= 0:
        return None
    signal = {
        "id": redact_secret_text(_text(row.get("exit_signal_id")))
        or _signal_id(run_id, code, "sell", index),
        "code": code,
        "jq_code": to_jq_code(code),
        "name": _text(row.get("name")),
        "action": "sell",
        "exit_stage": _exit_notification_stage(row.get("signal_action")),
        "price": round(price, 2),
        "reason": redact_secret_text(
            _text(
                row.get("risk_reason")
                or row.get("signal_note")
                or row.get("buy_reason")
            )
        ),
    }
    if row.get("target_qty") is not None and not pd.isna(row.get("target_qty")):
        signal["target_qty"] = max(0, int(_num(row.get("target_qty"))))
    position_cycle_id = _text(row.get("position_cycle_id"))
    if position_cycle_id:
        signal["position_cycle_id"] = position_cycle_id
    return signal


def _enqueue_exit_notification(
    store: TradingStore,
    conn: object,
    account_scope_id: str,
    signal: dict[str, Any],
) -> None:
    signal_id = str(signal["id"])
    row = conn.execute(
        "SELECT * FROM exit_intents WHERE signal_id=?", (signal_id,),
    ).fetchone()
    if row is None:
        raise ValueError("exit intent source fact is missing")
    code = str(row["stock_code"])
    signal_fact = conn.execute(
        "SELECT raw_json FROM signals WHERE signal_id=?", (signal_id,),
    ).fetchone()
    try:
        frozen_signal = json.loads(str(signal_fact["raw_json"])) if signal_fact else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        frozen_signal = {}
    position_cycle_id = str(
        frozen_signal.get("position_cycle_id") or f"legacy:{code}"
    )
    reason = redact_secret_text(row["reason"] or "sell")
    stage = _exit_notification_stage(
        frozen_signal.get("exit_stage"), signal_id, reason,
    )
    notification_reason = stage if stage != "sell" else "custom exit"
    reason = notification_reason
    occurred_at = store._shanghai_timestamp(
        row["created_at"], "exit_intent.created_at",
    )
    event = NotificationEvent(
        event_key=notification_event_key(
            "joinquant",
            account_scope_id,
            "exit",
            position_cycle_id=position_cycle_id,
            exit_intent_id=signal_id,
            stage=stage,
        ),
        account_scope_id=account_scope_id,
        adapter="joinquant",
        event_type="exit",
        object_type="exit_intent",
        object_id=signal_id,
        source_fact_id=signal_id,
        priority="high",
        payload_version=1,
        occurred_at=occurred_at,
        expires_at=None,
        title="JoinQuant 退出意图",
        body=(
            f"> 卖出 {code} | 阶段 {stage} | 目标持仓 {int(row['target_qty'])}股\n"
            f"> 原因：{reason[:240]}\n"
            f"> 业务时间：{occurred_at}"
        ),
        payload={
            "position_cycle_id": position_cycle_id,
            "exit_intent_id": signal_id,
            "stock_code": code,
            "stage": stage,
            "target_qty": int(row["target_qty"]),
            "reason_code": notification_reason,
        },
        metadata={"renderer": "exit-v1"},
    )
    store.enqueue_notification_or_gap(conn, event, occurred_at)


def _cancel_superseded_exit_notifications(
    store: TradingStore,
    conn: object,
    account_scope_id: str,
    signal_ids: list[str],
    occurred_at: str,
) -> None:
    occurred_at = store._shanghai_timestamp(
        occurred_at, "exit notification cancellation time",
    )
    for signal_id in signal_ids:
        rows = conn.execute(
            """SELECT event_key FROM notification_outbox
               WHERE account_scope_id=? AND object_type='exit_intent'
                 AND object_id=? AND state IN ('pending','leased')""",
            (account_scope_id, signal_id),
        ).fetchall()
        for row in rows:
            store.request_notification_cancel(
                conn, str(row["event_key"]), occurred_at,
                "exit intent superseded",
            )
        gaps = conn.execute(
            """SELECT event_key FROM notification_enqueue_gaps
               WHERE account_scope_id=? AND source_fact_id=?
                 AND resolved_at IS NULL""",
            (account_scope_id, signal_id),
        ).fetchall()
        for gap in gaps:
            store.resolve_notification_gap(
                conn, str(gap["event_key"]), occurred_at,
                "superseded_by_exit_intent",
            )


def _admit_exact_buys(
    payload: dict[str, Any],
    *,
    store: TradingStore,
    rows_by_signal_id: dict[str, pd.Series],
    decisions_by_signal_id: dict[str, dict[str, Any]],
    reject_reasons: Counter[str],
    parameter_snapshot: dict[str, Any],
) -> None:
    buys = [signal for signal in payload["signals"] if signal["action"] == "buy"]
    if not buys:
        return
    rejected_ids: set[str] = set()
    parameter_version = (
        f"risk-{app_config.RISK_MODE}-v2:{canonical_hash(parameter_snapshot)[:12]}"
    )
    try:
        account_scope_id, confirmed_regime, account_equity = (
            _primary_execution_context(store)
        )
        checked_at = _execution_now()
        for signal in buys:
            row = rows_by_signal_id[signal["id"]]
            candidate = _exact_candidate(
                row,
                signal,
                account_scope_id=account_scope_id,
                trade_date=str(payload["trade_date"]),
                run_id=str(payload.get("run_id") or "export"),
                parameter_version=parameter_version,
                account_equity=account_equity,
            )
            candidate = _freeze_candidate_valid_until(
                store, candidate, str(payload["trade_date"]),
            )
            quote, rules = _exact_quote_and_rules(row, candidate)
            admission = admit_candidate(
                store,
                AdmissionRequest(
                    candidate=candidate,
                    quote=quote,
                    instrument_rules=rules,
                    risk_policy=_exact_policy(
                        signal,
                        checked_at=checked_at,
                        market_regime=confirmed_regime,
                        parameter_version=parameter_version,
                    ),
                ),
                checked_at,
            )
            if not admission.allowed or admission.execution_intent is None:
                reason = (
                    admission.pre_trade_result.hard_blocks[0]
                    if admission.pre_trade_result.hard_blocks
                    else "ADMISSION_REJECTED"
                )
                rejected_ids.add(signal["id"])
                reject_reasons[f"admission_{reason.lower()}"] += 1
                decision = decisions_by_signal_id.get(signal["id"])
                if decision is not None:
                    decision.update({
                        "selected": False,
                        "rejection_stage": "execution_admission",
                        "rejection_code": reason,
                    })
                continue
            _publish_exact_intent(signal, admission.execution_intent)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception as exc:
        rejected_ids.update(signal["id"] for signal in buys)
        reject_reasons["buy_admission_error"] += len(buys)
        payload["diagnostics"]["ledger_error"] = str(exc)
        for signal in buys:
            decision = decisions_by_signal_id.get(signal["id"])
            if decision is not None:
                decision.update({
                    "selected": False,
                    "rejection_stage": "execution_admission",
                    "rejection_code": "BUY_ADMISSION_ERROR",
                })
    if rejected_ids:
        payload["signals"] = [
            signal for signal in payload["signals"] if signal["id"] not in rejected_ids
        ]
        payload["diagnostics"]["buy_publication_blocked"] = True
    payload["diagnostics"]["reject_reasons"] = dict(reject_reasons)


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
    ml_store: MlStore | None = None,
    cohort_mode: str = "audit",
    cohort_interval_sec: int | None = None,
) -> Path:
    dry_run = app_config.JOINQUANT_DRY_RUN_DEFAULT if dry_run is None else dry_run
    min_score = app_config.JOINQUANT_MIN_SCORE_DEFAULT if min_score is None else min_score
    output_path = output_path or app_config.JOINQUANT_SIGNAL_FILE
    payload = _base_payload(run_id, trade_date, dry_run)
    parameter_snapshot = _ml_parameter_snapshot(min_score, enforce_execution_contract)
    candidate_generated_at = str(payload["generated_at"])
    store = store or TradingStore(app_config.TRADING_DB_FILE)
    gap_store_ready = False
    try:
        store.initialize()
        gap_store_ready = True
    except (OSError, sqlite3.Error, ValueError):
        pass

    sample_rows: list[tuple[pd.Series, dict[str, Any]]] = []
    candidate_rows: list[pd.Series] = []
    candidate_decisions: list[dict[str, Any]] = []
    candidate_contract_error: ValueError | None = None
    reject_reasons: Counter[str] = Counter()
    if df is not None and not df.empty:
        signals: list[dict[str, Any]] = []
        ordered_rows = []
        for _, row in df.iterrows():
            try:
                if gap_store_ready:
                    prepared = _prepare_gap_reentry_row(
                        row, store=store, run_id=run_id,
                        trade_date=str(payload["trade_date"]),
                        generated_at=candidate_generated_at,
                        min_score=min_score, allow_buy=allow_buy,
                    )
                elif (
                    app_config.GAP_REENTRY_ENABLE_DEFAULT
                    and allow_buy and not _is_sell(row)
                ):
                    raise sqlite3.OperationalError("gap reentry store unavailable")
                else:
                    prepared = row
            except (KeyboardInterrupt, SystemExit, MemoryError):
                raise
            except Exception:
                prepared = row.copy()
                prepared["entry_path"] = "gap_reentry"
                prepared["gap_reentry_state"] = "STATE_UNAVAILABLE"
                prepared["gap_reentry_reason"] = "gap_reentry_state_unavailable"
                prepared["gap_reentry_transitioned"] = False
            ordered_rows.append(prepared)
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
            candidate_decision = None
            try:
                stage = rejection_stage(buy_reject_reason)
            except ValueError as exc:
                candidate_contract_error = candidate_contract_error or exc
            else:
                candidate_rows.append(row)
                candidate_decision = {
                    "code": clean_code(row.get("code")),
                    "selected": not bool(buy_reject_reason),
                    "rejection_stage": stage,
                    "rejection_code": buy_reject_reason,
                    "is_sell": _is_sell(row),
                    "has_holding": _has_holding(row),
                }
                candidate_decisions.append(candidate_decision)
            if not buy_reject_reason:
                signal = _buy_signal(row, run_id, index)
                opportunity_id = _text(row.get("gap_reentry_opportunity_id"))
                if opportunity_id:
                    signal["gap_reentry_opportunity_id"] = opportunity_id
                if candidate_decision is not None:
                    candidate_decision["signal_id"] = signal["id"]
                signals.append(signal)
                current_position_count += 1
                current_position_pct += float(signal.get("position_pct") or 0)
                entry = float(signal.get("entry_price") or 0)
                gap_open_risk_pct = _num(row.get("gap_reentry_open_risk_pct"))
                current_open_risk_pct += (
                    gap_open_risk_pct
                    if _confirmed_gap_reentry(row) and gap_open_risk_pct > 0
                    else float(signal.get("position_pct") or 0) * max(
                        entry - float(signal.get("stop_loss") or entry), 0,
                    ) / entry if entry > 0 else 0
                )
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
                    gap_cash_required = _num(row.get("gap_reentry_cash_required_yuan"))
                    available_cash -= (
                        gap_cash_required
                        if _confirmed_gap_reentry(row) and gap_cash_required > 0
                        else account_total_value * float(signal.get("position_pct") or 0) / 100.0
                    )
                sample_rows.append((row, signal))
            elif _is_sell(row) and _has_holding(row) and allow_sell:
                sell = _sell_signal(row, run_id, index)
                if sell:
                    if candidate_decision is not None:
                        candidate_decision["signal_id"] = sell["id"]
                    signals.append(sell)
                    sample_rows.append((row, sell))
            elif _is_sell(row) and _has_holding(row):
                reject_reasons["sell_disabled"] += 1
            elif _is_sell(row):
                reject_reasons["sell_without_holding"] += 1
            else:
                reject_reasons[buy_reject_reason] += 1
        payload["signals"] = signals
        for signal in payload["signals"]:
            signal["created_at"] = payload["generated_at"]
            signal["validated_at"] = payload["generated_at"]
            signal["published_at"] = payload["generated_at"]
        payload["signals"].sort(key=lambda signal: (
            signal["action"] != "sell",
            -_num(signal.get("final_score")) if signal["action"] == "buy" else 0,
        ))
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
        "gap_reentry_transitions": [
            {
                "code": clean_code(row.get("code")),
                "state": _text(row.get("gap_reentry_state")),
                "reason": _text(row.get("gap_reentry_reason")),
            }
            for row in (ordered_rows if df is not None and not df.empty else [])
            if bool(row.get("gap_reentry_transitioned"))
        ],
    }

    rows_by_signal_id = {signal["id"]: row for row, signal in sample_rows}
    decisions_by_signal_id = {
        str(decision.get("signal_id")): decision
        for decision in candidate_decisions
        if decision.get("signal_id")
    }
    if enforce_execution_contract:
        _admit_exact_buys(
            payload,
            store=store,
            rows_by_signal_id=rows_by_signal_id,
            decisions_by_signal_id=decisions_by_signal_id,
            reject_reasons=reject_reasons,
            parameter_snapshot=parameter_snapshot,
        )

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
    decisions = [
        (
            signal,
            None
            if enforce_execution_contract and signal["action"] == "buy"
            else evaluate_observation(signal, PortfolioState.empty(), limits),
        )
        for signal in payload["signals"]
    ]
    ledger_run_id = run_id or f"export-{payload['generated_at'].replace(' ', 'T')}"
    new_ledger_run = False
    try:
        store.initialize()
        inserted_signal_count = 0
        with store.transaction() as conn:
            account_scope_id = store.get_or_create_account_scope(
                conn, "joinquant", "primary",
            )
            buy_row = conn.execute("SELECT value FROM system_state WHERE key='buy_enabled'").fetchone()
            kill_row = conn.execute("SELECT value FROM system_state WHERE key='kill_switch'").fetchone()
            buy_enabled = str(buy_row[0]) if buy_row else "1"
            kill_switch = str(kill_row[0]) if kill_row else "0"
            inserted_ledger_run = store.record_strategy_run(conn, StrategyRunRecord(
                run_id=ledger_run_id,
                trade_date=payload["trade_date"],
                started_at=payload["generated_at"],
                strategy_version="a_share_strategy",
                parameter_version="risk-observe-v1",
            ))
            for signal, decision in decisions:
                opportunity_id = _text(signal.get("gap_reentry_opportunity_id"))
                if (
                    opportunity_id and signal["action"] == "buy"
                    and buy_enabled != "0" and kill_switch != "1"
                ):
                    store.mark_gap_reentry_signal(conn, opportunity_id, signal)
                if (
                    enforce_execution_contract
                    and signal["action"] == "buy"
                    and buy_enabled != "0"
                    and kill_switch != "1"
                ):
                    _enqueue_buy_plan_notification(
                        store,
                        conn,
                        account_scope_id,
                        str(payload["trade_date"]),
                        signal,
                    )
                existing = conn.execute(
                    "SELECT generated_at, raw_json FROM signals WHERE signal_id=?",
                    (signal["id"],),
                ).fetchone()
                if existing is not None:
                    previous = json.loads(existing["raw_json"])
                    signal["created_at"] = str(
                        previous.get("created_at") or existing["generated_at"]
                    )
                    if signal["action"] == "sell":
                        if previous.get("position_cycle_id"):
                            signal["position_cycle_id"] = str(
                                previous["position_cycle_id"]
                            )
                        else:
                            signal.pop("position_cycle_id", None)
                inserted_signal_count += int(store.record_signal(conn, SignalRecord(
                    signal_id=signal["id"], run_id=ledger_run_id,
                    trade_date=payload["trade_date"], code=signal["code"],
                    jq_code=signal["jq_code"], action=signal["action"],
                    position_pct=float(signal.get("position_pct") or 0),
                    generated_at=payload["generated_at"],
                    expires_at=str(signal.get("expires_at") or ""),
                    raw_json=canonical_json(signal), validated_at=signal["validated_at"],
                    published_at=signal["published_at"],
                    signal_price=(
                        float(signal.get("entry_price") or signal.get("price"))
                        if signal.get("entry_price") or signal.get("price") else None
                    ),
                    stop_loss=float(signal["stop_loss"]) if signal.get("stop_loss") else None,
                    take_profit=float(signal["take_profit"]) if signal.get("take_profit") else None,
                    final_score=float(signal["final_score"]) if signal.get("final_score") is not None else None,
                    strategy_mode=str(signal.get("signal_type") or ""),
                )))
                if enforce_execution_contract and signal["action"] == "buy":
                    store.bind_execution_intent_signal(
                        conn,
                        str(signal["account_scope_id"]),
                        str(signal["client_order_id"]),
                        str(signal["id"]),
                    )
                if signal["action"] == "sell":
                    superseded_ids = [
                        str(row["signal_id"])
                        for row in conn.execute(
                            """SELECT signal_id FROM exit_intents
                               WHERE stock_code=? AND status='active'
                                 AND signal_id<>?""",
                            (signal["code"], signal["id"]),
                        ).fetchall()
                    ]
                    accepted = store.upsert_exit_intent(
                        conn, signal["id"], signal["code"], int(signal.get("target_qty") or 0),
                        str(signal.get("reason") or "sell"), payload["generated_at"],
                    )
                    if accepted:
                        _cancel_superseded_exit_notifications(
                            store, conn, account_scope_id, superseded_ids,
                            payload["generated_at"],
                        )
                        _enqueue_exit_notification(
                            store, conn, account_scope_id, signal,
                        )
                if decision is None:
                    continue
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
        new_ledger_run = inserted_ledger_run
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
    except (sqlite3.Error, OSError, SignalConflictError, ValueError) as exc:
        had_buys = any(signal["action"] == "buy" for signal in payload["signals"])
        payload["signals"] = [signal for signal in payload["signals"] if signal["action"] == "sell"]
        payload["diagnostics"]["ledger_error"] = str(exc)
        payload["diagnostics"]["buy_publication_blocked"] = had_buys

    _finalize_candidate_decisions(
        candidate_decisions,
        payload,
        allow_buy=allow_buy,
        allow_sell=allow_sell,
    )

    if new_ledger_run and ml_store is None and app_config.ML_TRAINED_SHADOW_ENABLE:
        ml_store = MlStore(app_config.ML_DB_FILE, app_config.ML_DB_MAX_BYTES)
    if new_ledger_run and ml_store is not None:
        try:
            if candidate_contract_error is not None:
                raise candidate_contract_error
            ml_store.initialize()
            decision_at = _ml_decision_at(candidate_generated_at)
            candidate_frame = pd.DataFrame(candidate_rows)
            ml_context = {
                "source": "joinquant_live",
                "dataset_id": str(run_id or ledger_run_id),
                "decision_at": decision_at,
                "strategy_version": "a_share_strategy-v1",
                "parameter_version": (
                    f"risk-observe-v1:{canonical_hash(parameter_snapshot)[:12]}"
                ),
                "feature_schema_version": "live-candidate-v1",
                "cohort_mode": cohort_mode,
                "cohort_interval_sec": cohort_interval_sec,
                "parameter_snapshot": parameter_snapshot,
                "universe_hash": canonical_hash(
                    [decision["code"] for decision in candidate_decisions]
                ),
                "market_data_version": "live-scan-v1",
                "code_hash": _ml_code_hash(),
                "generator_hash": canonical_hash({
                    "rejection_stages": _REJECTION_STAGES,
                    "feature_columns": FEATURE_COLUMNS,
                }),
            }
            candidate_samples = build_candidate_samples(
                candidate_frame,
                candidate_decisions,
                ml_context,
            )
            ml_store.record_candidates(candidate_samples)
            if app_config.ML_TRAINED_SHADOW_ENABLE:
                observation = observe_candidate_samples(
                    ml_store,
                    app_config.ML_MODEL_DIR,
                    candidate_samples,
                    expected_versions={
                        "strategy_version": str(ml_context["strategy_version"]),
                        "parameter_version": str(ml_context["parameter_version"]),
                        "feature_schema_version": str(
                            ml_context["feature_schema_version"]
                        ),
                        **runtime_dependency_versions(),
                    },
                    created_at=decision_at,
                    timeout_sec=app_config.ML_INFERENCE_TIMEOUT_SEC,
                    max_permission_level=app_config.ML_PERMISSION_LEVEL_MAX,
                )
                if observation.status == "fallback_rules":
                    print(
                        "ML inference fell back to rules: "
                        + ",".join(observation.reasons),
                        flush=True,
                    )
        except (MlCapacityError, MlDataConflict, sqlite3.Error, OSError, TypeError, ValueError) as exc:
            detail = str(exc).replace("\n", " ")[:200]
            print(f"ML candidate batch skipped: {type(exc).__name__}: {detail}", flush=True)

    try:
        published_ids = {signal["id"] for signal in payload["signals"]}
        append_signal_samples(
            [(row, signal) for row, signal in sample_rows if signal["id"] in published_ids],
            payload, ml_sample_path,
        )
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
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
