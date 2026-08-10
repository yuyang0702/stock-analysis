"""Portable, Python 3.6-compatible strategy decision runtime.

The live server and JoinQuant strategy snapshots share this module for the
deterministic parts of candidate pooling, scoring, tradability and final buy
admission.  It deliberately has no database, network, environment-file or
account credential access.  Historical news/theme/project data is supplied by
an explicit point-in-time provider and is never invented here.
"""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

try:
    from collections.abc import Mapping
except ImportError:  # Python 3.6
    from collections import Mapping

if "size_strategy_order" not in globals():
    try:
        from strategy_economics import size_strategy_order
    except ImportError:
        pass

SNAPSHOT_RUNTIME_VERSION = "2026-08-10.1-multipath"
SNAPSHOT_SCHEMA_VERSION = 1
EXECUTION_PLAN_VERSION = "2026-08-01.1-small-capital-live-risk"
SHANGHAI_TZ = timezone(timedelta(hours=8))

REQUIRED_ML_FEATURES = frozenset((
    "price", "pct_chg", "amount", "turnover", "market_cap", "score",
    "final_score", "global_risk_score", "trade_score", "news_score",
    "risk_reward", "entry_price", "stop_loss", "take_profit", "pressure_pct",
    "pressure_label", "ma5", "ma10", "ma20", "ma30", "atr14", "theme_label",
    "theme_heat_level", "theme_heat_score", "market_state", "signal_state",
    "signal_age_days", "buy_state", "market_regime",
))

REQUIRED_PORTABLE_FEATURES = REQUIRED_ML_FEATURES.union(frozenset((
    "position_pct", "execution_plan_version", "execution_allowed",
)))

PORTFOLIO_STATE_FIELDS = (
    "allow_buy", "account_total_value", "current_position_pct",
    "current_open_risk_pct", "current_position_count", "sector_exposure_pct",
    "theme_exposure_pct", "cooldown_codes", "available_cash",
    "new_positions_today", "orders_today", "daily_turnover_pct",
    "daily_pnl_pct", "account_drawdown_pct", "consecutive_losses",
)

SAFE_ML_PARAMETER_KEYS = (
    "min_score", "caution_min_score", "near_limit_up_pct", "board_lot_size",
    "special_listing_days", "quote_stale_sec", "chasing_max_pct",
    "chasing_atr_multiplier", "min_tradable_amount", "enforce_execution_contract",
    "portfolio_risk_enabled", "max_positions", "max_new_positions_per_day",
    "max_orders_per_day", "max_daily_turnover_pct", "daily_loss_warn_pct",
    "account_drawdown_warn_pct", "max_consecutive_losses", "exit_cooldown_enabled",
    "tradability_filter_enabled", "max_uncategorized_position_pct",
    "max_open_risk_caution_pct", "max_open_risk_normal_pct",
    "max_industry_position_pct", "max_theme_position_pct",
    "max_total_position_pct",
)

REJECTION_STAGES = {
    "score": (
        "buy_low_score", "buy_pool_amount_below_threshold",
        "buy_pool_pct_below_threshold", "buy_pool_score_below_cutoff",
        "wave3_score_below_threshold", "limitdown_score_below_threshold",
    ),
    "tradability": (
        "buy_suspended", "buy_st", "buy_delisting", "buy_special_listing_stage",
        "buy_quote_stale", "buy_chasing", "buy_illiquid", "buy_invalid_price",
        "buy_near_limit_up", "gap_reentry_locked_limit",
        "gap_reentry_open_observing", "gap_reentry_resealed",
        "gap_reentry_too_far", "gap_reentry_falling",
        "gap_reentry_attempts_exhausted", "gap_reentry_too_late",
        "gap_reentry_parent_invalid", "gap_reentry_current_score_low",
        "gap_reentry_quote_stale",
        "wave3_history_insufficient", "wave3_setup_invalid",
        "wave3_trigger_pending", "wave3_relative_strength_weak",
        "wave3_chasing", "limitdown_board_ineligible",
        "limitdown_listing_age_insufficient", "limitdown_history_insufficient",
        "limitdown_liquidity_insufficient", "limitdown_event_absent",
        "limitdown_absorption_unconfirmed", "limitdown_wait_next_day_confirm",
        "limitdown_setup_expired", "limitdown_resealed",
        "limitdown_next_day_gap_invalid", "limitdown_below_vwap",
        "limitdown_open_low_broken", "limitdown_sector_accelerating_down",
        "factor_disclosure_risk_veto", "factor_market_risk_off",
    ),
    "risk": (
        "buy_disabled", "buy_max_positions", "buy_daily_new_positions_limit",
        "buy_daily_orders_limit", "buy_daily_turnover_limit",
        "buy_daily_loss_limit", "buy_account_drawdown_limit",
        "buy_consecutive_loss_limit", "buy_cooldown", "buy_risk_disallowed",
        "buy_bad_position", "buy_open_risk_limit", "buy_sector_limit",
        "buy_theme_limit", "buy_uncategorized_limit",
        "buy_insufficient_available_cash", "buy_total_position_limit",
        "buy_too_small_for_board_lot", "buy_single_position_limit",
        "gap_reentry_min_lot_risk_exceeded", "gap_reentry_insufficient_cash",
        "gap_reentry_per_trade_risk_exceeded",
        "gap_reentry_portfolio_open_risk_exceeded",
        "gap_reentry_per_trade_and_portfolio_risk_exceeded",
        "gap_reentry_fee_schedule_required", "gap_reentry_pending_order",
        "gap_reentry_current_risk_disallowed", "gap_reentry_state_unavailable",
        "factor_portfolio_state_unavailable", "factor_path_position_limit",
        "factor_path_daily_limit", "factor_economic_state_unavailable",
        "factor_round_trip_cost_rate_high", "factor_target_edge_insufficient",
        "factor_risk_budget_exceeded_after_cost",
    ),
    "execution": (
        "not_buy_sell_signal", "buy_execution_plan_missing",
        "buy_execution_plan_invalid", "buy_invalid_take_profit",
        "buy_invalid_stop_loss", "buy_not_reached_entry",
        "gap_reentry_rr_invalid", "factor_trigger_required",
        "factor_invalid_price_plan",
    ),
}

