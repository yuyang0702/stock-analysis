"""Point-in-time third-wave continuation factor (Python 3.6 compatible)."""

import math

if "FactorDecision" not in globals():
    from factor_contracts import (
        FACTOR_PATH_WAVE3,
        FactorContractError,
        FactorDecision,
        factor_clean_code,
        factor_number,
        factor_setup_id,
        normalize_factor_bars,
    )

WAVE3_FACTOR_VERSION = "2026-08-10.1"
WAVE3_DEFAULTS = {
    "daily_lookback": 90,
    "pivot_window": 2,
    "pivot_atr": 0.8,
    "leg_min_days": 3,
    "leg_max_days": 12,
    "total_min_days": 8,
    "total_max_days": 35,
    "duration_similarity_min": 0.60,
    "gain_similarity_min": 0.50,
    "pullback_min": 0.15,
    "pullback_max": 0.50,
    "pullback_volume_ratio_max": 0.85,
    "setup_valid_days": 3,
    "score_threshold": 75.0,
    "position_cap_pct": 12.0,
    "risk_budget_pct": 0.50,
    "max_hold_days": 10,
    "max_concurrent": 2,
    "max_new_per_day": 1,
}


def _wave3_n(value, default=0.0):
    return factor_number(value, default)


def _wave3_atr(rows, period=14):
    if len(rows) < 2:
        return 0.0
    values = []
    previous = _wave3_n(rows[0].get("close"))
    for row in rows[1:]:
        high = _wave3_n(row.get("high"))
        low = _wave3_n(row.get("low"))
        if min(high, low, previous) <= 0:
            previous = _wave3_n(row.get("close"), previous)
            continue
        values.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = _wave3_n(row.get("close"), previous)
    return sum(values[-period:]) / len(values[-period:]) if values else 0.0


def _wave3_adjusted(rows):
    if not rows:
        return []
    last_factor = _wave3_n(rows[-1].get("factor"), 1.0)
    if last_factor <= 0:
        last_factor = 1.0
    result = []
    for row in rows:
        factor = _wave3_n(row.get("factor"), last_factor)
        if factor <= 0:
            raise FactorContractError("WAVE3_ADJUSTMENT_FACTOR_REQUIRED")
        item = dict(row)
        for key in ("open", "high", "low", "close"):
            value = _wave3_n(row.get(key))
            item[key] = value * factor / last_factor
        item["volume"] = _wave3_n(row.get("volume"))
        item["amount"] = _wave3_n(row.get("amount"), _wave3_n(row.get("money")))
        result.append(item)
    return result


def _wave3_local_pivots(rows, window, atr_multiplier):
    """Return pivots confirmed only by bars already completed at decision time."""
    if len(rows) < window * 2 + 5:
        return []
    atr = _wave3_atr(rows)
    if atr <= 0:
        return []
    raw = []
    for index in range(window, len(rows) - window):
        low = _wave3_n(rows[index].get("low"))
        high = _wave3_n(rows[index].get("high"))
        neighbours = rows[index - window:index] + rows[index + 1:index + window + 1]
        if low > 0 and all(low < _wave3_n(item.get("low"), low) for item in neighbours):
            raw.append({"kind": "L", "index": index, "price": low, "confirmed": index + window})
        if high > 0 and all(high > _wave3_n(item.get("high"), high) for item in neighbours):
            raw.append({"kind": "H", "index": index, "price": high, "confirmed": index + window})
    raw.sort(key=lambda item: (item["index"], item["kind"]))
    collapsed = []
    for item in raw:
        if collapsed and collapsed[-1]["kind"] == item["kind"]:
            better = (
                item["price"] < collapsed[-1]["price"]
                if item["kind"] == "L" else item["price"] > collapsed[-1]["price"]
            )
            if better:
                collapsed[-1] = item
            continue
        if collapsed and abs(item["price"] - collapsed[-1]["price"]) < atr * atr_multiplier:
            continue
        collapsed.append(item)
    return collapsed


def _wave3_mean_volume(rows, start, end):
    values = [
        _wave3_n(row.get("volume"))
        for row in rows[max(0, start):min(len(rows), end + 1)]
        if _wave3_n(row.get("volume")) > 0
    ]
    return sum(values) / len(values) if values else 0.0


