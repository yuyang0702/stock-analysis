from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from typing import Any

import config as app_config
from notification_outbox import (
    HIGH_CAPACITY_STOP_BYTES,
    HIGH_CAPACITY_STOP_ROWS,
)
from trading_store import TradingStore


FAILED_STATUSES = {"failed", "rejected", "cancelled"}
REPORTED_STATUSES = FAILED_STATUSES | {"skipped"}
NON_TRADING_NOISE_ISSUES = {
    "signal_file_error",
    "signal_time_missing",
    "signal_stale",
    "snapshot_file_error",
    "snapshot_time_missing",
    "snapshot_stale",
}
NON_TRADING_NOISE_ISSUES.update({"ledger_unavailable", "ledger_json_signal_mismatch"})


def _is_a_share_trading_day(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return now.strftime("%Y-%m-%d") not in app_config.A_SHARE_HOLIDAYS_DEFAULT


def _is_a_share_trading_time(now: datetime) -> bool:
    if not _is_a_share_trading_day(now):
        return False
    current = now.time()
    return (time(9, 30) <= current <= time(11, 30)) or (time(13, 0) <= current <= time(15, 0))


def _alert_required(issue_codes: list[str], now: datetime, fresh_executable_buy: bool = False) -> bool:
    if not issue_codes:
        return False
    if _is_a_share_trading_time(now):
        return True
    if fresh_executable_buy and any(code in {"ledger_unavailable", "ledger_json_signal_mismatch"} for code in issue_codes):
        return True
    return any(code not in NON_TRADING_NOISE_ISSUES for code in issue_codes)


def _sanitize_ledger_error(value: str) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())[:240]


def _ledger_status(db_file: Path, signals: list[dict[str, Any]]) -> tuple[bool, int, int, bool, str]:
    if not Path(db_file).is_file():
        return False, 0, 0, False, "trading database is unavailable"
    store = TradingStore(db_file)
    health = store.health()
    if not health.ok:
        return False, health.schema_version, 0, False, _sanitize_ledger_error(health.error)
    if not signals:
        return True, health.schema_version, 0, True, ""
    try:
        count, parity = store.current_signal_parity(signals)
        return True, health.schema_version, count, parity, ""
    except Exception as exc:
        return False, health.schema_version, 0, False, _sanitize_ledger_error(str(exc))