_REJECTION_STAGE_BY_REASON = {}
for _stage_name, _stage_reasons in REJECTION_STAGES.items():
    for _stage_reason in _stage_reasons:
        _REJECTION_STAGE_BY_REASON[_stage_reason] = _stage_name

SNAPSHOT_MANIFEST = None
SNAPSHOT_PARAMETERS = None
_STRICT_FEATURE_PROVIDER = None
_PORTFOLIO_STATE_PROVIDER = None
_DECISION_OBSERVER = None


class StrategySnapshotError(ValueError):
    """Stable fail-closed snapshot runtime error."""


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def clean_code(value):
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def _number(value, default=0.0):
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _flag(value):
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(value)


def _market_flag(value):
    """Match the live tradability flag semantics exactly."""
    return value is not None and not (
        isinstance(value, float) and math.isnan(value)
    ) and bool(value)


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise StrategySnapshotError("%s_MUST_BE_MAPPING" % name.upper())
    return value


def _ml_parameters(parameters):
    value = _mapping(parameters, "parameters")
    candidate = value.get("ml") if isinstance(value.get("ml"), Mapping) else value
    missing = [key for key in SAFE_ML_PARAMETER_KEYS if key not in candidate]
    if missing:
        raise StrategySnapshotError("SNAPSHOT_PARAMETERS_INCOMPLETE: " + ",".join(missing))
    return candidate


def build_ml_parameter_snapshot(config_module, min_score, enforce_execution_contract):
    """Return the exact non-secret live candidate parameter allowlist."""
    result = {
        "min_score": float(min_score),
        "caution_min_score": 85.0,
        "near_limit_up_pct": 9.8,
        "board_lot_size": 100,
        "special_listing_days": 5,
        "quote_stale_sec": 120,
        "chasing_max_pct": 0.02,
        "chasing_atr_multiplier": 0.5,
        "min_tradable_amount": 20000000,
        "enforce_execution_contract": bool(enforce_execution_contract),
        "portfolio_risk_enabled": bool(config_module.JOINQUANT_PORTFOLIO_RISK_ENABLE_DEFAULT),
        "max_positions": int(config_module.JOINQUANT_MAX_POSITIONS_DEFAULT),
        "max_new_positions_per_day": int(config_module.MAX_NEW_POSITIONS_PER_DAY),
        "max_orders_per_day": int(config_module.MAX_ORDERS_PER_DAY),
        "max_daily_turnover_pct": float(config_module.MAX_DAILY_TURNOVER_PCT),
        "daily_loss_warn_pct": float(config_module.DAILY_LOSS_WARN_PCT),
        "account_drawdown_warn_pct": float(config_module.ACCOUNT_DRAWDOWN_WARN_PCT),
        "max_consecutive_losses": int(config_module.MAX_CONSECUTIVE_LOSSES),
        "exit_cooldown_enabled": bool(config_module.JOINQUANT_EXIT_COOLDOWN_ENABLE_DEFAULT),
        "tradability_filter_enabled": bool(config_module.JOINQUANT_TRADABILITY_FILTER_ENABLE_DEFAULT),
        "max_uncategorized_position_pct": float(config_module.MAX_UNCATEGORIZED_POSITION_PCT),
        "max_open_risk_caution_pct": float(config_module.MAX_OPEN_RISK_CAUTION_PCT),
        "max_open_risk_normal_pct": float(config_module.MAX_OPEN_RISK_NORMAL_PCT),
        "max_industry_position_pct": float(config_module.MAX_INDUSTRY_POSITION_PCT),
        "max_theme_position_pct": float(config_module.MAX_THEME_POSITION_PCT),
        "max_total_position_pct": float(config_module.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT),
    }
    if tuple(sorted(result)) != tuple(sorted(SAFE_ML_PARAMETER_KEYS)):
        raise StrategySnapshotError("SAFE_PARAMETER_ALLOWLIST_MISMATCH")
    return result


