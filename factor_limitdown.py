"""Point-in-time limit-down selling-exhaustion factor."""

from datetime import time

if "FactorDecision" not in globals():
    from factor_contracts import (
        FACTOR_PATH_LIMITDOWN,
        FactorDecision,
        factor_aware_datetime,
        factor_clean_code,
        factor_number,
        factor_setup_id,
        normalize_factor_bars,
    )


LIMITDOWN_FACTOR_VERSION = "2026-08-10.1"
LIMITDOWN_DEFAULTS = {
    "min_locked_days": 2,
    "max_locked_days": 4,
    "min_listing_days": 250,
    "min_avg_amount_20d": 100000000.0,
    "first_open_turnover_ratio_min": 1.5,
    "close_location_min": 0.65,
    "rebound_from_low_min": 0.03,
    "decision_not_before": "09:50",
    "next_day_gap_min": -0.03,
    "next_day_gap_max": 0.05,
    "score_threshold": 75.0,
    "position_cap_pct": 8.0,
    "risk_budget_pct": 0.35,
    "max_hold_days": 3,
    "max_concurrent": 1,
    "max_new_per_day": 1,
}


def _ld_n(value, default=0.0):
    return factor_number(value, default)


def _ld_main_board(code):
    clean = factor_clean_code(code)
    return clean.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


def _ld_amount(row):
    return _ld_n(row.get("amount"), _ld_n(row.get("money")))


def _ld_locked(row):
    low_limit = _ld_n(row.get("low_limit"))
    close = _ld_n(row.get("close"))
    high = _ld_n(row.get("high"))
    if min(low_limit, close, high) <= 0:
        previous = _ld_n(row.get("pre_close"), _ld_n(row.get("prev_close")))
        return previous > 0 and close / previous - 1.0 <= -0.095 and high <= close * 1.003
    return close <= low_limit + 0.011 and high <= low_limit + 0.011


def _ld_first_open(row):
    low_limit = _ld_n(row.get("low_limit"))
    close = _ld_n(row.get("close"))
    low = _ld_n(row.get("low"))
    high = _ld_n(row.get("high"))
    if min(close, low, high) <= 0:
        return False
    if low_limit > 0:
        return low <= low_limit + 0.011 and high > low_limit + 0.011 and close > low_limit + 0.011
    previous = _ld_n(row.get("pre_close"), _ld_n(row.get("prev_close")))
    return previous > 0 and low / previous - 1.0 <= -0.095 and close > low * 1.01


def _ld_event(daily, settings):
    for open_index in range(len(daily) - 1, max(0, len(daily) - 5), -1):
        if not _ld_first_open(daily[open_index]):
            continue
        locked = 0
        cursor = open_index - 1
        while cursor >= 0 and _ld_locked(daily[cursor]) and locked <= settings["max_locked_days"]:
            locked += 1
            cursor -= 1
        if settings["min_locked_days"] <= locked <= settings["max_locked_days"]:
            return open_index, locked
    return None


def _ld_first_open_quality(daily, index, settings):
    row = daily[index]
    prior = [_ld_amount(item) for item in daily[max(0, index - 20):index] if _ld_amount(item) > 0]
    baseline = sum(prior) / len(prior) if prior else 0.0
    turnover_ratio = _ld_amount(row) / baseline if baseline > 0 else 0.0
    high = _ld_n(row.get("high"))
    low = _ld_n(row.get("low"))
    close = _ld_n(row.get("close"))
    location = (close - low) / (high - low) if high > low else 0.0
    rebound = close / low - 1.0 if low > 0 else 0.0
    return turnover_ratio, location, rebound


