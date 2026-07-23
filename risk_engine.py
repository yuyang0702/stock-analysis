from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any, Mapping

from strategy_profile import StrategyProfile, build_strategy_profile, normalize_mode


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        if not isfinite(result):
            return default
        return result
    except Exception:
        return default


def _market_state(row: Mapping[str, Any], market_info: Mapping[str, Any] | None) -> str:
    return _text((market_info or {}).get("state") or row.get("market_state"))


def _trend_state(row: Mapping[str, Any]) -> str:
    return _text(row.get("trend_state"))


def _limit_quality(row: Mapping[str, Any]) -> str:
    return _text(row.get("limit_quality"))


def _news_score(row: Mapping[str, Any]) -> float:
    return _float(row.get("news_score"), 0.0)


def _amount(row: Mapping[str, Any]) -> float:
    return _float(row.get("amount"), 0.0)


def _pct(row: Mapping[str, Any]) -> float:
    return _float(row.get("pct_chg"), 0.0)


def _price(row: Mapping[str, Any]) -> float:
    for key in ("price", "close", "current_price", "hold_current_price"):
        value = _float(row.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def _support_level(row: Mapping[str, Any], price: float) -> float:
    support = _float(row.get("support_level"), 0.0)
    if support > 0:
        return support
    ma20 = _float(row.get("ma20"), 0.0)
    ma30 = _float(row.get("ma30"), 0.0)
    if ma20 > 0 and ma30 > 0:
        return min(ma20, ma30)
    if ma20 > 0:
        return ma20
    return price * 0.97 if price > 0 else 0.0


def _pressure_level(row: Mapping[str, Any], price: float) -> float:
    pressure = _float(row.get("pressure_level"), 0.0)
    if pressure > 0:
        return pressure
    high = _float(row.get("high"), 0.0)
    if high > 0:
        return high
    return price * 1.03 if price > 0 else 0.0


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    mode: str
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_per_share: float
    risk_reward: float
    position_pct: float
    reason: str
    confidence: float
    take_profit_2: float | None = None
    stop_loss_pct: float | None = None
    entry_gap_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SignalLifecycle:
    """Track how long a buy signal has remained unresolved."""

    first_seen: str
    age_days: int
    state: str
    action: str
    note: str
    stale_days: int
    stop_days: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def signal_policy(mode: str) -> dict[str, int]:
    """Return the default age thresholds for a signal."""

    mode = normalize_mode(mode)
    if mode == "short":
        return {"fresh_days": 1, "stale_days": 2, "stop_days": 3}
    return {"fresh_days": 3, "stale_days": 5, "stop_days": 10}


def _parse_day(value: Any) -> date | None:
    raw = _text(value)
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw[: len(fmt)], fmt).date()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(raw.replace("/", "-")).date()
    except Exception:
        return None


def build_signal_lifecycle(
    row: Mapping[str, Any],
    mode: str,
    current_day: date | None = None,
    first_seen: Any | None = None,
) -> SignalLifecycle:
    """Convert a signal age into a human-readable lifecycle state."""

    current_day = current_day or datetime.now().date()
    policy = signal_policy(mode)
    first_day = _parse_day(first_seen) or current_day
    age_days = max(0, (current_day - first_day).days)
    if age_days <= policy["fresh_days"]:
        state = "fresh"
        action = "continue"
        note = "信号新鲜，继续观察"
    elif age_days <= policy["stale_days"]:
        state = "watch"
        action = "watch"
        note = "信号开始衰减，注意是否还能走出来"
    elif age_days <= policy["stop_days"]:
        state = "stale"
        action = "reevaluate"
        note = "信号已偏旧，建议重新评估"
    else:
        state = "time_stop"
        action = "time_stop"
        note = "超过有效期，建议时间止损"

    price = _price(row)
    entry_price = _float(row.get("entry_price"), price)
    take_profit = _float(row.get("take_profit"), 0.0)
    stop_loss = _float(row.get("stop_loss"), 0.0)
    if price > 0 and take_profit > 0 and stop_loss > 0:
        if price >= take_profit:
            state = "target_hit"
            action = "take_profit"
            note = "已达到目标位"
        elif price <= stop_loss:
            state = "stop_hit"
            action = "stop_loss"
            note = "已触及止损位"
        elif entry_price > 0 and abs(price - entry_price) / entry_price > 0.08 and age_days >= policy["stale_days"]:
            state = "stale"
            action = "reevaluate"
            note = "价格偏离入场逻辑，建议重新定价"

    return SignalLifecycle(
        first_seen=first_day.isoformat(),
        age_days=age_days,
        state=state,
        action=action,
        note=note,
        stale_days=policy["stale_days"],
        stop_days=policy["stop_days"],
    )