def build_safe_strategy_parameters(config_module):
    """Freeze only explicitly allowed strategy values from the effective environment."""
    fee = config_module.SIMULATION_FEE_SCHEDULE.to_dict()
    fee.pop("contract_sha256", None)
    return {
        "ml": build_ml_parameter_snapshot(
            config_module,
            config_module.JOINQUANT_MIN_SCORE_DEFAULT,
            True,
        ),
        "scan": {
            "mode": str(config_module.SCAN_MODE_DEFAULT),
            "top": int(config_module.SCAN_TOP_DEFAULT),
            "interval_sec": int(config_module.SCAN_INTERVAL_DEFAULT),
            "min_price": float(config_module.MIN_PRICE_DEFAULT),
            "min_amount": float(config_module.MIN_AMOUNT_DEFAULT),
            "skip_pressure": bool(config_module.SKIP_PRESSURE_DEFAULT),
            "skip_lhb": bool(config_module.SKIP_LHB_DEFAULT),
            "skip_news": bool(config_module.SKIP_NEWS_DEFAULT),
            "stock_news_limit": int(config_module.STOCK_NEWS_LIMIT_DEFAULT),
            "notice_days_back": int(config_module.NOTICE_DAYS_BACK_DEFAULT),
            "max_candidates_for_news": int(config_module.MAX_CANDIDATES_FOR_NEWS_DEFAULT),
            "notify_min_score": float(config_module.NOTIFY_MIN_SCORE_DEFAULT),
            "intraday_near_pressure_pct": float(config_module.INTRADAY_NEAR_PRESSURE_PCT_DEFAULT),
            "intraday_trigger_pressure_pct": float(config_module.INTRADAY_TRIGGER_PRESSURE_PCT_DEFAULT),
            "intraday_watch_multiplier": int(config_module.INTRADAY_WATCH_MULTIPLIER_DEFAULT),
        },
        "execution": {
            "execution_plan_version": EXECUTION_PLAN_VERSION,
            "gap_reentry_enabled": bool(config_module.GAP_REENTRY_ENABLE_DEFAULT),
            "max_single_position_pct": float(config_module.MAX_SINGLE_POSITION_PCT),
            "fee_schedule": fee,
        },
        "multipath": {
            "enabled": bool(config_module.MULTIPATH_ENABLE_DEFAULT),
            "wave3_enabled": bool(config_module.WAVE3_ENABLE_DEFAULT),
            "limitdown_enabled": bool(
                config_module.LIMITDOWN_EXHAUSTION_ENABLE_DEFAULT
            ),
            "economic_gate_enabled": bool(
                config_module.MULTIPATH_ECONOMIC_GATE_ENABLE_DEFAULT
            ),
            "momentum_max": int(config_module.MULTIPATH_MOMENTUM_MAX),
            "wave3_max": int(config_module.MULTIPATH_WAVE3_MAX),
            "limitdown_max": int(config_module.MULTIPATH_LIMITDOWN_MAX),
            "total_max": int(config_module.MULTIPATH_TOTAL_MAX),
            "factor_screen_max": int(config_module.MULTIPATH_FACTOR_SCREEN_MAX),
            "max_round_trip_cost_rate": float(
                config_module.MULTIPATH_MAX_ROUND_TRIP_COST_RATE
            ),
            "min_target_cost_multiple": float(
                config_module.MULTIPATH_MIN_TARGET_COST_MULTIPLE
            ),
        },
    }


def rejection_stage(reason):
    value = _text(reason)
    if not value:
        return "selected"
    stage = _REJECTION_STAGE_BY_REASON.get(value)
    if not stage:
        raise StrategySnapshotError("UNKNOWN_REJECTION_CODE: " + value)
    return stage


def strict_market_regime(value):
    text = _text(value)
    if text in ("RISK_OFF", "风险释放"):
        return "RISK_OFF"
    if text in ("CAUTION", "弱势震荡"):
        return "CAUTION"
    if text in ("NORMAL", "强势进攻", "温和修复"):
        return "NORMAL"
    return ""


def market_regime(value):
    return strict_market_regime(value) or "NORMAL"


def board_type(code, entry_price, atr14):
    if str(code).startswith(("300", "301", "688")):
        return "growth"
    if entry_price > 0 and atr14 / entry_price <= 0.02:
        return "main_low"
    return "main_active"


def initial_stop_price(entry_price, support_price, atr14, board):
    if entry_price <= 0:
        return 0.0
    limits = {"main_low": (1.8, 0.06), "main_active": (2.0, 0.07), "growth": (2.5, 0.09)}
    atr_mult, max_loss_pct = limits.get(board, limits["main_active"])
    candidates = []
    if support_price > 0:
        candidates.append(support_price * 0.99)
    if atr14 > 0:
        candidates.append(entry_price - atr_mult * atr14)
    technical = min(candidates) if candidates else entry_price * (1 - max_loss_pct)
    stop = max(technical, entry_price * (1 - max_loss_pct))
    return round(min(stop, entry_price - 0.01), 2)


def trade_risk_budget_pct(board, regime):
    normalized = market_regime(regime)
    if normalized == "RISK_OFF":
        return 0.0
    budget = {"main_low": 0.65, "main_active": 0.5, "growth": 0.4}.get(board, 0.5)
    return budget * 0.5 if normalized == "CAUTION" else budget


def risk_position_pct(entry, stop, board, cap, regime):
    if entry <= 0 or stop <= 0 or stop >= entry or regime == "RISK_OFF":
        return 0.0
    distance = (entry - stop) / entry * 100
    return round(max(min(cap, trade_risk_budget_pct(board, regime) / distance * 100), 0.0), 2)


def resolved_buy_plan(row, parameters):
    ml = _ml_parameters(parameters)
    execution = parameters.get("execution", {}) if isinstance(parameters, Mapping) else {}
    version = _text(row.get("execution_plan_version"))
    entry = _number(row.get("entry_price"), _number(row.get("price")))
    stop = _number(row.get("stop_loss"))
    take = _number(row.get("take_profit"))
    position = _number(row.get("position_pct"))
    if version == EXECUTION_PLAN_VERSION and entry > 0 and 0 < stop < entry < take and position > 0:
        result = {
            "version": version,
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": take,
            "risk_per_share": _number(row.get("risk_per_share"), entry - stop),
            "risk_reward": _number(row.get("risk_reward"), 2.0),
            "position_pct": position,
            "board_type": _text(row.get("board_type")),
            "market_regime": _text(row.get("market_regime")) or market_regime(row.get("market_state")),
        }
    else:
        regime = market_regime(row.get("market_state"))
        board = board_type(clean_code(row.get("code")), entry, _number(row.get("atr14")))
        stop = initial_stop_price(entry, _number(row.get("support_level")), _number(row.get("atr14")), board)
        risk = round(max(entry - stop, 0), 2)
        result = {
            "version": EXECUTION_PLAN_VERSION,
            "entry_price": round(entry, 2),
            "stop_loss": stop,
            "take_profit": round(entry + 2 * risk, 2) if risk > 0 else 0.0,
            "risk_per_share": risk,
            "risk_reward": 2.0 if risk > 0 else 0.0,
            "position_pct": risk_position_pct(entry, stop, board, position, regime),
            "board_type": board,
            "market_regime": regime,
        }
    industry = _text(row.get("industry") or row.get("sector"))
    theme = _text(row.get("theme") or row.get("theme_label") or row.get("concept") or industry)
    if not industry and not theme:
        result["position_pct"] = min(float(result["position_pct"]), float(ml["max_uncategorized_position_pct"]))
    if _confirmed_gap_reentry(row, parameters) and int(_number(row.get("target_qty"))) != 100:
        result["position_pct"] = float(result["position_pct"]) / 3.0
    return result


