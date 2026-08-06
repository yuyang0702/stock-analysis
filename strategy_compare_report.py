from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

import config as app_config
from ml_store import MlStore


DEFAULT_REPORT_FILE = app_config.STRATEGY_COMPARE_REPORT_FILE
SHANGHAI = ZoneInfo("Asia/Shanghai")
STORE_QUERY_LIMIT = 5_000


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


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace a latest report, even if a reader is active."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(str(content))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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


def _rate(values: Sequence[object], expected: object = 1) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return round(sum(value == expected for value in clean) / len(clean) * 100.0, 2)


def _finite(value: object) -> float | None:
    number = _num(value)
    return number if number is not None and math.isfinite(number) else None


def _canonical_sha256(value: object) -> str | None:
    """Return a deterministic hash for report evidence without exposing content."""

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _runtime_evidence(runtime: Mapping[str, object] | None) -> dict[str, object]:
    """Keep report runtime evidence bounded and free of arbitrary payloads."""

    runtime = runtime or {}
    keys = (
        "active_model_id",
        "permission_level",
        "health_status",
        "health_reason",
        "last_attempt_at",
        "last_success_at",
        "last_prediction_count",
        "last_trading_equivalent",
    )
    result: dict[str, object] = {}
    for key in keys:
        value = runtime.get(key)
        if key == "health_reason":
            # Runtime reasons are diagnostic strings; cap their size so a malformed
            # exception cannot turn the report into an unbounded log sink.
            result[key] = " ".join(str(value or "").split())[:256]
        elif key == "last_prediction_count":
            try:
                result[key] = max(0, int(value)) if value is not None else 0
            except (TypeError, ValueError):
                result[key] = 0
        else:
            result[key] = value
    return result


def _prediction_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ret3 = [_finite(row.get("ret_3d_net")) for row in rows]
    ret5 = [_finite(row.get("ret_5d_net")) for row in rows]
    ret10 = [_finite(row.get("ret_10d_net")) for row in rows]
    downside = [_finite(row.get("downside_loss")) for row in rows]
    confidence = [_finite(row.get("confidence")) for row in rows]
    psi = [_finite(row.get("max_feature_psi")) for row in rows]
    # SQLite ML labels are decimal return/loss rates.  Report percentages are
    # human-facing, so convert 0.01 to 1.00% exactly once here.
    ret3_clean = [value * 100.0 for value in ret3 if value is not None]
    ret5_clean = [value * 100.0 for value in ret5 if value is not None]
    ret10_clean = [value * 100.0 for value in ret10 if value is not None]
    downside_clean = [value * 100.0 for value in downside if value is not None]
    confidence_clean = [value for value in confidence if value is not None]
    psi_clean = [value for value in psi if value is not None]
    return {
        "count": len(rows),
        "avg_ret_3d": _avg(ret3_clean),
        "avg_ret_5d": _avg(ret5_clean),
        "avg_ret_10d": _avg(ret10_clean),
        "avg_downside_loss": _avg(downside_clean),
        "win_rate": _rate([value > 0 for value in ret5_clean], True),
        "fill_rate": _rate([row.get("fill_label") for row in rows], 1),
        "avg_confidence": _avg(confidence_clean),
        "confidence_coverage": _rate(
            [value is not None for value in confidence], True
        ),
        "max_feature_psi": max(psi_clean) if psi_clean else None,
        "drift_ready_rate": _rate(
            [row.get("drift_status") for row in rows], "ready"
        ),
    }


def _rank_per_batch(
    rows: Sequence[dict[str, Any]], *, score_key: str, top_n: int, ascending: bool = False
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("decision_at") or ""), []).append(row)
    selected: list[dict[str, Any]] = []
    for decision_at in sorted(grouped):
        values = [
            row
            for row in grouped[decision_at]
            if _finite(row.get(score_key)) is not None
        ]
        values.sort(
            key=lambda row: (
                _finite(row.get(score_key)) or 0.0,
                str(row.get("sample_id") or ""),
            ),
            reverse=not ascending,
        )
        selected.extend(values[:top_n])
    return selected


