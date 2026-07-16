from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import config as app_config
from ml_contracts import CandidateSample, TimedFeature
from ml_store import MlStore


FEATURE_COLUMNS = [
    "price",
    "pct_chg",
    "amount",
    "turnover",
    "market_cap",
    "score",
    "final_score",
    "enhanced_score",
    "shadow_adjust_score",
    "original_rank",
    "shadow_rank",
    "shadow_rank_change",
    "shadow_base_score",
    "news_catalyst_score",
    "sector_position_score",
    "market_emotion_score",
    "global_risk_score",
    "shadow_reason",
    "trade_score",
    "news_score",
    "risk_reward",
    "position_pct",
    "entry_price",
    "stop_loss",
    "take_profit",
    "pressure_pct",
    "pressure_label",
    "ma5",
    "ma10",
    "ma20",
    "ma30",
    "atr14",
    "theme_label",
    "theme_heat_level",
    "theme_heat_score",
    "market_state",
    "signal_state",
    "signal_action",
    "signal_age_days",
    "buy_state",
]

DEFAULT_LABELS = {
    "order_status": "",
    "order_reason": "",
    "order_id": "",
    "amount": None,
    "filled": None,
    "order_price": None,
    "order_datetime": "",
    "ret_1d": None,
    "ret_3d": None,
    "ret_5d": None,
    "ret_10d": None,
    "max_favorable_excursion": None,
    "max_adverse_excursion": None,
    "hit_stop": None,
    "hit_take": None,
    "net_pnl": None,
}