def _is_sell(row):
    action = _text(row.get("signal_action")).lower()
    return action in (
        "sell", "stop_loss", "hard_stop", "take_profit", "take_profit_1",
        "trailing_stop", "time_stop", "market_risk_exit",
    ) or "sell" in action


def _confirmed_gap_reentry(row, parameters):
    execution = parameters.get("execution", {}) if isinstance(parameters, Mapping) else {}
    return (
        bool(execution.get("gap_reentry_enabled"))
        and _text(row.get("entry_path")) == "gap_reentry"
        and _text(row.get("gap_reentry_state")) == "OPEN_CONFIRMED"
        and bool(_text(row.get("parent_signal_id")))
    )


def tradability_reject_reason(row, parameters=None):
    ml = _ml_parameters(parameters) if parameters is not None else {
        "special_listing_days": 5, "quote_stale_sec": 120,
        "chasing_max_pct": 0.02, "chasing_atr_multiplier": 0.5,
        "min_tradable_amount": 20000000,
    }
    if _market_flag(row.get("paused")):
        return "buy_suspended"
    if _market_flag(row.get("is_st")) or "ST" in _text(row.get("name")).upper():
        return "buy_st"
    if _market_flag(row.get("delisting")):
        return "buy_delisting"
    if _market_flag(row.get("special_listing_stage")):
        return "buy_special_listing_stage"
    listing_days = _number(row.get("listing_days"))
    if 0 < listing_days < float(ml["special_listing_days"]):
        return "buy_special_listing_stage"
    if _number(row.get("quote_age_sec")) > float(ml["quote_stale_sec"]):
        return "buy_quote_stale"
    entry = _number(row.get("entry_price"))
    price = _number(row.get("price"))
    atr = _number(row.get("atr14"))
    chasing = min(
        float(ml["chasing_max_pct"]),
        float(ml["chasing_atr_multiplier"]) * atr / entry if atr > 0 and entry > 0 else float(ml["chasing_max_pct"]),
    )
    if entry > 0 and price > entry * (1 + chasing):
        return "buy_chasing"
    if row.get("amount") is not None and _number(row.get("amount")) < float(ml["min_tradable_amount"]):
        return "buy_illiquid"
    return ""


def _decimal(value, name):
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise StrategySnapshotError("INVALID_DECIMAL: " + name)
    if not result.is_finite() or result < 0:
        raise StrategySnapshotError("INVALID_DECIMAL: " + name)
    return result


def _money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fee_total(side, price, qty, schedule):
    notional = price * qty
    commission_rate = _decimal(schedule["buy_commission_rate" if side == "buy" else "sell_commission_rate"], "commission_rate")
    minimum = _decimal(schedule["buy_minimum_commission_yuan" if side == "buy" else "sell_minimum_commission_yuan"], "minimum_commission")
    commission = max(minimum, notional * commission_rate)
    stamp = notional * _decimal(schedule["stamp_tax_rate"], "stamp_tax_rate") if side == "sell" else Decimal("0")
    transfer = notional * _decimal(schedule["transfer_fee_rate"], "transfer_fee_rate")
    other = notional * _decimal(schedule["other_fee_rate"], "other_fee_rate")
    slippage = notional * _decimal(schedule["buy_slippage_rate" if side == "buy" else "sell_slippage_rate"], "slippage_rate")
    return sum((_money(commission), _money(stamp), _money(transfer), _money(other), _money(slippage)), Decimal("0"))


def minimum_lot_decision(entry, stop, state, plan, parameters):
    execution = parameters.get("execution", {})
    schedule = execution.get("fee_schedule")
    if not isinstance(schedule, Mapping):
        return {"reason": "gap_reentry_fee_schedule_required", "qty": 0}
    equity = _decimal(state.get("account_total_value"), "account_total_value")
    cash = _decimal(state.get("available_cash"), "available_cash")
    entry_value = _decimal(entry, "entry")
    stop_value = _decimal(stop, "stop")
    if entry_value <= 0 or stop_value <= 0 or equity <= 0 or stop_value >= entry_value:
        return {"reason": "gap_reentry_rr_invalid", "qty": 0}
    qty = int(_ml_parameters(parameters)["board_lot_size"])
    buy_fee = _fee_total("buy", entry_value, qty, schedule)
    sell_fee = _fee_total("sell", stop_value, qty, schedule)
    cash_required = entry_value * qty + buy_fee
    loss = (entry_value - stop_value) * qty + buy_fee + sell_fee
    position_pct = entry_value * qty / equity * 100
    risk_pct = loss / equity * 100
    ml = _ml_parameters(parameters)
    per_trade = Decimal(str(trade_risk_budget_pct(plan.get("board_type"), plan.get("market_regime")))) / Decimal("100") * equity
    open_limit = Decimal(str(ml["max_open_risk_caution_pct"] if plan.get("market_regime") == "CAUTION" else ml["max_open_risk_normal_pct"]))
    remaining = max(Decimal("0"), (open_limit - Decimal(str(state.get("current_open_risk_pct", 0)))) / Decimal("100") * equity)
    reasons = []
    if loss > per_trade:
        reasons.append("PER_TRADE")
    if loss > remaining:
        reasons.append("PORTFOLIO")
    if cash < cash_required:
        reason = "gap_reentry_insufficient_cash"
    elif position_pct > Decimal(str(execution.get("max_single_position_pct", 100))):
        reason = "buy_single_position_limit"
    elif Decimal(str(state.get("current_position_pct", 0))) + position_pct > Decimal(str(ml["max_total_position_pct"])):
        reason = "buy_total_position_limit"
    elif len(reasons) == 2:
        reason = "gap_reentry_per_trade_and_portfolio_risk_exceeded"
    elif reasons == ["PER_TRADE"]:
        reason = "gap_reentry_per_trade_risk_exceeded"
    elif reasons == ["PORTFOLIO"]:
        reason = "gap_reentry_portfolio_open_risk_exceeded"
    else:
        reason = ""
    return {
        "reason": reason,
        "qty": qty if not reason else 0,
        "position_pct": float(position_pct),
        "risk_pct": float(risk_pct),
        "cash_required_yuan": float(cash_required),
    }


