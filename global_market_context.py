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


def fetch_global_context(fetcher: Any | None = None) -> dict[str, Any]:
    fetcher = fetcher or ak.index_global_spot_em
    df = fetcher()
    if df is None or df.empty:
        return {
            **build_global_context(),
            "global_reason": "美日韩指数抓取为空，按中性处理",
            "fetch_status": "empty",
        }
    name_col = _find_col(df, ["名称", "name", "指数名称", "代码名称"])
    pct_col = _find_col(df, ["涨跌幅", "涨跌幅%", "change_pct", "pct_chg", "最新涨跌幅"])
    if not name_col or not pct_col:
        return {
            **build_global_context(),
            "global_reason": "美日韩指数字段缺失，按中性处理",
            "fetch_status": "bad_schema",
        }
    us_pct, us_names = _avg_pct(df, name_col, pct_col, ["纳斯达克", "NASDAQ", "标普", "S&P", "道琼斯", "DOW"])
    japan_pct, japan_names = _avg_pct(df, name_col, pct_col, ["日经", "NIKKEI", "TOPIX", "东证"])
    korea_pct, korea_names = _avg_pct(df, name_col, pct_col, ["KOSPI", "KOSDAQ", "韩国"])
    context = build_global_context(us_pct=us_pct, japan_pct=japan_pct, korea_pct=korea_pct)
    context.update(
        {
            "fetch_status": "ok",
            "source": "ak.index_global_spot_em",
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
    try:
        context = fetch_global_context()
    except Exception as exc:
        context = {
            **build_global_context(),
            "global_reason": f"美日韩指数抓取失败，按中性处理：{exc}",
            "fetch_status": "error",
        }
    path = save_global_context(context, args.output)
    print(f"Global market context saved: {path}")
    print(context.get("global_reason", ""))


if __name__ == "__main__":
    main()