def compare_trained_model_rows(
    rows: Sequence[dict[str, Any]],
    *,
    top_n: int = 5,
    min_samples: int = 20,
    runtime: Mapping[str, object] | None = None,
    model_evidence: Mapping[str, object] | None = None,
    coverage_evidence: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Compare only trusted SQLite rule facts and registered-model predictions."""

    if not 1 <= int(top_n) <= 30:
        raise ValueError("top_n must be between 1 and 30")
    if not 1 <= int(min_samples) <= STORE_QUERY_LIMIT:
        raise ValueError("min_samples is out of bounds")
    normalized = [dict(row) for row in rows]
    rule_eligible = [
        row
        for row in normalized
        if bool(row.get("rule_selected"))
        and str(row.get("rule_final_action") or "").lower() == "buy"
    ]
    rule_rank_key = "rule_order" if any(_finite(row.get("rule_order")) is not None for row in rule_eligible) else "rule_score"
    base = _rank_per_batch(
        rule_eligible,
        score_key=rule_rank_key,
        top_n=top_n,
        ascending=rule_rank_key == "rule_order",
    )
    predicted = [row for row in rule_eligible if str(row.get("model_id") or "")]
    model = _rank_per_batch(predicted, score_key="ml_score", top_n=top_n)
    model_ids = sorted({str(row["model_id"]) for row in model if row.get("model_id")})
    model_available = len(model) >= int(min_samples) and bool(model_ids)
    dates = sorted({str(row.get("trade_date") or "") for row in normalized if row.get("trade_date")})
    quality_failures: dict[str, int] = {}
    for row in normalized:
        reason = str(row.get("quality_reason") or row.get("quality_failure_reason") or "").strip()
        if reason:
            quality_failures[reason] = quality_failures.get(reason, 0) + 1
    runtime_value = _runtime_evidence(runtime)
    equivalence = runtime_value.get("last_trading_equivalent")
    if equivalence in {1, True, "1", "true", "True"}:
        equivalence_status = "observed_equal"
    elif equivalence in {0, False, "0", "false", "False"}:
        equivalence_status = "mismatch"
    else:
        equivalence_status = "not_recorded"
    conclusion = (
        "样本不足：仅保留数据质量与模型运行证据，不评价优劣。"
        if not model_available
        else "已形成原规则与训练模型的样本外对照；是否放权仍由独立人工治理决定。"
    )
    coverage_value = dict(coverage_evidence or {})
    total_candidates = int(coverage_value.get("sample_count") or len(normalized))

    def aggregate_rate(count_key: str, fallback: list[object]) -> float | None:
        if coverage_evidence is None:
            return _rate(fallback, True)
        if total_candidates <= 0:
            return None
        return round(
            int(coverage_value.get(count_key) or 0) / total_candidates * 100.0,
            2,
        )

    prediction_availability = aggregate_rate(
        "prediction_count",
        [bool(row.get("model_id")) for row in normalized],
    )
    return {
        "generated_at": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
        "start_date": dates[0] if dates else "",
        "end_date": dates[-1] if dates else "",
        "sample_count": total_candidates,
        "evaluable_count": len(rule_eligible),
        "top_n": int(top_n),
        "base": _prediction_stats(base),
        "model": _prediction_stats(model),
        "model_available": model_available,
        "model_ids": model_ids,
        "prediction_availability": prediction_availability,
        "label_coverage": {
            "fill": aggregate_rate(
                "fill_count",
                [row.get("fill_label") is not None for row in normalized],
            ),
            "d3": aggregate_rate(
                "d3_count",
                [_finite(row.get("ret_3d_net")) is not None for row in normalized],
            ),
            "d5": aggregate_rate(
                "d5_count",
                [_finite(row.get("ret_5d_net")) is not None for row in normalized],
            ),
            "d10": aggregate_rate(
                "d10_count",
                [_finite(row.get("ret_10d_net")) is not None for row in normalized],
            ),
            "downside": aggregate_rate(
                "downside_count",
                [_finite(row.get("downside_loss")) is not None for row in normalized],
            ),
        },
        "quality_failures": dict(sorted(quality_failures.items())),
        "runtime": runtime_value,
        "model_evidence": dict(model_evidence or {}),
        "l0_trading_equivalence": equivalence_status,
        "conclusion": conclusion,
    }


def _feature_payload(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, object] = {}
    for key, raw in parsed.items():
        result[str(key)] = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
    return result


def query_store_comparison_rows(
    store: MlStore,
    *,
    model_id: str,
    start_date: str,
    end_date: str,
    label_source: str,
    label_version: str,
    cost_sha256: str,
    policy_sha256: str,
    limit: int = STORE_QUERY_LIMIT,
) -> list[dict[str, Any]]:
    if not 1 <= int(limit) <= STORE_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {STORE_QUERY_LIMIT}")
    required = (model_id, start_date, end_date, label_source, label_version, cost_sha256, policy_sha256)
    if not all(str(value).strip() for value in required):
        raise ValueError("complete model and label contract is required")
    with store.transaction() as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ml_predictions)")}
        optional_prediction = [
            name
            for name in ("feature_coverage", "max_feature_psi", "drift_status", "reasons_json")
            if name in columns
        ]
        optional_sql = "".join(f",p.{name} AS {name}" for name in optional_prediction)
        rows = connection.execute(
            f"""SELECT c.sample_id,c.trade_date,c.decision_at,c.code,
                       c.features_json,c.selected AS rule_selected,
                       c.rejection_stage AS rule_rejection_stage,
                       c.rejection_code AS rule_rejection_code,
                       c.final_action AS rule_final_action,
                       p.model_id,p.ml_score,p.ml_filter,p.position_multiplier,
                       p.confidence,p.expected_ret_3d,p.expected_ret_5d,
                       p.expected_ret_10d,p.downside_risk,p.fill_probability
                       {optional_sql},
                       l.fill_label,l.fill_status,l.ret_3d_net,l.ret_5d_net,
                       l.ret_10d_net,l.downside_loss,l.quality_status,
                       l.quality_reasons_json,l.failure_reasons_json
                FROM ml_candidate_samples c
                LEFT JOIN ml_predictions p
                  ON p.sample_id=c.sample_id AND p.model_id=?
                LEFT JOIN ml_labels l ON l.sample_id=c.sample_id
                  AND l.label_source=? AND l.label_version=?
                  AND l.cost_sha256=? AND l.policy_sha256=?
                WHERE c.trade_date BETWEEN ? AND ?
                  AND c.selected=1 AND lower(c.final_action)='buy'
                ORDER BY c.decision_at,c.sample_id LIMIT ?""",
            (
                model_id,
                label_source,
                label_version,
                cost_sha256,
                policy_sha256,
                start_date,
                end_date,
                int(limit) + 1,
            ),
        ).fetchall()
    if len(rows) > int(limit):
        raise ValueError("strategy comparison row limit exceeded")
    result: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        features = _feature_payload(row.pop("features_json", "{}"))
        row["rule_score"] = features.get("rule_score", features.get("final_score"))
        row["rule_order"] = features.get("rule_order")
        quality = []
        for key in ("quality_reasons_json", "failure_reasons_json"):
            try:
                values = json.loads(str(row.pop(key, "[]") or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                values = []
            if isinstance(values, list):
                quality.extend(str(value) for value in values if str(value).strip())
        row["quality_reason"] = ",".join(sorted(set(quality)))
        result.append(row)
    return result


def query_store_coverage_evidence(
    store: MlStore,
    *,
    model_id: str,
    start_date: str,
    end_date: str,
    label_source: str,
    label_version: str,
    cost_sha256: str,
    policy_sha256: str,
) -> dict[str, int]:
    """Return aggregate candidate/prediction/label coverage without row scans."""

    required = (
        model_id, start_date, end_date, label_source, label_version,
        cost_sha256, policy_sha256,
    )
    if not all(str(value).strip() for value in required):
        raise ValueError("complete model and label contract is required")
    with store.transaction() as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS sample_count,
                      SUM(CASE WHEN p.sample_id IS NOT NULL THEN 1 ELSE 0 END)
                        AS prediction_count,
                      SUM(CASE WHEN l.fill_label IS NOT NULL THEN 1 ELSE 0 END)
                        AS fill_count,
                      SUM(CASE WHEN l.ret_3d_net IS NOT NULL THEN 1 ELSE 0 END)
                        AS d3_count,
                      SUM(CASE WHEN l.ret_5d_net IS NOT NULL THEN 1 ELSE 0 END)
                        AS d5_count,
                      SUM(CASE WHEN l.ret_10d_net IS NOT NULL THEN 1 ELSE 0 END)
                        AS d10_count,
                      SUM(CASE WHEN l.downside_loss IS NOT NULL THEN 1 ELSE 0 END)
                        AS downside_count
               FROM ml_candidate_samples c
               LEFT JOIN ml_predictions p
                 ON p.sample_id=c.sample_id AND p.model_id=?
               LEFT JOIN ml_labels l ON l.sample_id=c.sample_id
                 AND l.label_source=? AND l.label_version=?
                 AND l.cost_sha256=? AND l.policy_sha256=?
               WHERE c.trade_date BETWEEN ? AND ?""",
            (
                model_id,
                label_source,
                label_version,
                cost_sha256,
                policy_sha256,
                start_date,
                end_date,
            ),
        ).fetchone()
    names = (
        "sample_count", "prediction_count", "fill_count", "d3_count",
        "d5_count", "d10_count", "downside_count",
    )
    return {
        name: int((row[index] if row is not None else 0) or 0)
        for index, name in enumerate(names)
    }