def buy_reject_reason(row, state, parameters):
    """Return the current stable rejection code and deterministic plan updates."""
    row = dict(_mapping(row, "row"))
    state = dict(_mapping(state, "portfolio_state"))
    ml = _ml_parameters(parameters)
    updates = {}
    if not _flag(state.get("allow_buy")):
        return "buy_disabled", updates
    open_risk = _number(state.get("current_open_risk_pct"), -1)
    if open_risk < 0:
        return "buy_risk_disallowed", updates
    if int(_number(state.get("current_position_count"))) >= int(ml["max_positions"]):
        return "buy_max_positions", updates
    if ml["portfolio_risk_enabled"]:
        if int(_number(state.get("new_positions_today"))) >= int(ml["max_new_positions_per_day"]):
            return "buy_daily_new_positions_limit", updates
        if int(_number(state.get("orders_today"))) >= int(ml["max_orders_per_day"]):
            return "buy_daily_orders_limit", updates
        if _number(state.get("daily_turnover_pct")) >= float(ml["max_daily_turnover_pct"]):
            return "buy_daily_turnover_limit", updates
        if _number(state.get("daily_pnl_pct")) <= -float(ml["daily_loss_warn_pct"]):
            return "buy_daily_loss_limit", updates
        if _number(state.get("account_drawdown_pct")) <= -float(ml["account_drawdown_warn_pct"]):
            return "buy_account_drawdown_limit", updates
        if int(_number(state.get("consecutive_losses"))) >= int(ml["max_consecutive_losses"]):
            return "buy_consecutive_loss_limit", updates
    code = clean_code(row.get("code"))
    if ml["exit_cooldown_enabled"] and code in set(state.get("cooldown_codes") or ()):
        return "buy_cooldown", updates
    price = _number(row.get("price"))
    entry = _number(row.get("entry_price"), price)
    take = _number(row.get("take_profit"))
    if not code or price <= 0 or entry <= 0:
        return "buy_invalid_price", updates
    if _is_sell(row):
        return "not_buy_sell_signal", updates
    has_contract = _text(row.get("execution_plan_version")) == EXECUTION_PLAN_VERSION
    allowed_value = row.get("execution_allowed")
    allowed_missing = allowed_value is None or (
        isinstance(allowed_value, float) and math.isnan(allowed_value)
    )
    if ml["enforce_execution_contract"] and (not has_contract or allowed_missing):
        return "buy_execution_plan_missing", updates
    valid_contract = has_contract and 0 < _number(row.get("stop_loss")) < entry < take and _number(row.get("position_pct")) > 0
    if ml["enforce_execution_contract"] and not valid_contract:
        return "buy_execution_plan_invalid", updates
    if allowed_value is not None and not _flag(allowed_value):
        return "buy_risk_disallowed", updates
    if take > 0 and take <= entry:
        return "buy_invalid_take_profit", updates
    execution = parameters.get("execution", {})
    if execution.get("gap_reentry_enabled") and _text(row.get("entry_path")) == "gap_reentry" and _text(row.get("gap_reentry_state")) != "OPEN_CONFIRMED":
        return _text(row.get("gap_reentry_reason")) or "gap_reentry_parent_invalid", updates
    gap = _confirmed_gap_reentry(row, parameters)
    strict_regime = strict_market_regime(row.get("market_state"))
    if gap and not strict_regime:
        return "gap_reentry_current_risk_disallowed", updates
    regime = strict_regime if gap else market_regime(row.get("market_state"))
    if regime == "RISK_OFF":
        return "buy_disabled", updates
    if gap and price > _number(row.get("reentry_cap_price")):
        return "gap_reentry_too_far", updates
    tradability = tradability_reject_reason(row, parameters) if ml["tradability_filter_enabled"] else ""
    if gap and tradability == "buy_chasing":
        tradability = ""
    if tradability:
        return tradability, updates
    required_score = max(float(ml["min_score"]), float(ml["caution_min_score"])) if regime == "CAUTION" else float(ml["min_score"])
    if _number(row.get("final_score")) < required_score:
        return "buy_low_score", updates
    if _number(row.get("position_pct")) <= 0:
        return "buy_bad_position", updates
    if _number(row.get("pct_chg")) >= float(ml["near_limit_up_pct"]):
        return "buy_near_limit_up", updates
    if price < entry:
        return "buy_not_reached_entry", updates
    plan = resolved_buy_plan(row, parameters)
    stop = float(plan["stop_loss"])
    position = float(plan["position_pct"])
    if stop <= 0 or stop >= entry:
        return "buy_invalid_stop_loss", updates
    equity = _number(state.get("account_total_value"))
    factor_path = _text(row.get("factor_path"))
    factor_active = factor_path in ("wave3_v1", "limitdown_exhaustion_v1")
    if factor_active:
        if not _flag(row.get("factor_triggered")):
            return (
                _text(row.get("factor_rejection_code"))
                or "factor_trigger_required"
            ), updates
        path_counts = state.get("factor_position_counts")
        path_daily = state.get("factor_new_positions_today")
        if not isinstance(path_counts, Mapping) or not isinstance(path_daily, Mapping):
            return "factor_portfolio_state_unavailable", updates
        if int(_number(path_counts.get(factor_path))) >= int(
            _number(row.get("factor_max_concurrent"))
        ):
            return "factor_path_position_limit", updates
        if int(_number(path_daily.get(factor_path))) >= int(
            _number(row.get("factor_max_new_per_day"))
        ):
            return "factor_path_daily_limit", updates
        multipath = parameters.get("multipath", {})
        if multipath.get("economic_gate_enabled"):
            if state.get("available_cash") is None or equity <= 0:
                return "factor_economic_state_unavailable", updates
            sizing = size_strategy_order(
                entry,
                stop,
                take,
                equity,
                state.get("available_cash"),
                _number(row.get("factor_risk_budget_pct")),
                min(
                    position,
                    _number(row.get("factor_position_cap_pct"), position),
                ),
                parameters.get("execution", {}).get("fee_schedule", {}),
                lot_size=int(ml["board_lot_size"]),
                current_position_pct=_number(state.get("current_position_pct")),
                max_total_position_pct=float(ml["max_total_position_pct"]),
                max_round_trip_rate=_number(
                    multipath.get("max_round_trip_cost_rate"), 0.004
                ),
                min_target_cost_multiple=_number(
                    multipath.get("min_target_cost_multiple"), 3.0
                ),
            )
            if not sizing.get("allowed"):
                return _text(sizing.get("reason")), updates
            position = float(sizing["position_pct"])
            updates.update({
                "position_pct": position,
                "target_qty": int(sizing["target_qty"]),
                "factor_economics_version": sizing["version"],
                "factor_round_trip_cost_yuan": sizing["target_economics"]["round_trip_cost_yuan"],
                "factor_round_trip_cost_rate": sizing["target_economics"]["round_trip_cost_rate"],
            })
    if equity > 0 and equity * position / 100.0 <= entry * int(ml["board_lot_size"]):
        if not gap:
            return "buy_too_small_for_board_lot", updates
        if _text(plan.get("board_type")) not in ("main_low", "main_active", "growth"):
            return "buy_execution_plan_invalid", updates
        lot = minimum_lot_decision(entry, stop, state, plan, parameters)
        if lot["reason"]:
            return lot["reason"], updates
        position = float(lot["position_pct"])
        updates.update({
            "position_pct": position,
            "target_qty": int(lot["qty"]),
            "gap_reentry_open_risk_pct": float(lot["risk_pct"]),
            "gap_reentry_cash_required_yuan": float(lot["cash_required_yuan"]),
        })
    industry = _text(row.get("industry") or row.get("sector"))
    theme = _text(row.get("theme") or row.get("theme_label") or row.get("concept") or industry)
    sector_exposure = dict(state.get("sector_exposure_pct") or {})
    theme_exposure = dict(state.get("theme_exposure_pct") or {})
    if not industry and not theme:
        position = min(position, float(ml["max_uncategorized_position_pct"]))
        if ml["portfolio_risk_enabled"] and _number(sector_exposure.get("__UNCATEGORIZED__")) + position > float(ml["max_uncategorized_position_pct"]):
            return "buy_uncategorized_limit", updates
    open_limit = float(ml["max_open_risk_caution_pct"] if regime == "CAUTION" else ml["max_open_risk_normal_pct"])
    added_risk = position * max(entry - stop, 0) / entry if entry > 0 else 0
    if ml["portfolio_risk_enabled"] and open_risk + added_risk > open_limit:
        return "buy_open_risk_limit", updates
    if ml["portfolio_risk_enabled"] and industry and _number(sector_exposure.get(industry)) + position > float(ml["max_industry_position_pct"]):
        return "buy_sector_limit", updates
    if ml["portfolio_risk_enabled"] and theme and _number(theme_exposure.get(theme)) + position > float(ml["max_theme_position_pct"]):
        return "buy_theme_limit", updates
    if equity > 0:
        target_value = equity * position / 100.0
        available = state.get("available_cash")
        if available is not None and target_value > _number(available):
            return "buy_insufficient_available_cash", updates
        if target_value < entry * int(ml["board_lot_size"]):
            return "buy_too_small_for_board_lot", updates
        if _number(state.get("current_position_pct")) + position > float(ml["max_total_position_pct"]):
            return "buy_total_position_limit", updates
    updates.setdefault("position_pct", position)
    updates.setdefault("open_risk_pct", added_risk)
    return "", updates


