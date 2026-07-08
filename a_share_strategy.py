from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import socket
import time
import uuid
import warnings
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import akshare as ak
import pandas as pd

import config as app_config
from paper_trading import apply_paper_trades, build_paper_trade_markdown, load_account, save_account
from risk_engine import RiskDecision, build_risk_decision, build_signal_lifecycle, classify_trade_mode
from strategy_profile import build_strategy_profile
from notifier import WeComNotifier

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


warnings.filterwarnings("ignore")
socket.setdefaulttimeout(float(app_config.STOCK_SCAN_TIMEOUT))


ROOT = app_config.BASE_DIR
OUTPUT_DIR = app_config.OUTPUT_DIR
CACHE_DIR = app_config.CACHE_DIR
INDUSTRY_CACHE = app_config.INDUSTRY_CACHE
SIGNAL_WATCHLIST_FILE = CACHE_DIR / "signal_watchlist.json"


@dataclass
class Config:
    mode: str = app_config.SCAN_MODE_DEFAULT
    top: int = app_config.SCAN_TOP_DEFAULT
    ai: bool = app_config.ENABLE_AI_DEFAULT
    watch: bool = False
    interval: int = app_config.SCAN_INTERVAL_DEFAULT
    jitter: int = app_config.SCAN_JITTER_DEFAULT
    market_only: bool = False
    refresh_industry: bool = False
    min_price: float = app_config.MIN_PRICE_DEFAULT
    min_amount: float = app_config.MIN_AMOUNT_DEFAULT
    skip_pressure: bool = app_config.SKIP_PRESSURE_DEFAULT
    skip_lhb: bool = app_config.SKIP_LHB_DEFAULT
    skip_news: bool = app_config.SKIP_NEWS_DEFAULT
    stock_news_limit: int = app_config.STOCK_NEWS_LIMIT_DEFAULT
    notice_days_back: int = app_config.NOTICE_DAYS_BACK_DEFAULT
    max_candidates_for_news: int = app_config.MAX_CANDIDATES_FOR_NEWS_DEFAULT
    notify: bool = app_config.NOTIFY_ENABLE_DEFAULT
    notify_only_signal: bool = app_config.NOTIFY_ONLY_SIGNAL_DEFAULT
    notify_top: int = app_config.NOTIFY_TOP_N_DEFAULT
    notify_cooldown: int = app_config.NOTIFY_COOLDOWN_SEC_DEFAULT
    notify_min_score: float = app_config.NOTIFY_MIN_SCORE_DEFAULT
    notify_non_trading_day: bool = app_config.NOTIFY_NON_TRADING_DAY_DEFAULT
    notify_webhook: str | None = app_config.WECOM_WEBHOOK_URL
    intraday_watch_multiplier: int = app_config.INTRADAY_WATCH_MULTIPLIER_DEFAULT
    intraday_near_pressure_pct: float = app_config.INTRADAY_NEAR_PRESSURE_PCT_DEFAULT
    intraday_trigger_pressure_pct: float = app_config.INTRADAY_TRIGGER_PRESSURE_PCT_DEFAULT
    intraday_max_alerts: int = app_config.INTRADAY_MAX_ALERTS_DEFAULT
    paper_trade: bool = app_config.PAPER_TRADE_ENABLE_DEFAULT
    paper_trade_cash: float = app_config.PAPER_TRADE_CASH_DEFAULT
    paper_trade_commission_rate: float = app_config.PAPER_TRADE_COMMISSION_RATE_DEFAULT
    paper_trade_stamp_tax_rate: float = app_config.PAPER_TRADE_STAMP_TAX_RATE_DEFAULT
    paper_trade_slippage_pct: float = app_config.PAPER_TRADE_SLIPPAGE_PCT_DEFAULT
    paper_trade_cooldown_days: int = app_config.PAPER_TRADE_COOLDOWN_DAYS_DEFAULT
    paper_trade_max_positions: int = app_config.PAPER_TRADE_MAX_POSITIONS_DEFAULT
    paper_trade_max_position_pct: float = app_config.PAPER_TRADE_MAX_POSITION_PCT_DEFAULT
    paper_trade_max_total_position_pct: float = app_config.PAPER_TRADE_MAX_TOTAL_POSITION_PCT_DEFAULT
    joinquant: bool = app_config.JOINQUANT_ENABLE_DEFAULT
    joinquant_dry_run: bool = app_config.JOINQUANT_DRY_RUN_DEFAULT
    joinquant_min_score: float = app_config.JOINQUANT_MIN_SCORE_DEFAULT