def classify_trade_mode(
    row: Mapping[str, Any],
    market_info: Mapping[str, Any] | None = None,
    market_news_state: str = "",
) -> str:
    """根据当前扫描信息选择 short / mid。

    short 偏突破和事件驱动，mid 偏趋势和回踩确认。
    """

    market_state = _market_state(row, market_info)
    trend_state = _trend_state(row)
    limit_quality = _limit_quality(row)
    pressure_label = _text(row.get("pressure_label"))
    news_score = _news_score(row)
    pct_chg = _pct(row)

    breakout_signals = 0
    if limit_quality in {"一字涨停", "封板较强", "强势拉升"}:
        breakout_signals += 2
    if pressure_label in {"突破/新高", "贴近前高"}:
        breakout_signals += 2
    if news_score >= 6:
        breakout_signals += 1
    if pct_chg >= 5:
        breakout_signals += 1
    if market_news_state in {"题材催化", "情绪偏强"}:
        breakout_signals += 1

    trend_signals = 0
    if trend_state in {"强势上行", "趋势修复", "回踩企稳"}:
        trend_signals += 2
    if _float(row.get("ma5"), 0.0) >= _float(row.get("ma10"), 0.0) > 0:
        trend_signals += 1
    if _float(row.get("ma10"), 0.0) >= _float(row.get("ma20"), 0.0) > 0:
        trend_signals += 1
    if market_state in {"强势进攻", "温和修复"}:
        trend_signals += 1

    if breakout_signals >= 4 and limit_quality in {"一字涨停", "封板较强", "强势拉升"}:
        return "short"
    if breakout_signals >= max(3, trend_signals):
        return "short"
    return "mid"