def build_candidate_pool_frame(frame, config):
    """Pandas wrapper kept byte-for-behaviour compatible with the live pool."""
    active = frame.copy()
    active = active[~active["name"].str.contains("ST|退", regex=True, na=False)]
    active = active[(active["price"] >= config.min_price) & (active["amount"] >= config.min_amount)]
    if config.mode == "pre":
        active = active[(active["gap"] >= 1.5) & (active["gap"] <= 8.5)]
        active["score"] = active["gap"].rank(pct=True) * 50 + active["amount"].rank(pct=True) * 50
    elif config.mode == "after":
        active = active[active["pct_chg"] >= 3]
        active["score"] = active["pct_chg"].rank(pct=True) * 55 + active["amount"].rank(pct=True) * 35
        if "turnover" in active.columns:
            active["score"] += active["turnover"].rank(pct=True).fillna(0) * 10
    else:
        active = active[active["pct_chg"] >= 4]
        active["score"] = active["pct_chg"].rank(pct=True) * 60 + active["amount"].rank(pct=True) * 40
    return active.sort_values("score", ascending=False).head(int(config.limit)).copy()


def score_candidate_frame(frame):
    """Shared live/JoinQuant score with the same pandas average-tie ranks."""
    import pandas as pd
    result = frame.copy()
    result["final_score"] = pd.to_numeric(result["score"], errors="coerce").fillna(0)
    news = result["news_score"] if "news_score" in result.columns else pd.Series(0.0, index=result.index)
    result["final_score"] += pd.to_numeric(news, errors="coerce").fillna(0) * 1.2
    result["final_score"] += pd.to_numeric(result["pct_chg"], errors="coerce").rank(pct=True).fillna(0) * 5
    if "turnover" in result.columns:
        result["final_score"] += pd.to_numeric(result["turnover"], errors="coerce").rank(pct=True).fillna(0) * 2
    return result