class DiskCache:
    """一个很轻量的磁盘缓存，主要用来给新闻、龙虎榜、压力位降频。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        payload = json.dumps(self.db, ensure_ascii=False, indent=2, default=str)
        last_error: Exception | None = None
        for attempt in range(5):
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(payload, encoding="utf-8")
                tmp.replace(self.path)
                return
            except PermissionError as exc:
                last_error = exc
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                time.sleep(0.05 * (attempt + 1))
            except OSError as exc:
                last_error = exc
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                time.sleep(0.05 * (attempt + 1))
        print(f"缓存写入失败，已跳过本次落盘：{last_error}", flush=True)

    def get(self, key: str, ttl_sec: int) -> Any | None:
        item = self.db.get(key)
        if not item:
            return None
        if time.time() - float(item.get("ts", 0)) > ttl_sec:
            return None
        return item.get("value")

    def set(self, key: str, value: Any) -> None:
        self.db[key] = {"ts": time.time(), "value": value}
        self._save()

    def get_df(self, key: str, ttl_sec: int) -> pd.DataFrame | None:
        value = self.get(key, ttl_sec)
        if value is None:
            return None
        return pd.DataFrame(value)

    def set_df(self, key: str, df: pd.DataFrame) -> None:
        self.set(key, df.to_dict(orient="records"))


def clean_code(value: Any) -> str:
    """统一成 6 位股票代码。"""
    return "".join(filter(str.isdigit, str(value))).zfill(6)


def market_symbol(code: str) -> str:
    code = clean_code(code)
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return f"sz{code}"


def pick_col(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def to_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def safe_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def load_portfolio_positions() -> dict[str, dict[str, Any]]:
    """读取手机网页写入的持仓文件，供盘中推送和风控联动使用。"""
    path = app_config.POSITIONS_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        items = raw["positions"]
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = list(raw.values())
    else:
        items = []

    db: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = clean_code(item.get("code"))
        if code:
            db[code] = item
    return db


def enrich_portfolio_frame(df: pd.DataFrame, portfolio: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """把网页端持仓状态挂到候选股上，方便盘中推送显示是否已经持仓。"""
    if df.empty or "code" not in df.columns:
        return df

    df = df.copy()

    def _lookup(code: Any) -> dict[str, Any]:
        return portfolio.get(clean_code(code), {})

    snapshot = df["code"].apply(_lookup)
    df["hold_status"] = snapshot.apply(lambda x: safe_text(x.get("status")))
    df["hold_qty"] = snapshot.apply(lambda x: x.get("qty"))
    df["hold_name"] = snapshot.apply(lambda x: safe_text(x.get("name")))
    df["hold_cost_price"] = snapshot.apply(lambda x: x.get("cost_price"))
    df["hold_current_price"] = snapshot.apply(lambda x: x.get("current_price"))
    df["hold_stop_pct"] = snapshot.apply(lambda x: x.get("stop_pct"))
    df["hold_take_pct"] = snapshot.apply(lambda x: x.get("take_pct"))
    df["hold_stop_price"] = snapshot.apply(lambda x: x.get("stop_price"))
    df["hold_take_price"] = snapshot.apply(lambda x: x.get("take_price"))
    df["hold_note"] = snapshot.apply(lambda x: safe_text(x.get("note")))
    df["has_holding"] = df["hold_status"].isin(["holding", "partial_sell"])
    df["holding_brief"] = df.apply(build_holding_brief, axis=1)
    return df


def build_holding_brief(row: pd.Series) -> str:
    status = safe_text(row.get("hold_status"))
    if not status:
        return "未持仓"
    qty = safe_text(row.get("hold_qty"))
    cost = row.get("hold_cost_price")
    current = row.get("hold_current_price")
    stop_price = row.get("hold_stop_price")
    take_price = row.get("hold_take_price")
    parts = [status]
    if qty and qty != "0":
        parts.append(f"{qty}股")
    if cost is not None and not pd.isna(cost):
        parts.append(f"成本{float(cost):.2f}")
    if current is not None and not pd.isna(current):
        parts.append(f"现价{float(current):.2f}")
    if stop_price is not None and not pd.isna(stop_price):
        parts.append(f"止损{float(stop_price):.2f}")
    if take_price is not None and not pd.isna(take_price):
        parts.append(f"止盈{float(take_price):.2f}")
    return " | ".join(parts)


def notification_title(mode: str, title: str) -> str:
    labels = {"pre": "盘前", "intraday": "盘中", "after": "盘后", "auto": "自动"}
    label = labels.get(safe_text(mode), safe_text(mode) or "扫描")
    clean_title = safe_text(title)
    prefix = f"【{label}】"
    return clean_title if clean_title.startswith(prefix) else f"{prefix}{clean_title}"


def load_signal_watchlist(path: Path = SIGNAL_WATCHLIST_FILE) -> dict[str, Any]:
    if not path.exists():
        return {"items": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}
    items = raw.get("items") if isinstance(raw, dict) else []
    if not isinstance(items, list):
        items = []
    return {"items": [item for item in items if isinstance(item, dict)]}


def save_signal_watchlist(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _parse_watchlist_time(value: Any) -> datetime | None:
    raw = safe_text(value)
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[: len(fmt)], fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(raw.replace("/", "-"))
    except Exception:
        return None


def prune_signal_watchlist_items(items: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now()
    keep_days = max(1, int(getattr(app_config, "SIGNAL_WATCHLIST_DAYS_DEFAULT", 10)))
    kept: list[dict[str, Any]] = []
    for item in items:
        pushed_at = _parse_watchlist_time(item.get("pushed_at"))
        if pushed_at and (now - pushed_at).days > keep_days:
            continue
        kept.append(item)
    return kept[-80:]


def _series_float(row: pd.Series, key: str) -> float | None:
    try:
        value = row.get(key)
        if value is None or pd.isna(value):
            return None
        result = float(value)
        return result if result > 0 else None
    except Exception:
        return None


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _active_portfolio_items(portfolio: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in portfolio.values()
        if safe_text(item.get("status")) in {"holding", "partial_sell"}
    ]


def build_portfolio_risk_markdown(portfolio: dict[str, dict[str, Any]]) -> str:
    items = _active_portfolio_items(portfolio)
    if not items:
        return ""

    rows: list[dict[str, Any]] = []
    missing_stop = 0
    for item in items:
        qty = _float_value(item.get("qty"))
        price = _float_value(item.get("current_price")) or _float_value(item.get("cost_price"))
        stop = _float_value(item.get("stop_price"))
        value = qty * price if qty > 0 and price > 0 else 0.0
        stop_risk = max(price - stop, 0.0) * qty if value > 0 and stop > 0 else 0.0
        if value > 0 and stop <= 0:
            missing_stop += 1
        rows.append({**item, "value": value, "stop_risk": stop_risk})

    total_value = sum(row["value"] for row in rows)
    total_stop_risk = sum(row["stop_risk"] for row in rows)
    largest = max(rows, key=lambda row: row["value"], default={})
    largest_pct = largest.get("value", 0.0) / total_value * 100 if total_value > 0 else 0.0
    risk_pct = total_stop_risk / total_value * 100 if total_value > 0 else 0.0
    warnings: list[str] = []
    if largest_pct >= 35:
        warnings.append(f"单票集中度{largest_pct:.1f}%")
    if risk_pct >= 5:
        warnings.append(f"组合止损风险{risk_pct:.1f}%")
    if missing_stop:
        warnings.append(f"{missing_stop}只缺止损价")

    lines = [
        "#### 组合风控",
        f"> 持仓{len(items)}只 | 估算市值{total_value / 10000:.1f}万 | 止损风险{total_stop_risk / 10000:.1f}万({risk_pct:.1f}%)",
    ]
    if largest and total_value > 0:
        lines.append(
            f"> 最大持仓：{clean_code(largest.get('code'))} {safe_text(largest.get('name'))} {largest_pct:.1f}%"
        )
    if warnings:
        lines.append(f"> 提醒：{'；'.join(warnings)}")
    return "\n".join(lines)


def build_signal_performance_markdown(path: Path = SIGNAL_WATCHLIST_FILE) -> str:
    items = prune_signal_watchlist_items(load_signal_watchlist(path)["items"])
    closed = [item for item in items if item.get("active") is False and safe_text(item.get("review_result"))]
    if not closed:
        return ""

    total = len(closed)
    take_hits = sum(1 for item in closed if item.get("review_result") == "take_profit")
    stop_hits = sum(1 for item in closed if item.get("review_result") == "stop_loss")
    timeout_hits = total - take_hits - stop_hits
    pct_values = [_float_value(item.get("review_return_pct")) for item in closed if item.get("review_return_pct") is not None]
    avg_pct = sum(pct_values) / len(pct_values) if pct_values else 0.0
    return (
        "#### 推送质量\n"
        f"> 已闭环{total}只 | 止盈{take_hits} | 止损{stop_hits} | 其他{timeout_hits} | 均值{avg_pct:+.2f}%"
    )


def record_signal_watchlist(
    path: Path,
    row: pd.Series,
    kind: str,
    mode: str,
    pushed_at: datetime | None = None,
) -> None:
    code = clean_code(row.get("code"))
    if not code:
        return
    pushed_at = pushed_at or datetime.now()
    signal_id = safe_text(row.get("signal_anchor_id")) or safe_text(row.get("signal_first_seen")) or f"{code}:{mode}:{pushed_at.date().isoformat()}"
    item = {
        "code": code,
        "name": safe_text(row.get("name")),
        "kind": safe_text(kind),
        "mode": safe_text(row.get("mode")) or safe_text(mode),
        "pushed_at": pushed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "pushed_price": _series_float(row, "price") or _series_float(row, "entry_price"),
        "entry_price": _series_float(row, "entry_price"),
        "stop_loss": _series_float(row, "stop_loss"),
        "take_profit": _series_float(row, "take_profit"),
        "position_pct": _series_float(row, "position_pct"),
        "final_score": _series_float(row, "final_score"),
        "trade_score": _series_float(row, "trade_score"),
        "market_state": safe_text(row.get("market_state")),
        "theme_label": safe_text(row.get("theme_label")),
        "theme_heat_level": safe_text(row.get("theme_heat_level")),
        "risk_reason": compact_text(row.get("risk_reason") or row.get("buy_reason") or row.get("entry_reason"), 120),
        "buy_state": safe_text(row.get("buy_state")),
        "signal_id": signal_id,
        "last_reviewed_at": "",
        "active": True,
    }
    payload = load_signal_watchlist(path)
    items = payload["items"]
    for idx, existing in enumerate(items):
        if clean_code(existing.get("code")) == code and safe_text(existing.get("signal_id")) == signal_id:
            items[idx] = {**existing, **item}
            break
    else:
        items.append(item)
    save_signal_watchlist(path, {"items": prune_signal_watchlist_items(items)})


def build_watchlist_review_markdown(
    result: pd.DataFrame,
    path: Path = SIGNAL_WATCHLIST_FILE,
    max_rows: int = 6,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
    payload = load_signal_watchlist(path)
    items = prune_signal_watchlist_items(payload["items"])
    if result.empty or not items:
        return ""
    rows_by_code = {clean_code(row.get("code")): row for _, row in result.iterrows()}
    updated: list[dict[str, Any]] = []
    lines = ["#### 推送跟踪复盘"]
    matched = 0
    entered_count = 0
    take_hits = 0
    stop_hits = 0
    max_gain_values: list[float] = []
    drawdown_values: list[float] = []
    detail_lines: list[str] = []
    reviewed_rows: list[dict[str, Any]] = []

    for item in items:
        code = clean_code(item.get("code"))
        row = rows_by_code.get(code)
        if row is None:
            updated.append(item)
            continue

        price = _series_float(row, "price")
        high = _series_float(row, "high") or price
        low = _series_float(row, "low") or price
        pct_chg = _series_float(row, "pct_chg")
        entry = float(item.get("entry_price") or 0)
        stop = float(item.get("stop_loss") or 0)
        take = float(item.get("take_profit") or 0)
        signal_action = safe_text(row.get("signal_action"))
        pushed_at = _parse_watchlist_time(item.get("pushed_at"))
        age_days = max(0, (now.date() - pushed_at.date()).days) if pushed_at else 0
        review_day = f"D+{age_days}"
        active = True
        status = "继续观察"
        distance_text = ""
        entered = bool(entry and high and high >= entry)

        if entered:
            entered_count += 1
            if high and entry:
                max_gain_values.append((high - entry) / entry * 100.0)
            if low and entry:
                drawdown_values.append((low - entry) / entry * 100.0)

        if entered and high and take and high >= take:
            status = "触及止盈"
            active = False
            take_hits += 1
        elif entered and low and stop and low <= stop:
            status = "触及止损"
            active = False
            stop_hits += 1
        elif signal_action in {"time_stop", "stop_loss", "take_profit"}:
            status = safe_text(row.get("signal_state")) or signal_action
            active = False
        elif entry and not entered:
            status = "未入场"
            distance_text = f"距入场{(entry - price) / price * 100:.2f}%"
        elif price and take:
            distance_text = f"距止盈{max(0.0, (take - price) / price * 100):.2f}%"

        price_text = f"入{entry:.2f} | 高{high:.2f} | 低{low:.2f} | 收{price:.2f}" if price and high and low and entry else f"现价{price:.2f}" if price else "现价-"
        pct_text = f"{pct_chg:+.2f}%" if pct_chg is not None else "-"
        tail = f" | {distance_text}" if distance_text else ""
        detail_lines.append(
            f"{matched + 1}. {code} {safe_text(row.get('name') or item.get('name'))} | "
            f"{safe_text(item.get('kind'))} | {review_day} | {price_text} {pct_text} | {status}{tail}"
        )
        note = compact_text(row.get("risk_reason") or row.get("buy_reason") or row.get("signal_note") or item.get("risk_reason"), 28)
        if note:
            detail_lines.append(f"> {note}")
        review_return = (price - entry) / entry * 100.0 if price and entry and entered else None
        review_result = "take_profit" if status == "触及止盈" else "stop_loss" if status == "触及止损" else "entered" if entered else "not_entered"
        history = [entry for entry in item.get("review_history", []) if isinstance(entry, dict)]
        history = [entry for entry in history if safe_text(entry.get("date")) != now.date().isoformat()]
        history.append(
            {
                "date": now.date().isoformat(),
                "day": review_day,
                "close": round(price, 2) if price else None,
                "high": round(high, 2) if high else None,
                "low": round(low, 2) if low else None,
                "return_pct": round(review_return, 2) if review_return is not None else None,
                "result": review_result,
            }
        )
        reviewed = {
            **item,
            "last_reviewed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "active": active,
            "review_day": review_day,
            "review_result": review_result,
            "review_return_pct": round(review_return, 2) if review_return is not None else None,
            "review_high": high,
            "review_low": low,
            "review_close": price,
            "review_history": history[-12:],
        }
        reviewed_rows.append(reviewed)
        updated.append(
            {
                **reviewed,
            }
        )
        matched += 1
        if matched >= max_rows:
            break

    if matched == 0:
        return ""
    avg_gain = sum(max_gain_values) / len(max_gain_values) if max_gain_values else 0.0
    avg_drawdown = sum(drawdown_values) / len(drawdown_values) if drawdown_values else 0.0
    lines.append(
        f"> 今日跟踪{matched}只 | 已入场{entered_count} | 未入场{matched - entered_count} | "
        f"止盈{take_hits} | 止损{stop_hits}"
    )
    lines.append(f"> 平均最大浮盈{avg_gain:+.2f}% | 平均最大回撤{avg_drawdown:+.2f}%")
    quality_parts: list[str] = []
    for label, key in (("模式", "mode"), ("题材", "theme_heat_level"), ("市场", "market_state")):
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in reviewed_rows:
            group_key = safe_text(row.get(key)) or "未标记"
            groups.setdefault(group_key, []).append(row)
        if groups:
            best_key, rows = max(groups.items(), key=lambda item: len(item[1]))
            wins = sum(1 for row in rows if row.get("review_result") in {"take_profit", "entered"} and _float_value(row.get("review_return_pct")) >= 0)
            avg_ret_values = [_float_value(row.get("review_return_pct")) for row in rows if row.get("review_return_pct") is not None]
            avg_ret = sum(avg_ret_values) / len(avg_ret_values) if avg_ret_values else 0.0
            quality_parts.append(f"{label}{best_key} 胜率{wins / len(rows):.0%} 均值{avg_ret:+.2f}%")
    if quality_parts:
        lines.append(f"> 策略质量：{'；'.join(quality_parts[:3])}")
    lines.extend(detail_lines)
    seen = {clean_code(item.get("code")) for item in updated}
    updated.extend(item for item in items if clean_code(item.get("code")) not in seen)
    save_signal_watchlist(path, {"items": prune_signal_watchlist_items(updated)})
    return "\n".join(lines)


class IndustryMapper:
    def __init__(self, cache_file: Path = INDUSTRY_CACHE, refresh: bool = False):
        self.cache_file = cache_file
        self.pending_file = self.cache_file.with_name("industry_pending.json")
        self.db = self._load()
        if refresh or not self.db:
            self.sync()

    def _load(self) -> dict[str, str]:
        if not self.cache_file.exists():
            return {}
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def sync(self) -> None:
        print("同步行业缓存，第一次运行会慢一些...", flush=True)
        new_db: dict[str, str] = {}

        def _merge_source(board_loader, cons_loader, label: str) -> None:
            try:
                boards = board_loader()
                name_col = pick_col(boards, ["板块名称", "名称", "行业名称", "题材名称"])
                if not name_col:
                    return

                for board_name in boards[name_col].dropna().astype(str):
                    try:
                        cons = cons_loader(symbol=board_name)
                        code_col = pick_col(cons, ["代码", "股票代码"])
                        if code_col:
                            for code in cons[code_col]:
                                code = clean_code(code)
                                current = new_db.get(code) or self.db.get(code, "未识别")
                                if current in {"未识别", "热点题材", "热点概念"}:
                                    new_db[code] = board_name
                    except Exception:
                        continue
                    time.sleep(0.05)
            except Exception:
                return

        try:
            _merge_source(ak.stock_board_industry_name_em, ak.stock_board_industry_cons_em, "行业")
            if hasattr(ak, "stock_board_concept_name_em") and hasattr(ak, "stock_board_concept_cons_em"):
                _merge_source(ak.stock_board_concept_name_em, ak.stock_board_concept_cons_em, "题材")

            if new_db:
                merged = dict(self.db)
                merged.update(new_db)
                self.db = merged
                self.cache_file.write_text(
                    json.dumps(self.db, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                print(f"行业缓存已更新，共 {len(self.db)} 条。", flush=True)
            elif not self.db:
                print("行业缓存未同步到数据，将优先使用行情自带行业字段。", flush=True)
        except Exception as exc:
            if not self.db:
                print("行业缓存同步失败，将优先使用行情自带行业字段。", flush=True)
            else:
                print(f"行业缓存同步失败：{exc}", flush=True)

    def get(self, code: str) -> str:
        return self.db.get(clean_code(code), "未识别")

    def seed_from_frame(self, df: pd.DataFrame) -> int:
        """用行情里已经带出的行业字段做兜底，避免首跑没有行业信息。"""
        if df is None or df.empty:
            return 0
        code_col = pick_col(df, ["code", "代码"])
        industry_col = pick_col(df, ["industry", "行业", "所属行业", "板块", "概念"])
        if not code_col or not industry_col:
            return 0

        added = 0
        for _, row in df[[code_col, industry_col]].dropna().iterrows():
            code = clean_code(row[code_col])
            industry = safe_text(row[industry_col])
            if code and industry and code not in self.db:
                self.db[code] = industry
                added += 1

        if added:
            try:
                self.cache_file.write_text(
                    json.dumps(self.db, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return added

    def refresh_stale_pending(self, max_age_days: int = 7) -> int:
        """对超过一周还没识别出来的票做一次更强的补题材尝试。"""
        if not self.pending_file.exists():
            return 0
        try:
            pending = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except Exception:
            return 0

        cutoff = datetime.now() - timedelta(days=max_age_days)
        stale = False
        for item in pending.values():
            last_seen = safe_text(item.get("last_seen"))
            try:
                if last_seen and datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S") < cutoff:
                    stale = True
                    break
            except Exception:
                stale = True
                break

        if not stale:
            return 0

        before = len(self.db)
        self.sync()
        after = len(self.db)
        if after > before:
            try:
                remaining = {}
                for code, item in pending.items():
                    if code not in self.db or self.db.get(code) in {"未识别", "未识别题材", "题材待确认", "热点题材", "热点概念"}:
                        remaining[code] = item
                self.pending_file.write_text(
                    json.dumps(remaining, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return after - before

    def note_pending(self, df: pd.DataFrame) -> int:
        """记录当前还没认出来的行业，方便后续持续补库。"""
        if df is None or df.empty:
            return 0

        code_col = pick_col(df, ["code", "代码"])
        name_col = pick_col(df, ["name", "名称"])
        industry_col = pick_col(df, ["industry", "行业", "所属行业", "板块", "概念"])
        if not code_col:
            return 0

        pending: dict[str, dict[str, str]] = {}
        if self.pending_file.exists():
            try:
                pending = json.loads(self.pending_file.read_text(encoding="utf-8"))
            except Exception:
                pending = {}

        added = 0
        for _, row in df[[code_col] + ([name_col] if name_col else []) + ([industry_col] if industry_col else [])].dropna(how="all").iterrows():
            code = clean_code(row[code_col])
            if not code or code in self.db:
                continue
            name = safe_text(row[name_col]) if name_col else ""
            industry = safe_text(row[industry_col]) if industry_col else ""
            if industry and industry not in {"未识别", "未识别题材", "题材待确认", "热点题材", "热点概念"}:
                continue
            current = pending.get(code, {})
            current["name"] = name or current.get("name", "")
            current["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            pending[code] = current
            added += 1

        if added:
            try:
                self.pending_file.write_text(
                    json.dumps(pending, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return added


def fetch_spot_data() -> pd.DataFrame:
    """获取 A 股实时行情，并统一列名。"""
    providers = [ak.stock_zh_a_spot_em, ak.stock_zh_a_spot]
    last_error = None
    df = None

    for provider in providers:
        try:
            tmp = provider()
            if tmp is not None and not tmp.empty:
                df = tmp
                break
        except Exception as exc:
            last_error = exc

    if df is None or df.empty:
        raise RuntimeError(f"无法获取实时行情：{last_error}")

    rename_map = {}
    mapping = {
        "code": ["代码", "symbol"],
        "name": ["名称", "name"],
        "industry": ["行业", "所属行业", "板块", "概念", "industry"],
        "price": ["最新价", "最新报价", "trade", "close"],
        "pct_chg": ["涨跌幅", "changepercent", "pct_chg"],
        "open": ["今开", "开盘", "open"],
        "prev_close": ["昨收", "昨收盘", "settlement"],
        "high": ["最高", "high"],
        "low": ["最低", "low"],
        "amount": ["成交额", "成交金额", "amount"],
        "volume": ["成交量", "volume"],
        "turnover": ["换手率", "turnoverratio"],
        "market_cap": ["流通市值", "总市值", "nmc", "mktcap"],
    }
    for std_name, candidates in mapping.items():
        col = pick_col(df, candidates)
        if col:
            rename_map[col] = std_name

    df = df.rename(columns=rename_map)
    required = {"code", "name", "price", "pct_chg", "amount"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"实时行情缺少必要列：{sorted(missing)}")

    numeric_cols = ["price", "pct_chg", "open", "prev_close", "high", "low", "amount", "volume", "turnover", "market_cap"]
    df = to_numeric(df, numeric_cols)
    df["code"] = df["code"].astype(str).map(clean_code)
    df["name"] = df["name"].astype(str)

    if "open" in df.columns and "prev_close" in df.columns:
        df["gap"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100
    else:
        df["gap"] = 0.0

    return df.dropna(subset=["price", "pct_chg", "amount"]).copy()


def market_sentiment(spot_df: pd.DataFrame | None = None) -> dict[str, Any]:
    """用上证指数和市场宽度给当天环境打一个粗分。"""
    result = {
        "state": "未知",
        "sh_price": None,
        "sh_pct": None,
        "up_count": None,
        "down_count": None,
        "limit_up_count": None,
        "limit_down_count": None,
        "median_pct": None,
    }

    try:
        index_df = ak.stock_zh_index_spot_sina()
        code_col = pick_col(index_df, ["代码", "code"])
        price_col = pick_col(index_df, ["最新价", "最新点位", "price"])
        pct_col = pick_col(index_df, ["涨跌幅", "changepercent"])
        if code_col and price_col and pct_col:
            row = index_df[index_df[code_col] == "sh000001"].iloc[0]
            result["sh_price"] = float(row[price_col])
            result["sh_pct"] = float(row[pct_col])
    except Exception:
        pass

    try:
        spot = spot_df if spot_df is not None else fetch_spot_data()
        result["up_count"] = int((spot["pct_chg"] > 0).sum())
        result["down_count"] = int((spot["pct_chg"] < 0).sum())
        result["median_pct"] = float(spot["pct_chg"].median())
        result["limit_up_count"] = int((spot["pct_chg"] >= 9.5).sum())
        result["limit_down_count"] = int((spot["pct_chg"] <= -9.5).sum())
    except Exception:
        pass

    sh_pct = result["sh_pct"]
    up_count = result["up_count"] or 0
    down_count = result["down_count"] or 0
    median_pct = result["median_pct"] or 0

    if sh_pct is not None and sh_pct > 0.8 and up_count > down_count and median_pct >= 0:
        result["state"] = "强势进攻"
    elif sh_pct is not None and sh_pct > 0:
        result["state"] = "温和修复"
    elif sh_pct is not None and sh_pct > -0.8:
        result["state"] = "弱势震荡"
    else:
        result["state"] = "风险释放"

    return result


def pressure_20d(code: str, cache: DiskCache | None = None) -> tuple[float | None, str]:
    """计算 20 日压力位，属于技术面里的非常实用一层。"""
    cache_key = f"pressure:{clean_code(code)}"
    if cache:
        cached = cache.get(cache_key, ttl_sec=1800)
        if cached is not None:
            return cached.get("pressure_pct"), cached.get("pressure_label", "缓存命中")

    try:
        df = fetch_daily_history(code, cache=cache)
        if df is None or df.empty or len(df) < 5:
            return None, "K线不足"

        recent = df.tail(20)
        high_20 = float(recent["high"].max())
        close = float(recent["close"].iloc[-1])
        if close <= 0:
            return None, "价格异常"
        if close >= high_20:
            result = (0.0, "突破/新高")
        else:
            pressure = round((high_20 - close) / close * 100, 2)
            if pressure <= 5:
                result = (pressure, "贴近前高")
            elif pressure <= 12:
                result = (pressure, "近端压力")
            else:
                result = (pressure, "上方套牢重")

        if cache:
            cache.set(cache_key, {"pressure_pct": result[0], "pressure_label": result[1]})
        return result
    except Exception:
        return None, "压力位失败"


def fetch_daily_history(code: str, cache: DiskCache | None = None, lookback_days: int = 260) -> pd.DataFrame:
    """获取最近一段日线，并缓存，避免反复请求同一只股票。"""
    cache_key = f"daily_history:{clean_code(code)}"
    if cache:
        cached = cache.get_df(cache_key, ttl_sec=1800)
        if cached is not None and not cached.empty:
            return cached.copy()

    try:
        df = ak.stock_zh_a_daily(symbol=market_symbol(code), start_date="20240101", adjust="qfq")
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.tail(lookback_days).copy().reset_index(drop=True)
        if cache:
            cache.set_df(cache_key, df)
        return df
    except Exception:
        return pd.DataFrame()


POSITIVE_KEYWORDS = {
    "政策": ["政策", "支持", "印发", "发布", "试点", "规划", "批复", "落地", "启动", "推进"],
    "业绩": ["预增", "扭亏", "增长", "超预期", "盈利", "利润", "业绩"],
    "并购": ["并购", "重组", "收购", "资产注入", "控制权变更", "合作"],
    "回购增持": ["回购", "增持", "回购股份", "股份回购", "大股东增持"],
    "题材": ["AI", "算力", "半导体", "机器人", "低空经济", "固态电池", "创新药", "国产替代", "储能", "光伏"],
}
NEGATIVE_KEYWORDS = {
    "风险": ["减持", "解禁", "问询", "立案", "处罚", "风险提示", "下修", "亏损", "终止", "诉讼", "冻结", "退市", "ST", "停牌"],
}


def classify_text(text: str) -> tuple[str, int, list[str]]:
    """把一条新闻粗分为事件类型，并给一个简单分数。"""
    text = safe_text(text)
    if not text:
        return "空", 0, []

    matched: list[str] = []
    score = 0
    category = "其他"

    for cat, keywords in POSITIVE_KEYWORDS.items():
        hits = [kw for kw in keywords if kw.lower() in text.lower()]
        if hits:
            matched.extend(hits)
            category = cat
            score += 8 if cat != "题材" else 6

    for cat, keywords in NEGATIVE_KEYWORDS.items():
        hits = [kw for kw in keywords if kw.lower() in text.lower()]
        if hits:
            matched.extend(hits)
            category = cat
            score -= 10

    if not matched and any(x in text for x in ["指数", "大盘", "市场", "经济"]):
        category = "宏观"
        score += 1

    return category, score, sorted(set(matched))


def normalize_news_frame(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["title", "content", "published_at", "source"])

    rename_map = {}
    mapping = {
        "title": ["标题", "title", "名称", "新闻标题"],
        "content": ["内容", "content", "摘要", "正文"],
        "published_at": ["时间", "发布时间", "日期", "date", "publish_time"],
        "code": ["代码", "symbol"],
        "name": ["名称", "股票名称"],
    }
    for std_name, candidates in mapping.items():
        col = pick_col(df, candidates)
        if col:
            rename_map[col] = std_name

    df = df.rename(columns=rename_map).copy()
    if "title" not in df.columns:
        df["title"] = ""
    if "content" not in df.columns:
        df["content"] = ""
    if "published_at" not in df.columns:
        df["published_at"] = ""
    df["source"] = source

    for col in ["title", "content", "published_at"]:
        df[col] = df[col].astype(str).fillna("")
    keep_cols = ["title", "content", "published_at", "source"]
    for col in ["code", "name"]:
        if col in df.columns:
            keep_cols.append(col)
    return df[keep_cols].copy()


def fetch_market_news(cache: DiskCache, limit: int = 50) -> pd.DataFrame:
    """抓市场新闻，作为题材催化和情绪背景。"""
    cache_key = f"market_news:{datetime.now().strftime('%Y%m%d')}"
    cached = cache.get_df(cache_key, ttl_sec=900)
    if cached is not None:
        return cached.head(limit).copy()

    frames = []
    try:
        today = datetime.now().strftime("%Y%m%d")
        cctv = ak.news.news_cctv.news_cctv(date=today)
        frames.append(normalize_news_frame(cctv, "CCTV"))
    except Exception:
        pass

    if not frames:
        news_df = pd.DataFrame(columns=["title", "content", "published_at", "source"])
    else:
        news_df = pd.concat(frames, ignore_index=True)
        news_df = news_df.drop_duplicates(subset=["title", "content"], keep="first")

    cache.set_df(cache_key, news_df)
    return news_df.head(limit).copy()


def fetch_stock_news(code: str, cache: DiskCache, notice_days_back: int = 2, news_limit: int = 5) -> pd.DataFrame:
    """抓单只股票的新闻和公告，实战里这一步很重要。"""
    code = clean_code(code)
    cache_key = f"stock_news:{code}"
    cached = cache.get_df(cache_key, ttl_sec=1800)
    if cached is not None:
        return cached.head(news_limit).copy()

    frames = []
    try:
        news_df = ak.stock_news_em(symbol=code)
        frames.append(normalize_news_frame(news_df, "个股新闻"))
    except Exception:
        pass

    for offset in range(max(1, notice_days_back)):
        date_str = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            notice_df = ak.stock_notice_report(symbol=code, date=date_str)
            frames.append(normalize_news_frame(notice_df, "公司公告"))
        except Exception:
            continue

    if not frames:
        result = pd.DataFrame(columns=["title", "content", "published_at", "source"])
    else:
        result = pd.concat(frames, ignore_index=True)
        result = result.drop_duplicates(subset=["title", "content"], keep="first")

    cache.set_df(cache_key, result)
    return result.head(news_limit).copy()


def score_news_for_row(row: pd.Series, industry_name: str, market_news: pd.DataFrame, stock_news: pd.DataFrame) -> tuple[int, str, list[str]]:
    """把新闻分数映射到个股上，核心是“催化剂加分、风险项减分”。"""
    score = 0
    tags: list[str] = []
    hits: list[str] = []
    name = safe_text(row.get("name"))

    def _scan_texts(df: pd.DataFrame, source_label: str) -> None:
        nonlocal score, tags, hits
        for _, item in df.iterrows():
            title = safe_text(item.get("title"))
            content = safe_text(item.get("content"))
            text = f"{title} {content}"
            if not text.strip():
                continue

            category, item_score, matched = classify_text(text)
            if matched:
                hits.extend(matched)

            if name and name in text:
                score += max(item_score, 3)
                tags.append(f"{source_label}:{category}")
            elif industry_name and industry_name not in {"未识别", "热点题材", "热点概念"} and industry_name in text:
                score += max(item_score // 2, 1)
                tags.append(f"{source_label}:{category}")
            elif category != "其他" and source_label == "公司公告":
                score += item_score
                tags.append(f"{source_label}:{category}")

    _scan_texts(market_news, "市场新闻")
    _scan_texts(stock_news, "公司公告")
    _scan_texts(stock_news, "个股新闻")

    tag_text = ",".join(sorted(set(tags))) if tags else "无明显催化"
    hit_text = sorted(set(hits))
    return score, tag_text, hit_text


def analyze_market_news(market_news: pd.DataFrame) -> str:
    if market_news is None or market_news.empty:
        return "无新闻数据"

    text_blob = " ".join(
        (safe_text(r.get("title")) + " " + safe_text(r.get("content")))
        for _, r in market_news.iterrows()
    )
    score = 0
    for _, keywords in POSITIVE_KEYWORDS.items():
        if any(kw in text_blob for kw in keywords):
            score += 1
    for _, keywords in NEGATIVE_KEYWORDS.items():
        if any(kw in text_blob for kw in keywords):
            score -= 1

    if score >= 2:
        return "题材催化偏强"
    if score == 1:
        return "存在局部催化"
    if score == 0:
        return "新闻中性"
    return "新闻风险偏高"


def limit_quality(row: pd.Series) -> str:
    pct = float(row.get("pct_chg", 0))
    price = float(row.get("price", 0))
    high = float(row.get("high", 0)) if pd.notna(row.get("high")) else 0
    low = float(row.get("low", 0)) if pd.notna(row.get("low")) else 0

    if pct < 4:
        return "趋势观察"
    if pct < 9.5:
        return "强势拉升"
    if high == price and low == price:
        return "一字涨停"
    if high == price:
        return "封板较强"
    return "炸板/回落"


def lhb_status(code: str, cache: DiskCache | None = None) -> tuple[str, str]:
    """龙虎榜检查：这是 A 股短线里很实用的资金面辅助。"""
    code = clean_code(code)
    today = datetime.now().strftime("%Y%m%d")
    cache_key = f"lhb:{code}:{today}"
    if cache:
        cached = cache.get(cache_key, ttl_sec=1800)
        if cached is not None:
            return cached.get("tag", "缓存命中"), cached.get("seats", "")

    try:
        dates = ak.stock_lhb_stock_detail_date_em(symbol=code)
        if dates is None or dates.empty:
            result = ("未上榜", "")
            if cache:
                cache.set(cache_key, {"tag": result[0], "seats": result[1]})
            return result

        date_col = pick_col(dates, ["交易日", "日期", "date"])
        if not date_col:
            result = ("已上榜", "")
            if cache:
                cache.set(cache_key, {"tag": result[0], "seats": result[1]})
            return result

        latest = pd.to_datetime(dates.iloc[0][date_col]).strftime("%Y%m%d")
        if latest != today:
            result = ("非今日榜", latest)
            if cache:
                cache.set(cache_key, {"tag": result[0], "seats": result[1]})
            return result

        detail = ak.stock_lhb_stock_detail_em(symbol=code, date=today, flag="买入")
        if detail is None or detail.empty:
            result = ("今日上榜", "")
            if cache:
                cache.set(cache_key, {"tag": result[0], "seats": result[1]})
            return result

        net_col = pick_col(detail, ["净额", "买入净额"])
        seat_col = pick_col(detail, ["交易营业部名称", "营业部名称"])
        type_col = pick_col(detail, ["类型"])

        net = float(pd.to_numeric(detail[net_col], errors="coerce").fillna(0).sum()) if net_col else 0
        seats = " | ".join(detail[seat_col].dropna().astype(str).head(3).tolist()) if seat_col else ""

        if type_col and detail[type_col].astype(str).str.contains("机构专用").any():
            result = ("机构参与", seats)
        elif net > 0:
            result = ("席位净买", seats)
        elif net < 0:
            result = ("席位净卖", seats)
        else:
            result = ("今日上榜", seats)

        if cache:
            cache.set(cache_key, {"tag": result[0], "seats": result[1]})
        return result
    except Exception:
        result = ("龙虎榜异常", "")
        if cache:
            cache.set(cache_key, {"tag": result[0], "seats": result[1]})
        return result


def build_pool(df: pd.DataFrame, cfg: Config, limit: int | None = None) -> pd.DataFrame:
    active = df.copy()
    active = active[~active["name"].str.contains("ST|退", regex=True, na=False)]
    active = active[(active["price"] >= cfg.min_price) & (active["amount"] >= cfg.min_amount)]

    if cfg.mode == "pre":
        active = active[(active["gap"] >= 1.5) & (active["gap"] <= 8.5)]
        active["score"] = active["gap"].rank(pct=True) * 50 + active["amount"].rank(pct=True) * 50
    elif cfg.mode == "after":
        active = active[active["pct_chg"] >= 3]
        active["score"] = active["pct_chg"].rank(pct=True) * 55 + active["amount"].rank(pct=True) * 35
        if "turnover" in active.columns:
            active["score"] += active["turnover"].rank(pct=True).fillna(0) * 10
    else:
        active = active[active["pct_chg"] >= 4]
        active["score"] = active["pct_chg"].rank(pct=True) * 60 + active["amount"].rank(pct=True) * 40

    limit = cfg.top if limit is None else max(1, int(limit))
    return active.sort_values("score", ascending=False).head(limit).copy()


def get_ai_client() -> tuple[OpenAI | None, str | None]:
    if OpenAI is None:
        return None, None

    api_key = os.getenv(app_config.ENV_ARK_API_KEY) or os.getenv("OPENAI_API_KEY")
    model = os.getenv(app_config.ENV_ARK_MODEL) or app_config.ARK_MODEL_DEFAULT or os.getenv("OPENAI_MODEL")
    base_url = os.getenv(app_config.ENV_ARK_BASE_URL) or app_config.ARK_BASE_URL_DEFAULT or os.getenv("OPENAI_BASE_URL")
    if not api_key or not model:
        return None, None

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs), model


def ai_comment(client, model: str, row: pd.Series, market_state: str, market_news_state: str) -> str:
    """把结构化指标交给模型做一句话复盘，便于盘后读。"""
    prompt = (
        "你是A股短线复盘助手，只做风险提示和交易计划，不承诺收益。"
        f"大盘状态：{market_state}；新闻状态：{market_news_state}。"
        f"个股：{row['name']}({row['code']})，行业：{row['industry']}；"
        f"涨幅：{row['pct_chg']:.2f}%；价格：{row['price']}；成交额：{row['amount'] / 1e8:.2f}亿；"
        f"形态：{row['limit_quality']}；龙虎榜：{row['lhb_tag']}；"
        f"20日压力：{row['pressure_pct']}%；压力标签：{row['pressure_label']}；"
        f"新闻标签：{row.get('news_tags', '无')}。"
        "请用 80 字以内输出：强弱判断、明日观察位、风险点。"
    )

    try:
        res = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        return res.choices[0].message.content.strip()
    except Exception as exc:
        return f"AI失败: {exc}"


def compact_text(value: Any, limit: int = 24) -> str:
    text = safe_text(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def build_news_brief(row: pd.Series) -> str:
    """把新闻和公告压缩成一句话，适合手机端快速浏览。"""
    score = float(row.get("news_score", 0) or 0)
    tags = safe_text(row.get("news_tags", ""))
    hits = [compact_text(item, 8) for item in safe_text(row.get("news_hits", "")).split(" | ") if safe_text(item)]
    hits = [item for item in hits if item]

    if "新闻失败" in tags:
        return "新闻抓取失败，暂不参考。"
    if "无明显催化" in tags or "已限制到前几只候选股" in tags:
        return "新闻暂无明确催化。"

    if score <= -8:
        lead = "新闻偏风险"
    elif score >= 8:
        lead = "新闻偏利好"
    else:
        lead = "新闻中性偏正"

    keywords: list[str] = []
    for item in hits:
        if item not in keywords:
            keywords.append(item)
        if len(keywords) >= 2:
            break

    if not keywords:
        for token in [piece.strip() for piece in tags.replace("：", ":").split(",") if piece.strip()]:
            tail = token.split(":")[-1].strip()
            if tail and tail not in keywords and tail not in {"公司公告", "个股新闻", "市场新闻"}:
                keywords.append(compact_text(tail, 8))
            if len(keywords) >= 2:
                break

    if keywords:
        return f"{lead}，关注{'、'.join(keywords)}。"
    return f"{lead}。"


THEME_KEYWORDS: list[tuple[str, list[str]]] = [
    ("AI算力", ["AI", "算力", "大模型", "服务器", "液冷", "数据中心"]),
    ("机器人", ["机器人", "人形机器人", "工业母机", "自动化", "伺服", "减速器"]),
    ("半导体", ["半导体", "芯片", "EDA", "光刻", "存储", "封装", "晶圆"]),
    ("创新药", ["创新药", "医药", "生物制品", "CXO", "减肥药", "仿制药"]),
    ("低空经济", ["低空经济", "无人机", "飞行汽车", "通航", "eVTOL"]),
    ("新能源", ["储能", "光伏", "锂电", "固态电池", "充电桩", "氢能"]),
    ("国产替代", ["国产替代", "自主可控", "信创", "工业软件", "国防", "军工"]),
    ("消费复苏", ["消费", "零售", "白酒", "旅游", "餐饮", "家电"]),
    ("并购重组", ["并购", "重组", "收购", "资产注入", "控制权变更"]),
    ("回购增持", ["回购", "增持", "股份回购", "大股东增持"]),
    ("政策催化", ["政策", "规划", "发布", "试点", "批复", "推进", "支持"]),
]


def infer_theme_label(row: pd.Series) -> str:
    """尽量把票落到更具体的题材标签，而不是停在泛行业名。"""
    industry = safe_text(row.get("industry"))
    if industry and industry not in {"未识别", "热点题材", "热点概念"}:
        return industry

    evidence: dict[str, int] = {}
    fields = ["news_tags", "news_hits", "entry_reason", "history_replay", "trade_playbook", "name"]
    for label, keywords in THEME_KEYWORDS:
        matched_sources = 0
        for field in fields:
            text = safe_text(row.get(field)).lower()
            if text and any(kw.lower() in text for kw in keywords):
                matched_sources += 1
        evidence[label] = matched_sources

    best_label = ""
    best_score = 0
    for label, score in evidence.items():
        if score > best_score:
            best_label = label
            best_score = score

    if best_score >= 2:
        return best_label

    return "题材待确认"


def build_theme_heat_bundle(row: pd.Series, market_news_state: str) -> pd.Series:
    """给题材补一个热度分层，宁愿保守一点，也不硬猜。

    额外参考因子：
    - 资金活跃度：成交额、龙虎榜净买
    - 新闻/公告：业绩、政策、并购、回购增持等
    - 市场情绪：大盘新闻环境是否偏正
    - 价格行为：封板质量、突破形态作为确认项
    """

    score = 0.0
    reasons: list[str] = []

    amount_rank = float(row.get("amount_rank_pct", 0) or 0)
    if amount_rank >= 0.85:
        score += 2.5
        reasons.append("资金非常活跃")
    elif amount_rank >= 0.65:
        score += 1.5
        reasons.append("资金活跃")
    elif amount_rank >= 0.45:
        score += 0.8
        reasons.append("资金一般")

    news_score = float(row.get("news_score", 0) or 0)
    news_tags = safe_text(row.get("news_tags"))
    news_hits = safe_text(row.get("news_hits"))
    policy_keywords = ["政策", "规划", "批复", "支持", "推进", "落地", "发布", "试点", "国务院", "部委"]
    event_keywords = ["业绩", "预增", "回购", "增持", "并购", "重组", "合作", "订单", "中标", "融资"]

    if news_score >= 8:
        score += 2.0
        reasons.append("新闻催化强")
    elif news_score >= 4:
        score += 1.0
        reasons.append("新闻有催化")
    elif news_score <= -8:
        score -= 2.5
        reasons.append("新闻偏风险")

    if any(keyword in news_tags or keyword in news_hits for keyword in policy_keywords):
        score += 1.8
        reasons.append("政策线索明确")
    if any(keyword in news_tags or keyword in news_hits for keyword in event_keywords):
        score += 1.2
        reasons.append("事件驱动明确")

    lhb_tag = safe_text(row.get("lhb_tag"))
    if lhb_tag in {"席位净买", "机构参与"}:
        score += 1.2
        reasons.append("资金席位偏强")
    elif lhb_tag in {"席位净卖", "龙虎榜异常"}:
        score -= 1.5
        reasons.append("资金席位偏弱")

    market_news_state = safe_text(market_news_state)
    if market_news_state == "题材催化偏强":
        score += 1.5
        reasons.append("市场题材共振")
    elif market_news_state == "存在局部催化":
        score += 0.8
        reasons.append("局部题材活跃")
    elif market_news_state == "新闻风险偏高":
        score -= 1.2
        reasons.append("大环境偏谨慎")

    limit_quality = safe_text(row.get("limit_quality"))
    if limit_quality in {"封板较强", "一字涨停"}:
        score += 1.0
        reasons.append("价格行为确认")
    elif limit_quality == "炸板/回落":
        score -= 0.8
        reasons.append("价格行为转弱")

    pressure_label = safe_text(row.get("pressure_label"))
    if pressure_label == "突破/新高":
        score += 0.8
        reasons.append("突破确认")

    if score >= 6:
        level = "高"
    elif score >= 3:
        level = "中"
    elif score >= 1:
        level = "低"
    else:
        level = "待确认"

    if level == "待确认" or not reasons:
        reason_text = "题材证据不足，建议保持题材待确认。"
    else:
        reason_text = "，".join(dict.fromkeys(reasons[:4]))

    return pd.Series(
        {
            "theme_heat_score": round(score, 2),
            "theme_heat_level": level,
            "theme_heat_reason": reason_text,
        }
    )


def _rebalance_buy_state(price: float, entry_price: float, has_holding: bool, fallback_state: str, fallback_reason: str) -> tuple[str, str]:
    """按锚定后的入场价重新给买点状态，避免价格下跌后不断把买点往下挪。"""

    if has_holding:
        return fallback_state or "持仓观察", fallback_reason

    if price <= 0 or entry_price <= 0:
        return fallback_state or "等待确认", fallback_reason

    gap_pct = (entry_price - price) / price * 100.0 if price > 0 else 0.0
    if gap_pct <= 0:
        return "已到买点", fallback_reason or "价格已到锚定入场位"
    if gap_pct <= 1.2:
        return "临近买点", f"{fallback_reason or '距离入场位不远'}；距离买点{gap_pct:.2f}%"
    if gap_pct <= 3.0:
        return "等待确认", f"{fallback_reason or '等确认再说'}；距离买点{gap_pct:.2f}%"
    return "观察", f"{fallback_reason or '暂不追'}；距离买点{gap_pct:.2f}%"


def build_signal_anchor_bundle(row: pd.Series, cache: DiskCache) -> pd.Series:
    """把同一只票的信号锚定住，避免下跌后入场价越改越低。"""

    code = safe_text(row.get("code"))
    mode = safe_text(row.get("mode")) or "mid"
    if not code:
        return pd.Series(
            {
                "signal_anchor_id": "",
                "signal_anchor_locked": False,
                "signal_anchor_first_seen": safe_text(row.get("signal_first_seen")),
            }
        )

    current_day = datetime.now().date().isoformat()
    first_seen = safe_text(row.get("signal_first_seen")) or safe_text(row.get("entry_time")) or current_day
    signal_state = safe_text(row.get("signal_state"))
    signal_action = safe_text(row.get("signal_action"))
    signal_note = safe_text(row.get("signal_note")) or safe_text(row.get("buy_reason"))
    active = signal_state not in {"target_hit", "stop_hit", "time_stop"}
    cache_key = f"signal_anchor:{code}:{mode}"
    cached = cache.get(cache_key, ttl_sec=365 * 24 * 3600) or {}
    cached_active = bool(cached.get("active"))
    cached_first_seen = safe_text(cached.get("first_seen"))
    signal_anchor_id = f"{code}:{mode}:{first_seen}"

    price = float(row.get("price", 0) or row.get("close", 0) or 0)
    entry_price = float(row.get("entry_price", 0) or 0)
    stop_loss = float(row.get("stop_loss", 0) or 0)
    take_profit = float(row.get("take_profit", 0) or 0)
    risk_reward = float(row.get("risk_reward", 0) or 0)
    position_pct = float(row.get("position_pct", 0) or 0)
    risk_reason = safe_text(row.get("risk_reason") or row.get("buy_reason") or row.get("entry_reason"))
    risk_confidence = float(row.get("risk_confidence", 0) or 0)
    buy_state = safe_text(row.get("buy_state"))
    buy_reason = safe_text(row.get("buy_reason"))
    theme_label = safe_text(row.get("theme_label")) or "题材待确认"
    theme_heat_level = safe_text(row.get("theme_heat_level")) or "待确认"
    theme_heat_reason = safe_text(row.get("theme_heat_reason")) or "题材证据不足"

    use_cache = active and cached_active and cached_first_seen == first_seen
    if use_cache:
        entry_price = float(cached.get("entry_price", entry_price) or entry_price)
        stop_loss = float(cached.get("stop_loss", stop_loss) or stop_loss)
        take_profit = float(cached.get("take_profit", take_profit) or take_profit)
        risk_reward = float(cached.get("risk_reward", risk_reward) or risk_reward)
        position_pct = float(cached.get("position_pct", position_pct) or position_pct)
        risk_reason = safe_text(cached.get("risk_reason")) or risk_reason
        risk_confidence = float(cached.get("risk_confidence", risk_confidence) or risk_confidence)
        theme_label = safe_text(cached.get("theme_label")) or theme_label
        theme_heat_level = safe_text(cached.get("theme_heat_level")) or theme_heat_level
        theme_heat_reason = safe_text(cached.get("theme_heat_reason")) or theme_heat_reason
        buy_state = safe_text(cached.get("buy_state")) or buy_state
        buy_reason = safe_text(cached.get("buy_reason")) or buy_reason
    elif active:
        payload = {
            "first_seen": first_seen,
            "active": True,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": risk_reward,
            "position_pct": position_pct,
            "risk_reason": risk_reason,
            "risk_confidence": risk_confidence,
            "buy_state": buy_state,
            "buy_reason": buy_reason,
            "theme_label": theme_label,
            "theme_heat_level": theme_heat_level,
            "theme_heat_reason": theme_heat_reason,
            "signal_state": signal_state,
            "signal_action": signal_action,
            "updated_at": current_day,
        }
        cache.set(cache_key, payload)
    else:
        cache.set(
            cache_key,
            {
                "first_seen": first_seen,
                "active": False,
                "resolved_state": signal_state,
                "signal_action": signal_action,
                "signal_note": signal_note,
                "updated_at": current_day,
            },
        )

    if active:
        buy_state, buy_reason = _rebalance_buy_state(price, entry_price, bool(row.get("has_holding")), buy_state, buy_reason or signal_note)
        if entry_price > 0 and price > 0:
            entry_gap_pct = round((entry_price - price) / price * 100.0, 2)
        else:
            entry_gap_pct = row.get("entry_gap_pct")
    else:
        entry_gap_pct = row.get("entry_gap_pct")

    return pd.Series(
        {
            "signal_anchor_id": signal_anchor_id,
            "signal_anchor_locked": active,
            "signal_anchor_first_seen": first_seen,
            "signal_anchor_entry": round(entry_price, 2) if entry_price > 0 else entry_price,
            "signal_anchor_stop_loss": round(stop_loss, 2) if stop_loss > 0 else stop_loss,
            "signal_anchor_take_profit": round(take_profit, 2) if take_profit > 0 else take_profit,
            "signal_anchor_risk_reward": round(risk_reward, 2) if risk_reward > 0 else risk_reward,
            "signal_anchor_position_pct": round(position_pct, 2) if position_pct > 0 else position_pct,
            "signal_anchor_risk_reason": risk_reason,
            "signal_anchor_risk_confidence": round(risk_confidence, 3),
            "signal_anchor_buy_state": buy_state,
            "signal_anchor_buy_reason": buy_reason or signal_note,
            "signal_anchor_theme_label": theme_label,
            "signal_anchor_theme_heat_level": theme_heat_level,
            "signal_anchor_theme_heat_reason": theme_heat_reason,
            "entry_price": round(entry_price, 2) if entry_price > 0 else entry_price,
            "stop_loss": round(stop_loss, 2) if stop_loss > 0 else stop_loss,
            "take_profit": round(take_profit, 2) if take_profit > 0 else take_profit,
            "risk_reward": round(risk_reward, 2) if risk_reward > 0 else risk_reward,
            "position_pct": round(position_pct, 2) if position_pct > 0 else position_pct,
            "risk_reason": risk_reason,
            "risk_confidence": round(risk_confidence, 3),
            "buy_state": buy_state,
            "buy_reason": buy_reason or signal_note,
            "entry_gap_pct": entry_gap_pct,
        }
    )


def build_entry_reason(row: pd.Series, market_info: dict[str, Any]) -> str:
    """给出入选理由，尽量短，适合直接发到企业微信。"""
    reasons: list[str] = []
    market_state = safe_text(market_info.get("state"))

    if market_state in {"强势进攻", "温和修复"}:
        reasons.append(f"大盘{market_state}")
    elif market_state == "弱势震荡":
        reasons.append("弱市相对强")
    else:
        reasons.append("逆势观察")

    pct_chg = float(row.get("pct_chg", 0) or 0)
    if pct_chg >= 4:
        reasons.append("涨幅靠前")

    amount_rank = float(row.get("amount_rank_pct", 0) or 0)
    if amount_rank >= 0.7:
        reasons.append("成交活跃")

    limit_quality = safe_text(row.get("limit_quality"))
    if limit_quality in {"封板较强", "一字涨停"}:
        reasons.append("封板质量高")
    elif limit_quality == "强势拉升":
        reasons.append("走势偏强")
    elif limit_quality == "炸板/回落":
        reasons.append("波动较大")

    pressure_label = safe_text(row.get("pressure_label"))
    if pressure_label in {"贴近前高", "近端压力"}:
        reasons.append("接近前高")
    elif pressure_label == "突破/新高":
        reasons.append("创出新高")

    lhb_tag = safe_text(row.get("lhb_tag"))
    if lhb_tag in {"席位净买", "机构参与"}:
        reasons.append("资金净买")
    elif lhb_tag in {"席位净卖", "龙虎榜异常"}:
        reasons.append("资金需谨慎")

    news_score = float(row.get("news_score", 0) or 0)
    if news_score >= 8:
        reasons.append("新闻催化")
    elif news_score <= -8:
        reasons.append("新闻风险")

    theme_heat_level = safe_text(row.get("theme_heat_level"))
    if theme_heat_level == "高":
        reasons.append("题材热度高")
    elif theme_heat_level == "中":
        reasons.append("题材热度中")
    elif theme_heat_level == "低":
        reasons.append("题材热度低")

    if not reasons:
        return "入选理由：条件均衡。"

    picked = reasons[:3]
    return "入选理由：" + "，".join(picked) + "。"


def build_ai_overview_summary(
    client,
    model: str,
    result: pd.DataFrame,
    market_info: dict[str, Any],
    market_news_state: str,
    cfg: Config,
) -> str:
    """对整批候选做一次 AI 汇总，适合放在通知最上方。"""
    if client is None or not model or result.empty:
        return ""

    top_rows = result.head(min(cfg.notify_top, 5))
    candidate_lines = []
    for _, row in top_rows.iterrows():
        candidate_lines.append(
            f"{row['code']} {row['name']} | 总分{float(row.get('final_score', 0) or 0):.1f} | "
            f"{compact_text(row.get('entry_reason', ''), 20)} | "
            f"{compact_text(row.get('history_replay', ''), 18)} | "
            f"{compact_text(row.get('news_brief', ''), 18)}"
        )

    prompt = (
        "你是A股短线复盘助手。"
        f"大盘状态：{market_info.get('state', '未知')}；新闻环境：{market_news_state}。"
        "请把下面这批候选股做一个100字以内的总判断，"
        "输出要求：1句市场结论，1句机会方向，1句风险提醒，语言尽量口语化。"
        "候选如下：\n"
        + "\n".join(candidate_lines)
    )

    try:
        res = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        return compact_text(res.choices[0].message.content.strip(), 120)
    except Exception:
        return ""


def build_next_day_opportunity(row: pd.Series, market_info: dict[str, Any]) -> str:
    """盘后给出次日参与机会，专门回答“没买到的话第二天还能不能上”。"""
    market_state = safe_text(market_info.get("state"))
    limit_quality = safe_text(row.get("limit_quality"))
    pressure_label = safe_text(row.get("pressure_label"))
    news_score = float(row.get("news_score", 0) or 0)
    pct_chg = float(row.get("pct_chg", 0) or 0)
    lhb_tag = safe_text(row.get("lhb_tag"))

    if "风险" in safe_text(row.get("news_tags")) or lhb_tag in {"席位净卖", "龙虎榜异常"}:
        return "次日参与：低，先看风险消化，不追。"
    if limit_quality == "炸板/回落":
        return "次日参与：低，先等修复和承接确认。"
    if limit_quality == "一字涨停":
        if market_state in {"强势进攻", "温和修复"} and news_score >= 0:
            return "次日参与：中，重点盯分歧后的回封，不追一致。"
        return "次日参与：低，一字板次日通常先看换手。"
    if limit_quality == "封板较强":
        if pressure_label == "突破/新高" or news_score >= 8:
            return "次日参与：高，回踩或分歧后更值得盯。"
        return "次日参与：中，优先看竞价和回踩承接。"
    if limit_quality == "强势拉升":
        if market_state in {"强势进攻", "温和修复"} and pct_chg >= 5:
            return "次日参与：中高，等回踩确认再考虑。"
        return "次日参与：中，追高性价比一般。"
    if pressure_label == "突破/新高" and news_score >= 0:
        return "次日参与：中高，突破后更适合等确认。"
    if news_score >= 8:
        return "次日参与：中，题材强但先等分歧。"
    return "次日参与：中低，以观察为主。"


def build_intraday_buy_state(row: pd.Series, market_info: dict[str, Any], cfg: Config) -> tuple[str, str]:
    """根据盘中强弱、压力位和持仓状态生成买点提醒。"""
    market_state = safe_text(market_info.get("state"))
    limit_quality = safe_text(row.get("limit_quality"))
    pressure_label = safe_text(row.get("pressure_label"))
    pressure_pct = row.get("pressure_pct")
    pct_chg = float(row.get("pct_chg", 0) or 0)
    amount_rank = float(row.get("amount_rank_pct", 0) or 0)
    news_score = float(row.get("news_score", 0) or 0)
    lhb_tag = safe_text(row.get("lhb_tag"))

    hold_status = safe_text(row.get("hold_status"))
    hold_qty = safe_text(row.get("hold_qty"))
    hold_cost = float(row.get("hold_cost_price") or 0)
    hold_current = float(row.get("hold_current_price") or row.get("price", 0) or 0)
    hold_stop = row.get("hold_stop_price")
    hold_take = row.get("hold_take_price")
    hold_stop_value = float(hold_stop) if hold_stop is not None and pd.notna(hold_stop) else None
    hold_take_value = float(hold_take) if hold_take is not None and pd.notna(hold_take) else None

    if hold_status in {"holding", "partial_sell"}:
        if hold_stop_value is not None and hold_current > 0 and hold_current <= hold_stop_value:
            return "止损提醒", f"现价已触及持仓止损价 {hold_stop_value:.2f}，优先处理风险。"
        if hold_take_value is not None and hold_current > 0 and hold_current >= hold_take_value:
            return "止盈提醒", f"现价已触及持仓止盈价 {hold_take_value:.2f}，可考虑分批兑现。"
        hold_desc = "持仓观察"
        if hold_qty and hold_qty != "0":
            hold_desc += f"，数量{hold_qty}"
        if hold_cost > 0:
            hold_desc += f"，成本{hold_cost:.2f}"
        if hold_stop_value is not None:
            hold_desc += f"，止损{hold_stop_value:.2f}"
        if hold_take_value is not None:
            hold_desc += f"，止盈{hold_take_value:.2f}"
        return "持仓观察", hold_desc

    if limit_quality == "炸板/回落" or lhb_tag in {"席位净卖", "龙虎榜异常"}:
        return "不建议介入", "炸板回落或资金面异常，先等风险释放。"
    if limit_quality == "一字涨停":
        return "观察", "一字板不适合盘中追，等分歧换手。"
    if pct_chg < 3 and pressure_label != "突破/新高":
        return "观察", "涨幅和突破确认不足，暂不追。"

    has_pressure = pressure_pct is not None and pd.notna(pressure_pct)
    pressure_value = float(pressure_pct) if has_pressure else None
    near_pressure = pressure_value is not None and pressure_value <= cfg.intraday_near_pressure_pct
    trigger_pressure = pressure_value is not None and pressure_value <= cfg.intraday_trigger_pressure_pct

    if pressure_label == "突破/新高":
        if market_state in {"强势进攻", "温和修复"} and (pct_chg >= 4 or amount_rank >= 0.65 or news_score >= 6):
            return "已到买点", "已经突破前高，且市场或资金条件配合。"
        return "临近买点", "已突破前高，但还要观察承接。"

    if limit_quality == "封板较强":
        if trigger_pressure or (market_state == "强势进攻" and amount_rank >= 0.6):
            return "已到买点", "封板较强且临近突破位，适合小仓确认。"
        if near_pressure:
            return "临近买点", "封板较强，等待突破确认。"
        return "观察", "封板较强但离压力位仍有距离。"

    if limit_quality == "强势拉升":
        if trigger_pressure and news_score >= 0:
            return "已到买点", "强势拉升接近突破位，短线条件到位。"
        if near_pressure or pct_chg >= 5:
            return "临近买点", "强势拉升，等突破或回踩确认。"
        return "观察", "拉升力度有了，但买点还不够明确。"

    if near_pressure:
        return "临近买点", "距离前高或压力位较近，盯 5 分钟承接。"

    if news_score >= 8 and market_state in {"强势进攻", "温和修复"}:
        return "临近买点", "新闻催化较强，等技术确认。"

    return "观察", "条件还不够集中，先放在观察池。"

def build_risk_plan(row: pd.Series, market_info: dict[str, Any]) -> str:
    """给出止盈止损参考，用当前已拿到的数据直接推，不额外加接口请求。"""
    market_state = safe_text(market_info.get("state"))
    limit_quality = safe_text(row.get("limit_quality"))
    pressure_label = safe_text(row.get("pressure_label"))
    news_score = float(row.get("news_score", 0) or 0)
    pct_chg = float(row.get("pct_chg", 0) or 0)
    price = float(row.get("price", 0) or 0)

    stop_pct = 3.5
    take_pct = 7.0

    if limit_quality == "一字涨停":
        stop_pct, take_pct = 2.8, 8.0
    elif limit_quality == "封板较强":
        stop_pct, take_pct = 3.2, 8.5
    elif limit_quality == "强势拉升":
        stop_pct, take_pct = 4.0, 9.5
    elif limit_quality == "炸板/回落":
        stop_pct, take_pct = 2.5, 5.5

    if market_state == "强势进攻":
        take_pct += 1.0
    elif market_state == "风险释放":
        stop_pct -= 0.5
        take_pct -= 1.0

    if pressure_label == "突破/新高":
        take_pct += 1.0
    elif pressure_label in {"贴近前高", "近端压力"}:
        take_pct += 0.5

    if news_score >= 8:
        take_pct += 0.8
    elif news_score <= -8:
        stop_pct -= 0.5
        take_pct -= 1.0

    if pct_chg >= 8:
        stop_pct += 0.5
    elif pct_chg < 4:
        take_pct -= 0.5

    stop_pct = max(1.8, round(stop_pct, 1))
    take_pct = max(stop_pct + 1.5, round(take_pct, 1))

    if price > 0:
        stop_price = price * (1 - stop_pct / 100)
        take_price = price * (1 + take_pct / 100)
        return (
            f"止盈参考：{take_price:.2f}（+{take_pct:.1f}%）；"
            f"止损参考：{stop_price:.2f}（-{stop_pct:.1f}%）"
        )
    return f"止盈参考：+{take_pct:.1f}%；止损参考：-{stop_pct:.1f}%"


def build_history_playbook(row: pd.Series, market_info: dict[str, Any], cache: DiskCache | None = None) -> tuple[str, str]:
    """根据历史日线推演当前买卖结构，尽量不增加接口压力。"""
    code = safe_text(row.get("code"))
    market_state = safe_text(market_info.get("state"))
    daily = fetch_daily_history(code, cache=cache)
    if daily.empty or len(daily) < 40:
        fallback = "历史推演：样本不足，先按实时结构观察。"
        return fallback, "卖出策略：跌破关键位先走，冲高不放量可减。"

    df = daily.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(df) < 40:
        fallback = "历史推演：有效样本不足，先看盘口确认。"
        return fallback, "卖出策略：盯住前低和分时回落。"

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["prev_high20"] = df["high"].rolling(20).max().shift(1)
    df["signal"] = (
        (df["close"] >= df["prev_high20"] * 0.995)
        & (df["volume"] >= df["vol_ma5"] * 1.1)
        & (df["close"] >= df["open"])
    )

    signal_idx = [int(i) for i in df.index[df["signal"]].tolist() if i + 1 < len(df)]
    recent_idx = signal_idx[-10:]
    samples: list[dict[str, float]] = []
    for idx in recent_idx:
        entry = float(df.at[idx, "close"])
        if entry <= 0:
            continue
        forward = df.iloc[idx + 1 : min(idx + 4, len(df))]
        if forward.empty:
            continue
        next1 = float(forward["close"].iloc[0])
        next3 = float(forward["close"].iloc[-1])
        max_up = float(forward["high"].max() / entry - 1)
        min_down = float(forward["low"].min() / entry - 1)
        samples.append(
            {
                "ret1": next1 / entry - 1,
                "ret3": next3 / entry - 1,
                "max_up": max_up,
                "min_down": min_down,
            }
        )

    if not samples:
        return "历史推演：近期没有足够相似样本。", "卖出策略：先看前低是否守住，再决定是否减仓。"

    win_rate = sum(1 for s in samples if s["ret3"] > 0) / len(samples)
    fast_rate = sum(1 for s in samples if s["ret1"] > 0.02) / len(samples)
    avg_ret3 = sum(s["ret3"] for s in samples) / len(samples)
    avg_max = sum(s["max_up"] for s in samples) / len(samples)
    avg_dd = sum(abs(s["min_down"]) for s in samples) / len(samples)

    last = df.iloc[-1]
    close = float(last["close"])
    ma5 = float(last["ma5"]) if pd.notna(last["ma5"]) else close
    ma10 = float(last["ma10"]) if pd.notna(last["ma10"]) else close
    ma20 = float(last["ma20"]) if pd.notna(last["ma20"]) else close
    vol_ratio = float(last["volume"] / last["vol_ma5"]) if pd.notna(last["vol_ma5"]) and last["vol_ma5"] else 0.0
    trend_bias = "偏强" if close >= ma20 and ma5 >= ma10 else "偏弱"

    if win_rate >= 0.6 and avg_ret3 > 0:
        replay = (
            f"历史推演：近{len(samples)}次相似突破，次日正收益率{fast_rate:.0%}，"
            f"3日延续率{win_rate:.0%}，平均3日{avg_ret3 * 100:.1f}%。"
        )
        if trend_bias == "偏强" and vol_ratio >= 1.1:
            entry_tone = "买入建议：偏激进，放量突破可试仓，回踩不破更佳。"
        else:
            entry_tone = "买入建议：偏稳健，等回踩确认或分时二次放量。"
        exit_tone = (
            f"卖出建议：先看+{max(6.0, avg_max * 100 * 0.7):.1f}%分批止盈，"
            f"若回吐到-{max(2.8, avg_dd * 100 * 0.9):.1f}%先减。"
        )
    elif win_rate >= 0.45:
        replay = (
            f"历史推演：近{len(samples)}次相似形态，3日延续率{win_rate:.0%}，"
            f"平均3日{avg_ret3 * 100:.1f}%。"
        )
        entry_tone = "买入建议：中性偏进攻，必须等确认，不建议直接追高。"
        exit_tone = (
            f"卖出建议：冲高无量先落袋，跌破-{max(2.5, avg_dd * 100):.1f}%先止损。"
        )
    else:
        replay = (
            f"历史推演：近{len(samples)}次相似形态，延续率{win_rate:.0%}，"
            f"说明这类结构更吃确认，不适合无脑追。"
        )
        entry_tone = "买入建议：偏保守，最好只做确定性更高的回踩。"
        exit_tone = "卖出建议：一旦跌破前低或开盘转弱，尽早处理。"

    context = f"当前：{trend_bias}，收盘{close:.2f}，MA5/10/20={ma5:.2f}/{ma10:.2f}/{ma20:.2f}，量比{vol_ratio:.2f}。"
    return f"{replay}{context}{entry_tone}", exit_tone


def build_weighted_trade_plan(row: pd.Series, market_info: dict[str, Any], market_news_state: str) -> tuple[float, str, str]:
    """把技术、新闻、资金、题材、大盘一起加权，输出更敢说的买卖建议。"""
    market_state = safe_text(market_info.get("state"))
    limit_quality = safe_text(row.get("limit_quality"))
    pressure_label = safe_text(row.get("pressure_label"))
    news_score = float(row.get("news_score", 0) or 0)
    amount_rank = float(row.get("amount_rank_pct", 0) or 0)
    pct_chg = float(row.get("pct_chg", 0) or 0)
    lhb_tag = safe_text(row.get("lhb_tag"))
    news_tags = safe_text(row.get("news_tags"))
    industry = safe_text(row.get("industry"))

    tech = 20.0
    if limit_quality == "一字涨停":
        tech += 16
    elif limit_quality == "封板较强":
        tech += 14
    elif limit_quality == "强势拉升":
        tech += 8
    elif limit_quality == "炸板/回落":
        tech -= 10

    if pressure_label == "突破/新高":
        tech += 8
    elif pressure_label in {"贴近前高", "近端压力"}:
        tech += 4
    elif pressure_label == "上方套牢重":
        tech -= 4

    if pct_chg >= 7:
        tech += 5
    elif pct_chg >= 4:
        tech += 3
    elif pct_chg < 3:
        tech -= 3

    if amount_rank >= 0.8:
        tech += 5
    elif amount_rank >= 0.6:
        tech += 3
    elif amount_rank < 0.3:
        tech -= 2

    news = 10.0 + max(-10.0, min(10.0, news_score * 1.1))
    if "利好" in news_tags:
        news += 2
    if "风险" in news_tags:
        news -= 4

    fund = 8.0
    if lhb_tag in {"席位净买", "机构参与"}:
        fund += 6
    elif lhb_tag == "未上榜":
        fund += 1
    elif lhb_tag in {"席位净卖", "龙虎榜异常"}:
        fund -= 8

    theme = 8.0
    theme_heat_level = safe_text(row.get("theme_heat_level"))
    theme_heat_score = float(row.get("theme_heat_score", 0) or 0)
    if theme_heat_level == "高" or theme_heat_score >= 6:
        theme += 6
    elif theme_heat_level == "中" or theme_heat_score >= 3:
        theme += 4
    elif theme_heat_level == "低" or theme_heat_score >= 1:
        theme += 2
    else:
        theme -= 2
    if market_news_state == "题材催化偏强":
        theme += 7
    elif market_news_state == "存在局部催化":
        theme += 4
    elif market_news_state == "新闻风险偏高":
        theme -= 4
    if any(keyword in news_tags for keyword in ["政策", "业绩", "并购", "回购增持", "题材"]):
        theme += 4
    if industry and industry not in {"未识别", "热点题材", "热点概念"} and industry in news_tags:
        theme += 2

    market = 4.0
    if market_state == "强势进攻":
        market += 6
    elif market_state == "温和修复":
        market += 4
    elif market_state == "弱势震荡":
        market += 1
    else:
        market -= 4

    total = max(0.0, min(100.0, round(tech + news + fund + theme + market, 1)))
    profile = f"技术{tech:.1f}/新闻{news:.1f}/资金{fund:.1f}/题材{theme:.1f}/大盘{market:.1f}"

    if total >= 78:
        bias = "激进可试"
        buy = "买入：放量突破或回踩不破可分两笔试仓。"
        sell = "卖出：冲高先分批止盈，跌破风控线立刻减。"
    elif total >= 65:
        bias = "偏多可跟"
        buy = "买入：只等回踩确认，不追第一根。"
        sell = "卖出：冲高无量先收一半，弱转强失败就走。"
    elif total >= 52:
        bias = "中性偏多"
        buy = "买入：小仓观察，等二次放量再说。"
        sell = "卖出：见冲高滞涨先落袋，回吐明显就减。"
    else:
        bias = "偏弱观望"
        buy = "买入：暂不追，等结构重新站稳。"
        sell = "卖出：反弹到压力位先减，转弱及时退出。"

    playbook = f"权重[{profile}] | 结论：{bias} | {buy} | {sell}"
    return total, bias, playbook


def select_signal_rows(result: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    lifecycle_block = result["signal_state"].astype(str).isin({"target_hit", "stop_hit", "time_stop"}) if "signal_state" in result.columns else pd.Series(False, index=result.index)
    strong = result[(result["final_score"] >= cfg.notify_min_score) & ~lifecycle_block].copy()
    risk = result[
        (result["news_score"] <= -8)
        | (result["lhb_tag"].astype(str).str.contains("净卖|异常", na=False))
        | (result["limit_quality"].astype(str).eq("炸板/回落"))
        | lifecycle_block
    ].copy()
    return strong, risk


def select_intraday_buy_rows(result: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    if result.empty or "buy_state" not in result.columns:
        return result.iloc[0:0].copy()
    pct_chg = pd.to_numeric(result.get("pct_chg", 0), errors="coerce").fillna(0)
    entry = pd.to_numeric(
        result["entry_price"] if "entry_price" in result.columns else pd.Series(0, index=result.index),
        errors="coerce",
    ).fillna(0)
    take = pd.to_numeric(
        result["take_profit"] if "take_profit" in result.columns else pd.Series(0, index=result.index),
        errors="coerce",
    ).fillna(0)
    return result[
        result["buy_state"].isin(["已到买点", "临近买点"])
        & ~result["limit_quality"].astype(str).eq("一字涨停")
        & (pct_chg < 9.8)
        & ((take <= 0) | (entry <= 0) | (take > entry))
        & (result["final_score"] >= max(cfg.notify_min_score * 0.7, 50))
    ].copy()


def build_summary_markdown(
    result: pd.DataFrame,
    market_info: dict[str, Any],
    market_news_state: str,
    cfg: Config,
    ai_overview: str = "",
) -> str:
    lines = []
    lines.append("## A股扫描汇总")
    lines.append(f"> 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 模式：{cfg.mode} | 大盘：{market_info['state']} | 新闻：{market_news_state}")
    lines.append(
        f"> 上证：{market_info.get('sh_price') or '未知'} / "
        f"{market_info.get('sh_pct') if market_info.get('sh_pct') is not None else '未知'}%"
    )
    if ai_overview:
        lines.append(f"> AI汇总：{compact_text(ai_overview, 120)}")
    lines.append(f"> 候选：{len(result)} 只")
    lines.append("")

    top_rows = result.head(min(cfg.notify_top, 3)).copy()
    for idx, (_, row) in enumerate(top_rows.iterrows(), start=1):
        amount_yi = float(row["amount"]) / 1e8
        lines.append(f"### {idx}. {row['code']} {row['name']}")
        lines.append(
            f"> 行业：{row.get('industry', '未知')} | 总分：{row.get('final_score', row.get('score', 0)):.1f}"
        )
        lines.append(
            f"> 涨幅：{row['pct_chg']:.2f}% | 成交额：{amount_yi:.2f}亿 | 形态：{row.get('limit_quality', '')}"
        )
        lines.append(f"> 持仓：{compact_text(row.get('holding_brief', '未持仓'), 44)}")
        lines.append(f"> 入选理由：{compact_text(row.get('entry_reason', ''), 42)}")
        lines.append(f"> 题材：{compact_text(row.get('theme_label', '题材待确认'), 24)}")
        if row.get("history_replay"):
            lines.append(f"> 历史推演：{compact_text(row.get('history_replay', ''), 48)}")
        if row.get("next_day_opportunity") and cfg.mode == "after":
            lines.append(f"> 次日参与：{compact_text(row.get('next_day_opportunity', ''), 42)}")
        if row.get("trade_playbook"):
            lines.append(f"> 交易推演：{compact_text(row.get('trade_playbook', ''), 56)}")
        lines.append(f"> 新闻：{compact_text(row.get('news_brief', ''), 42)}")
        if row.get("ai"):
            lines.append(f"> AI：{compact_text(row['ai'], 60)}")
        lines.append("")
    return "\n".join(lines).strip()


def build_alert_markdown(row: pd.Series, kind: str, market_info: dict[str, Any], market_news_state: str, mode: str) -> str:
    amount_yi = float(row["amount"]) / 1e8
    lines = []
    lines.append(f"## A股{kind}提醒")
    lines.append(f"> 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> {row['code']} {row['name']} | {row.get('industry', '未知')}")
    lines.append(f"> 大盘：{market_info['state']} | 新闻：{market_news_state}")
    lines.append(
        f"> 涨幅：{row['pct_chg']:.2f}% | 成交额：{amount_yi:.2f}亿 | "
        f"总分：{row.get('final_score', row.get('score', 0)):.1f}"
    )
    lines.append(f"> 持仓：{compact_text(row.get('holding_brief', '未持仓'), 48)}")
    lines.append(f"> 入选理由：{compact_text(row.get('entry_reason', ''), 48)}")
    lines.append(f"> 题材：{compact_text(row.get('theme_label', '题材待确认'), 24)}")
    if row.get("history_replay"):
        lines.append(f"> 历史推演：{compact_text(row.get('history_replay', ''), 48)}")
    if row.get("next_day_opportunity") and mode == "after":
        lines.append(f"> 次日参与：{compact_text(row.get('next_day_opportunity', ''), 48)}")
    if row.get("trade_playbook"):
        lines.append(f"> 交易推演：{compact_text(row.get('trade_playbook', ''), 58)}")
    lines.append(f"> 新闻：{compact_text(row.get('news_brief', ''), 48)}")
    lines.append(
        f"> 形态：{compact_text(row.get('limit_quality', ''), 16)} | "
        f"压力：{compact_text(row.get('pressure_label', ''), 16)} | "
        f"龙虎榜：{compact_text(row.get('lhb_tag', ''), 16)}"
    )
    if row.get("ai"):
        lines.append(f"> AI：{compact_text(row['ai'], 70)}")
    return "\n".join(lines).strip()


def save_outputs(
    result: pd.DataFrame,
    market_info: dict[str, Any],
    market_news_state: str,
    ai_overview: str = "",
) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    csv_path = OUTPUT_DIR / f"scan_{stamp}.csv"
    md_path = OUTPUT_DIR / f"scan_{stamp}.md"

    columns = [
        c
        for c in [
            "code",
            "name",
            "industry",
            "price",
            "pct_chg",
            "amount",
            "score",
            "final_score",
            "amount_rank_pct",
            "limit_quality",
            "pressure_pct",
            "pressure_label",
            "lhb_tag",
            "news_score",
            "news_tags",
            "news_hits",
            "entry_reason",
            "theme_label",
            "theme_heat_score",
            "theme_heat_level",
            "theme_heat_reason",
            "history_replay",
            "next_day_opportunity",
            "hold_status",
            "hold_qty",
            "hold_cost_price",
            "hold_current_price",
            "hold_stop_price",
            "hold_take_price",
            "holding_brief",
            "has_holding",
            "buy_state",
            "buy_reason",
            "risk_plan",
            "exit_plan",
            "news_brief",
            "ai",
        ]
        if c in result.columns
    ]
    result[columns].to_csv(csv_path, index=False, encoding="utf-8-sig")

    lines = []
    lines.append("# A股扫描报告")
    lines.append("")
    lines.append(f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 大盘状态：{market_info['state']}")
    lines.append(f"- 新闻状态：{market_news_state}")
    if ai_overview:
        lines.append(f"- AI汇总：{compact_text(ai_overview, 140)}")
    lines.append(f"- 上证指数：{market_info.get('sh_price') or '未知'}")
    lines.append(f"- 上证涨跌幅：{market_info.get('sh_pct') if market_info.get('sh_pct') is not None else '未知'}")
    lines.append("")
    lines.append("| 代码 | 名称 | 行业 | 涨幅 | 成交额(亿) | 评分 | 买点 | 历史 | 卖出 |")
    lines.append("|---|---|---|---:|---:|---:|---|---|---|")
    for _, row in result.iterrows():
        lines.append(
            f"| {row['code']} | {row['name']} | {row.get('industry', '')} | "
            f"{row['pct_chg']:.2f}% | {row['amount'] / 1e8:.2f} | "
            f"{row.get('final_score', row.get('score', 0)):.2f} | "
            f"{safe_text(row.get('buy_state', ''))} | "
            f"{safe_text(row.get('history_replay', ''))} | "
            f"{safe_text(row.get('exit_plan', ''))} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"结果已保存：{csv_path}", flush=True)
    print(f"报告已保存：{md_path}", flush=True)
    return csv_path, md_path


def print_console_report(result: pd.DataFrame, market_info: dict[str, Any], market_news_state: str) -> None:
    print("\n" + "=" * 110)
    print(
        f"A股实战扫描 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"大盘：{market_info['state']} | 新闻：{market_news_state} | "
        f"上证：{market_info.get('sh_price') or '未知'}"
    )
    print("=" * 110)

    for _, row in result.iterrows():
        amount_yi = float(row["amount"]) / 1e8
        print(
            f"[{row['code']}] {row['name']} | {row.get('industry', '未知')} | "
            f"涨幅 {row['pct_chg']:.2f}% | 成交额 {amount_yi:.2f}亿 | "
            f"基础分 {row.get('score', 0):.1f} | 总分 {row.get('final_score', 0):.1f}"
        )
        print(f"  持仓：{row.get('holding_brief', '未持仓')}")
        print(f"  题材：{row.get('theme_label', '题材待确认')} | 热度：{row.get('theme_heat_level', '待确认')}")
        if row.get("buy_state"):
            print(f"  买点：{row.get('buy_state', '')} | {row.get('buy_reason', '')}")
        if row.get("next_day_opportunity"):
            print(f"  次日参与：{row.get('next_day_opportunity', '')}")
        if row.get("risk_plan"):
            print(f"  风控：{row.get('risk_plan', '')}")
        print(
            f"  形态：{row.get('limit_quality', '')} | "
            f"压力：{row.get('pressure_pct')}% {row.get('pressure_label', '')} | "
            f"龙虎榜：{row.get('lhb_tag', '')}"
        )
        print(f"  新闻：{row.get('news_tags', '')} | 命中：{row.get('news_hits', '')}")
        if row.get("ai"):
            print(f"  AI：{row['ai']}")
        print("-" * 110)


def is_market_time(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    return ("09:15" <= hhmm <= "11:35") or ("13:00" <= hhmm <= "15:10")


def is_a_share_trading_day(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return now.strftime("%Y-%m-%d") not in app_config.A_SHARE_HOLIDAYS_DEFAULT


def is_a_share_trading_time(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not is_a_share_trading_day(now):
        return False
    hhmm = now.strftime("%H:%M")
    return ("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "15:00")


def resolve_runtime_phase(now: datetime | None = None) -> str:
    """把当前时间映射成脚本运行阶段。"""
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    if "09:15" <= hhmm <= "11:35" or "13:00" <= hhmm <= "15:10":
        return "intraday"
    if "11:35" < hhmm < "13:00":
        return "lunch"
    if hhmm < "09:15":
        return "pre"
    return "after"


def sleep_with_jitter(base_sec: int, jitter_sec: int) -> None:
    jitter = random.randint(0, max(0, jitter_sec))
    time.sleep(max(5, base_sec + jitter))


def build_technical_snapshot(code: str, cache: DiskCache | None = None, price_hint: float | None = None) -> dict[str, Any]:
    """生成技术面快照。

    参数说明：
    - code：股票代码
    - cache：本地磁盘缓存，减少重复请求
    - price_hint：当前价兜底，用于盘中价格比日线收盘更实时的时候
    """

    daily = fetch_daily_history(code, cache=cache)
    if daily is None or daily.empty:
        base_price = float(price_hint or 0)
        return {
            "ma5": None,
            "ma10": None,
            "ma20": None,
            "ma30": None,
            "atr14": None,
            "support_level": round(base_price * 0.97, 2) if base_price > 0 else None,
            "pressure_level": round(base_price * 1.03, 2) if base_price > 0 else None,
            "pressure_pct": None,
            "pressure_label": "日线不足",
            "trend_state": "数据不足",
        }

    df = daily.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "close" not in df.columns or df["close"].dropna().empty:
        base_price = float(price_hint or 0)
        return {
            "ma5": None,
            "ma10": None,
            "ma20": None,
            "ma30": None,
            "atr14": None,
            "support_level": round(base_price * 0.97, 2) if base_price > 0 else None,
            "pressure_level": round(base_price * 1.03, 2) if base_price > 0 else None,
            "pressure_pct": None,
            "pressure_label": "数据不足",
            "trend_state": "数据不足",
        }

    df = df.dropna(subset=["close"]).reset_index(drop=True)
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma30"] = df["close"].rolling(30).mean()
    prev_close = df["close"].shift(1)
    tr1 = (df["high"] - df["low"]).abs() if "high" in df.columns and "low" in df.columns else pd.Series(index=df.index, dtype=float)
    tr2 = (df["high"] - prev_close).abs() if "high" in df.columns else pd.Series(index=df.index, dtype=float)
    tr3 = (df["low"] - prev_close).abs() if "low" in df.columns else pd.Series(index=df.index, dtype=float)
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    last = df.iloc[-1]
    close_price = float(price_hint or last["close"] or 0)
    ma5 = float(last["ma5"]) if pd.notna(last["ma5"]) else close_price
    ma10 = float(last["ma10"]) if pd.notna(last["ma10"]) else close_price
    ma20 = float(last["ma20"]) if pd.notna(last["ma20"]) else close_price
    ma30 = float(last["ma30"]) if pd.notna(last["ma30"]) else close_price
    atr14 = float(last["atr14"]) if pd.notna(last["atr14"]) else None

    recent = df.tail(20)
    pressure_level = float(recent["high"].max()) if "high" in recent.columns and not recent["high"].dropna().empty else close_price * 1.03
    pressure_pct = round((pressure_level - close_price) / close_price * 100, 2) if close_price > 0 else None
    if pressure_pct is None:
        pressure_label = "数据不足"
    elif pressure_level <= close_price:
        pressure_label = "突破/新高"
    elif pressure_pct <= 5:
        pressure_label = "贴近前高"
    elif pressure_pct <= 12:
        pressure_label = "近端压力"
    else:
        pressure_label = "上方套牢"

    support_candidates = []
    if "low" in df.columns:
        low_10 = df["low"].tail(10).min()
        if pd.notna(low_10):
            support_candidates.append(float(low_10))
    for value in (ma20, ma30):
        if value and value > 0:
            support_candidates.append(float(value))
    support_candidates = [value for value in support_candidates if value > 0 and value <= close_price * 1.05]
    if support_candidates:
        support_level = max(support_candidates)
    else:
        support_level = close_price * 0.97 if close_price > 0 else None

    if close_price >= ma5 >= ma10 >= ma20 >= ma30:
        trend_state = "强势上行"
    elif close_price >= ma20 and ma5 >= ma10:
        trend_state = "趋势修复"
    elif close_price < ma20 and ma5 < ma10:
        trend_state = "弱势整理"
    else:
        trend_state = "震荡"

    return {
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "ma20": round(ma20, 2),
        "ma30": round(ma30, 2),
        "atr14": round(atr14, 4) if atr14 is not None and atr14 > 0 else None,
        "support_level": round(support_level, 2) if support_level else None,
        "pressure_level": round(pressure_level, 2) if pressure_level else None,
        "pressure_pct": pressure_pct,
        "pressure_label": pressure_label,
        "trend_state": trend_state,
    }


def format_risk_plan_text(decision: RiskDecision, holding: bool = False) -> str:
    """把结构化风控结果压成一行，便于表格和手机消息阅读。

    参数说明：
    - decision：风险引擎输出
    - holding：是否属于已有持仓，影响文案措辞
    """

    prefix = "持仓风控" if holding else "交易建议"
    return (
        f"{prefix}｜{decision.mode}｜入场{decision.entry_price:.2f}｜"
        f"止损{decision.stop_loss:.2f}｜止盈{decision.take_profit:.2f}｜"
        f"仓位{decision.position_pct:.1f}%｜R/R {decision.risk_reward:.2f}"
    )


def format_buy_state_text(decision: RiskDecision, has_holding: bool, row: pd.Series) -> tuple[str, str]:
    """生成更适合盘中推送的买点状态和说明。"""

    holding_text = safe_text(row.get("holding_brief", "未持仓"))
    if has_holding:
        return "持仓风控", f"{holding_text}；{decision.reason}"
    if decision.allowed and decision.entry_gap_pct is not None and decision.entry_gap_pct > 0:
        if decision.entry_gap_pct <= 1.2:
            return "临近买点", decision.reason
        return "等待确认", f"{decision.reason}；距离买点{decision.entry_gap_pct:.2f}%"
    if decision.allowed:
        return "已到买点", decision.reason
    return "不建议介入", decision.reason


def build_risk_bundle(row: pd.Series, market_info: dict[str, Any], market_news_state: str) -> pd.Series:
    """把风控引擎输出整理成一组可直接写回 DataFrame 的字段。"""

    holding_row = row if bool(row.get("has_holding")) else None
    decision = build_risk_decision(
        row,
        market_info=market_info,
        profile=None,
        market_news_state=market_news_state,
        holding=holding_row,
    )
    buy_state, buy_reason = format_buy_state_text(decision, bool(row.get("has_holding")), row)
    return pd.Series(
        {
            "mode": decision.mode,
            "entry_price": decision.entry_price,
            "stop_loss": decision.stop_loss,
            "take_profit": decision.take_profit,
            "risk_per_share": decision.risk_per_share,
            "risk_reward": decision.risk_reward,
            "position_pct": decision.position_pct,
            "risk_reason": decision.reason,
            "risk_confidence": decision.confidence,
            "take_profit_2": decision.take_profit_2,
            "stop_loss_pct": decision.stop_loss_pct,
            "entry_gap_pct": decision.entry_gap_pct,
            "risk_plan": format_risk_plan_text(decision, bool(row.get("has_holding"))),
            "buy_state": buy_state,
            "buy_reason": buy_reason,
        }
    )


def build_signal_lifecycle_bundle(
    row: pd.Series,
    cache: DiskCache,
    current_day: date | None = None,
) -> pd.Series:
    """Persist and classify how long a buy signal has stayed unresolved.

    This keeps a buy idea from living forever just because it once looked good.
    """

    current_day = current_day or datetime.now().date()
    code = safe_text(row.get("code"))
    mode = safe_text(row.get("mode")) or "mid"
    cache_key = f"signal_lifecycle:{code}:{mode}"
    cached = cache.get(cache_key, ttl_sec=365 * 24 * 3600) or {}

    first_seen = safe_text(cached.get("first_seen"))
    if not first_seen:
        first_seen = safe_text(row.get("signal_first_seen")) or safe_text(row.get("entry_time")) or current_day.isoformat()

    lifecycle = build_signal_lifecycle(row, mode=mode, current_day=current_day, first_seen=first_seen)

    payload = {
        "first_seen": lifecycle.first_seen,
        "last_seen": current_day.isoformat(),
        "mode": mode,
        "code": code,
        "state": lifecycle.state,
        "action": lifecycle.action,
        "age_days": lifecycle.age_days,
        "note": lifecycle.note,
    }
    cache.set(cache_key, payload)

    signal_state = lifecycle.state
    signal_action = lifecycle.action
    signal_note = lifecycle.note
    buy_state = safe_text(row.get("buy_state"))
    buy_reason = safe_text(row.get("buy_reason"))

    if signal_state == "time_stop":
        buy_state = "时间止损"
        buy_reason = signal_note
    elif signal_state == "stale" and not bool(row.get("has_holding")):
        buy_state = "重新评估"
        buy_reason = signal_note
    elif signal_state == "watch" and not buy_state:
        buy_state = "继续观察"
        buy_reason = signal_note
    elif signal_state == "target_hit" and bool(row.get("has_holding")):
        buy_state = "止盈提醒"
        buy_reason = signal_note
    elif signal_state == "stop_hit" and bool(row.get("has_holding")):
        buy_state = "止损提醒"
        buy_reason = signal_note

    return pd.Series(
        {
            "signal_first_seen": lifecycle.first_seen,
            "signal_age_days": lifecycle.age_days,
            "signal_state": signal_state,
            "signal_action": signal_action,
            "signal_note": signal_note,
            "signal_stale_days": lifecycle.stale_days,
            "signal_stop_days": lifecycle.stop_days,
            "buy_state": buy_state,
            "buy_reason": buy_reason or signal_note,
        }
    )


def run_paper_trading(
    cfg: Config,
    result: pd.DataFrame,
    notifier: WeComNotifier | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now()
    account = load_account(app_config.PAPER_TRADE_FILE, cfg.paper_trade_cash)
    if not is_a_share_trading_time(now):
        md = build_paper_trade_markdown(account, [])
        md += f"\n> 非A股交易时间，本地模拟盘本轮不执行买卖。当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"
        print(md.replace("\n", " | "), flush=True)
        if notifier and notifier.enabled and cfg.mode == "after":
            digest = hashlib.sha256(md.encode("utf-8")).hexdigest()
            notifier.send_markdown(notification_title(cfg.mode, "本地模拟盘账户"), md, dedupe_key=f"paper:{cfg.mode}:{digest}")
        return md

    events = apply_paper_trades(
        account,
        result,
        trade_date=now.strftime("%Y-%m-%d"),
        min_score=cfg.notify_min_score,
        commission_rate=cfg.paper_trade_commission_rate,
        stamp_tax_rate=cfg.paper_trade_stamp_tax_rate,
        slippage_pct=cfg.paper_trade_slippage_pct,
        cooldown_days=cfg.paper_trade_cooldown_days,
        max_positions=cfg.paper_trade_max_positions,
        max_position_pct=cfg.paper_trade_max_position_pct,
        max_total_position_pct=cfg.paper_trade_max_total_position_pct,
    )
    save_account(app_config.PAPER_TRADE_FILE, account)
    md = build_paper_trade_markdown(account, events)
    print(md.replace("\n", " | "), flush=True)
    if notifier and notifier.enabled and (events or cfg.mode == "after"):
        digest = hashlib.sha256(md.encode("utf-8")).hexdigest()
        notifier.send_markdown(notification_title(cfg.mode, "本地模拟盘账户"), md, dedupe_key=f"paper:{cfg.mode}:{digest}")
    return md


def build_joinquant_dry_run_markdown(payload: dict[str, Any]) -> str:
    signals = payload.get("signals", [])
    buy_count = sum(1 for item in signals if item.get("action") == "buy")
    sell_count = sum(1 for item in signals if item.get("action") == "sell")
    dry_run = payload.get("dry_run", True)
    title = "JoinQuant Dry-Run" if dry_run else "JoinQuant 模拟盘"
    status = "dry-run，未真实下单" if dry_run else "已交给 JoinQuant 模拟盘执行"
    lines = [
        f"#### 【{title}】下单计划",
        f"> 状态：{status}。来源：JoinQuant 执行器计划，不是本地模拟盘。",
        f"> 交易日：{payload.get('trade_date', '-')} | run_id：{payload.get('run_id', '-')}",
        f"> 计划买入 {buy_count} 条 | 计划卖出 {sell_count} 条 | 合计 {len(signals)} 条",
    ]
    if not signals:
        lines.append("> 本轮没有可发送给 JoinQuant 的买入/卖出计划。")
        diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
        reasons = diagnostics.get("reject_reasons") if isinstance(diagnostics.get("reject_reasons"), dict) else {}
        if diagnostics:
            lines.append(f"> 候选 {diagnostics.get('candidate_count', 0)} 只 | 买入开关：{'开' if diagnostics.get('allow_buy') else '关'} | 最低分 {diagnostics.get('min_score', '-')}")
        if reasons:
            labels = {
                "buy_disabled": "非交易时间禁止买入",
                "buy_invalid_price": "价格/入场价无效",
                "buy_invalid_take_profit": "止盈无有效空间",
                "not_buy_sell_signal": "卖出类信号不买入",
                "buy_low_score": "分数不足",
                "buy_bad_position": "仓位无效",
                "buy_near_limit_up": "接近涨停",
                "buy_not_reached_entry": "未到入场价",
                "sell_without_holding": "未持仓不卖出",
            }
            summary = "；".join(
                f"{labels.get(str(key), str(key))} {value}"
                for key, value in sorted(reasons.items(), key=lambda item: item[1], reverse=True)[:5]
            )
            lines.append(f"> 过滤原因：{summary}")
        return "\n".join(lines)

    for item in signals[:8]:
        action = "计划买入" if item.get("action") == "buy" else "计划卖出"
        code = item.get("jq_code") or item.get("code", "")
        name = item.get("name", "")
        price = item.get("price", "-")
        reason = compact_text(item.get("reason", ""), 48)
        if item.get("action") == "buy":
            lines.append(
                f"- {action} {code} {name} | 目标仓位 {item.get('position_pct', 0)}% | "
                f"价格 {price} | 分数 {item.get('final_score', '-')}"
            )
        else:
            lines.append(f"- {action} {code} {name} | 价格 {price}")
        if reason:
            lines.append(f"  > 原因：{reason}")
    if len(signals) > 8:
        lines.append(f"> 还有 {len(signals) - 8} 条未展开，详见 cache/joinquant/signals.json。")
    return "\n".join(lines)


def run_joinquant_export(cfg: Config, result: pd.DataFrame, notifier: WeComNotifier | None = None) -> Path:
    from joinquant_exporter import export_signals

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = export_signals(
        result,
        run_id=run_id,
        trade_date=datetime.now().strftime("%Y-%m-%d"),
        dry_run=cfg.joinquant_dry_run,
        min_score=cfg.joinquant_min_score,
        allow_buy=is_a_share_trading_time(),
    )
    print(f"JoinQuant signals exported: {path}", flush=True)
    if notifier and notifier.enabled and (cfg.notify_non_trading_day or is_a_share_trading_day()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        md = build_joinquant_dry_run_markdown(payload)
        digest = hashlib.sha256(md.encode("utf-8")).hexdigest()
        title = "JoinQuant Dry-Run" if payload.get("dry_run", True) else "JoinQuant 模拟盘"
        notifier.send_markdown(notification_title(cfg.mode, title), md, dedupe_key=f"joinquant:{cfg.mode}:{digest}")
    elif notifier and notifier.enabled:
        print("非A股交易日，已跳过 JoinQuant 计划微信推送。", flush=True)
    return path


def run_once(cfg: Config, cache: DiskCache, notifier: WeComNotifier | None = None) -> pd.DataFrame | None:
    print("获取全市场实时行情...", flush=True)
    spot = fetch_spot_data()
    print("读取大盘状态...", flush=True)
    market_info = market_sentiment(spot)
    print("加载行业缓存...", flush=True)
    industry = IndustryMapper(refresh=cfg.refresh_industry)
    seeded = industry.seed_from_frame(spot)
    if seeded:
        print(f"已用行情自带行业字段补全 {seeded} 条映射。", flush=True)
    refreshed = industry.refresh_stale_pending()
    if refreshed:
        print(f"已对一周以上未识别题材的票补充映射 {refreshed} 条。", flush=True)
    print("构建候选池...", flush=True)
    pool = build_pool(spot, cfg)
    if pool.empty:
        print("没有筛到符合条件的股票，可以降低 --min-amount 或切换 --mode。", flush=True)
        return None

    watch_pool = pool
    if cfg.mode == "intraday":
        watch_limit = max(cfg.top * cfg.intraday_watch_multiplier, cfg.top + 12)
        watch_pool = build_pool(spot, cfg, limit=watch_limit)

    print("抓取市场新闻...", flush=True)
    market_news = pd.DataFrame()
    if not cfg.skip_news:
        market_news = fetch_market_news(cache, limit=50)

    print("补充个股维度...", flush=True)
    rows = []
    market_state = market_info["state"]
    market_news_state = analyze_market_news(market_news)
    portfolio_positions = load_portfolio_positions()
    client, model = get_ai_client() if cfg.ai else (None, None)

    for idx, (_, row) in enumerate(pool.iterrows()):
        row = row.copy()
        row["industry"] = industry.get(row["code"])
        row["limit_quality"] = limit_quality(row)
        row["market_state"] = market_state
        tech_snapshot = build_technical_snapshot(row["code"], cache=cache, price_hint=float(row.get("price", 0) or 0))
        row["ma5"] = tech_snapshot["ma5"]
        row["ma10"] = tech_snapshot["ma10"]
        row["ma20"] = tech_snapshot["ma20"]
        row["ma30"] = tech_snapshot["ma30"]
        row["atr14"] = tech_snapshot["atr14"]
        row["support_level"] = tech_snapshot["support_level"]
        row["pressure_level"] = tech_snapshot["pressure_level"]
        row["pressure_pct"] = tech_snapshot["pressure_pct"]
        row["pressure_label"] = tech_snapshot["pressure_label"]
        row["trend_state"] = tech_snapshot["trend_state"]

        if cfg.skip_pressure:
            row["pressure_pct"], row["pressure_label"] = None, "已跳过"
        else:
            print(f"计算压力位：{row['code']} {row['name']}", flush=True)
            row["pressure_pct"], row["pressure_label"] = pressure_20d(row["code"], cache=cache)

        if cfg.skip_lhb or cfg.mode != "after":
            row["lhb_tag"], row["lhb_seats"] = ("已跳过" if cfg.skip_lhb else "未检查"), ""
        else:
            print(f"检查龙虎榜：{row['code']} {row['name']}", flush=True)
            row["lhb_tag"], row["lhb_seats"] = lhb_status(row["code"], cache=cache)

        row["news_score"] = 0
        row["news_tags"] = "无明显催化"
        row["news_hits"] = ""
        if (not cfg.skip_news) and idx < cfg.max_candidates_for_news:
            try:
                print(f"抓取新闻：{row['code']} {row['name']}", flush=True)
                stock_news = fetch_stock_news(
                    row["code"],
                    cache=cache,
                    notice_days_back=cfg.notice_days_back,
                    news_limit=cfg.stock_news_limit,
                )
                news_score, news_tags, news_hits = score_news_for_row(
                    row,
                    row["industry"],
                    market_news,
                    stock_news,
                )
                row["news_score"] = news_score
                row["news_tags"] = news_tags
                row["news_hits"] = " | ".join(news_hits[:8])
            except Exception as exc:
                row["news_tags"] = f"新闻失败: {exc}"
        elif not cfg.skip_news:
            row["news_tags"] = "已限制到前几只候选股"

        if client and model and idx < cfg.max_candidates_for_news:
            row["ai"] = ai_comment(client, model, row, market_state, market_news_state)
        else:
            row["ai"] = ""
        rows.append(row)
        time.sleep(0.08)

    result = pd.DataFrame(rows)
    if result.empty:
        print("结果为空。", flush=True)
        return None

    result["amount_rank_pct"] = result["amount"].rank(pct=True).fillna(0)
    result["final_score"] = result["score"].fillna(0)
    result["final_score"] += result["news_score"].fillna(0) * 1.2
    result["final_score"] += result["pct_chg"].rank(pct=True).fillna(0) * 5
    if "turnover" in result.columns:
        result["final_score"] += result["turnover"].rank(pct=True).fillna(0) * 2

    result["entry_reason"] = result.apply(lambda row: build_entry_reason(row, market_info), axis=1)
    result["news_brief"] = result.apply(build_news_brief, axis=1)
    result["next_day_opportunity"] = result.apply(lambda row: build_next_day_opportunity(row, market_info), axis=1)
    result["risk_plan"] = result.apply(lambda row: build_risk_plan(row, market_info), axis=1)
    result["theme_label"] = result.apply(infer_theme_label, axis=1)
    theme_frame = result.apply(lambda row: build_theme_heat_bundle(row, market_news_state), axis=1, result_type="expand")
    for col in theme_frame.columns:
        result[col] = theme_frame[col]
    history_frame = result.apply(
        lambda row: build_history_playbook(row, market_info, cache),
        axis=1,
        result_type="expand",
    )
    result["history_replay"] = history_frame[0]
    result["exit_plan"] = history_frame[1]
    trade_frame = result.apply(
        lambda row: build_weighted_trade_plan(row, market_info, market_news_state),
        axis=1,
        result_type="expand",
    )
    result["trade_score"] = trade_frame[0]
    result["trade_bias"] = trade_frame[1]
    result["trade_playbook"] = trade_frame[2]
    result = enrich_portfolio_frame(result, portfolio_positions)
    risk_frame = result.apply(lambda row: build_risk_bundle(row, market_info, market_news_state), axis=1, result_type="expand")
    for col in risk_frame.columns:
        result[col] = risk_frame[col]
    lifecycle_frame = result.apply(lambda row: build_signal_lifecycle_bundle(row, cache), axis=1, result_type="expand")
    for col in lifecycle_frame.columns:
        result[col] = lifecycle_frame[col]
    anchor_frame = result.apply(lambda row: build_signal_anchor_bundle(row, cache), axis=1, result_type="expand")
    for col in anchor_frame.columns:
        result[col] = anchor_frame[col]
    pending_theme_mask = result["theme_label"].isin(["未识别题材", "题材待确认"])
    industry.note_pending(result[pending_theme_mask][["code", "name", "theme_label"]].copy())

    result = result.sort_values("final_score", ascending=False).reset_index(drop=True)

    watch_result = pd.DataFrame()
    if cfg.mode == "intraday" and not watch_pool.empty:
        print("构建盘中买点观察池...", flush=True)
        watch_rows = []
        for idx, (_, row) in enumerate(watch_pool.iterrows()):
            row = row.copy()
            row["industry"] = industry.get(row["code"])
            row["limit_quality"] = limit_quality(row)
            row["market_state"] = market_state
            tech_snapshot = build_technical_snapshot(row["code"], cache=cache, price_hint=float(row.get("price", 0) or 0))
            row["ma5"] = tech_snapshot["ma5"]
            row["ma10"] = tech_snapshot["ma10"]
            row["ma20"] = tech_snapshot["ma20"]
            row["ma30"] = tech_snapshot["ma30"]
            row["atr14"] = tech_snapshot["atr14"]
            row["support_level"] = tech_snapshot["support_level"]
            row["pressure_level"] = tech_snapshot["pressure_level"]
            row["pressure_pct"] = tech_snapshot["pressure_pct"]
            row["pressure_label"] = tech_snapshot["pressure_label"]
            row["trend_state"] = tech_snapshot["trend_state"]
            if cfg.skip_pressure:
                row["pressure_pct"], row["pressure_label"] = None, "已跳过"
            else:
                row["pressure_pct"], row["pressure_label"] = pressure_20d(row["code"], cache=cache)
            row["lhb_tag"], row["lhb_seats"] = ("未检查", "")
            row["news_score"] = 0
            row["news_tags"] = "未展开深度新闻"
            row["news_hits"] = ""
            row["ai"] = ""
            watch_rows.append(row)
            time.sleep(0.04)

        watch_result = pd.DataFrame(watch_rows)
        if not watch_result.empty:
            watch_result["amount_rank_pct"] = watch_result["amount"].rank(pct=True).fillna(0)
            watch_result["final_score"] = watch_result["score"].fillna(0)
            watch_result["final_score"] += watch_result["pct_chg"].rank(pct=True).fillna(0) * 5
            if "turnover" in watch_result.columns:
                watch_result["final_score"] += watch_result["turnover"].rank(pct=True).fillna(0) * 2
            watch_result["entry_reason"] = watch_result.apply(lambda row: build_entry_reason(row, market_info), axis=1)
            watch_result["news_brief"] = watch_result.apply(build_news_brief, axis=1)
            watch_result["next_day_opportunity"] = watch_result.apply(lambda row: build_next_day_opportunity(row, market_info), axis=1)
            watch_result["risk_plan"] = watch_result.apply(lambda row: build_risk_plan(row, market_info), axis=1)
            watch_result["theme_label"] = watch_result.apply(infer_theme_label, axis=1)
            watch_theme_frame = watch_result.apply(lambda row: build_theme_heat_bundle(row, market_news_state), axis=1, result_type="expand")
            for col in watch_theme_frame.columns:
                watch_result[col] = watch_theme_frame[col]
            watch_history = watch_result.apply(
                lambda row: build_history_playbook(row, market_info, cache),
                axis=1,
                result_type="expand",
            )
            watch_result["history_replay"] = watch_history[0]
            watch_result["exit_plan"] = watch_history[1]
            watch_trade = watch_result.apply(
                lambda row: build_weighted_trade_plan(row, market_info, market_news_state),
                axis=1,
                result_type="expand",
            )
            watch_result["trade_score"] = watch_trade[0]
            watch_result["trade_bias"] = watch_trade[1]
            watch_result["trade_playbook"] = watch_trade[2]
            watch_result = enrich_portfolio_frame(watch_result, portfolio_positions)
            watch_risk_frame = watch_result.apply(lambda row: build_risk_bundle(row, market_info, market_news_state), axis=1, result_type="expand")
            for col in watch_risk_frame.columns:
                watch_result[col] = watch_risk_frame[col]
            watch_lifecycle_frame = watch_result.apply(lambda row: build_signal_lifecycle_bundle(row, cache), axis=1, result_type="expand")
            for col in watch_lifecycle_frame.columns:
                watch_result[col] = watch_lifecycle_frame[col]
            watch_anchor_frame = watch_result.apply(lambda row: build_signal_anchor_bundle(row, cache), axis=1, result_type="expand")
            for col in watch_anchor_frame.columns:
                watch_result[col] = watch_anchor_frame[col]
            watch_result = watch_result.sort_values("final_score", ascending=False).reset_index(drop=True)
            watch_pending_theme_mask = watch_result["theme_label"].isin(["未识别题材", "题材待确认"])
            industry.note_pending(watch_result[watch_pending_theme_mask][["code", "name", "theme_label"]].copy())

    ai_overview = build_ai_overview_summary(client, model, result, market_info, market_news_state, cfg) if cfg.ai else ""
    print_console_report(result, market_info, market_news_state)
    save_outputs(result, market_info, market_news_state, ai_overview=ai_overview)
    if cfg.joinquant:
        export_source = watch_result if cfg.mode == "intraday" and not watch_result.empty else result
        run_joinquant_export(cfg, export_source, notifier if cfg.notify else None)
    if cfg.paper_trade:
        paper_source = watch_result if cfg.mode == "intraday" and not watch_result.empty else result
        run_paper_trading(cfg, paper_source, notifier if cfg.notify else None)

    if cfg.notify and notifier:
        dispatch_notifications(
            cfg,
            notifier,
            result,
            market_info,
            market_news_state,
            watch_result=watch_result,
            ai_overview=ai_overview,
        )

    return result


def build_notification_digest(result: pd.DataFrame, market_info: dict[str, Any], market_news_state: str, cfg: Config) -> str:
    top_rows = result.head(cfg.notify_top)
    digest_source = [
        market_info.get("state", ""),
        market_news_state,
        str(market_info.get("sh_pct", "")),
    ]
    for _, row in top_rows.iterrows():
        digest_source.append(
            f"{row['code']}|{row.get('final_score', 0):.2f}|{row.get('news_score', 0):.2f}|{row.get('lhb_tag', '')}|"
            f"{safe_text(row.get('holding_brief', ''))}|{safe_text(row.get('trade_bias', ''))}|{safe_text(row.get('history_replay', ''))}|"
            f"{safe_text(row.get('next_day_opportunity', ''))}|{safe_text(row.get('trade_playbook', ''))}|"
            f"{safe_text(row.get('mode', ''))}|{safe_text(row.get('entry_price', ''))}|{safe_text(row.get('stop_loss', ''))}|"
            f"{safe_text(row.get('take_profit', ''))}|{safe_text(row.get('position_pct', ''))}|{safe_text(row.get('risk_reason', ''))}|"
            f"{safe_text(row.get('signal_state', ''))}|{safe_text(row.get('signal_age_days', ''))}|{safe_text(row.get('signal_action', ''))}|"
            f"{safe_text(row.get('theme_label', ''))}|{safe_text(row.get('theme_heat_level', ''))}"
        )
    return hashlib.sha256("::".join(digest_source).encode("utf-8")).hexdigest()


def dispatch_notifications(
    cfg: Config,
    notifier: WeComNotifier,
    result: pd.DataFrame,
    market_info: dict[str, Any],
    market_news_state: str,
    watch_result: pd.DataFrame | None = None,
    ai_overview: str = "",
) -> None:
    """通知和策略彻底解耦：通知失败不影响主流程。"""
    if not notifier.enabled:
        print("通知未启用或缺少企业微信 Webhook。", flush=True)
        return
    if not cfg.notify_non_trading_day and not is_a_share_trading_day():
        print("非A股交易日，已跳过微信推送。设置 NOTIFY_NON_TRADING_DAY=1 可用于联调。", flush=True)
        return

    digest = build_notification_digest(result, market_info, market_news_state, cfg)
    summary_key = f"summary:{cfg.mode}:{digest}"
    summary_md = build_summary_markdown(result, market_info, market_news_state, cfg, ai_overview=ai_overview)

    if cfg.notify_only_signal:
        if cfg.mode == "intraday" and watch_result is not None and not watch_result.empty:
            buy_rows = select_intraday_buy_rows(watch_result, cfg)
            if buy_rows.empty:
                print("本轮没有触发买点提醒。", flush=True)
                return
            row = buy_rows.head(1).iloc[0]
            signal_id = safe_text(row.get("signal_anchor_id")) or safe_text(row.get("signal_first_seen")) or safe_text(row.get("buy_state"))
            key = f"intraday:{datetime.now().strftime('%Y%m%d')}:{row['code']}:{signal_id}:{safe_text(row.get('buy_state'))}"
            md = build_alert_markdown(row, "买点", market_info, market_news_state, cfg.mode)
            if notifier.send_markdown(notification_title(cfg.mode, f"买点提醒 {row['code']} {row['name']}"), md, dedupe_key=key):
                record_signal_watchlist(SIGNAL_WATCHLIST_FILE, row, "买点", cfg.mode)
            return

        strong_rows, risk_rows = select_signal_rows(result, cfg)
        chosen_row = None
        chosen_kind = ""
        if not risk_rows.empty:
            chosen_row = risk_rows.sort_values(["news_score", "final_score"]).iloc[0]
            chosen_kind = "风险"
        elif not strong_rows.empty:
            chosen_row = strong_rows.sort_values("final_score", ascending=False).iloc[0]
            chosen_kind = "强势"

        if chosen_row is None:
            print("本轮没有触发需要推送的信号。", flush=True)
            return

        signal_id = safe_text(chosen_row.get("signal_anchor_id")) or safe_text(chosen_row.get("signal_first_seen")) or safe_text(chosen_row.get("buy_state"))
        key = f"signal:{chosen_kind}:{chosen_row['code']}:{signal_id}:{safe_text(chosen_row.get('signal_state'))}:{safe_text(chosen_row.get('signal_action'))}"
        md = build_alert_markdown(chosen_row, chosen_kind, market_info, market_news_state, cfg.mode)
        if notifier.send_markdown(notification_title(cfg.mode, f"{chosen_kind}提醒 {chosen_row['code']} {chosen_row['name']}"), md, dedupe_key=key):
            record_signal_watchlist(SIGNAL_WATCHLIST_FILE, chosen_row, chosen_kind, cfg.mode)
        return

    # 默认模式：发一条汇总，盘后模式再补一条更完整的复盘
    title = "盘后复盘" if cfg.mode == "after" else "扫描汇总"
    notifier.send_markdown(notification_title(cfg.mode, title), summary_md, dedupe_key=summary_key)

    if cfg.mode == "after":
        review_md = build_watchlist_review_markdown(result, SIGNAL_WATCHLIST_FILE, max_rows=min(max(cfg.notify_top, 1), 6))
        if review_md:
            review_key = hashlib.sha256(review_md.encode("utf-8")).hexdigest()
            notifier.send_markdown(notification_title(cfg.mode, "推送跟踪复盘"), review_md, dedupe_key=f"watch-review:{review_key}")

    # 盘中或盘后都只再补一条重点卡片，避免消息过杂
    highlight_row = None
    highlight_kind = ""
    if cfg.mode == "intraday" and watch_result is not None and not watch_result.empty:
        buy_rows = select_intraday_buy_rows(watch_result, cfg)
        if not buy_rows.empty:
            highlight_row = buy_rows.sort_values("final_score", ascending=False).iloc[0]
            highlight_kind = "买点"
    else:
        strong_rows, risk_rows = select_signal_rows(result, cfg)
        if not risk_rows.empty:
            highlight_row = risk_rows.sort_values(["news_score", "final_score"]).iloc[0]
            highlight_kind = "风险"
        elif not strong_rows.empty:
            highlight_row = strong_rows.sort_values("final_score", ascending=False).iloc[0]
            highlight_kind = "强势"

    if highlight_row is not None:
        signal_id = safe_text(highlight_row.get("signal_anchor_id")) or safe_text(highlight_row.get("signal_first_seen")) or safe_text(highlight_row.get("buy_state"))
        key = f"highlight:{cfg.mode}:{highlight_kind}:{highlight_row['code']}:{signal_id}:{safe_text(highlight_row.get('signal_state'))}:{safe_text(highlight_row.get('signal_action'))}"
        md = build_alert_markdown(highlight_row, highlight_kind, market_info, market_news_state, cfg.mode)
        if notifier.send_markdown(notification_title(cfg.mode, f"{highlight_kind}提醒 {highlight_row['code']} {highlight_row['name']}"), md, dedupe_key=key):
            record_signal_watchlist(SIGNAL_WATCHLIST_FILE, highlight_row, highlight_kind, cfg.mode)


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="A股实战扫描与新闻分析")
    parser.add_argument("--mode", choices=["pre", "intraday", "after", "auto"], default=app_config.SCAN_MODE_DEFAULT)
    parser.add_argument("--top", type=int, default=app_config.SCAN_TOP_DEFAULT)
    parser.add_argument("--ai", action=argparse.BooleanOptionalAction, default=app_config.ENABLE_AI_DEFAULT)
    parser.add_argument("--watch", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--interval", type=int, default=app_config.SCAN_INTERVAL_DEFAULT)
    parser.add_argument("--jitter", type=int, default=app_config.SCAN_JITTER_DEFAULT)
    parser.add_argument("--market-only", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--refresh-industry", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--min-price", type=float, default=app_config.MIN_PRICE_DEFAULT)
    parser.add_argument("--min-amount", type=float, default=app_config.MIN_AMOUNT_DEFAULT)
    parser.add_argument("--skip-pressure", action=argparse.BooleanOptionalAction, default=app_config.SKIP_PRESSURE_DEFAULT)
    parser.add_argument("--skip-lhb", action=argparse.BooleanOptionalAction, default=app_config.SKIP_LHB_DEFAULT)
    parser.add_argument("--skip-news", action=argparse.BooleanOptionalAction, default=app_config.SKIP_NEWS_DEFAULT)
    parser.add_argument("--stock-news-limit", type=int, default=app_config.STOCK_NEWS_LIMIT_DEFAULT)
    parser.add_argument("--notice-days-back", type=int, default=app_config.NOTICE_DAYS_BACK_DEFAULT)
    parser.add_argument("--max-candidates-for-news", type=int, default=app_config.MAX_CANDIDATES_FOR_NEWS_DEFAULT)
    parser.add_argument("--notify", action=argparse.BooleanOptionalAction, default=app_config.NOTIFY_ENABLE_DEFAULT)
    parser.add_argument("--notify-only-signal", action=argparse.BooleanOptionalAction, default=app_config.NOTIFY_ONLY_SIGNAL_DEFAULT)
    parser.add_argument("--notify-top", type=int, default=app_config.NOTIFY_TOP_N_DEFAULT)
    parser.add_argument("--notify-cooldown", type=int, default=app_config.NOTIFY_COOLDOWN_SEC_DEFAULT)
    parser.add_argument("--notify-min-score", type=float, default=app_config.NOTIFY_MIN_SCORE_DEFAULT)
    parser.add_argument("--notify-non-trading-day", action=argparse.BooleanOptionalAction, default=app_config.NOTIFY_NON_TRADING_DAY_DEFAULT)
    parser.add_argument("--notify-webhook", default=app_config.WECOM_WEBHOOK_URL)
    parser.add_argument("--intraday-watch-multiplier", type=int, default=app_config.INTRADAY_WATCH_MULTIPLIER_DEFAULT)
    parser.add_argument("--intraday-near-pressure-pct", type=float, default=app_config.INTRADAY_NEAR_PRESSURE_PCT_DEFAULT)
    parser.add_argument("--intraday-trigger-pressure-pct", type=float, default=app_config.INTRADAY_TRIGGER_PRESSURE_PCT_DEFAULT)
    parser.add_argument("--intraday-max-alerts", type=int, default=app_config.INTRADAY_MAX_ALERTS_DEFAULT)
    parser.add_argument("--paper-trade", action=argparse.BooleanOptionalAction, default=app_config.PAPER_TRADE_ENABLE_DEFAULT)
    parser.add_argument("--paper-trade-cash", type=float, default=app_config.PAPER_TRADE_CASH_DEFAULT)
    parser.add_argument("--paper-trade-commission-rate", type=float, default=app_config.PAPER_TRADE_COMMISSION_RATE_DEFAULT)
    parser.add_argument("--paper-trade-stamp-tax-rate", type=float, default=app_config.PAPER_TRADE_STAMP_TAX_RATE_DEFAULT)
    parser.add_argument("--paper-trade-slippage-pct", type=float, default=app_config.PAPER_TRADE_SLIPPAGE_PCT_DEFAULT)
    parser.add_argument("--paper-trade-cooldown-days", type=int, default=app_config.PAPER_TRADE_COOLDOWN_DAYS_DEFAULT)
    parser.add_argument("--paper-trade-max-positions", type=int, default=app_config.PAPER_TRADE_MAX_POSITIONS_DEFAULT)
    parser.add_argument("--paper-trade-max-position-pct", type=float, default=app_config.PAPER_TRADE_MAX_POSITION_PCT_DEFAULT)
    parser.add_argument("--paper-trade-max-total-position-pct", type=float, default=app_config.PAPER_TRADE_MAX_TOTAL_POSITION_PCT_DEFAULT)
    parser.add_argument("--joinquant", action=argparse.BooleanOptionalAction, default=app_config.JOINQUANT_ENABLE_DEFAULT)
    parser.add_argument("--joinquant-dry-run", action=argparse.BooleanOptionalAction, default=app_config.JOINQUANT_DRY_RUN_DEFAULT)
    parser.add_argument("--joinquant-min-score", type=float, default=app_config.JOINQUANT_MIN_SCORE_DEFAULT)
    args = parser.parse_args()

    return Config(
        mode=args.mode,
        top=args.top,
        ai=args.ai,
        watch=args.watch,
        interval=args.interval,
        jitter=args.jitter,
        market_only=args.market_only,
        refresh_industry=args.refresh_industry,
        min_price=args.min_price,
        min_amount=args.min_amount,
        skip_pressure=args.skip_pressure,
        skip_lhb=args.skip_lhb,
        skip_news=args.skip_news,
        stock_news_limit=args.stock_news_limit,
        notice_days_back=args.notice_days_back,
        max_candidates_for_news=args.max_candidates_for_news,
        notify=args.notify,
        notify_only_signal=args.notify_only_signal,
        notify_top=args.notify_top,
        notify_cooldown=args.notify_cooldown,
        notify_min_score=args.notify_min_score,
        notify_non_trading_day=args.notify_non_trading_day,
        notify_webhook=args.notify_webhook or None,
        intraday_watch_multiplier=args.intraday_watch_multiplier,
        intraday_near_pressure_pct=args.intraday_near_pressure_pct,
        intraday_trigger_pressure_pct=args.intraday_trigger_pressure_pct,
        intraday_max_alerts=args.intraday_max_alerts,
        paper_trade=args.paper_trade,
        paper_trade_cash=args.paper_trade_cash,
        paper_trade_commission_rate=args.paper_trade_commission_rate,
        paper_trade_stamp_tax_rate=args.paper_trade_stamp_tax_rate,
        paper_trade_slippage_pct=args.paper_trade_slippage_pct,
        paper_trade_cooldown_days=args.paper_trade_cooldown_days,
        paper_trade_max_positions=args.paper_trade_max_positions,
        paper_trade_max_position_pct=args.paper_trade_max_position_pct,
        paper_trade_max_total_position_pct=args.paper_trade_max_total_position_pct,
        joinquant=args.joinquant,
        joinquant_dry_run=args.joinquant_dry_run,
        joinquant_min_score=args.joinquant_min_score,
    )


def _fmt_num(value: Any, digits: int = 2, default: str = "-") -> str:
    """把可能为空的数字格式化成稳定文本。"""

    try:
        num = float(value)
        if pd.isna(num):
            return default
        return f"{num:.{digits}f}"
    except Exception:
        return default


def build_summary_markdown(
    result: pd.DataFrame,
    market_info: dict[str, Any],
    market_news_state: str,
    cfg: Config,
    ai_overview: str = "",
) -> str:
    """生成更适合手机阅读的总览消息。

    参数说明：
    - result：扫描结果
    - market_info：大盘信息
    - market_news_state：新闻情绪摘要
    - cfg：运行配置
    - ai_overview：可选 AI 汇总
    """

    lines: list[str] = []
    lines.append(f"### {market_info.get('state', '未知')}")
    lines.append(f"> 新闻：{compact_text(market_news_state or '无明显催化', 60)}")
    lines.append("")

    top_rows = result.head(cfg.notify_top)
    if top_rows.empty:
        lines.append("暂无候选。")
    else:
        lines.append("#### 候选摘要")
        for idx, (_, row) in enumerate(top_rows.iterrows(), start=1):
            holding_flag = "已持仓" if row.get("has_holding") else "未持仓"
            lines.append(
                f"{idx}. {row.get('code', '')} {row.get('name', '')} | "
                f"{row.get('mode', 'mid')} | 入场 {_fmt_num(row.get('entry_price'))} | "
                f"止损 {_fmt_num(row.get('stop_loss'))} | 止盈 {_fmt_num(row.get('take_profit'))} | "
                f"仓位 {_fmt_num(row.get('position_pct'), 1)}% | {holding_flag}"
            )
            lines.append(f"> 理由：{compact_text(row.get('risk_reason') or row.get('buy_reason') or row.get('entry_reason') or '', 72)}")
            lines.append(
                f"> 题材：{compact_text(row.get('theme_label', '题材待确认'), 18)} | "
                f"热度：{compact_text(row.get('theme_heat_level', '待确认'), 8)}"
            )
            if safe_text(row.get("theme_heat_reason")):
                lines.append(f"> 热度依据：{compact_text(row.get('theme_heat_reason'), 48)}")
            lines.append(
                f"> 信号：{compact_text(row.get('signal_state', 'fresh'), 12)} / "
                f"{compact_text(row.get('signal_action', 'continue'), 12)} | "
                f"{compact_text(row.get('signal_note', ''), 42)}"
            )
    if ai_overview:
        lines.append("")
        lines.append("#### AI 汇总")
        lines.append(compact_text(ai_overview, 400))
    return "\n".join(lines)


def _entry_status_text(row: pd.Series) -> str:
    price = _float_value(row.get("price"))
    entry = _float_value(row.get("entry_price"))
    if price <= 0 or entry <= 0:
        return ""
    if price >= entry:
        return "入场状态：已到确认位"
    return f"距入场：+{(entry - price) / price * 100:.2f}%，未到确认位"


def _take_profit_text(row: pd.Series) -> str:
    entry = _float_value(row.get("entry_price"))
    take = _float_value(row.get("take_profit"))
    if entry > 0 and take > 0 and take <= entry:
        return "无有效空间（上方空间不足）"
    return _fmt_num(row.get("take_profit"))


def build_alert_markdown(row: pd.Series, kind: str, market_info: dict[str, Any], market_news_state: str, mode: str) -> str:
    """生成单只股票的手机友好提醒卡片。"""

    holding_flag = "已持仓" if row.get("has_holding") else "未持仓"
    entry_status = _entry_status_text(row)
    lines = [
        f"### {kind} | {row.get('code', '')} {row.get('name', '')}",
        f"> 市场：{market_info.get('state', '未知')} | 新闻：{compact_text(market_news_state or '无', 48)}",
        f"> 持仓：{holding_flag} | 模式：{row.get('mode', mode)} | 置信度：{_fmt_num(row.get('risk_confidence'), 2)}",
        "",
        f"- 当前价：{_fmt_num(row.get('price'))}",
        f"- 建议入场：{_fmt_num(row.get('entry_price'))}",
        f"- 止损：{_fmt_num(row.get('stop_loss'))}",
        f"- 止盈：{_take_profit_text(row)}",
        f"- 仓位：{_fmt_num(row.get('position_pct'), 1)}%",
        f"- R/R：{_fmt_num(row.get('risk_reward'))}",
        f"- 理由：{compact_text(row.get('risk_reason') or row.get('buy_reason') or row.get('entry_reason') or '', 120)}",
        f"- 题材：{compact_text(row.get('theme_label', '题材待确认'), 18)} | 热度：{compact_text(row.get('theme_heat_level', '待确认'), 8)}",
    ]
    if entry_status:
        lines.insert(7, f"- {entry_status}")
    if safe_text(row.get("theme_heat_reason")):
        lines.append(f"- 热度依据：{compact_text(row.get('theme_heat_reason'), 88)}")
    lines.append(
        f"- 信号：{compact_text(row.get('signal_state', 'fresh'), 12)} / "
        f"{compact_text(row.get('signal_action', 'continue'), 12)}"
    )
    if safe_text(row.get("holding_brief")):
        lines.append(f"- 持仓：{compact_text(row.get('holding_brief'), 80)}")
    if safe_text(row.get("news_brief")):
        lines.append(f"- 新闻：{compact_text(row.get('news_brief'), 100)}")
    if safe_text(row.get("ai")):
        lines.append(f"- AI：{compact_text(row.get('ai'), 120)}")
    return "\n".join(lines)


def save_outputs(result: pd.DataFrame, market_info: dict[str, Any], market_news_state: str, ai_overview: str = "") -> None:
    """保存 CSV 和 Markdown 结果。

    参数说明：
    - result：扫描结果
    - market_info：大盘信息
    - market_news_state：新闻情绪
    - ai_overview：AI 总结
    """

    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"scan_{stamp}.csv"
    md_path = OUTPUT_DIR / f"scan_{stamp}.md"
    result.to_csv(csv_path, index=False, encoding="utf-8-sig")
    md_path.write_text(
        build_summary_markdown(
            result,
            market_info,
            market_news_state,
            Config(),
            ai_overview=ai_overview,
        ),
        encoding="utf-8",
    )
    print(f"已保存：{csv_path.name} / {md_path.name}", flush=True)


def print_console_report(result: pd.DataFrame, market_info: dict[str, Any], market_news_state: str) -> None:
    """打印更紧凑的终端摘要。"""

    print("\n========== 扫描结果 ==========")
    print(f"大盘：{market_info.get('state', '未知')} | 新闻：{compact_text(market_news_state or '无', 60)}")
    for idx, (_, row) in enumerate(result.head(8).iterrows(), start=1):
        print(
            f"{idx:02d}. {row.get('code', '')} {row.get('name', '')} | "
            f"{row.get('mode', 'mid')} | 入{_fmt_num(row.get('entry_price'))} "
            f"止{_fmt_num(row.get('stop_loss'))} 止盈{_fmt_num(row.get('take_profit'))} "
            f"仓位{_fmt_num(row.get('position_pct'), 1)}% | {compact_text(row.get('buy_state', ''), 18)}"
        )


def main() -> None:
    cfg = parse_args()
    CACHE_DIR.mkdir(exist_ok=True)
    cache = DiskCache(CACHE_DIR / "scan_cache.json")
    notifier = WeComNotifier(
        webhook_url=cfg.notify_webhook,
        state_file=CACHE_DIR / "wecom_notify_state.json",
        cooldown_sec=cfg.notify_cooldown,
        timeout_sec=app_config.WECOM_TIMEOUT_SEC,
    )

    auto_daemon = cfg.mode == "auto"
    if not cfg.watch and not auto_daemon:
        run_once(cfg, cache, notifier if cfg.notify else None)
        return

    print("进入常驻模式，脚本会自动识别盘前、盘中、盘后。", flush=True)
    last_stage_key = ""
    while True:
        runtime_phase = cfg.mode
        try:
            now = datetime.now()
            runtime_phase = resolve_runtime_phase(now) if auto_daemon else cfg.mode

            if runtime_phase == "lunch":
                print("午休时段，稍后再检查。", flush=True)
                sleep_with_jitter(max(cfg.interval * 2, 900), cfg.jitter)
                continue

            if cfg.market_only and runtime_phase == "intraday" and not is_market_time(now):
                print("当前不在交易时段，稍后再检查。", flush=True)
                sleep_with_jitter(max(cfg.interval * 2, 600), cfg.jitter)
                continue

            stage_key = f"{now.strftime('%Y%m%d')}:{runtime_phase}"
            if auto_daemon and runtime_phase in {"pre", "after"} and stage_key == last_stage_key:
                sleep_with_jitter(max(cfg.interval * 2, 900), cfg.jitter)
                continue

            runtime_cfg = replace(cfg, mode=runtime_phase)
            run_once(runtime_cfg, cache, notifier if cfg.notify else None)
            last_stage_key = stage_key
        except Exception as exc:
            print(f"本轮执行失败：{exc}", flush=True)

        sleep_with_jitter(cfg.interval if runtime_phase == "intraday" else max(cfg.interval * 2, 900), cfg.jitter)


if __name__ == "__main__":
    main()