def _strategy_stats(rows: list[dict[str, Any]], score_key: str, top_n: int) -> dict[str, Any]:
    def score(row: dict[str, Any]) -> float:
        values = row.get("features")
        value = _num(values.get(score_key)) if isinstance(values, dict) else None
        return -999.0 if value is None else value

    ranked = sorted(rows, key=score, reverse=True)[:top_n]
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
    model = _strategy_stats([], "final_score", top_n)
    conclusion = "训练模型 unavailable：受治理的训练与预测链路尚未实现。"
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start_date": dates[0] if dates else "",
        "end_date": dates[-1] if dates else "",
        "sample_count": len(rows),
        "evaluable_count": len(buy_rows),
        "top_n": top_n,
        "base": base,
        "model": model,
        "model_available": False,
        "model_ids": [],
        "conclusion": conclusion,
    }


def _fmt(value: Any, suffix: str = "%") -> str:
    num = _num(value)
    return "-" if num is None else f"{num:+.2f}{suffix}"


def build_report_markdown(result: dict[str, Any]) -> str:
    top_n = result.get("top_n", 5)
    base = result.get("base", {})
    model = result.get("model", {})
    lines = [
            "# 原规则策略 vs 训练模型",
            "",
            f"- 生成时间：{result.get('generated_at')}",
            f"- 区间：{result.get('start_date') or '-'} ~ {result.get('end_date') or '-'}",
            f"- 样本：{result.get('sample_count', 0)} | 可评估：{result.get('evaluable_count', 0)}",
            "",
            f"## 原规则策略 Top{top_n}",
            f"- D+3：{_fmt(base.get('avg_ret_3d'))}",
            f"- D+5：{_fmt(base.get('avg_ret_5d'))}",
            f"- D+10：{_fmt(base.get('avg_ret_10d'))}",
            f"- 平均下行损失：{_fmt(base.get('avg_downside_loss'))}",
            f"- 历史兼容最大回撤：{_fmt(base.get('max_drawdown'))}",
            f"- 胜率：{_fmt(base.get('win_rate'))}",
            f"- 成交率：{_fmt(base.get('fill_rate'))}",
            "",
            f"## 训练模型 Top{top_n}",
    ]
    if result.get("model_available"):
        lines.extend([
            f"- 模型：{', '.join(result.get('model_ids') or [])}",
            f"- D+3：{_fmt(model.get('avg_ret_3d'))}",
            f"- D+5：{_fmt(model.get('avg_ret_5d'))}",
            f"- D+10：{_fmt(model.get('avg_ret_10d'))}",
            f"- 平均下行损失：{_fmt(model.get('avg_downside_loss'))}",
            f"- 历史兼容最大回撤：{_fmt(model.get('max_drawdown'))}",
            f"- 胜率：{_fmt(model.get('win_rate'))}",
            f"- 成交率：{_fmt(model.get('fill_rate'))}",
            f"- 平均置信度：{_fmt(model.get('avg_confidence'), '')}",
            f"- 置信度覆盖率：{_fmt(model.get('confidence_coverage'))}",
            f"- 最大特征 PSI：{_fmt(model.get('max_feature_psi'), '')}",
            f"- 漂移样本充分率：{_fmt(model.get('drift_ready_rate'))}",
        ])
    else:
        lines.append("- 状态：unavailable（尚无已训练模型预测）")
    coverage = result.get("label_coverage")
    if isinstance(coverage, dict):
        lines.extend([
            "",
            "## 标签与运行证据",
            f"- fill 覆盖率：{_fmt(coverage.get('fill'))}",
            f"- D+3 覆盖率：{_fmt(coverage.get('d3'))}",
            f"- D+5 覆盖率：{_fmt(coverage.get('d5'))}",
            f"- D+10 覆盖率：{_fmt(coverage.get('d10'))}",
            f"- downside 覆盖率：{_fmt(coverage.get('downside'))}",
            f"- 预测可用率：{_fmt(result.get('prediction_availability'))}",
            f"- L0 交易等价证据：`{result.get('l0_trading_equivalence', 'not_recorded')}`",
        ])
        failures = result.get("quality_failures")
        if isinstance(failures, dict) and failures:
            lines.extend(
                f"- 质量失败 `{reason}`：{count}"
                for reason, count in sorted(failures.items())
            )
    model_evidence = result.get("model_evidence")
    if isinstance(model_evidence, Mapping):
        lines.extend(
            [
                "",
                "## 模型、权限与完整性证据",
                f"- 模型 ID：{model_evidence.get('model_id') or '-'}",
                f"- 登记状态：{model_evidence.get('status') or '-'}",
                f"- 模型权限：L{model_evidence.get('permission_level') if model_evidence.get('permission_level') is not None else '-'}",
                f"- artifact SHA-256：`{model_evidence.get('artifact_sha256') or '-'}`",
                f"- manifest SHA-256：`{model_evidence.get('manifest_sha256') or '-'}`",
                f"- 是否为活动模型：{'是' if model_evidence.get('is_active') else '否'}",
                f"- 审批事件：`{model_evidence.get('approval_event_id') or '-'}`",
                f"- 已批准层级：{('L' + str(model_evidence.get('approved_level'))) if model_evidence.get('approved_level') is not None else '-'}",
            ]
        )
    runtime_evidence = result.get("runtime")
    if isinstance(runtime_evidence, Mapping):
        lines.extend(
            [
                "",
                "## 运行健康与降级证据",
                f"- 健康状态：{runtime_evidence.get('health_status') or '-'}",
                f"- 健康原因：{runtime_evidence.get('health_reason') or '-'}",
                f"- 最近尝试：{runtime_evidence.get('last_attempt_at') or '-'}",
                f"- 最近成功：{runtime_evidence.get('last_success_at') or '-'}",
                f"- 最近预测数：{runtime_evidence.get('last_prediction_count', 0)}",
                f"- 当前权限：L{runtime_evidence.get('permission_level') if runtime_evidence.get('permission_level') is not None else '-'}",
            ]
        )
    lines.extend(["", "## 结论", result.get("conclusion", "继续观察。")])
    return "\n".join(lines)


