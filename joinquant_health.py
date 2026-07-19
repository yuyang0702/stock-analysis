from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from typing import Any

import config as app_config
from trading_store import TradingStore
from notifier import WeComNotifier


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


def _execution_ledger_metrics(db_file: Path) -> dict[str, Any]:
    metrics = {
        "buy_enabled": "1", "kill_switch": "0", "latest_reconciliation_result": "",
        "latest_reconciliation_severity": "", "reconciliation_mismatch_count": 0,
        "account_snapshot_count": 0, "order_count": 0, "fill_count": 0,
        "recovery_ready": False, "active_execution_issue_count": 0,
        "active_execution_error_count": 0, "auto_resume_owned": False,
    }
    try:
        with TradingStore(db_file).connect() as conn:
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
            matched = conn.execute(
                """SELECT snapshot_id FROM reconciliation_runs WHERE mode='full' AND result='matched'
                   ORDER BY finished_at DESC LIMIT 2"""
            ).fetchall()
            metrics["recovery_ready"] = len({str(row[0]) for row in matched if row[0]}) >= 2
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
    today = now.date().isoformat()
    history_today = [row for row in history if _snapshot_time(row)[:10] == today]
    snapshots_for_orders = history_today if history_today else ([snapshot_payload] if snapshot_payload else [])
    signal_age = _age_minutes(signal_payload.get("generated_at"), now)
    snapshot_age = _age_minutes(_snapshot_time(snapshot_payload), now)
    signals = signal_payload.get("signals", []) if isinstance(signal_payload.get("signals"), list) else []
    signal_ids = {str(item.get("id")) for item in signals if isinstance(item, dict) and item.get("id")}
    ledger_ok, ledger_schema_version, ledger_signal_count, ledger_json_parity, ledger_error = _ledger_status(db_file, signals)
    execution_metrics = _execution_ledger_metrics(db_file)
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
    if execution_metrics["latest_reconciliation_result"] == "mismatch":
        issue_codes.append("reconciliation_mismatch")
        issues.append("最近一次自动对账存在差异")

    if signal_error:
        issue_codes.append("signal_file_error")
        issues.append(f"信号文件异常：{signal_error}")
    elif signal_payload.get("schema_version") != 1:
        issue_codes.append("signal_schema_invalid")
        issues.append("信号文件 schema_version 不是 1")
    elif signal_age is None:
        issue_codes.append("signal_time_missing")
        issues.append("信号生成时间缺失")
    elif signal_age > signal_max_age_min:
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
    elif snapshot_age > snapshot_max_age_min:
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

    status = "ok" if not issues else "critical"
    is_trading_time = _is_a_share_trading_time(now)
    fresh_executable_buy = signal_age is not None and signal_age <= signal_max_age_min and any(
        isinstance(item, dict) and str(item.get("action") or "").lower() == "buy"
        for item in signals
    )
    alert_required = _alert_required(issue_codes, now, fresh_executable_buy)
    stability_score = _stability_score(issue_codes, failed_orders_today, api_counts["api_error_count_today"])
    result = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "is_trading_time": is_trading_time,
        "alert_required": alert_required,
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
        "stable_gate_pass": status == "ok" and stability_score >= 80,
        "latest_total_value": _num(snapshot_payload.get("total_value")),
        "latest_cash": _num(snapshot_payload.get("cash")),
        **execution_metrics,
    }
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
        f"- 当前交易时段：{'是' if result.get('is_trading_time') else '否'}",
        f"- 是否触发微信报警：{'是' if result.get('alert_required') else '否'}",
        f"- 稳定性评分：{result.get('stability_score', 0)}",
        f"- 实盘准入：{'通过' if result.get('stable_gate_pass') else '不通过'}",
        f"- 信号数量：{result.get('signal_count', 0)}",
        f"- SQLite 交易账本：{'正常' if result.get('ledger_ok') else '未就绪'}",
        f"- SQLite schema_version：{result.get('ledger_schema_version', 0)}",
        f"- SQLite/JSON 信号一致：{'是' if result.get('ledger_json_parity') else '否'}",
        f"- SQLite 错误：{_sanitize_ledger_error(result.get('ledger_error') or '') or '-'}",
        f"- 跳空二次确认状态：{json.dumps(result.get('gap_reentry_states') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- 信号年龄：{result.get('signal_age_min')} 分钟",
        f"- 快照年龄：{result.get('snapshot_age_min')} 分钟",
        f"- 今日信号拉取：{result.get('signal_pull_count_today', 0)} 次",
        f"- 今日摘要访问：{result.get('latest_pull_count_today', 0)} 次",
        f"- 今日快照回传：{result.get('snapshot_post_count_today', 0)} 次",
        f"- 今日 API 异常：{result.get('api_error_count_today', 0)} 次",
        f"- 今日快照：{result.get('snapshot_count_today', 0)} 次",
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
    return "\n".join(lines)


def notify_if_needed(result: dict[str, Any]) -> bool:
    if not result.get("alert_required") or not app_config.WECOM_WEBHOOK_URL:
        return False
    notifier = WeComNotifier(
        webhook_url=app_config.WECOM_WEBHOOK_URL,
        state_file=app_config.CACHE_DIR / "wecom_notify_state.json",
        cooldown_sec=app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
        timeout_sec=app_config.WECOM_TIMEOUT_SEC,
    )
    key = "joinquant-health:" + ",".join(result.get("issue_codes") or ["unknown"])
    return notifier.send_markdown("JoinQuant 健康异常", build_alert_markdown(result), dedupe_key=key)


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


def main() -> None:
    args = build_arg_parser().parse_args()
    result = build_health_report(
        args.signal_file,
        args.snapshot_file,
        args.history_file,
        args.report_file,
        api_event_file=args.api_event_file,
        positions_file=args.positions_file,
    )
    if args.notify:
        notify_if_needed(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