def _allowed_trade(row: Mapping[str, Any], market_info: Mapping[str, Any] | None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    market_state = _market_state(row, market_info)
    trend_state = _trend_state(row)
    price = _price(row)
    amount = _amount(row)
    pct_chg = _pct(row)

    if price <= 0:
        reasons.append("价格缺失")
    if amount and amount < 30_000_000:
        reasons.append("成交额偏低")
    if market_state == "风险释放":
        reasons.append("大盘风险释放")
    if trend_state == "明显破坏" or trend_state == "弱势整理" and pct_chg < -1.5:
        reasons.append("趋势走弱")
    if _text(row.get("limit_quality")) == "一字跌停":
        reasons.append("跌停封死")

    return (len(reasons) == 0, reasons)


def _confidence_score(row: Mapping[str, Any], allowed: bool, mode: str) -> float:
    market_state = _text(row.get("market_state"))
    trend_state = _trend_state(row)
    limit_quality = _limit_quality(row)
    news_score = _news_score(row)
    pct_chg = _pct(row)

    score = 0.52 if mode == "short" else 0.48
    if market_state in {"强势进攻", "温和修复"}:
        score += 0.10
    elif market_state == "风险释放":
        score -= 0.28
    if trend_state in {"强势上行", "趋势修复"}:
        score += 0.10
    if limit_quality in {"一字涨停", "封板较强", "强势拉升"}:
        score += 0.12
    if news_score >= 6:
        score += 0.08
    if abs(pct_chg) >= 6:
        score += 0.05
    if not allowed:
        score -= 0.18
    return max(0.0, min(1.0, round(score, 3)))


def build_risk_decision(
    row: Mapping[str, Any],
    market_info: Mapping[str, Any] | None = None,
    profile: StrategyProfile | None = None,
    market_news_state: str = "",
    holding: Mapping[str, Any] | None = None,
) -> RiskDecision:
    """根据扫描结果给出结构化交易建议。"""

    mode = normalize_mode(profile.mode if profile else classify_trade_mode(row, market_info, market_news_state))
    profile = profile or build_strategy_profile(mode)

    allowed, disallow_reasons = _allowed_trade(row, market_info)
    price = _price(row)
    if holding is not None:
        price = _float(holding.get("current_price"), price) or price
        if price <= 0:
            price = _float(holding.get("cost_price"), price)

    support = _support_level(row, price)
    pressure = _pressure_level(row, price)
    atr = _float(row.get("atr14"), 0.0)
    if atr <= 0:
        atr = max(price * profile.atr_floor_pct / 100.0, 0.01) if price > 0 else 0.01
    support_gap_pct = round((price - support) / support * 100.0, 2) if price > 0 and support > 0 else None

    breakout_like = mode == "short" or _limit_quality(row) in {"一字涨停", "封板较强", "强势拉升"}
    if breakout_like:
        base_entry = pressure * (1 + profile.entry_confirm_pct / 100.0) if pressure > 0 else price
        reason_parts = ["突破型"]
    else:
        base_entry = support * (1 + profile.entry_confirm_pct / 100.0) if support > 0 else price
        reason_parts = ["回踩型"]

    entry_price = max(price, base_entry) if price > 0 else base_entry
    entry_price = round(entry_price, 2)

    atr_stop = entry_price - atr * profile.stop_atr_mult
    support_stop = support * (1 - profile.support_buffer_pct / 100.0) if support > 0 else 0.0
    if support_stop > 0:
        stop_loss = min(atr_stop, support_stop)
    else:
        stop_loss = atr_stop

    if stop_loss >= entry_price:
        fallback_stop = entry_price * (1 - profile.max_loss_pct / 100.0)
        stop_loss = fallback_stop if fallback_stop < entry_price else entry_price * 0.97

    stop_loss = round(max(stop_loss, 0.01), 2)
    risk_per_share = round(max(entry_price - stop_loss, 0.0), 2)
    if risk_per_share <= 0:
        risk_per_share = round(max(entry_price * profile.max_loss_pct / 100.0, 0.01), 2)
        stop_loss = round(max(entry_price - risk_per_share, 0.01), 2)

    raw_take_profit = entry_price + risk_per_share * profile.take_profit_r
    if mode == "mid" and pressure > 0:
        pressure_target = pressure * (1 - profile.pressure_buffer_pct / 100.0)
        take_profit = min(raw_take_profit, pressure_target)
        if take_profit <= entry_price:
            take_profit = entry_price
    else:
        take_profit = raw_take_profit
    take_profit = round(take_profit, 2)
    take_profit_2 = round(entry_price + risk_per_share * (profile.take_profit_r + 1.0), 2)
    risk_reward = round((take_profit - entry_price) / risk_per_share, 2) if risk_per_share > 0 else 0.0
    stop_loss_pct = round(risk_per_share / entry_price * 100.0, 2) if entry_price > 0 else None
    entry_gap_pct = round((entry_price - price) / price * 100.0, 2) if price > 0 else None

    position_pct = profile.position_cap_pct
    if stop_loss_pct and stop_loss_pct > 0:
        position_pct = min(profile.position_cap_pct, round(profile.max_loss_pct / stop_loss_pct * 100.0, 2))
    position_pct = round(max(0.0, position_pct), 2)

    market_state = _market_state(row, market_info)
    trend_state = _trend_state(row)
    news_score = _news_score(row)
    if market_state == "风险释放":
        allowed = False
        reason_parts.append("大盘风险释放")
    if _amount(row) and _amount(row) < 30_000_000:
        allowed = False
        reason_parts.append("流动性不足")
    if mode == "mid" and support_gap_pct is not None and support_gap_pct > max(5.0, profile.entry_confirm_pct + 4.0):
        allowed = False
        reason_parts.append("追高偏离支撑")
    if risk_reward < profile.min_risk_reward:
        allowed = False
        reason_parts.append("盈亏比偏低")
    if trend_state in {"明显破坏", "弱势整理"} and mode == "mid":
        allowed = False
        reason_parts.append("趋势未修复")

    if news_score >= 6:
        reason_parts.append("新闻催化")
    if mode == "short":
        reason_parts.append("适合短线突破")
    else:
        reason_parts.append("适合回踩确认")

    reason = "，".join(dict.fromkeys(reason_parts))
    confidence = _confidence_score(row, allowed, mode)

    if disallow_reasons:
        reason = "；".join(disallow_reasons) + "。"
        if news_score >= 6:
            reason += "但新闻面仍有催化。"

    return RiskDecision(
        allowed=allowed,
        mode=mode,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_per_share=risk_per_share,
        risk_reward=risk_reward,
        position_pct=position_pct,
        reason=reason,
        confidence=confidence,
        take_profit_2=take_profit_2,
        stop_loss_pct=stop_loss_pct,
        entry_gap_pct=entry_gap_pct,
    )