def _ld_intraday_quality(intraday, event_low, settings, decision_at):
    decision = factor_aware_datetime(decision_at)
    threshold_hour, threshold_minute = [int(value) for value in settings["decision_not_before"].split(":")]
    if decision.time() < time(threshold_hour, threshold_minute):
        return False, "limitdown_wait_next_day_confirm", {}
    if len(intraday) < 4:
        return False, "limitdown_wait_next_day_confirm", {}
    first = intraday[0]
    latest = intraday[-1]
    previous_close = _ld_n(first.get("prev_close"), _ld_n(first.get("pre_close")))
    open_price = _ld_n(first.get("open"))
    price = _ld_n(latest.get("close"))
    total_volume = sum(max(_ld_n(row.get("volume")), 0.0) for row in intraday)
    total_money = sum(max(_ld_n(row.get("money"), _ld_n(row.get("amount"))), 0.0) for row in intraday)
    vwap = total_money / total_volume if total_volume > 0 else 0.0
    gap = open_price / previous_close - 1.0 if previous_close > 0 else 99.0
    current_low = min(_ld_n(row.get("low"), price) for row in intraday)
    last_60 = intraday[-12:]
    resealed = any(
        _ld_n(row.get("low_limit")) > 0
        and _ld_n(row.get("close")) <= _ld_n(row.get("low_limit")) + 0.011
        for row in last_60
    )
    features = {
        "limitdown_next_day_gap_pct": gap * 100.0,
        "limitdown_next_day_vwap": vwap,
        "limitdown_next_day_low": current_low,
        "limitdown_resealed_last_60m": resealed,
    }
    if resealed:
        return False, "limitdown_resealed", features
    if not (settings["next_day_gap_min"] <= gap <= settings["next_day_gap_max"]):
        return False, "limitdown_next_day_gap_invalid", features
    if not (price > vwap > 0):
        return False, "limitdown_below_vwap", features
    if event_low > 0 and current_low < event_low - 0.011:
        return False, "limitdown_open_low_broken", features
    return True, "", features