def _wave3_pattern(rows, config):
    pivots = _wave3_local_pivots(
        rows,
        int(config["pivot_window"]),
        float(config["pivot_atr"]),
    )
    best = None
    for offset in range(max(0, len(pivots) - 14), max(0, len(pivots) - 4)):
        group = pivots[offset:offset + 5]
        if len(group) != 5 or [item["kind"] for item in group] != ["L", "H", "L", "H", "L"]:
            continue
        l0, h1, l1, h2, l2 = group
        leg1_days = h1["index"] - l0["index"]
        leg2_days = h2["index"] - l1["index"]
        total_days = l2["index"] - l0["index"]
        if not (
            config["leg_min_days"] <= leg1_days <= config["leg_max_days"]
            and config["leg_min_days"] <= leg2_days <= config["leg_max_days"]
            and config["total_min_days"] <= total_days <= config["total_max_days"]
        ):
            continue
        gain1 = h1["price"] / l0["price"] - 1.0
        gain2 = h2["price"] / l1["price"] - 1.0
        if min(gain1, gain2) <= 0:
            continue
        duration_similarity = min(leg1_days, leg2_days) / float(max(leg1_days, leg2_days))
        gain_similarity = min(gain1, gain2) / max(gain1, gain2)
        pullback1 = (h1["price"] - l1["price"]) / max(h1["price"] - l0["price"], 1e-9)
        pullback2 = (h2["price"] - l2["price"]) / max(h2["price"] - l1["price"], 1e-9)
        setup_age = len(rows) - 1 - l2["confirmed"]
        if not (
            duration_similarity >= config["duration_similarity_min"]
            and gain_similarity >= config["gain_similarity_min"]
            and h2["price"] > h1["price"]
            and l2["price"] > l1["price"]
            and config["pullback_min"] <= pullback1 <= config["pullback_max"]
            and config["pullback_min"] <= pullback2 <= config["pullback_max"]
            and 0 <= setup_age <= config["setup_valid_days"]
        ):
            continue
        up1_volume = _wave3_mean_volume(rows, l0["index"], h1["index"])
        down1_volume = _wave3_mean_volume(rows, h1["index"] + 1, l1["index"])
        up2_volume = _wave3_mean_volume(rows, l1["index"], h2["index"])
        down2_volume = _wave3_mean_volume(rows, h2["index"] + 1, l2["index"])
        volume_ratio1 = down1_volume / up1_volume if up1_volume > 0 else 99.0
        volume_ratio2 = down2_volume / up2_volume if up2_volume > 0 else 99.0
        if max(volume_ratio1, volume_ratio2) > config["pullback_volume_ratio_max"]:
            continue
        structure = 24.0
        structure += min(8.0, duration_similarity * 8.0)
        structure += min(8.0, gain_similarity * 8.0)
        price_volume = 10.0 * max(0.0, 1.0 - volume_ratio1)
        price_volume += 10.0 * max(0.0, 1.0 - volume_ratio2)
        if l2["price"] > h1["price"]:
            structure = min(40.0, structure + 4.0)
        score = min(60.0, structure + price_volume)
        candidate = {
            "anchors": (l0, h1, l1, h2, l2),
            "score": score,
            "structure_score": min(40.0, structure),
            "price_volume_score": min(20.0, price_volume),
            "duration_similarity": duration_similarity,
            "gain_similarity": gain_similarity,
            "gain1": gain1,
            "gain2": gain2,
            "pullback1": pullback1,
            "pullback2": pullback2,
            "pullback_volume_ratio1": volume_ratio1,
            "pullback_volume_ratio2": volume_ratio2,
            "setup_age_days": setup_age,
        }
        if best is None or (candidate["score"], l2["index"]) > (best["score"], best["anchors"][-1]["index"]):
            best = candidate
    return best


def _wave3_intraday_trigger(intraday, atr14, relative_strength):
    if len(intraday) < 4:
        return False, "wave3_trigger_pending", {}
    bars = intraday[-4:]
    latest = bars[-1]
    price = _wave3_n(latest.get("close"))
    total_volume = sum(max(_wave3_n(row.get("volume")), 0.0) for row in intraday)
    total_money = sum(max(_wave3_n(row.get("money"), _wave3_n(row.get("amount"))), 0.0) for row in intraday)
    vwap = total_money / total_volume if total_volume > 0 else 0.0
    breakout = max(_wave3_n(row.get("high")) for row in bars[:-1])
    if relative_strength <= 0:
        return False, "wave3_relative_strength_weak", {"vwap": vwap, "breakout_price": breakout}
    if not (price > vwap > 0 and price > breakout > 0):
        return False, "wave3_trigger_pending", {"vwap": vwap, "breakout_price": breakout}
    chase_limit = min(0.02, 0.5 * atr14 / breakout if breakout > 0 and atr14 > 0 else 0.02)
    chase_pct = price / breakout - 1.0
    if chase_pct > chase_limit:
        return False, "wave3_chasing", {
            "vwap": vwap, "breakout_price": breakout,
            "chase_pct": chase_pct, "chase_limit": chase_limit,
        }
    return True, "", {
        "vwap": vwap, "breakout_price": breakout,
        "chase_pct": chase_pct, "chase_limit": chase_limit,
    }


