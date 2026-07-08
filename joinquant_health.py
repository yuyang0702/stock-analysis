from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config as app_config
from notifier import WeComNotifier


FAILED_STATUSES = {"failed", "rejected", "cancelled", "skipped"}


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


def _read_history(path: Path) -> list[dict[str, Any]]:
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


def _orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("orders", [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _failed_orders(payload: dict[str, Any]) -> int:
    return sum(1 for item in _orders(payload) if str(item.get("status", "")).lower() in FAILED_STATUSES)


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
) -> dict[str, Any]:
    now = now or datetime.now()
    signal_file = signal_file or app_config.JOINQUANT_SIGNAL_FILE
    snapshot_file = snapshot_file or app_config.JOINQUANT_ACCOUNT_FILE
    history_file = history_file or snapshot_file.parent / "account_snapshot_history.jsonl"
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
    history = _read_history(history_file)
    today = now.date().isoformat()
    history_today = [row for row in history if _snapshot_time(row)[:10] == today]
    signal_time = signal_payload.get("generated_at")
    snapshot_time = _snapshot_time(snapshot_payload)
    signal_age = _age_minutes(signal_time, now)
    snapshot_age = _age_minutes(snapshot_time, now)
    signals = signal_payload.get("signals", []) if isinstance(signal_payload.get("signals"), list) else []
    positions = snapshot_payload.get("positions", []) if isinstance(snapshot_payload.get("positions"), list) else []
    failed_orders_today = sum(_failed_orders(row) for row in history_today) + _failed_orders(snapshot_payload)
    issues: list[str] = []
    issue_codes: list[str] = []

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

    status = "ok" if not issues else "critical"
    result = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "issues": issues,
        "issue_codes": issue_codes,
        "signal_count": len(signals),
        "signal_age_min": round(signal_age, 1) if signal_age is not None else None,
        "snapshot_age_min": round(snapshot_age, 1) if snapshot_age is not None else None,
        "snapshot_count_today": len(history_today),
        "failed_orders_today": failed_orders_today,
        "position_count": len(positions),
        "latest_total_value": _num(snapshot_payload.get("total_value")),
        "latest_cash": _num(snapshot_payload.get("cash")),
    }
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(build_report_markdown(result), encoding="utf-8")
    return result


def build_report_markdown(result: dict[str, Any]) -> str:
    status_text = "正常" if result.get("status") == "ok" else "异常"
    lines = [
        "# JoinQuant 健康检查",
        "",
        f"- 生成时间：{result.get('generated_at')}",
        f"- 状态：{status_text}",
        f"- 信号数量：{result.get('signal_count', 0)}",
        f"- 信号年龄：{result.get('signal_age_min')} 分钟",
        f"- 快照年龄：{result.get('snapshot_age_min')} 分钟",
        f"- 今日快照：{result.get('snapshot_count_today', 0)} 次",
        f"- 今日失败订单：{result.get('failed_orders_today', 0)} 笔",
        f"- 持仓数量：{result.get('position_count', 0)}",
        f"- 总资产：{_num(result.get('latest_total_value')):.2f}",
        f"- 现金：{_num(result.get('latest_cash')):.2f}",
        "",
        "## 异常",
        "",
    ]
    issues = result.get("issues") or []
    if issues:
        lines.extend(f"- {item}" for item in issues)
    else:
        lines.append("- 暂无异常。")
    return "\n".join(lines) + "\n"


def build_alert_markdown(result: dict[str, Any]) -> str:
    lines = [
        "#### 【JoinQuant】健康异常",
        f"> 时间：{result.get('generated_at', '-')}",
        f"> 状态：{result.get('status', '-')}",
        f"> 快照年龄：{result.get('snapshot_age_min')} 分钟 | 信号年龄：{result.get('signal_age_min')} 分钟",
        f"> 今日快照：{result.get('snapshot_count_today', 0)} | 失败订单：{result.get('failed_orders_today', 0)}",
        f"> 总资产：{_num(result.get('latest_total_value')):.2f} | 现金：{_num(result.get('latest_cash')):.2f} | 持仓：{result.get('position_count', 0)}",
    ]
    for issue in result.get("issues") or []:
        lines.append(f"- {issue}")
    return "\n".join(lines)


def notify_if_needed(result: dict[str, Any]) -> bool:
    if result.get("status") == "ok" or not app_config.WECOM_WEBHOOK_URL:
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
    parser.add_argument("--report-file", type=Path)
    parser.add_argument("--notify", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    result = build_health_report(args.signal_file, args.snapshot_file, args.history_file, args.report_file)
    if args.notify:
        notify_if_needed(result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