def _parse_aware(value, name):
    text = _text(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) < 6 or text[-6] not in ("+", "-") or text[-3] != ":":
        raise StrategySnapshotError("TIMEZONE_REQUIRED: " + name)
    body, sign, hour, minute = text[:-6], text[-6], text[-5:-3], text[-2:]
    try:
        offset_minutes = (int(hour) * 60 + int(minute)) * (1 if sign == "+" else -1)
        parsed = None
        for pattern in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(body, pattern)
                break
            except ValueError:
                pass
        if parsed is None:
            raise ValueError(text)
        return parsed.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
    except (TypeError, ValueError):
        raise StrategySnapshotError("INVALID_TIMESTAMP: " + name)


def _timed_features(raw, decision_at):
    features = _mapping(raw, "features")
    decision = _parse_aware(decision_at, "decision_at")
    missing = sorted(REQUIRED_PORTABLE_FEATURES.difference(features))
    if missing:
        raise StrategySnapshotError("MISSING_STRICT_FEATURES: " + ",".join(missing))
    normalized = {}
    plain = {}
    for name, item in features.items():
        item = _mapping(item, "feature_%s" % name)
        available_at = _text(item.get("available_at"))
        if _parse_aware(available_at, "features.%s.available_at" % name) > decision:
            raise StrategySnapshotError("FEATURE_FROM_FUTURE: " + str(name))
        normalized[str(name)] = {"value": item.get("value"), "available_at": available_at}
        plain[str(name)] = item.get("value")
    return normalized, plain


def configure_snapshot(manifest, parameters):
    global SNAPSHOT_MANIFEST, SNAPSHOT_PARAMETERS
    SNAPSHOT_MANIFEST = dict(_mapping(manifest, "snapshot_manifest"))
    SNAPSHOT_PARAMETERS = dict(_mapping(parameters, "snapshot_parameters"))


def configure_strict_providers(
    feature_provider, portfolio_state_provider, decision_observer=None,
):
    global _STRICT_FEATURE_PROVIDER, _PORTFOLIO_STATE_PROVIDER, _DECISION_OBSERVER
    if not callable(feature_provider):
        raise StrategySnapshotError("STRICT_FEATURE_PROVIDER_REQUIRED")
    if not callable(portfolio_state_provider):
        raise StrategySnapshotError("PORTFOLIO_STATE_PROVIDER_REQUIRED")
    if decision_observer is not None and not callable(decision_observer):
        raise StrategySnapshotError("DECISION_OBSERVER_MUST_BE_CALLABLE")
    _STRICT_FEATURE_PROVIDER = feature_provider
    _PORTFOLIO_STATE_PROVIDER = portfolio_state_provider
    _DECISION_OBSERVER = decision_observer


def snapshot_export_config():
    if not isinstance(SNAPSHOT_MANIFEST, Mapping):
        raise StrategySnapshotError("SNAPSHOT_NOT_CONFIGURED")
    return {
        "strategy_version": SNAPSHOT_MANIFEST["strategy_version"],
        "parameter_version": SNAPSHOT_MANIFEST["parameter_version"],
        "feature_schema_version": SNAPSHOT_MANIFEST["feature_schema_version"],
        "market_data_version": SNAPSHOT_MANIFEST["market_data_version"],
        "code_hash": SNAPSHOT_MANIFEST["code_hash"],
        "generator_hash": SNAPSHOT_MANIFEST["snapshot_id"],
    }


def _target_qty(row, state, position_pct, parameters):
    supplied = int(_number(row.get("rule_target_qty") or row.get("target_qty")))
    if supplied > 0:
        return supplied
    equity = _number(state.get("account_total_value"))
    price = _number(row.get("entry_price"), _number(row.get("price")))
    lot = int(_ml_parameters(parameters)["board_lot_size"])
    if min(equity, price, position_pct) <= 0:
        return 0
    return int(equity * position_pct / 100.0 / price / lot) * lot


def _update_state_after_selection(state, row, updates, parameters):
    position = float(updates.get("position_pct", _number(row.get("position_pct"))))
    state["current_position_count"] = int(_number(state.get("current_position_count"))) + 1
    state["current_position_pct"] = _number(state.get("current_position_pct")) + position
    added_open_risk = updates.get(
        "gap_reentry_open_risk_pct", updates.get("open_risk_pct", 0),
    )
    state["current_open_risk_pct"] = _number(state.get("current_open_risk_pct")) + float(added_open_risk)
    equity = _number(state.get("account_total_value"))
    if state.get("available_cash") is not None and equity > 0:
        cash_required = updates.get(
            "gap_reentry_cash_required_yuan", equity * position / 100.0,
        )
        state["available_cash"] = max(
            0.0, _number(state.get("available_cash")) - float(cash_required),
        )
    industry = _text(row.get("industry") or row.get("sector"))
    theme = _text(row.get("theme") or row.get("theme_label") or row.get("concept") or industry)
    if industry:
        exposures = dict(state.get("sector_exposure_pct") or {})
        exposures[industry] = _number(exposures.get(industry)) + position
        state["sector_exposure_pct"] = exposures
    if theme:
        exposures = dict(state.get("theme_exposure_pct") or {})
        exposures[theme] = _number(exposures.get(theme)) + position
        state["theme_exposure_pct"] = exposures
    if not industry and not theme:
        exposures = dict(state.get("sector_exposure_pct") or {})
        exposures["__UNCATEGORIZED__"] = _number(exposures.get("__UNCATEGORIZED__")) + position
        state["sector_exposure_pct"] = exposures
    factor_path = _text(row.get("factor_path"))
    if factor_path in ("wave3_v1", "limitdown_exhaustion_v1"):
        counts = dict(state.get("factor_position_counts") or {})
        counts[factor_path] = int(_number(counts.get(factor_path))) + 1
        state["factor_position_counts"] = counts
        daily = dict(state.get("factor_new_positions_today") or {})
        daily[factor_path] = int(_number(daily.get(factor_path))) + 1
        state["factor_new_positions_today"] = daily