def evaluate_limitdown_exhaustion_factor(
    code,
    decision_at,
    daily_bars,
    intraday_bars=(),
    listing_days=0,
    is_st=False,
    delisting=False,
    sector_limitdown_acceleration=0.0,
    disclosure_risk="clear",
    market_state="NORMAL",
    config=None,
):
    settings = dict(LIMITDOWN_DEFAULTS)
    settings.update(dict(config or {}))
    clean = factor_clean_code(code)
    if not _ld_main_board(clean) or bool(is_st) or bool(delisting):
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="universe_veto",
            rejection_code="limitdown_board_ineligible",
            reasons=("仅允许非ST、非退市风险的沪深主板股票",),
        )
    if int(_ld_n(listing_days)) < int(settings["min_listing_days"]):
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="listing_age_veto",
            rejection_code="limitdown_listing_age_insufficient",
            reasons=("上市交易历史不足250天",),
        )
    if str(disclosure_risk or "").lower() in ("severe", "unknown_negative"):
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="disclosure_veto",
            rejection_code="factor_disclosure_risk_veto",
            reasons=("严重或无法判定的负面公告风险",),
        )
    if _ld_n(sector_limitdown_acceleration) > 0:
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="sector_veto",
            rejection_code="limitdown_sector_accelerating_down",
            reasons=("所属板块跌停家数仍在加速",),
        )
    if str(market_state).upper() == "RISK_OFF":
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="market_veto",
            rejection_code="factor_market_risk_off",
            reasons=("市场处于风险释放状态",),
        )
    daily = normalize_factor_bars(daily_bars, decision_at, require_prior_day=True)
    if len(daily) < 25:
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="insufficient_history",
            rejection_code="limitdown_history_insufficient",
            reasons=("日线少于25个已完成交易日",),
        )
    average_amount = sum(_ld_amount(row) for row in daily[-20:]) / 20.0
    if average_amount < float(settings["min_avg_amount_20d"]):
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="liquidity_veto",
            rejection_code="limitdown_liquidity_insufficient",
            reasons=("近20日平均成交额不足1亿元",),
        )
    event = _ld_event(daily, settings)
    if event is None:
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="event_absent",
            rejection_code="limitdown_event_absent",
            reasons=("未发现2至4个连续跌停后的首次开板日",),
        )
    open_index, locked_days = event
    turnover_ratio, location, rebound = _ld_first_open_quality(daily, open_index, settings)
    if not (
        turnover_ratio >= settings["first_open_turnover_ratio_min"]
        and location >= settings["close_location_min"]
        and rebound >= settings["rebound_from_low_min"]
    ):
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN, state="absorption_unconfirmed",
            rejection_code="limitdown_absorption_unconfirmed",
            reasons=("首次开板换手、收盘位置或低点反弹未达到吸筹确认阈值",),
            features={
                "limitdown_locked_days": locked_days,
                "limitdown_turnover_ratio": turnover_ratio,
                "limitdown_close_location": location,
                "limitdown_rebound_from_low_pct": rebound * 100.0,
            },
        )
    event_age = len(daily) - 1 - open_index
    event_row = daily[open_index]
    anchors = [
        "locked=%s" % locked_days,
        "opened=%s" % event_row["available_at"][:10],
    ]
    setup_id = factor_setup_id(
        FACTOR_PATH_LIMITDOWN, clean, anchors, LIMITDOWN_FACTOR_VERSION,
    )
    base_score = 40.0
    base_score += min(20.0, max(0.0, (turnover_ratio - 1.0) * 20.0))
    base_score += min(15.0, max(0.0, location * 15.0))
    base_score += min(15.0, max(0.0, rebound * 200.0))
    base_score += 10.0 if locked_days in (2, 3) else 5.0
    score = min(100.0, base_score)
    features = {
        "factor_version": LIMITDOWN_FACTOR_VERSION,
        "factor_path": FACTOR_PATH_LIMITDOWN,
        "factor_score": round(score, 6),
        "limitdown_locked_days": locked_days,
        "limitdown_event_age_days": event_age,
        "limitdown_turnover_ratio": round(turnover_ratio, 6),
        "limitdown_close_location": round(location, 6),
        "limitdown_rebound_from_low_pct": round(rebound * 100.0, 6),
        "limitdown_avg_amount_20d": round(average_amount, 2),
    }
    raw_intraday = list(intraday_bars or ())
    if event_age == 0 and not raw_intraday:
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN,
            setup_id=setup_id,
            eligible=True,
            triggered=False,
            score=score,
            state="open_confirmed",
            rejection_code="limitdown_wait_next_day_confirm",
            reasons=("首次开板日质量通过，等待下一交易日09:50确认",),
            features=features,
            position_cap_pct=settings["position_cap_pct"],
            risk_budget_pct=settings["risk_budget_pct"],
            max_hold_days=settings["max_hold_days"],
            max_concurrent=settings["max_concurrent"],
            max_new_per_day=settings["max_new_per_day"],
        )
    if event_age != 0:
        return FactorDecision(
            FACTOR_PATH_LIMITDOWN,
            setup_id=setup_id,
            eligible=False,
            triggered=False,
            score=score,
            state="expired",
            rejection_code="limitdown_setup_expired",
            reasons=("首次开板后的下一交易日确认窗口已经结束",),
            features=features,
        )
    intraday = normalize_factor_bars(
        raw_intraday, decision_at, require_prior_day=False
    )
    event_low = _ld_n(event_row.get("low"))
    triggered, rejection, trigger_features = _ld_intraday_quality(
        intraday, event_low, settings, decision_at,
    )
    features.update(trigger_features)
    if score < float(settings["score_threshold"]):
        triggered = False
        rejection = "limitdown_score_below_threshold"
    entry = _ld_n(intraday[-1].get("close")) if intraday else 0.0
    atr_values = []
    for previous, current in zip(daily[-15:-1], daily[-14:]):
        prior_close = _ld_n(previous.get("close"))
        high = _ld_n(current.get("high"))
        low = _ld_n(current.get("low"))
        if min(prior_close, high, low) > 0:
            atr_values.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    atr14 = sum(atr_values) / len(atr_values) if atr_values else 0.0
    stop = round(event_low - 0.3 * atr14, 2)
    risk = entry - stop
    take = round(entry + 1.5 * risk, 2) if risk > 0 else 0.0
    if triggered and not (0 < stop < entry < take):
        triggered = False
        rejection = "factor_invalid_price_plan"
    features["atr14"] = round(atr14, 6)
    return FactorDecision(
        FACTOR_PATH_LIMITDOWN,
        setup_id=setup_id,
        eligible=True,
        triggered=triggered,
        score=score,
        state="signal" if triggered else "next_day_confirming",
        rejection_code="" if triggered else rejection,
        reasons=(
            "连续跌停后的首次开板出现放量承接",
            "收盘位置和低点反弹确认抛压衰竭",
            "下一交易日09:50后价格位于VWAP上方且未破开板低点",
        ),
        features=features,
        entry_price=entry if triggered else 0.0,
        stop_loss=stop if triggered else 0.0,
        take_profit=take if triggered else 0.0,
        position_cap_pct=settings["position_cap_pct"],
        risk_budget_pct=settings["risk_budget_pct"],
        max_hold_days=settings["max_hold_days"],
        max_concurrent=settings["max_concurrent"],
        max_new_per_day=settings["max_new_per_day"],
        simulation_only=True,
    )
