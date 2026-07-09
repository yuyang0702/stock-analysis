from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config as app_config


DEFAULT_GLOBAL_CONTEXT_FILE = app_config.CACHE_DIR / "market" / "global_context.json"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _market_score(pct: float) -> float:
    if pct <= -2:
        return -3.0
    if pct <= -1:
        return -2.0
    if pct < -0.3:
        return -1.0
    if pct >= 2:
        return 2.0
    if pct >= 0.8:
        return 1.0
    return 0.0


def build_global_context(us_pct: float = 0.0, japan_pct: float = 0.0, korea_pct: float = 0.0) -> dict[str, Any]:
    us_score = _market_score(float(us_pct))
    japan_score = _market_score(float(japan_pct))
    korea_score = _market_score(float(korea_pct))
    risk_score = max(-5.0, min(5.0, us_score * 0.5 + japan_score * 0.25 + korea_score * 0.25))
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "us_pct": float(us_pct),
        "japan_pct": float(japan_pct),
        "korea_pct": float(korea_pct),
        "global_risk_score": round(risk_score, 2),
        "global_reason": f"美股{us_pct:+.2f}%；日本{japan_pct:+.2f}%；韩国{korea_pct:+.2f}%",
    }


def load_global_context(path: Path | None = None) -> dict[str, Any]:
    path = path or DEFAULT_GLOBAL_CONTEXT_FILE
    if not path.exists():
        return {
            "global_risk_score": 0.0,
            "global_reason": "未提供美日韩市场上下文，按中性处理",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "global_risk_score": 0.0,
            "global_reason": "美日韩市场上下文读取失败，按中性处理",
        }
    if not isinstance(payload, dict):
        return {
            "global_risk_score": 0.0,
            "global_reason": "美日韩市场上下文格式异常，按中性处理",
        }
    return {
        **payload,
        "global_risk_score": _num(payload.get("global_risk_score")),
        "global_reason": str(payload.get("global_reason") or payload.get("reason") or "美日韩市场上下文已读取"),
    }
