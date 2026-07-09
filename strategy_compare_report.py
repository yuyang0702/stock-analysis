from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import akshare as ak
import pandas as pd

import config as app_config
from notifier import WeComNotifier


DEFAULT_REPORT_FILE = app_config.STRATEGY_COMPARE_REPORT_FILE


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows), encoding="utf-8")
    tmp.replace(path)


def _default_history_provider(code: str, start_date: str) -> pd.DataFrame:
    df = ak.stock_zh_a_hist(symbol=code, start_date=start_date, adjust="qfq")
    mapping = {"日期": "date", "收盘": "close", "最高": "high", "最低": "low"}
    return df.rename(columns={k: v for k, v in mapping.items() if k in df.columns})


def _future_rows(history: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if history is None or history.empty or "date" not in history.columns:
        return pd.DataFrame()
    df = history.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df[df["date"] > trade_date].sort_values("date").head(5)


def _return_pct(value: Any, entry: float) -> float | None:
    price = _num(value)
    if price is None or entry <= 0:
        return None
    return round((price - entry) / entry * 100.0, 2)


def update_return_labels(
    sample_path: Path | None = None,
    history_provider: Callable[[str, str], pd.DataFrame] | None = None,
) -> int:
    sample_path = sample_path or app_config.ML_SIGNAL_SAMPLE_FILE
    history_provider = history_provider or _default_history_provider
    rows = _read_jsonl(sample_path)
    updated = 0
    for row in rows:
        signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
        if _text(signal.get("action")) != "buy":
            continue
        labels = row.setdefault("labels", {})
        if labels.get("ret_5d") is not None:
            continue
        trade_date = _text(row.get("trade_date") or signal.get("trade_date"))
        code = _text(row.get("code") or signal.get("code"))
        entry = _num(signal.get("price") or row.get("features", {}).get("entry_price"))
        if not trade_date or not code or not entry:
            continue
        try:
            history = history_provider(code, trade_date.replace("-", ""))
        except Exception:
            continue
        future = _future_rows(history, trade_date)
        if future.empty:
            continue
        closes = future["close"].tolist() if "close" in future.columns else []
        highs = future["high"].tolist() if "high" in future.columns else closes
        lows = future["low"].tolist() if "low" in future.columns else closes
        if len(closes) >= 1:
            labels["ret_1d"] = _return_pct(closes[0], entry)
        if len(closes) >= 3:
            labels["ret_3d"] = _return_pct(closes[2], entry)
        if len(closes) >= 5:
            labels["ret_5d"] = _return_pct(closes[4], entry)
        labels["max_favorable_excursion"] = _return_pct(max(_num(v, entry) or entry for v in highs), entry)
        labels["max_adverse_excursion"] = _return_pct(min(_num(v, entry) or entry for v in lows), entry)
        stop_loss = _num(row.get("features", {}).get("stop_loss") or signal.get("stop_loss"))
        take_profit = _num(row.get("features", {}).get("take_profit") or signal.get("take_profit"))
        labels["hit_stop"] = bool(stop_loss and min(_num(v, entry) or entry for v in lows) <= stop_loss)
        labels["hit_take"] = bool(take_profit and max(_num(v, entry) or entry for v in highs) >= take_profit)
        row["label_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updated += 1
    if updated:
        _write_jsonl(sample_path, rows)
    return updated


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _strategy_stats(rows: list[dict[str, Any]], score_key: str, top_n: int) -> dict[str, Any]:
    ranked = sorted(rows, key=lambda row: _num(row.get("features", {}).get(score_key), -999) or -999, reverse=True)[:top_n]
    ret3 = [_num(row.get("labels", {}).get("ret_3d")) for row in ranked]
    ret5 = [_num(row.get("labels", {}).get("ret_5d")) for row in ranked]
    drawdowns = [_num(row.get("labels", {}).get("max_adverse_excursion")) for row in ranked]
    ret3_clean = [v for v in ret3 if v is not None]
    ret5_clean = [v for v in ret5 if v is not None]
    dd_clean = [v for v in drawdowns if v is not None]
    wins = [v for v in ret3_clean if v > 0]
    return {
        "count": len(ranked),
        "avg_ret_3d": _avg(ret3_clean),
        "avg_ret_5d": _avg(ret5_clean),
        "max_drawdown": min(dd_clean) if dd_clean else None,
        "win_rate": round(len(wins) / len(ret3_clean) * 100.0, 1) if ret3_clean else None,
    }


def compare_strategies(rows: list[dict[str, Any]], top_n: int = 5, min_samples: int = 20) -> dict[str, Any]:
    buy_rows = [
        row
        for row in rows
        if _text(row.get("signal", {}).get("action")) == "buy" and _num(row.get("labels", {}).get("ret_3d")) is not None
    ]
    dates = sorted(_text(row.get("trade_date")) for row in buy_rows if _text(row.get("trade_date")))
    base = _strategy_stats(buy_rows, "final_score", top_n)
    shadow = _strategy_stats(buy_rows, "enhanced_score", top_n)
    if len(buy_rows) < min_samples:
        conclusion = "样本不足，继续观察，不参与下单。"
    elif (
        shadow["avg_ret_3d"] is not None
        and base["avg_ret_3d"] is not None
        and shadow["avg_ret_3d"] > base["avg_ret_3d"]
        and (shadow["max_drawdown"] or 0) >= (base["max_drawdown"] or 0)
    ):
        conclusion = "影子评分本期占优，但仍仅观察，不参与下单。"
    elif (
        base["avg_ret_3d"] is not None
        and shadow["avg_ret_3d"] is not None
        and base["avg_ret_3d"] > shadow["avg_ret_3d"]
    ):
        conclusion = "原策略本期占优，影子评分继续观察。"
    else:
        conclusion = "两套评分暂无明显优势，继续观察。"
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start_date": dates[0] if dates else "",
        "end_date": dates[-1] if dates else "",
        "sample_count": len(rows),
        "evaluable_count": len(buy_rows),
        "top_n": top_n,
        "base": base,
        "shadow": shadow,
        "conclusion": conclusion,
    }


def _fmt(value: Any, suffix: str = "%") -> str:
    num = _num(value)
    return "-" if num is None else f"{num:+.2f}{suffix}"


def build_report_markdown(result: dict[str, Any]) -> str:
    top_n = result.get("top_n", 5)
    base = result.get("base", {})
    shadow = result.get("shadow", {})
    return "\n".join(
        [
            "# 策略对照复盘",
            "",
            f"- 生成时间：{result.get('generated_at')}",
            f"- 区间：{result.get('start_date') or '-'} ~ {result.get('end_date') or '-'}",
            f"- 样本：{result.get('sample_count', 0)} | 可评估：{result.get('evaluable_count', 0)}",
            "",
            f"## 原策略 Top{top_n}",
            f"- D+3：{_fmt(base.get('avg_ret_3d'))}",
            f"- D+5：{_fmt(base.get('avg_ret_5d'))}",
            f"- 最大回撤：{_fmt(base.get('max_drawdown'))}",
            f"- 胜率：{_fmt(base.get('win_rate'))}",
            "",
            f"## 影子评分 Top{top_n}",
            f"- D+3：{_fmt(shadow.get('avg_ret_3d'))}",
            f"- D+5：{_fmt(shadow.get('avg_ret_5d'))}",
            f"- 最大回撤：{_fmt(shadow.get('max_drawdown'))}",
            f"- 胜率：{_fmt(shadow.get('win_rate'))}",
            "",
            f"## 结论",
            result.get("conclusion", "继续观察。"),
        ]
    )


def build_weekly_alert_markdown(result: dict[str, Any]) -> str:
    top_n = result.get("top_n", 5)
    base = result.get("base", {})
    shadow = result.get("shadow", {})
    return "\n".join(
        [
            "#### 【策略对照】周度复盘",
            f"> 区间：{result.get('start_date') or '-'} ~ {result.get('end_date') or '-'}",
            f"> 样本：{result.get('sample_count', 0)} | 可评估：{result.get('evaluable_count', 0)}",
            "",
            f"原策略 Top{top_n}：D+3 {_fmt(base.get('avg_ret_3d'))} | D+5 {_fmt(base.get('avg_ret_5d'))} | 回撤 {_fmt(base.get('max_drawdown'))}",
            f"影子评分 Top{top_n}：D+3 {_fmt(shadow.get('avg_ret_3d'))} | D+5 {_fmt(shadow.get('avg_ret_5d'))} | 回撤 {_fmt(shadow.get('max_drawdown'))}",
            "",
            f"结论：{result.get('conclusion', '继续观察。')}",
        ]
    )


def build_and_write_report(
    sample_path: Path | None = None,
    report_path: Path | None = None,
    notify: bool = False,
    weekly: bool = False,
) -> str:
    sample_path = sample_path or app_config.ML_SIGNAL_SAMPLE_FILE
    report_path = report_path or DEFAULT_REPORT_FILE
    update_return_labels(sample_path)
    result = compare_strategies(_read_jsonl(sample_path))
    md = build_report_markdown(result)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(md, encoding="utf-8")
    if notify and weekly and app_config.WECOM_WEBHOOK_URL:
        notifier = WeComNotifier(
            webhook_url=app_config.WECOM_WEBHOOK_URL,
            state_file=app_config.CACHE_DIR / "wecom_notify_state.json",
            cooldown_sec=app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
            timeout_sec=app_config.WECOM_TIMEOUT_SEC,
        )
        key = f"strategy-compare-weekly:{result.get('start_date')}:{result.get('end_date')}"
        notifier.send_markdown("策略对照周度复盘", build_weekly_alert_markdown(result), dedupe_key=key)
    return md


def main() -> None:
    parser = argparse.ArgumentParser(description="Build original-vs-shadow strategy compare report")
    parser.add_argument("--sample-file", type=Path, default=app_config.ML_SIGNAL_SAMPLE_FILE)
    parser.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_FILE)
    parser.add_argument("--notify", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--weekly", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args()
    print(build_and_write_report(args.sample_file, args.report_file, notify=args.notify, weekly=args.weekly))


if __name__ == "__main__":
    main()