def build_and_write_report(
    sample_path: Path | None = None,
    report_path: Path | None = None,
) -> str:
    sample_path = sample_path or app_config.ML_SIGNAL_SAMPLE_FILE
    report_path = report_path or DEFAULT_REPORT_FILE
    update_return_labels(sample_path)
    result = compare_strategies(_read_jsonl(sample_path))
    md = build_report_markdown(result)
    _atomic_write_text(report_path, md)
    return md


def _latest_registered_model_id(store: MlStore) -> str | None:
    with store.transaction() as connection:
        row = connection.execute(
            """SELECT model_id FROM ml_models
               ORDER BY created_at DESC,model_id DESC LIMIT 1"""
        ).fetchone()
    return None if row is None else str(row[0])


def build_and_write_store_report(
    *,
    store: MlStore,
    report_path: Path,
    start_date: str,
    end_date: str,
    model_id: str | None = None,
    label_source: str = "strict_counterfactual_v2",
    label_version: str | None = None,
    cost_sha256: str | None = None,
    policy_sha256: str | None = None,
    top_n: int = 5,
    min_samples: int = 20,
    limit: int = STORE_QUERY_LIMIT,
) -> str:
    store.initialize()
    runtime = store.runtime_state()
    health_reader = getattr(store, "runtime_health", None)
    if callable(health_reader):
        runtime = {**runtime, **health_reader()}
    active_model_id = str(runtime.get("active_model_id") or "").strip()
    selected_model = str(
        model_id or active_model_id or _latest_registered_model_id(store) or ""
    ).strip()
    result: dict[str, Any]
    if not selected_model:
        runtime["health_status"] = runtime.get("health_status") or "no_model"
        result = compare_trained_model_rows(
            [],
            top_n=top_n,
            min_samples=min_samples,
            runtime=runtime,
            model_evidence={
                "model_id": None,
                "status": "no_model",
                "permission_level": runtime.get("permission_level"),
                "artifact_sha256": None,
                "manifest_sha256": None,
            },
        )
        result["conclusion"] = "训练模型 unavailable：当前没有活动模型。"
    else:
        model = store.model_record(selected_model)
        if not isinstance(model, Mapping):
            raise ValueError(f"registered model not found: {selected_model}")
        manifest = model.get("manifest")
        if not isinstance(manifest, Mapping):
            raw = model.get("manifest_json")
            manifest = json.loads(str(raw)) if raw else {}
        is_active_model = bool(active_model_id and selected_model == active_model_id)
        runtime_for_model = dict(runtime)
        if not is_active_model:
            runtime_for_model["last_trading_equivalent"] = None
            runtime_for_model["permission_level"] = None
            runtime_for_model["health_reason"] = "MODEL_NOT_ACTIVE_REVIEW"
        artifact_sha256 = str(model.get("artifact_sha256") or "")
        approval = (
            store.approved_model_event(selected_model, artifact_sha256)
            if artifact_sha256
            else None
        )
        model_evidence = {
            "model_id": selected_model,
            "status": model.get("status"),
            "permission_level": (
                runtime.get("permission_level") if is_active_model else None
            ),
            "artifact_sha256": artifact_sha256,
            "manifest_sha256": _canonical_sha256(manifest),
            "approval_event_id": (
                approval.get("event_id") if isinstance(approval, Mapping) else None
            ),
            "approved_level": (
                approval.get("new_level") if isinstance(approval, Mapping) else None
            ),
            "is_active": is_active_model,
        }
        resolved_label_version = str(label_version or manifest.get("label_version") or "")
        resolved_cost_sha = str(cost_sha256 or manifest.get("cost_sha256") or "")
        resolved_policy_sha = str(
            policy_sha256
            or manifest.get("label_policy_sha256")
            or manifest.get("policy_sha256")
            or ""
        )
        rows = query_store_comparison_rows(
            store,
            model_id=selected_model,
            start_date=start_date,
            end_date=end_date,
            label_source=label_source,
            label_version=resolved_label_version,
            cost_sha256=resolved_cost_sha,
            policy_sha256=resolved_policy_sha,
            limit=limit,
        )
        coverage_evidence = query_store_coverage_evidence(
            store,
            model_id=selected_model,
            start_date=start_date,
            end_date=end_date,
            label_source=label_source,
            label_version=resolved_label_version,
            cost_sha256=resolved_cost_sha,
            policy_sha256=resolved_policy_sha,
        )
        result = compare_trained_model_rows(
            rows,
            top_n=top_n,
            min_samples=min_samples,
            runtime=runtime_for_model,
            model_evidence=model_evidence,
            coverage_evidence=coverage_evidence,
        )
    markdown = build_report_markdown(result)
    _atomic_write_text(report_path, markdown)
    return markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build rule-vs-trained-model compare report")
    parser.add_argument(
        "--sample-file",
        type=Path,
        help="legacy JSONL compatibility input; omitted means bounded SQLite mode",
    )
    parser.add_argument("--ml-db", type=Path, default=app_config.ML_DB_FILE)
    parser.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_FILE)
    parser.add_argument("--model-id")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--label-source", default=app_config.ML_LABEL_SOURCE)
    parser.add_argument("--label-version", default=app_config.ML_LABEL_VERSION)
    parser.add_argument("--cost-sha256", default=app_config.ML_COST_SHA256)
    parser.add_argument("--policy-sha256", default=app_config.ML_POLICY_SHA256)
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--limit", type=int, default=STORE_QUERY_LIMIT)
    args = parser.parse_args(argv)
    if args.sample_file is not None:
        markdown = build_and_write_report(args.sample_file, args.report_file)
    else:
        today = datetime.now(SHANGHAI).date()
        markdown = build_and_write_store_report(
            store=MlStore(args.ml_db, max_bytes=app_config.ML_DB_MAX_BYTES),
            report_path=args.report_file,
            start_date=args.start or (today - timedelta(days=7)).isoformat(),
            end_date=args.end or today.isoformat(),
            model_id=args.model_id,
            label_source=args.label_source,
            label_version=args.label_version or None,
            cost_sha256=args.cost_sha256 or None,
            policy_sha256=args.policy_sha256 or None,
            top_n=args.top_n,
            min_samples=args.min_samples,
            limit=args.limit,
        )
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