def evaluate_wave3_factor(
    code,
    decision_at,
    daily_bars,
    intraday_bars=(),
    industry_relative_strength=0.0,
    market_state="NORMAL",
    theme_heat_score=0.0,
    disclosure_risk="clear",
    config=None,
):
    settings = dict(WAVE3_DEFAULTS)
    settings.update(dict(config or {}))
    clean = factor_clean_code(code)
    daily = normalize_factor_bars(daily_bars, decision_at, require_prior_day=True)
    daily = _wave3_adjusted(daily[-int(settings["daily_lookback"]):])
    if len(daily) < 35:
        return FactorDecision(
            FACTOR_PATH_WAVE3, state="insufficient_history",
            rejection_code="wave3_history_insufficient",
            reasons=("日线少于35个已完成交易日",),
        )
    if str(disclosure_risk or "").lower() in ("severe", "unknown_negative"):
        return FactorDecision(
            FACTOR_PATH_WAVE3, state="disclosure_veto",
            rejection_code="factor_disclosure_risk_veto",
            reasons=("严重或无法判定的负面公告风险",),
        )
    pattern = _wave3_pattern(daily, settings)
    if pattern is None:
        return FactorDecision(
            FACTOR_PATH_WAVE3, state="setup_absent",
            rejection_code="wave3_setup_invalid",
            reasons=("未形成满足时长、幅度、抬高低点和缩量约束的两浪结构",),
        )
    relative = _wave3_n(industry_relative_strength)
    market_bonus = 0.0 if str(market_state).upper() == "RISK_OFF" else (8.0 if str(market_state).upper() == "CAUTION" else 12.0)
    relative_score = min(20.0, max(0.0, 10.0 + relative * 100.0))
    theme_bonus = min(8.0, max(0.0, _wave3_n(theme_heat_score) / 100.0 * 8.0))
    score = min(100.0, pattern["score"] + relative_score + market_bonus + theme_bonus)
    anchors = pattern["anchors"]
    anchor_tokens = [
        "%s@%s" % (item["kind"], daily[item["index"]]["available_at"][:10])
        for item in anchors
    ]
    setup_id = factor_setup_id(FACTOR_PATH_WAVE3, clean, anchor_tokens, WAVE3_FACTOR_VERSION)
    intraday = normalize_factor_bars(intraday_bars, decision_at, require_prior_day=False)
    atr14 = _wave3_atr(daily)
    triggered, trigger_rejection, trigger_features = _wave3_intraday_trigger(
        intraday, atr14, relative,
    )
    if score < float(settings["score_threshold"]):
        triggered = False
        trigger_rejection = "wave3_score_below_threshold"
    if str(market_state).upper() == "RISK_OFF":
        triggered = False
        trigger_rejection = "factor_market_risk_off"
    latest_price = _wave3_n(intraday[-1].get("close")) if intraday else _wave3_n(daily[-1].get("close"))
    l2 = anchors[-1]["price"]
    stop = round(l2 - 0.3 * atr14, 2)
    risk = latest_price - stop
    take = round(latest_price + 2.0 * risk, 2) if risk > 0 else 0.0
    if triggered and not (0 < stop < latest_price < take):
        triggered = False
        trigger_rejection = "factor_invalid_price_plan"
    features = {
        "factor_version": WAVE3_FACTOR_VERSION,
        "factor_path": FACTOR_PATH_WAVE3,
        "factor_score": round(score, 6),
        "wave3_structure_score": round(pattern["structure_score"], 6),
        "wave3_price_volume_score": round(pattern["price_volume_score"], 6),
        "wave3_duration_similarity": round(pattern["duration_similarity"], 6),
        "wave3_gain_similarity": round(pattern["gain_similarity"], 6),
        "wave3_gain1_pct": round(pattern["gain1"] * 100.0, 6),
        "wave3_gain2_pct": round(pattern["gain2"] * 100.0, 6),
        "wave3_pullback1_pct": round(pattern["pullback1"] * 100.0, 6),
        "wave3_pullback2_pct": round(pattern["pullback2"] * 100.0, 6),
        "wave3_pullback_volume_ratio1": round(pattern["pullback_volume_ratio1"], 6),
        "wave3_pullback_volume_ratio2": round(pattern["pullback_volume_ratio2"], 6),
        "wave3_setup_age_days": pattern["setup_age_days"],
        "industry_relative_strength": round(relative, 6),
        "atr14": round(atr14, 6),
    }
    features.update(trigger_features)
    return FactorDecision(
        FACTOR_PATH_WAVE3,
        setup_id=setup_id,
        eligible=True,
        triggered=triggered,
        score=score,
        state="triggered" if triggered else "setup_ready",
        rejection_code="" if triggered else trigger_rejection,
        reasons=(
            "两浪上涨时长和幅度相近",
            "第二浪高点与回撤低点同步抬高",
            "两次回撤成交量低于上涨阶段",
        ),
        features=features,
        entry_price=latest_price if triggered else 0.0,
        stop_loss=stop if triggered else 0.0,
        take_profit=take if triggered else 0.0,
        position_cap_pct=settings["position_cap_pct"],
        risk_budget_pct=settings["risk_budget_pct"],
        max_hold_days=settings["max_hold_days"],
        max_concurrent=settings["max_concurrent"],
        max_new_per_day=settings["max_new_per_day"],
        simulation_only=True,
    )