def _code(value: Any) -> str:
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def build_candidate_samples(
    rows: pd.DataFrame,
    decisions: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[CandidateSample]:
    ordered_rows = [row for _, row in rows.iterrows()]
    if len(ordered_rows) != len(decisions) or any(
        _code(row.get("code")) != _code(decision.get("code"))
        for row, decision in zip(ordered_rows, decisions)
    ):
        raise ValueError("CANDIDATE_DECISION_SET_MISMATCH")

    decision_at = str(context["decision_at"])
    cohort_mode = str(context.get("cohort_mode") or "audit")
    cohort_interval_sec = context.get("cohort_interval_sec")
    samples = []
    for row, decision in zip(ordered_rows, decisions):
        features = {
            key: TimedFeature(_value(row.get(key)), decision_at)
            for key in FEATURE_COLUMNS
            if key in row.index
        }
        features["cohort_mode"] = TimedFeature(cohort_mode, decision_at)
        features["cohort_interval_sec"] = TimedFeature(cohort_interval_sec, decision_at)
        features["training_eligible"] = TimedFeature(
            cohort_mode == "intraday" and cohort_interval_sec == 300, decision_at
        )
        selected = bool(decision["selected"])
        stage = str(decision["rejection_stage"])
        samples.append(CandidateSample.from_values(
            source=context["source"],
            dataset_id=context["dataset_id"],
            decision_at=decision_at,
            code=_code(row.get("code")),
            strategy_version=context["strategy_version"],
            parameter_version=context["parameter_version"],
            feature_schema_version=context["feature_schema_version"],
            features=features,
            selected=selected,
            rejection_stage=stage,
            rejection_code=str(decision["rejection_code"]),
            final_action="selected" if selected else f"{stage}_rejected",
            universe_hash=context["universe_hash"],
            market_data_version=context["market_data_version"],
            code_hash=context["code_hash"],
            generator_hash=context["generator_hash"],
        ))
    return samples


def record_candidate_batch(
    rows: pd.DataFrame,
    decisions: list[dict[str, Any]],
    context: dict[str, Any],
    store: MlStore,
) -> int:
    return store.record_candidates(build_candidate_samples(rows, decisions, context))


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _num(value: Any) -> float | None:
    value = _value(value)
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def build_signal_sample(row: pd.Series, signal: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    features = {key: _value(row.get(key)) for key in FEATURE_COLUMNS if key in row.index}
    return {
        "sample_version": 1,
        "sample_id": _text(signal.get("id")),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": payload.get("run_id"),
        "trade_date": payload.get("trade_date"),
        "generated_at": payload.get("generated_at"),
        "source": "joinquant_signal_export",
        "code": _text(signal.get("code")),
        "jq_code": _text(signal.get("jq_code")),
        "name": _text(signal.get("name")),
        "signal": {
            "id": _text(signal.get("id")),
            "action": _text(signal.get("action")),
            "code": _text(signal.get("code")),
            "jq_code": _text(signal.get("jq_code")),
            "position_pct": _num(signal.get("position_pct")),
            "price": _num(signal.get("price")),
            "reason": _text(signal.get("reason")),
        },
        "features": features,
        "labels": dict(DEFAULT_LABELS),
    }


def append_signal_samples(
    rows_and_signals: list[tuple[pd.Series, dict[str, Any]]],
    payload: dict[str, Any],
    sample_path: Path | None = None,
) -> int:
    sample_path = sample_path or app_config.ML_SIGNAL_SAMPLE_FILE
    if not rows_and_signals:
        return 0
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    with sample_path.open("a", encoding="utf-8") as fh:
        for row, signal in rows_and_signals:
            fh.write(json.dumps(build_signal_sample(row, signal, payload), ensure_ascii=False, default=str) + "\n")
    return len(rows_and_signals)


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
    tmp.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )
    tmp.replace(path)


def update_order_labels(sample_path: Path | None, snapshot: dict[str, Any]) -> int:
    sample_path = sample_path or app_config.ML_SIGNAL_SAMPLE_FILE
    rows = _read_jsonl(sample_path)
    if not rows:
        return 0
    orders = [item for item in snapshot.get("orders", []) if isinstance(item, dict)]
    by_id = {_text(order.get("id") or order.get("signal_id")): order for order in orders}
    updated = 0
    for row in rows:
        signal_id = _text(row.get("sample_id") or row.get("signal", {}).get("id"))
        order = by_id.get(signal_id)
        if not order:
            continue
        labels = row.setdefault("labels", {})
        labels.update(
            {
                "order_status": _text(order.get("status")),
                "order_reason": _text(order.get("reason")),
                "order_id": _text(order.get("order_id")),
                "amount": _num(order.get("amount")),
                "filled": _num(order.get("filled")),
                "order_price": _num(order.get("price")),
                "order_datetime": _text(order.get("datetime") or order.get("dt")),
            }
        )
        row["label_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updated += 1
    if updated:
        _write_jsonl(sample_path, rows)
    return updated


def _bucket(score: Any) -> str:
    value = _num(score)
    if value is None:
        return "未知"
    if value >= 90:
        return "90+"
    if value >= 80:
        return "80-90"
    if value >= 70:
        return "70-80"
    return "<70"


def build_review_report(sample_path: Path | None = None, report_path: Path | None = None) -> str:
    sample_path = sample_path or app_config.ML_SIGNAL_SAMPLE_FILE
    report_path = report_path or app_config.ML_REVIEW_REPORT_FILE
    rows = _read_jsonl(sample_path)
    actions = Counter(_text(row.get("signal", {}).get("action")) for row in rows)
    statuses = Counter(_text(row.get("labels", {}).get("order_status")) for row in rows)
    score_buckets = Counter(_bucket(row.get("features", {}).get("final_score")) for row in rows)
    shadow_buckets = Counter(_bucket(row.get("features", {}).get("enhanced_score")) for row in rows)
    success = sum(statuses.get(key, 0) for key in ("filled", "submitted", "held", "open", "done"))
    failed = sum(statuses.get(key, 0) for key in ("failed", "rejected", "cancelled", "skipped"))

    lines = [
        "# ML 样本复盘",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 样本 {len(rows)} | 买入 {actions.get('buy', 0)} | 卖出 {actions.get('sell', 0)}",
        f"- 成交/提交 {success} | 失败/跳过 {failed} | 未标注 {statuses.get('', 0)}",
        "",
        "## 分数分布",
    ]
    for key in ("90+", "80-90", "70-80", "<70", "未知"):
        if score_buckets.get(key):
            lines.append(f"- {key}: {score_buckets[key]}")
    lines.extend(["", "## 影子评分分布"])
    for key in ("90+", "80-90", "70-80", "<70", "未知"):
        if shadow_buckets.get(key):
            lines.append(f"- {key}: {shadow_buckets[key]}")
    lines.extend(["", "## 订单状态"])
    for key, count in statuses.most_common():
        lines.append(f"- {key or '未标注'}: {count}")
    lines.extend(["", "> 当前是 ML-3/ML-6 影子复盘：只统计样本、原分和影子分，不训练模型，不参与下单。"])
    md = "\n".join(lines)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(md, encoding="utf-8")
    return md


def main() -> None:
    parser = argparse.ArgumentParser(description="Build ML signal sample review report")
    parser.add_argument("--sample-file", type=Path, default=app_config.ML_SIGNAL_SAMPLE_FILE)
    parser.add_argument("--report-file", type=Path, default=app_config.ML_REVIEW_REPORT_FILE)
    args = parser.parse_args()
    print(build_review_report(args.sample_file, args.report_file))


if __name__ == "__main__":
    main()