def my_strict_candidate_builder(context):
    """JoinQuant exporter entrypoint bound to explicit historical providers."""
    if not isinstance(SNAPSHOT_PARAMETERS, Mapping) or not isinstance(SNAPSHOT_MANIFEST, Mapping):
        raise StrategySnapshotError("SNAPSHOT_NOT_CONFIGURED")
    if not callable(_STRICT_FEATURE_PROVIDER):
        raise StrategySnapshotError("STRICT_FEATURE_PROVIDER_REQUIRED")
    if not callable(_PORTFOLIO_STATE_PROVIDER):
        raise StrategySnapshotError("PORTFOLIO_STATE_PROVIDER_REQUIRED")
    rows = list(_STRICT_FEATURE_PROVIDER(context))
    state = dict(_mapping(_PORTFOLIO_STATE_PROVIDER(context), "portfolio_state"))
    missing_state = [name for name in PORTFOLIO_STATE_FIELDS if name not in state]
    if missing_state:
        raise StrategySnapshotError("PORTFOLIO_STATE_INCOMPLETE: " + ",".join(missing_state))
    decision_at = _text(context.decision_at)
    snapshot_by_code = {}
    for _, market in context.snapshot.iterrows():
        snapshot_by_code[clean_code(market.get("code"))] = market
    prepared = []
    for raw in rows:
        raw = dict(_mapping(raw, "candidate"))
        code = clean_code(raw.get("code"))
        if not code:
            raise StrategySnapshotError("CANDIDATE_CODE_REQUIRED")
        features, plain = _timed_features(raw.get("features"), decision_at)
        plain.update(dict(raw.get("decision_fields") or {}))
        plain["code"] = code
        market = snapshot_by_code.get(code)
        if market is None:
            raise StrategySnapshotError("CANDIDATE_MARKET_SNAPSHOT_MISSING: " + code)
        for target, source in (
            ("price", "close"), ("pct_chg", "pct_chg"), ("amount", "cum_amount"),
            ("paused", "paused"), ("prev_close", "prev_close"),
            ("limit_up_price", "high_limit"), ("limit_down_price", "low_limit"),
            ("is_st", "is_st"),
        ):
            if source in market.index:
                plain[target] = market.get(source)
        prepared.append((code, features, plain))
    if len({code for code, _, _ in prepared}) != len(prepared):
        raise StrategySnapshotError("DUPLICATE_CANDIDATE_CODE")
    if not prepared:
        raise StrategySnapshotError("EMPTY_STRICT_CANDIDATE_COHORT")
    try:
        import pandas as pd
    except ImportError:
        raise StrategySnapshotError("PANDAS_REQUIRED")
    scored = score_candidate_frame(pd.DataFrame([plain for _, _, plain in prepared]))
    indexed = []
    for index, (code, features, plain) in enumerate(prepared):
        plain = dict(plain)
        score_override = plain.get("strict_final_score_override")
        plain["final_score"] = float(
            scored.iloc[index]["final_score"]
            if score_override is None else _number(score_override)
        )
        features["final_score"] = {"value": plain["final_score"], "available_at": decision_at}
        indexed.append((code, features, plain))
    indexed.sort(key=lambda item: (-_number(item[2].get("final_score")), item[0]))
    slot_count = int(_ml_parameters(SNAPSHOT_PARAMETERS)["max_positions"])
    output = []
    for order, (code, features, row) in enumerate(indexed, start=1):
        pre_rejection = _text(row.get("pre_rejection_code"))
        if pre_rejection:
            if pre_rejection not in _REJECTION_STAGE_BY_REASON:
                raise StrategySnapshotError(
                    "UNKNOWN_PRE_REJECTION_CODE: " + pre_rejection
                )
            reason, updates = pre_rejection, {}
        else:
            reason, updates = buy_reject_reason(row, state, SNAPSHOT_PARAMETERS)
        selected = not bool(reason)
        target_qty = (
            int(_number(updates.get("target_qty")))
            or _target_qty(
                row,
                state,
                float(updates.get("position_pct", _number(row.get("position_pct")))),
                SNAPSHOT_PARAMETERS,
            )
        ) if selected else 0
        if selected and target_qty <= 0:
            reason, selected = "buy_too_small_for_board_lot", False
            updates = {}
        stage = rejection_stage(reason)
        final_action = "selected" if selected else stage + "_rejected"
        for name, value in (
            ("rule_order", order), ("rule_slot_count", slot_count),
            ("rule_target_qty", target_qty), ("rule_rejection_stage", stage),
            ("rule_rejection_code", reason), ("rule_final_action", final_action),
            ("parameter_snapshot", SNAPSHOT_PARAMETERS), ("training_eligible", True),
        ):
            features[name] = {"value": value, "available_at": decision_at}
        for name in (
            "factor_economics_version",
            "factor_round_trip_cost_yuan",
            "factor_round_trip_cost_rate",
        ):
            if name in updates:
                features[name] = {
                    "value": updates[name], "available_at": decision_at,
                }
        output.append({
            "code": code,
            "features": features,
            "selected": selected,
            "rejection_stage": stage,
            "rejection_code": reason,
            "final_action": final_action,
        })
        if selected:
            _update_state_after_selection(state, row, updates, SNAPSHOT_PARAMETERS)
    if callable(_DECISION_OBSERVER):
        _DECISION_OBSERVER(context, output)
    return output
