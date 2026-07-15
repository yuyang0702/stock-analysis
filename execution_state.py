from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any


EXIT_THRESHOLDS = {
    "hard_stop": (1, 2, 3),
    "protective_stop": (2, 3, 5),
    "time_stop": (3, 5, 10),
    "take_profit": (5, 10, 15),
}


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value))


def trading_minutes_between(start: str, end: str, holidays: set[str]) -> float:
    left, right = _dt(start), _dt(end)
    if right <= left:
        return 0.0
    seconds = 0.0
    day = left.date()
    while day <= right.date():
        if day.weekday() < 5 and day.isoformat() not in holidays:
            for begin, finish in ((time(9, 30), time(11, 30)), (time(13), time(15))):
                session_start = datetime.combine(day, begin)
                session_end = datetime.combine(day, finish)
                seconds += max(
                    0.0,
                    (min(right, session_end) - max(left, session_start)).total_seconds(),
                )
        day += timedelta(days=1)
    return round(seconds / 60.0, 6)


def exit_family(reason: str) -> str:
    value = str(reason or "").lower()
    if "hard_stop" in value:
        return "hard_stop"
    if "breakeven" in value or "trailing" in value:
        return "protective_stop"
    if "time_stop" in value:
        return "time_stop"
    return "take_profit"


def _severity(age: float, family: str) -> str:
    _, warning, error = EXIT_THRESHOLDS[family]
    if age >= error:
        return "ERROR"
    if age >= warning:
        return "WARNING"
    return "INFO"


def classify_exit_execution(
    intent: dict[str, Any], orders: list[dict[str, Any]], platform_qty: int,
    now: str, holidays: set[str],
) -> dict[str, object]:
    target = max(0, int(intent.get("target_qty") or 0))
    if int(platform_qty) <= target:
        return {
            "state": "EXIT_TARGET_REACHED", "severity": "INFO", "complete": True,
            "stage_started_at": now, "age_minutes": 0.0,
        }
    signal_id = str(intent.get("signal_id") or "")
    related = [row for row in orders if str(row.get("signal_id") or "") == signal_id]
    related.sort(key=lambda row: str(
        row.get("updated_at") or row.get("event_at")
        or row.get("first_submitted_at") or ""
    ))
    order = related[-1] if related else None
    published = str(
        intent.get("published_at") or intent.get("validated_at")
        or intent.get("created_at") or now
    )
    state = "SIGNAL_DELIVERY_PENDING"
    stage_started = published
    if order:
        status = str(order.get("status") or "").lower()
        reason = str(order.get("reason") or "").lower()
        event_at = str(order.get("event_at") or order.get("updated_at") or published)
        block = {
            "t_plus_one": "MARKET_BLOCKED_T1",
            "suspended": "MARKET_BLOCKED_SUSPENDED",
            "limit_down": "MARKET_BLOCKED_LIMIT_DOWN",
        }
        matched_block = next((value for key, value in block.items() if key in reason or key in status), "")
        if matched_block:
            stage_started = event_at
            return {
                "state": matched_block, "severity": "WARNING", "complete": False,
                "stage_started_at": stage_started,
                "age_minutes": trading_minutes_between(stage_started, now, holidays),
            }
        if status == "submit_unknown":
            state = "SUBMIT_UNKNOWN"
            stage_started = event_at
        elif "stale" in reason or status == "stale":
            state = "SIGNAL_STALE"
            stage_started = event_at
        elif int(order.get("filled_qty") or 0) > 0 or status == "partial":
            state = "PARTIAL_FILL_PENDING"
            stage_started = str(order.get("updated_at") or order.get("event_at") or published)
        elif status in {"submitted", "open", "held", "pending"}:
            state = "FILL_PENDING"
            stage_started = str(
                order.get("first_submitted_at") or order.get("event_at")
                or order.get("updated_at") or published
            )
        else:
            state = "ORDER_SUBMIT_PENDING"
            stage_started = event_at
    age = trading_minutes_between(stage_started, now, holidays)
    return {
        "state": state, "severity": _severity(age, exit_family(str(intent.get("reason") or ""))),
        "complete": False, "stage_started_at": stage_started, "age_minutes": age,
        "filled_qty": int(order.get("filled_qty") or 0) if order else 0,
    }
