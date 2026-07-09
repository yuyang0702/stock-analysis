from __future__ import annotations

import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd

import config as app_config


DEFAULT_GLOBAL_CONTEXT_FILE = app_config.GLOBAL_MARKET_CONTEXT_FILE
MAX_CACHE_AGE_HOURS = 24


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


def _find_col(df: pd.DataFrame, names: list[str]) -> str:
    for name in names:
        if name in df.columns:
            return name
    return ""


def _avg_pct(df: pd.DataFrame, name_col: str, pct_col: str, keywords: list[str]) -> tuple[float, list[str]]:
    rows: list[float] = []
    names: list[str] = []
    for _, row in df.iterrows():
        name = str(row.get(name_col) or "")
        if not any(key.lower() in name.lower() for key in keywords):
            continue
        rows.append(_num(row.get(pct_col)))
        names.append(name)
    if not rows:
        return 0.0, []
    return sum(rows) / len(rows), names


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


def _context_from_frame(df: pd.DataFrame, source: str) -> dict[str, Any]:
    if df is None or df.empty:
        return {
            **build_global_context(),
            "global_reason": "美日韩指数抓取为空，按中性处理",
            "fetch_status": "empty",
            "source": source,
        }
    name_col = _find_col(df, ["名称", "name", "指数名称", "代码名称"])
    pct_col = _find_col(df, ["涨跌幅", "涨跌幅%", "change_pct", "pct_chg", "最新涨跌幅"])
    if not name_col or not pct_col:
        return {
            **build_global_context(),
            "global_reason": "美日韩指数字段缺失，按中性处理",
            "fetch_status": "bad_schema",
            "source": source,
        }
    us_pct, us_names = _avg_pct(df, name_col, pct_col, ["纳斯达克", "NASDAQ", "标普", "S&P", "道琼斯", "DOW"])
    japan_pct, japan_names = _avg_pct(df, name_col, pct_col, ["日经", "NIKKEI", "TOPIX", "东证"])
    korea_pct, korea_names = _avg_pct(df, name_col, pct_col, ["KOSPI", "KOSDAQ", "韩国"])
    context = build_global_context(us_pct=us_pct, japan_pct=japan_pct, korea_pct=korea_pct)
    context.update(
        {
            "fetch_status": "ok",
            "source": source,
            "us_indices": us_names,
            "japan_indices": japan_names,
            "korea_indices": korea_names,
            "global_reason": (
                f"美股{us_pct:+.2f}%({','.join(us_names) or '未匹配'})；"
                f"日本{japan_pct:+.2f}%({','.join(japan_names) or '未匹配'})；"
                f"韩国{korea_pct:+.2f}%({','.join(korea_names) or '未匹配'})"
            ),
        }
    )
    return context


def _pct_from_history(df: pd.DataFrame) -> float | None:
    if df is None or len(df) < 2 or "close" not in df.columns:
        return None
    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(closes) < 2:
        return None
    prev = float(closes.iloc[-2])
    latest = float(closes.iloc[-1])
    if prev == 0:
        return None
    return (latest - prev) / prev * 100


def fetch_sina_global_snapshot() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for name, getter in [
        ("标普500", lambda: ak.index_us_stock_sina(symbol=".INX")),
        ("纳斯达克", lambda: ak.index_us_stock_sina(symbol=".IXIC")),
        ("日经225", lambda: ak.index_global_hist_sina(symbol="日经225指数")),
        ("KOSPI", lambda: ak.index_global_hist_sina(symbol="首尔综合指数")),
    ]:
        try:
            pct = _pct_from_history(getter())
        except Exception:
            pct = None
        if pct is not None:
            rows.append({"名称": name, "涨跌幅": pct})
    return pd.DataFrame(rows)


def _fresh_cached_context(path: Path | None, max_age_hours: int) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    context = load_global_context(path)
    if context.get("fetch_status") != "ok":
        return None
    generated_at = str(context.get("generated_at") or "")
    try:
        age_hours = (datetime.now() - datetime.strptime(generated_at, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except Exception:
        return None
    if age_hours > max_age_hours:
        return None
    return {
        **context,
        "fetch_status": "reused_cache",
        "global_reason": f"复用最近成功海外数据：{context.get('global_reason', '')}",
    }


def fetch_global_context(
    fetcher: Any | None = None,
    fetchers: list[tuple[str, Any]] | None = None,
    previous_path: Path | None = None,
    max_cache_age_hours: int = MAX_CACHE_AGE_HOURS,
) -> dict[str, Any]:
    if fetchers is None:
        fetchers = [("ak.index_global_spot_em", fetcher or ak.index_global_spot_em)]
        if fetcher is None:
            fetchers.append(("ak.sina_global_history", fetch_sina_global_snapshot))

    errors: list[str] = []
    for source, source_fetcher in fetchers:
        try:
            context = _context_from_frame(source_fetcher(), source)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            continue
        if context.get("fetch_status") == "ok":
            if errors:
                context["fallback_errors"] = errors
            return context
        errors.append(f"{source}: {context.get('fetch_status')}")

    cached = _fresh_cached_context(previous_path, max_cache_age_hours)
    if cached is not None:
        cached["fallback_errors"] = errors
        return cached

    return {
        **build_global_context(),
        "global_reason": f"美日韩指数抓取失败，按中性处理：{'; '.join(errors) or 'unknown'}",
        "fetch_status": "error",
        "fallback_errors": errors,
    }


def save_global_context(context: dict[str, Any], path: Path | None = None) -> Path:
    path = path or DEFAULT_GLOBAL_CONTEXT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(context, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch US/Japan/Korea market context for shadow scoring")
    parser.add_argument("--output", type=Path, default=DEFAULT_GLOBAL_CONTEXT_FILE)
    args = parser.parse_args()
    context = fetch_global_context(previous_path=args.output)
    path = save_global_context(context, args.output)
    print(f"Global market context saved: {path}")
    print(context.get("global_reason", ""))


if __name__ == "__main__":
    main()