def _execution_ledger_metrics(
    db_file: Path, trade_date: str | None = None
) -> dict[str, Any]:
    metrics = {
        "buy_enabled": "1", "kill_switch": "0", "latest_reconciliation_result": "",
        "latest_reconciliation_severity": "", "reconciliation_mismatch_count": 0,
        "account_snapshot_count": 0, "order_count": 0, "fill_count": 0,
        "recovery_ready": False, "active_execution_issue_count": 0,
        "active_execution_error_count": 0, "auto_resume_owned": False,
        "notification_capacity_auto_resume_owned": False,
        "strategy_run_count_today": 0, "strategy_run_failed_count_today": 0,
        "latest_strategy_run_result": "",
        "notification_normal_active_rows": 0,
        "notification_normal_active_bytes": 0,
        "notification_high_active_rows": 0,
        "notification_high_active_bytes": 0,
        "notification_dead_detail_rows": 0,
        "notification_dead_detail_bytes": 0,
        "notification_dead_total_rows": 0,
        "notification_dead_total_bytes": 0,
        "notification_high_dead_rows": 0,
        "notification_high_dead_total_rows": 0,
        "notification_unresolved_gap_rows": 0,
        "notification_high_unresolved_gap_rows": 0,
        "notification_tombstone_rows": 0,
    }
    if not Path(db_file).is_file():
        return metrics
    try:
        store = TradingStore(db_file)
        with store.connect() as conn:
            for key, fallback in (("buy_enabled", "1"), ("kill_switch", "0")):
                row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
                metrics[key] = str(row[0]) if row else fallback
            latest = conn.execute(
                "SELECT result, severity FROM reconciliation_runs ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            if latest:
                metrics["latest_reconciliation_result"] = str(latest[0])
                metrics["latest_reconciliation_severity"] = str(latest[1])
            metrics["reconciliation_mismatch_count"] = int(conn.execute(
                "SELECT count(*) FROM reconciliation_runs WHERE result<>'matched'"
            ).fetchone()[0])
            metrics["account_snapshot_count"] = int(conn.execute(
                "SELECT count(*) FROM account_snapshots"
            ).fetchone()[0])
            metrics["order_count"] = int(conn.execute("SELECT count(*) FROM orders").fetchone()[0])
            metrics["fill_count"] = int(conn.execute("SELECT count(*) FROM fills").fetchone()[0])
            if trade_date:
                metrics["strategy_run_count_today"] = int(conn.execute(
                    "SELECT count(*) FROM strategy_runs WHERE trade_date=?",
                    (trade_date,),
                ).fetchone()[0])
                metrics["strategy_run_failed_count_today"] = int(conn.execute(
                    """SELECT count(*) FROM strategy_runs
                       WHERE trade_date=? AND result='failed'""",
                    (trade_date,),
                ).fetchone()[0])
                latest_run = conn.execute(
                    """SELECT result FROM strategy_runs WHERE trade_date=?
                       ORDER BY started_at DESC LIMIT 1""",
                    (trade_date,),
                ).fetchone()
                if latest_run:
                    metrics["latest_strategy_run_result"] = str(latest_run[0] or "")
            metrics["active_execution_issue_count"] = int(conn.execute(
                "SELECT count(*) FROM execution_issue_state WHERE recovered_at IS NULL"
            ).fetchone()[0])
            metrics["active_execution_error_count"] = int(conn.execute(
                """SELECT count(*) FROM execution_issue_state WHERE recovered_at IS NULL
                   AND severity IN ('ERROR','CRITICAL')"""
            ).fetchone()[0])
            owner = conn.execute(
                "SELECT value FROM system_state WHERE key='reconciliation_auto_resume_owner'"
            ).fetchone()
            metrics["auto_resume_owned"] = bool(owner and str(owner[0]))
            capacity_owner = conn.execute(
                """SELECT value FROM system_state
                   WHERE key='notification_capacity_auto_resume_owner'"""
            ).fetchone()
            metrics["notification_capacity_auto_resume_owned"] = bool(
                capacity_owner and str(capacity_owner[0])
            )
            matched = conn.execute(
                """SELECT snapshot_id FROM reconciliation_runs WHERE mode='full' AND result='matched'
                   ORDER BY finished_at DESC LIMIT 2"""
            ).fetchall()
            metrics["recovery_ready"] = len({str(row[0]) for row in matched if row[0]}) >= 2
            capacity = store.notification_capacity(conn)
            metrics.update({
                "notification_normal_active_rows": capacity.normal_active_rows,
                "notification_normal_active_bytes": capacity.normal_active_bytes,
                "notification_high_active_rows": capacity.high_active_rows,
                "notification_high_active_bytes": capacity.high_active_bytes,
                "notification_dead_detail_rows": capacity.dead_detail_rows,
                "notification_dead_detail_bytes": capacity.dead_detail_bytes,
                "notification_dead_total_rows": capacity.dead_total_rows,
                "notification_dead_total_bytes": capacity.dead_total_bytes,
                "notification_high_dead_rows": capacity.high_dead_rows,
                "notification_high_dead_total_rows": (
                    capacity.high_dead_total_rows
                ),
                "notification_unresolved_gap_rows": capacity.unresolved_gap_rows,
                "notification_high_unresolved_gap_rows": (
                    capacity.high_unresolved_gap_rows
                ),
                "notification_tombstone_rows": capacity.tombstone_rows,
            })
    except Exception:
        pass
    return metrics


def _exit_intent_mismatches(db_file: Path, snapshot: dict[str, Any]) -> list[str]:
    try:
        intents = TradingStore(db_file).get_open_exit_intents()
    except Exception:
        return []
    quantities = _position_map(snapshot)
    return sorted(code for code, intent in intents.items()
                  if quantities.get(code, 0) <= int(intent.get("target_qty") or 0))


def _load_json(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, "invalid_json"
    return (data, "") if isinstance(data, dict) else ({}, "invalid_shape")


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text[:19], text[:10]]
    for candidate in candidates:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except Exception:
                continue
    return None


def _age_minutes(value: Any, now: datetime) -> float | None:
    dt = _parse_dt(value)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 60.0)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _snapshot_time(payload: dict[str, Any]) -> str:
    return str(payload.get("received_at") or payload.get("generated_at") or "").strip()


