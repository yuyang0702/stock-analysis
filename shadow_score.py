from __future__ import annotations

from typing import Any

import pandas as pd


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


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _theme_bonus(row: pd.Series) -> float:
    level = _text(row.get("theme_heat_level"))
    score = _num(row.get("theme_heat_score"))
    if level == "高" or score >= 6:
        return 4.0
    if level == "中" or score >= 3:
        return 2.0
    if level == "低":
        return -2.0
    return 0.0


def _market_bonus(row: pd.Series) -> float:
    state = _text(row.get("market_state"))
    if any(key in state for key in ("强势", "进攻", "修复")):
        return 3.0
    if any(key in state for key in ("弱势", "恐慌", "退潮")):
        return -4.0
    return 0.0


def _sector_bonus(row: pd.Series) -> float:
    rank_pct = _num(row.get("sector_rank_pct"), default=-1.0)
    level = _text(row.get("sector_hot_level"))
    if rank_pct >= 0.85 or level in {"强", "领涨"}:
        return 3.0
    if rank_pct >= 0.65 or level == "偏强":
        return 1.5
    if 0 <= rank_pct <= 0.3 or level == "弱":
        return -3.0
    return 0.0


def build_shadow_score(row: pd.Series, global_risk_score: float = 0.0) -> dict[str, Any]:
    base = _num(row.get("final_score"))
    news_bonus = _clip(_num(row.get("news_score")) * 0.8, -8.0, 8.0)
    theme_bonus = _theme_bonus(row)
    sector_bonus = _sector_bonus(row)
    market_bonus = _market_bonus(row)
    trade_bonus = _clip((_num(row.get("trade_score")) - 70.0) / 10.0, -3.0, 3.0)
    global_bonus = _clip(float(global_risk_score), -5.0, 5.0)
    adjust = news_bonus + theme_bonus + sector_bonus + market_bonus + trade_bonus + global_bonus
    enhanced = max(0.0, base + adjust)

    parts = [
        f"消息{news_bonus:+.1f}",
        f"题材{theme_bonus:+.1f}",
        f"板块{sector_bonus:+.1f}",
        f"市场{market_bonus:+.1f}",
        f"交易{trade_bonus:+.1f}",
        f"海外{global_bonus:+.1f}",
    ]
    return {
        "shadow_base_score": round(base, 2),
        "enhanced_score": round(enhanced, 2),
        "shadow_adjust_score": round(adjust, 2),
        "news_catalyst_score": round(news_bonus, 2),
        "theme_heat_adjust_score": round(theme_bonus, 2),
        "sector_position_score": round(sector_bonus, 2),
        "market_emotion_score": round(market_bonus, 2),
        "global_risk_score": round(global_bonus, 2),
        "shadow_reason": "；".join(parts),
    }


def apply_shadow_scores(df: pd.DataFrame, global_risk_score: float = 0.0) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    result = df.copy()
    shadow = result.apply(lambda row: build_shadow_score(row, global_risk_score), axis=1, result_type="expand")
    for col in shadow.columns:
        result[col] = shadow[col]
    result["original_rank"] = result["final_score"].rank(method="min", ascending=False).astype(int)
    result["shadow_rank"] = result["enhanced_score"].rank(method="min", ascending=False).astype(int)
    result["shadow_rank_change"] = result["original_rank"] - result["shadow_rank"]
    return result