def _event_time(payload: dict[str, Any]) -> str:
    return str(payload.get("received_at") or payload.get("ts") or payload.get("generated_at") or "").strip()


def _orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("orders", [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _failed_orders(payload: dict[str, Any]) -> int:
    return sum(1 for item in _orders(payload) if str(item.get("status", "")).lower() in FAILED_STATUSES)


def _failed_order_breakdown(snapshots: list[dict[str, Any]]) -> dict[str, int]:
    breakdown: dict[str, int] = {}
    for snapshot in snapshots:
        for order in _orders(snapshot):
            if str(order.get("status", "")).lower() not in REPORTED_STATUSES:
                continue
            action = str(order.get("action") or "unknown").lower()
            reason = str(order.get("reason") or order.get("status") or "unknown").strip().lower()
            key = f"{action}:{reason}"
            breakdown[key] = breakdown.get(key, 0) + 1
    return dict(sorted(breakdown.items()))


def _code(value: Any) -> str:
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def _qty(value: Any) -> int:
    return int(_num(value, 0) or 0)


def _position_map(payload: dict[str, Any]) -> dict[str, int]:
    positions = payload.get("positions", [])
    result: dict[str, int] = {}
    if not isinstance(positions, list):
        return result
    for item in positions:
        if not isinstance(item, dict):
            continue
        code = _code(item.get("code") or item.get("jq_code"))
        qty = _qty(item.get("qty") or item.get("amount") or item.get("total_amount"))
        if code and qty > 0:
            result[code] = qty
    return result


def _gap_reentry_metrics(db_file: Path, trade_date: str) -> dict[str, int]:
    try:
        with TradingStore(db_file).connect() as conn:
            rows = conn.execute(
                """SELECT state, COUNT(*) AS count
                   FROM gap_reentry_opportunities WHERE trade_date=?
                   GROUP BY state""",
                (trade_date,),
            ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}
    except Exception:
        return {}


def _position_consistency(snapshot_payload: dict[str, Any], positions_file: Path) -> tuple[str, list[str]]:
    if not positions_file.exists():
        return "missing", []
    local_payload, error = _load_json(positions_file)
    if error:
        return "invalid", []
    snapshot_positions = _position_map(snapshot_payload)
    local_positions = _position_map(local_payload)
    mismatches: list[str] = []
    for code in sorted(set(snapshot_positions) | set(local_positions)):
        if snapshot_positions.get(code, 0) != local_positions.get(code, 0):
            mismatches.append(code)
    return ("ok" if not mismatches else "mismatch"), mismatches


def _api_counts(events: list[dict[str, Any]], today: str) -> dict[str, int]:
    counts = {
        "signal_pull_count_today": 0,
        "latest_pull_count_today": 0,
        "snapshot_post_count_today": 0,
        "api_error_count_today": 0,
    }
    for event in events:
        if _event_time(event)[:10] != today:
            continue
        endpoint = str(event.get("endpoint") or "")
        status = int(_num(event.get("status_code"), 0) or 0)
        if endpoint == "signals" and status < 400:
            counts["signal_pull_count_today"] += 1
        elif endpoint == "latest" and status < 400:
            counts["latest_pull_count_today"] += 1
        elif endpoint == "account_snapshot" and status < 400:
            counts["snapshot_post_count_today"] += 1
        if status >= 400:
            counts["api_error_count_today"] += 1
    return counts


def _stability_score(issue_codes: list[str], failed_orders_today: int, api_error_count_today: int) -> int:
    score = 100
    score -= 20 * len(issue_codes)
    score -= min(20, failed_orders_today * 3)
    score -= min(20, api_error_count_today * 5)
    return max(0, min(100, score))


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _observation_day_status(
    history: list[dict[str, Any]],
    current_status: str,
    *,
    today: str,
) -> str:
    statuses = [
        str(row.get("observation_status") or "")
        for row in history
        if str(row.get("generated_at") or "")[:10] == today
        and bool(row.get("is_trading_time"))
    ]
    if current_status != "not_applicable":
        statuses.append(current_status)
    if "invalid" in statuses:
        return "invalid"
    if "degraded" in statuses:
        return "degraded"
    if "valid" in statuses:
        return "valid"
    return "not_observed"


def build_health_report(
    signal_file: Path | None = None,
    snapshot_file: Path | None = None,
    history_file: Path | None = None,
    report_file: Path | None = None,
    *,
    now: datetime | None = None,
    signal_max_age_min: int | None = None,
    snapshot_max_age_min: int | None = None,
    failed_order_limit: int | None = None,
    api_event_file: Path | None = None,
    positions_file: Path | None = None,
    health_history_file: Path | None = None,
    db_file: Path | None = None,
    persist_notifications: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now()
    signal_file = signal_file or app_config.JOINQUANT_SIGNAL_FILE
    snapshot_file = snapshot_file or app_config.JOINQUANT_ACCOUNT_FILE
    history_file = history_file or snapshot_file.parent / "account_snapshot_history.jsonl"
    api_event_file = api_event_file or snapshot_file.parent / "api_events.jsonl"
    positions_file = positions_file or app_config.POSITIONS_FILE
    health_history_file = health_history_file or snapshot_file.parent / "health_history.jsonl"
    db_file = db_file or app_config.TRADING_DB_FILE
    report_file = report_file or app_config.OUTPUT_DIR / f"joinquant_health_{now.strftime('%Y%m%d')}.md"
    signal_max_age_min = (
        app_config.JOINQUANT_HEALTH_SIGNAL_MAX_AGE_MIN_DEFAULT
        if signal_max_age_min is None
        else signal_max_age_min
    )
    snapshot_max_age_min = (
        app_config.JOINQUANT_HEALTH_SNAPSHOT_MAX_AGE_MIN_DEFAULT
        if snapshot_max_age_min is None
        else snapshot_max_age_min
    )
    failed_order_limit = (
        app_config.JOINQUANT_HEALTH_FAILED_ORDER_LIMIT_DEFAULT
        if failed_order_limit is None
        else failed_order_limit
    )

    signal_payload, signal_error = _load_json(signal_file)
    snapshot_payload, snapshot_error = _load_json(snapshot_file)
    history = _read_jsonl(history_file)
    api_events = _read_jsonl(api_event_file)
    health_history = _read_jsonl(health_history_file)
    today = now.date().isoformat()
    is_trading_time = _is_a_share_trading_time(now)
    history_today = [row for row in history if _snapshot_time(row)[:10] == today]
    snapshots_for_orders = history_today if history_today else ([snapshot_payload] if snapshot_payload else [])
    signal_age = _age_minutes(signal_payload.get("generated_at"), now)
    snapshot_age = _age_minutes(_snapshot_time(snapshot_payload), now)
    signals = signal_payload.get("signals", []) if isinstance(signal_payload.get("signals"), list) else []
    signal_ids = {str(item.get("id")) for item in signals if isinstance(item, dict) and item.get("id")}
    ledger_ok, ledger_schema_version, ledger_signal_count, ledger_json_parity, ledger_error = _ledger_status(db_file, signals)
    execution_metrics = _execution_ledger_metrics(db_file, today)
    gap_reentry_metrics = _gap_reentry_metrics(db_file, today) if ledger_ok else {}
    positions = snapshot_payload.get("positions", []) if isinstance(snapshot_payload.get("positions"), list) else []
    expected_template_version = app_config.JOINQUANT_TEMPLATE_VERSION
    strategy_template_version = str(snapshot_payload.get("strategy_template_version") or "").strip()
    failed_orders_today = sum(_failed_orders(row) for row in snapshots_for_orders)
    failed_order_breakdown = _failed_order_breakdown(snapshots_for_orders)
    api_counts = _api_counts(api_events, today)
    position_consistency, position_mismatches = _position_consistency(snapshot_payload, positions_file)
    exit_intent_mismatches = _exit_intent_mismatches(db_file, snapshot_payload) if ledger_ok else []
    issues: list[str] = []
    issue_codes: list[str] = []

    if not ledger_ok:
        issue_codes.append("ledger_unavailable")
        issues.append(f"SQLite 交易账本不可用：{ledger_error or '未初始化'}")
    elif not ledger_json_parity:
        issue_codes.append("ledger_json_signal_mismatch")
        issues.append("SQLite 与 JSON 信号 ID 不一致")

    if execution_metrics["buy_enabled"] == "0":
        issue_codes.append("buy_disabled_by_control")
        issues.append("自动对账已停止新买入")
    if execution_metrics["kill_switch"] == "1":
        issue_codes.append("kill_switch_active")
        issues.append("自动交易 KILL_SWITCH 已开启")
    if execution_metrics["notification_high_unresolved_gap_rows"] > 0:
        issue_codes.append("notification_high_enqueue_gap")
        issues.append("高优先级通知存在未解决 enqueue gap")
    if (
        execution_metrics["notification_high_active_rows"]
        >= HIGH_CAPACITY_STOP_ROWS
        or execution_metrics["notification_high_active_bytes"]
        >= HIGH_CAPACITY_STOP_BYTES
    ):
        issue_codes.append("notification_high_capacity_pressure")
        issues.append("高优先级通知容量达到停买水位")
    if execution_metrics["notification_high_dead_rows"] > 0:
        issue_codes.append("notification_high_dead_detail")
        issues.append("高优先级通知存在 dead 明细，自动恢复买入受阻")
    if execution_metrics["latest_reconciliation_result"] == "mismatch":
        issue_codes.append("reconciliation_mismatch")
        issues.append("最近一次自动对账存在差异")
    if is_trading_time and execution_metrics["strategy_run_failed_count_today"] > 0:
        issue_codes.append("strategy_run_failures_today")
        issues.append(
            f"今日策略扫描失败 {execution_metrics['strategy_run_failed_count_today']} 次"
        )

    if signal_error:
        issue_codes.append("signal_file_error")
        issues.append(f"信号文件异常：{signal_error}")
    elif signal_payload.get("schema_version") != 1:
        issue_codes.append("signal_schema_invalid")
        issues.append("信号文件 schema_version 不是 1")
    elif signal_age is None:
        issue_codes.append("signal_time_missing")
        issues.append("信号生成时间缺失")
    elif is_trading_time and signal_age > signal_max_age_min:
        issue_codes.append("signal_stale")
        issues.append(f"信号文件超时 {signal_age:.1f} 分钟")

    if snapshot_error:
        issue_codes.append("snapshot_file_error")
        issues.append(f"账户快照异常：{snapshot_error}")
    elif snapshot_payload.get("schema_version") != 1:
        issue_codes.append("snapshot_schema_invalid")
        issues.append("账户快照 schema_version 不是 1")
    elif snapshot_age is None:
        issue_codes.append("snapshot_time_missing")
        issues.append("账户快照回传时间缺失")
    elif is_trading_time and snapshot_age > snapshot_max_age_min:
        issue_codes.append("snapshot_stale")
        issues.append(f"账户快照超时 {snapshot_age:.1f} 分钟")

    if failed_orders_today > failed_order_limit:
        issue_codes.append("failed_orders_high")
        issues.append(f"今日失败/跳过订单 {failed_orders_today} 笔，超过阈值 {failed_order_limit}")

    if position_consistency == "mismatch":
        issue_codes.append("position_mismatch")
        issues.append(f"JoinQuant 快照与本地持仓不一致：{','.join(position_mismatches[:8])}")
    elif position_consistency == "invalid":
        issue_codes.append("position_file_invalid")
        issues.append("本地持仓文件异常，无法和 JoinQuant 快照对账")

    if exit_intent_mismatches:
        issue_codes.append("exit_intent_position_mismatch")
        issues.append(f"Exit intent already reached target but remains active: {','.join(exit_intent_mismatches[:8])}")

    if api_counts["api_error_count_today"] > 0:
        issue_codes.append("api_errors")
        issues.append(f"今日 JoinQuant API 异常请求 {api_counts['api_error_count_today']} 次")

    if strategy_template_version != expected_template_version:
        issue_codes.append("template_version_mismatch")
        actual = strategy_template_version or "missing"
        issues.append(f"JoinQuant 网站模板未更新：当前 {actual}，期望 {expected_template_version}")

    freshness_issue_codes = {
        "signal_file_error", "signal_schema_invalid", "signal_time_missing", "signal_stale",
        "snapshot_file_error", "snapshot_schema_invalid", "snapshot_time_missing", "snapshot_stale",
    }
    non_freshness_issues = [
        code for code in issue_codes if code not in {"signal_stale", "snapshot_stale"}
    ]
    system_status = "ok" if not non_freshness_issues else "critical"
    if not is_trading_time:
        freshness_status = "not_applicable"
        observation_status = "not_applicable"
    else:
        freshness_status = (
            "invalid" if freshness_issue_codes.intersection(issue_codes) else "valid"
        )
        degraded_codes = {"api_errors", "failed_orders_high"}
        if not issue_codes:
            observation_status = "valid"
        elif set(issue_codes) <= degraded_codes:
            observation_status = "degraded"
        else:
            observation_status = "invalid"
    observation_day_status = _observation_day_status(
        health_history, observation_status, today=today,
    )
    status = "ok" if not issues else "critical"
    fresh_executable_buy = signal_age is not None and signal_age <= signal_max_age_min and any(
        isinstance(item, dict) and str(item.get("action") or "").lower() == "buy"
        for item in signals
    )
    alert_required = _alert_required(issue_codes, now, fresh_executable_buy)
    stability_score = _stability_score(issue_codes, failed_orders_today, api_counts["api_error_count_today"])
    result = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "system_status": system_status,
        "freshness_status": freshness_status,
        "observation_status": observation_status,
        "observation_day_status": observation_day_status,
        "is_trading_time": is_trading_time,
        "alert_required": alert_required,
        "fresh_executable_buy": fresh_executable_buy,
        "issues": issues,
        "issue_codes": issue_codes,
        "signal_count": len(signals),
        "ledger_ok": ledger_ok,
        "ledger_schema_version": ledger_schema_version,
        "ledger_signal_count": ledger_signal_count,
        "json_signal_count": len(signal_ids),
        "ledger_json_parity": ledger_json_parity,
        "ledger_error": ledger_error,
        "gap_reentry_states": gap_reentry_metrics,
        "signal_age_min": round(signal_age, 1) if signal_age is not None else None,
        "snapshot_age_min": round(snapshot_age, 1) if snapshot_age is not None else None,
        "snapshot_count_today": len(history_today),
        "failed_orders_today": failed_orders_today,
        "failed_order_breakdown": failed_order_breakdown,
        "position_count": len(positions),
        "position_consistency": position_consistency,
        "position_mismatches": position_mismatches,
        "exit_intent_mismatches": exit_intent_mismatches,
        "strategy_template_version": strategy_template_version,
        "expected_template_version": expected_template_version,
        "signal_pull_count_today": api_counts["signal_pull_count_today"],
        "latest_pull_count_today": api_counts["latest_pull_count_today"],
        "snapshot_post_count_today": api_counts["snapshot_post_count_today"],
        "api_error_count_today": api_counts["api_error_count_today"],
        "stability_score": stability_score,
        "stable_gate_pass": (
            system_status == "ok"
            and observation_day_status == "valid"
            and stability_score >= 80
        ),
        "latest_total_value": _num(snapshot_payload.get("total_value")),
        "latest_cash": _num(snapshot_payload.get("cash")),
        **execution_metrics,
    }
    if persist_notifications:
        result["notification_persist_error"] = persist_health_issue_transitions(
            result, db_file, now,
        )
        if result["notification_persist_error"]:
            result["stable_gate_pass"] = False
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(build_report_markdown(result), encoding="utf-8")
    _append_jsonl(health_history_file, result)
    return result


def build_report_markdown(result: dict[str, Any]) -> str:
    status_text = "正常" if result.get("status") == "ok" else "异常"
    lines = [
        "# JoinQuant 健康检查",
        "",
        f"- 生成时间：{result.get('generated_at')}",
        f"- 状态：{status_text}",
        f"- 系统状态：{result.get('system_status', '-')}",
        f"- 会话新鲜度：{result.get('freshness_status', '-')}",
        f"- 当前观察点：{result.get('observation_status', '-')}",
        f"- 当日观察结论：{result.get('observation_day_status', '-')}",
        f"- 当前交易时段：{'是' if result.get('is_trading_time') else '否'}",
        f"- 是否触发微信报警：{'是' if result.get('alert_required') else '否'}",
        f"- 稳定性评分：{result.get('stability_score', 0)}",
        f"- 实盘准入：{'通过' if result.get('stable_gate_pass') else '不通过'}",
        f"- 信号数量：{result.get('signal_count', 0)}",
        f"- SQLite 交易账本：{'正常' if result.get('ledger_ok') else '未就绪'}",
        f"- SQLite schema_version：{result.get('ledger_schema_version', 0)}",
        f"- SQLite/JSON 信号一致：{'是' if result.get('ledger_json_parity') else '否'}",
        f"- SQLite 错误：{_sanitize_ledger_error(result.get('ledger_error') or '') or '-'}",
        (
            "- Notification capacity: "
            f"normal={result.get('notification_normal_active_rows', 0)}/"
            f"{result.get('notification_normal_active_bytes', 0)}B, "
            f"high={result.get('notification_high_active_rows', 0)}/"
            f"{result.get('notification_high_active_bytes', 0)}B, "
            f"dead_detail={result.get('notification_dead_detail_rows', 0)}/"
            f"{result.get('notification_dead_detail_bytes', 0)}B, "
            f"dead_total={result.get('notification_dead_total_rows', 0)}, "
            f"gaps={result.get('notification_unresolved_gap_rows', 0)}"
        ),
        f"- 跳空二次确认状态：{json.dumps(result.get('gap_reentry_states') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- 信号年龄：{result.get('signal_age_min')} 分钟",
        f"- 快照年龄：{result.get('snapshot_age_min')} 分钟",
        f"- 今日信号拉取：{result.get('signal_pull_count_today', 0)} 次",
        f"- 今日摘要访问：{result.get('latest_pull_count_today', 0)} 次",
        f"- 今日快照回传：{result.get('snapshot_post_count_today', 0)} 次",
        f"- 今日 API 异常：{result.get('api_error_count_today', 0)} 次",
        f"- 今日快照：{result.get('snapshot_count_today', 0)} 次",
        f"- 今日策略扫描：{result.get('strategy_run_count_today', 0)} 次",
        f"- 今日策略扫描失败：{result.get('strategy_run_failed_count_today', 0)} 次",
        f"- 今日失败订单：{result.get('failed_orders_today', 0)} 笔",
        f"- 持仓一致性：{result.get('position_consistency', '-')}",
        f"- JoinQuant 模板版本：{result.get('strategy_template_version') or 'missing'}",
        f"- 期望模板版本：{result.get('expected_template_version')}",
        f"- 持仓数量：{result.get('position_count', 0)}",
        f"- 总资产：{_num(result.get('latest_total_value')):.2f}",
        f"- 现金：{_num(result.get('latest_cash')):.2f}",
        "",
        "## 异常",
        "",
    ]
    issues = result.get("issues") or []
    lines.extend(f"- {item}" for item in issues) if issues else lines.append("- 暂无异常。")
    if result.get("notification_persist_error"):
        lines.append(
            f"- notification persistence: {result['notification_persist_error']}"
        )

    breakdown = result.get("failed_order_breakdown") or {}
    if breakdown:
        lines.extend(["", "## 失败原因统计", ""])
        lines.extend(f"- {key}: {value}" for key, value in breakdown.items())
    return "\n".join(lines) + "\n"


def build_alert_markdown(result: dict[str, Any]) -> str:
    lines = [
        "#### 【JoinQuant】健康异常",
        f"> 时间：{result.get('generated_at', '-')}",
        f"> 状态：{result.get('status', '-')}",
        f"> 评分：{result.get('stability_score', 0)} | 准入：{'通过' if result.get('stable_gate_pass') else '不通过'}",
        f"> 拉取：{result.get('signal_pull_count_today', 0)} | 回传：{result.get('snapshot_post_count_today', 0)} | API异常：{result.get('api_error_count_today', 0)}",
        f"> 快照年龄：{result.get('snapshot_age_min')} 分钟 | 信号年龄：{result.get('signal_age_min')} 分钟",
        f"> 失败订单：{result.get('failed_orders_today', 0)} | 持仓一致性：{result.get('position_consistency', '-')}",
        f"> 模板：{result.get('strategy_template_version') or 'missing'} | 期望：{result.get('expected_template_version')}",
        f"> 总资产：{_num(result.get('latest_total_value')):.2f} | 现金：{_num(result.get('latest_cash')):.2f} | 持仓：{result.get('position_count', 0)}",
    ]
    for issue in result.get("issues") or []:
        lines.append(f"- {issue}")
    if result.get("notification_persist_error"):
        lines.append(
            f"- notification persistence: {result['notification_persist_error']}"
        )
    return "\n".join(lines)


_HEALTH_ISSUE_OWNERS = {
    "buy_disabled_by_control",
    "kill_switch_active",
    "ledger_unavailable",
    "reconciliation_mismatch",
}


def _health_issue_details(code: str, result: dict[str, Any]) -> dict[str, object]:
    if code == "template_version_mismatch":
        return {
            "template_version": str(
                result.get("strategy_template_version") or "missing"
            ),
            "expected_template_version": str(
                result.get("expected_template_version") or ""
            ),
        }
    if code == "position_mismatch":
        return {
            "position_codes": sorted(
                str(item) for item in result.get("position_mismatches") or []
            )[:32],
        }
    return {}


def persist_health_issue_transitions(
    result: dict[str, Any],
    db_file: Path,
    now: datetime,
) -> str:
    fresh_buy = bool(result.get("fresh_executable_buy"))
    issue_codes = [str(code) for code in result.get("issue_codes") or []]
    active_codes = {
        code for code in issue_codes
        if code not in _HEALTH_ISSUE_OWNERS
        and _alert_required([code], now, fresh_buy)
    }
    alert_required = bool(result.get("alert_required"))
    db_file = Path(db_file)
    if not db_file.is_file():
        return "trading database is unavailable" if alert_required else ""
    store = TradingStore(db_file)
    health = store.health()
    if not health.ok:
        return (
            f"trading database schema {health.schema_version} is unavailable"
            if alert_required else ""
        )
    shanghai_now = store._shanghai_timestamp(now.isoformat(), "health time")
    messages = {
        str(code): str(message)
        for code, message in zip(
            result.get("issue_codes") or [], result.get("issues") or [],
        )
    }
    try:
        with store.transaction() as conn:
            scope_row = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter='joinquant' AND scope_alias='primary'"""
            ).fetchone()
            if scope_row is None:
                return "account scope is not registered" if alert_required else ""
            scope = str(scope_row[0])
            prefix = f"scope:{scope}:health:"
            for code in sorted(active_codes):
                store.upsert_execution_issue(conn, {
                    "account_scope_id": scope,
                    "issue_key": prefix + code,
                    "object_type": "health",
                    "object_id": code,
                    "state": code.upper(),
                    "severity": (
                        "WARNING"
                        if code in {"api_errors", "failed_orders_high"}
                        else "ERROR"
                    ),
                    "stage_started_at": shanghai_now,
                    "seen_at": shanghai_now,
                    "details": {
                        **_health_issue_details(code, result),
                        "message": messages.get(code, "")[:240],
                    },
                })
            rows = conn.execute(
                """SELECT issue_key, object_id FROM execution_issue_state
                   WHERE recovered_at IS NULL AND object_type='health'
                     AND substr(issue_key, 1, ?) = ?""",
                (len(prefix), prefix),
            ).fetchall()
            for row in rows:
                code = str(row["object_id"])
                if code in active_codes:
                    continue
                if (
                    code in NON_TRADING_NOISE_ISSUES
                    and not _is_a_share_trading_time(now)
                ):
                    continue
                store.recover_execution_issue(
                    conn,
                    str(row["issue_key"]),
                    shanghai_now,
                    account_scope_id=scope,
                )
    except Exception as exc:
        return _sanitize_ledger_error(str(exc)) or type(exc).__name__
    return ""


def notify_if_needed(result: dict[str, Any]) -> bool:
    """Deprecated compatibility hook; health transitions enqueue in SQLite."""
    del result
    return False


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build JoinQuant health report")
    parser.add_argument("--signal-file", type=Path, default=app_config.JOINQUANT_SIGNAL_FILE)
    parser.add_argument("--snapshot-file", type=Path, default=app_config.JOINQUANT_ACCOUNT_FILE)
    parser.add_argument("--history-file", type=Path)
    parser.add_argument("--api-event-file", type=Path)
    parser.add_argument("--positions-file", type=Path)
    parser.add_argument("--report-file", type=Path)
    parser.add_argument("--notify", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    result = build_health_report(
        args.signal_file,
        args.snapshot_file,
        args.history_file,
        args.report_file,
        api_event_file=args.api_event_file,
        positions_file=args.positions_file,
        persist_notifications=args.notify,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("notification_persist_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
