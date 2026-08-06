from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from types import MappingProxyType
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from execution_contracts import canonical_json, canonical_sha256, logical_signal_id


MAX_EVENT_KEY_BYTES = 1024
MAX_TYPE_BYTES = 128
MAX_ID_BYTES = 1024
MAX_TITLE_BYTES = 512
MAX_BODY_BYTES = 4000
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_METADATA_BYTES = 16 * 1024
CRITICAL_REMINDER_MINUTES = 180
NORMAL_ACTIVE_MAX_ROWS = 1000
NORMAL_ACTIVE_MAX_BYTES = 4 * 1024 * 1024
HIGH_ACTIVE_MAX_ROWS = 5000
HIGH_ACTIVE_MAX_BYTES = 20 * 1024 * 1024
DEAD_DETAIL_MAX_ROWS = 1000
DEAD_DETAIL_MAX_BYTES = 4 * 1024 * 1024
HIGH_CAPACITY_STOP_ROWS = HIGH_ACTIVE_MAX_ROWS * 80 // 100
HIGH_CAPACITY_STOP_BYTES = HIGH_ACTIVE_MAX_BYTES * 80 // 100
HIGH_CAPACITY_RECOVER_ROWS = HIGH_ACTIVE_MAX_ROWS * 20 // 100
HIGH_CAPACITY_RECOVER_BYTES = HIGH_ACTIVE_MAX_BYTES * 20 // 100
NOTIFICATION_WRITE_FAILURE_KEY = "notification_outbox_write_failure"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_A_SHARE_SESSIONS = (
    (datetime_time(9, 30), datetime_time(11, 30)),
    (datetime_time(13), datetime_time(15)),
)

_ASSIGNMENT_LOOKAHEAD = re.compile(
    r"(?i)(?=(?P<prefix>(?<![A-Za-z0-9_])[\"']?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)[\"']?\s*"
    r"(?:=|:(?!//))\s*[\"']?)(?P<value>[^&,\r\n\"'\]}]+))"
)
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_BASIC_SECRET = re.compile(r"(?i)\bbasic\s+[A-Za-z0-9._~+/=-]+")


class NotificationConflict(RuntimeError):
    """Raised when a stable notification key is reused for new semantics."""


class NotificationCapacityError(RuntimeError):
    """Raised only when a configured outbox capacity limit rejects an event."""


def _shanghai_datetime(value: str | datetime, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(_SHANGHAI)


def _is_a_share_day(day: date, calendar: object) -> bool:
    predicate = getattr(calendar, "is_trading_day", None)
    if callable(predicate):
        return bool(predicate(day))
    if callable(calendar):
        return bool(calendar(day))
    holidays = {
        (
            item.astimezone(_SHANGHAI).date().isoformat()
            if isinstance(item, datetime) and item.tzinfo is not None
            else item.date().isoformat()
            if isinstance(item, datetime)
            else item.isoformat()
            if isinstance(item, date)
            else str(item)
        )
        for item in (calendar or ())
    }
    return day.weekday() < 5 and day.isoformat() not in holidays


def critical_trading_minutes(
    start: str | datetime,
    end: str | datetime,
    calendar: object,
    paused_intervals: object = (),
) -> int:
    """Count complete A-share session minutes, excluding explicit pauses."""
    left = _shanghai_datetime(start, "start")
    right = _shanghai_datetime(end, "end")
    if right <= left:
        return 0
    pauses: list[tuple[datetime, datetime]] = []
    for index, interval in enumerate(paused_intervals or ()):
        try:
            pause_start, pause_end = interval
        except (TypeError, ValueError) as exc:
            raise ValueError("paused_intervals must contain timestamp pairs") from exc
        pause_left = _shanghai_datetime(pause_start, f"paused_intervals[{index}].start")
        pause_right = _shanghai_datetime(pause_end, f"paused_intervals[{index}].end")
        if pause_right <= pause_left:
            raise ValueError("paused interval end must be after start")
        if pause_right > left and pause_left < right:
            pauses.append((max(left, pause_left), min(right, pause_right)))
    pauses.sort()
    merged: list[tuple[datetime, datetime]] = []
    for pause_left, pause_right in pauses:
        if merged and pause_left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], pause_right))
        else:
            merged.append((pause_left, pause_right))

    seconds = 0.0
    day = left.date()
    while day <= right.date():
        if _is_a_share_day(day, calendar):
            for session_open, session_close in _A_SHARE_SESSIONS:
                session_start = datetime.combine(day, session_open, _SHANGHAI)
                session_end = datetime.combine(day, session_close, _SHANGHAI)
                active_start = max(left, session_start)
                active_end = min(right, session_end)
                if active_end <= active_start:
                    continue
                active_seconds = (active_end - active_start).total_seconds()
                for pause_left, pause_right in merged:
                    overlap_start = max(active_start, pause_left)
                    overlap_end = min(active_end, pause_right)
                    if overlap_end > overlap_start:
                        active_seconds -= (overlap_end - overlap_start).total_seconds()
                seconds += max(0.0, active_seconds)
        day += timedelta(days=1)
    return int(seconds // 60)


def next_critical_reminder_seq(minutes: int) -> int:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        raise ValueError("minutes must be a non-negative integer")
    return minutes // CRITICAL_REMINDER_MINUTES


def _text(value: object, name: str, *, max_bytes: int | None = None) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{name} is required")
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} exceeds {max_bytes} UTF-8 bytes")
    return text


def _timestamp(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _mapping(value: object, name: str, max_bytes: int) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    normalized_json = canonical_json(dict(value))
    encoded = normalized_json.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"{name} exceeds {max_bytes} UTF-8 bytes")
    normalized = json.loads(normalized_json)
    _reject_secrets(normalized, name)
    return _freeze(_redact_secret_values(normalized))


def redact_secret_text(value: object) -> str:
    text = str(value or "")
    text = _BEARER_SECRET.sub("Bearer [REDACTED]", text)
    text = _BASIC_SECRET.sub("Basic [REDACTED]", text)
    replacements: list[tuple[int, int]] = []
    covered_until = -1
    for match in _ASSIGNMENT_LOOKAHEAD.finditer(text):
        if not _is_secret_name(match.group("name")):
            continue
        start, end = match.span("value")
        if start < covered_until:
            continue
        replacements.append((start, end))
        covered_until = end
    if not replacements:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in replacements:
        parts.extend((text[cursor:start], "[REDACTED]"))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _redact_secret_values(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _redact_secret_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_values(item) for item in value]
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _reject_secrets(value: object, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_secret_name(key):
                raise ValueError(f"{path} must not contain {key}")
            _reject_secrets(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]")


def _is_secret_name(value: object) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    compact = "".join(character for character in normalized if character.isalnum())
    return (
        any(secret in compact for secret in (
            "token", "webhook", "password", "passwd", "secret",
            "credential", "passphrase", "authorization", "authheader",
            "brokerauth", "oauthbearer",
            "apikey", "accesskey", "privatekey", "signingkey",
            "sshkey", "authkey", "accountid", "accountno",
            "accountnumber",
        ))
        or compact in {
            "key", "userid", "username", "qmtaccount",
            "brokeraccount", "fundaccount", "securitiesaccount",
            "env", "environment", "environmentvariable",
            "environmentvariables",
        }
        or normalized.startswith("env_")
    )


def event_payload_sha256(payload: Mapping[str, object]) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    return canonical_sha256(payload)


def plan_version(
    code: str,
    side: str,
    target_qty: int,
    target_position: object,
    entry_tick: object,
    stop_tick: object,
    frozen_valid_until: str,
    strategy_version: str,
    parameters_version: str,
) -> str:
    return canonical_sha256({
        "code": _text(code, "code"),
        "side": _text(side, "side").lower(),
        "target_qty": target_qty,
        "target_position": target_position,
        "entry_tick": entry_tick,
        "stop_tick": stop_tick,
        "frozen_valid_until": _timestamp(
            frozen_valid_until, "frozen_valid_until",
        ),
        "strategy_version": _text(strategy_version, "strategy_version"),
        "parameters_version": _text(parameters_version, "parameters_version"),
    })[:16]


def notification_event_key(
    adapter: str,
    account_scope_id: str,
    event_type: str,
    **parts: object,
) -> str:
    adapter = _text(adapter, "adapter").lower()
    if adapter not in {"joinquant", "qmt"}:
        raise ValueError("adapter must be joinquant or qmt")
    scope = _key_part(account_scope_id, "account_scope_id")
    kind = _text(event_type, "event_type").lower().replace("_", "-")
    schemas = {
        "buy-plan": ("trade_date", "logical_signal_id", "plan_version"),
        "exit": ("position_cycle_id", "exit_intent_id", "stage"),
        "fill": ("fill_id",),
        "order-terminal": ("client_order_id", "status", "reason_code"),
        "control": ("control_event_id",),
        "pre": ("trade_date",),
        "close": ("trade_date",),
        "weekly": ("iso_week",),
    }
    if kind in {"issue", "issue-transition"} and "reminder_seq" not in parts:
        names = (
            "issue_key", "incident_id", "transition_seq", "transition",
            "severity",
        )
        label = "issue"
    elif kind in {"issue", "issue-reminder"} and "reminder_seq" in parts:
        names = ("issue_key", "incident_id", "reminder_seq")
        label = "issue"
    else:
        try:
            names = schemas[kind]
        except KeyError as exc:
            raise ValueError(f"unsupported notification event type: {event_type}") from exc
        label = kind
    if set(parts) != set(names):
        raise ValueError(
            f"{kind} event requires exactly: {', '.join(names)}"
        )
    values = [_key_part(parts[name], name) for name in names]
    if label == "issue" and names[-1] == "reminder_seq":
        values.insert(2, "reminder")
    result = ":".join((adapter, scope, label, *values))
    return _text(result, "event_key", max_bytes=MAX_EVENT_KEY_BYTES)


def _key_part(value: object, name: str) -> str:
    text = _text(value, name)
    return quote(text, safe="-._~")


@dataclass(frozen=True)
class NotificationEvent:
    event_key: str
    account_scope_id: str
    adapter: Literal["joinquant", "qmt"]
    event_type: str
    object_type: str
    object_id: str
    source_fact_id: str
    priority: Literal["normal", "high"]
    payload_version: int
    occurred_at: str
    expires_at: str | None
    title: str
    body: str
    payload: Mapping[str, object]
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        adapter = _text(self.adapter, "adapter").lower()
        if adapter not in {"joinquant", "qmt"}:
            raise ValueError("adapter must be joinquant or qmt")
        priority = _text(self.priority, "priority").lower()
        if priority not in {"normal", "high"}:
            raise ValueError("priority must be normal or high")
        if (
            isinstance(self.payload_version, bool)
            or not isinstance(self.payload_version, int)
            or self.payload_version < 1
        ):
            raise ValueError("payload_version must be a positive integer")
        scope = _key_part(self.account_scope_id, "account_scope_id")
        event_key = _text(
            self.event_key, "event_key", max_bytes=MAX_EVENT_KEY_BYTES,
        )
        if not event_key.startswith(f"{adapter}:{scope}:"):
            raise ValueError("event_key must match adapter and account scope")
        object.__setattr__(self, "event_key", event_key)
        object.__setattr__(self, "account_scope_id", scope)
        object.__setattr__(self, "adapter", adapter)
        object.__setattr__(
            self, "event_type", _text(
                self.event_type, "event_type", max_bytes=MAX_TYPE_BYTES,
            ),
        )
        object.__setattr__(
            self, "object_type", _text(
                self.object_type, "object_type", max_bytes=MAX_TYPE_BYTES,
            ),
        )
        object.__setattr__(
            self, "object_id", _text(
                self.object_id, "object_id", max_bytes=MAX_ID_BYTES,
            ),
        )
        object.__setattr__(
            self, "source_fact_id", _text(
                self.source_fact_id, "source_fact_id", max_bytes=MAX_ID_BYTES,
            ),
        )
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        object.__setattr__(
            self,
            "expires_at",
            None if self.expires_at is None else _timestamp(self.expires_at, "expires_at"),
        )
        object.__setattr__(
            self,
            "title",
            redact_secret_text(
                _text(self.title, "title", max_bytes=MAX_TITLE_BYTES)
            ),
        )
        body = redact_secret_text(self.body)
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError(f"body exceeds {MAX_BODY_BYTES} UTF-8 bytes")
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "payload", _mapping(self.payload, "payload", MAX_PAYLOAD_BYTES))
        object.__setattr__(self, "metadata", _mapping(self.metadata, "metadata", MAX_METADATA_BYTES))

    @property
    def semantic_payload_sha256(self) -> str:
        return event_payload_sha256({
            "account_scope_id": self.account_scope_id,
            "adapter": self.adapter,
            "event_type": self.event_type,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "source_fact_id": self.source_fact_id,
            "priority": self.priority,
            "payload_version": self.payload_version,
            "occurred_at": self.occurred_at,
            "expires_at": self.expires_at,
            "title": self.title,
            "body": self.body,
            "payload": self.payload,
            "metadata": self.metadata,
        })


@dataclass(frozen=True)
class OutboxRecord:
    event_key: str
    account_scope_id: str
    adapter: str
    event_type: str
    object_type: str
    object_id: str
    source_fact_id: str
    priority: str
    payload_version: int
    payload_sha256: str
    payload: Mapping[str, object] | None
    title: str | None
    body: str | None
    body_sha256: str | None
    metadata: Mapping[str, object] | None
    state: str
    lease_owner: str | None
    lease_until: str | None
    attempt_count: int
    next_attempt_at: str | None
    occurred_at: str
    created_at: str
    expires_at: str | None
    sent_at: str | None
    cancel_requested_at: str | None
    cancel_reason: str | None
    terminal_at: str | None
    last_error_code: str | None
    last_error: str | None
    ambiguous_attempt_mask: int
    last_ambiguous_at: str | None
    last_ambiguous_code: str | None

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> "OutboxRecord":
        values = dict(row)
        return cls(
            event_key=str(values["event_key"]),
            account_scope_id=str(values["account_scope_id"]),
            adapter=str(values["adapter"]),
            event_type=str(values["event_type"]),
            object_type=str(values["object_type"]),
            object_id=str(values["object_id"]),
            source_fact_id=str(values["source_fact_id"]),
            priority=str(values["priority"]),
            payload_version=int(values["payload_version"]),
            payload_sha256=str(values["payload_sha256"]),
            payload=_json_mapping(values.get("payload_json")),
            title=_optional_text(values.get("title")),
            body=_optional_text(values.get("body")),
            body_sha256=_optional_text(values.get("body_sha256")),
            metadata=_json_mapping(values.get("metadata_json")),
            state=str(values["state"]),
            lease_owner=_optional_text(values.get("lease_owner")),
            lease_until=_optional_text(values.get("lease_until")),
            attempt_count=int(values["attempt_count"]),
            next_attempt_at=_optional_text(values.get("next_attempt_at")),
            occurred_at=str(values["occurred_at"]),
            created_at=str(values["created_at"]),
            expires_at=_optional_text(values.get("expires_at")),
            sent_at=_optional_text(values.get("sent_at")),
            cancel_requested_at=_optional_text(values.get("cancel_requested_at")),
            cancel_reason=_optional_text(values.get("cancel_reason")),
            terminal_at=_optional_text(values.get("terminal_at")),
            last_error_code=_optional_text(values.get("last_error_code")),
            last_error=_optional_text(values.get("last_error")),
            ambiguous_attempt_mask=int(values.get("ambiguous_attempt_mask") or 0),
            last_ambiguous_at=_optional_text(values.get("last_ambiguous_at")),
            last_ambiguous_code=_optional_text(values.get("last_ambiguous_code")),
        )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _json_mapping(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(
            str(value),
            parse_constant=_reject_json_constant,
        )
    except RecursionError as exc:
        raise ValueError("stored notification JSON is nested too deeply") from exc
    if not isinstance(parsed, dict):
        raise ValueError("stored notification JSON must be an object")
    return _freeze(parsed)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"stored notification JSON contains {value}")


@dataclass(frozen=True)
class EnqueueResult:
    event_key: str
    inserted: bool
    payload_sha256: str
    state: str

    @property
    def idempotent(self) -> bool:
        return not self.inserted


@dataclass(frozen=True)
class CapacitySnapshot:
    normal_active_rows: int
    normal_active_bytes: int
    high_active_rows: int
    high_active_bytes: int
    dead_rows: int
    dead_bytes: int
    unresolved_gap_rows: int
    tombstone_rows: int
    dead_total_rows: int = 0
    dead_total_bytes: int = 0
    high_dead_rows: int = 0
    high_unresolved_gap_rows: int = 0
    high_dead_total_rows: int = 0

    @property
    def active_rows(self) -> int:
        return self.normal_active_rows + self.high_active_rows

    @property
    def active_bytes(self) -> int:
        return self.normal_active_bytes + self.high_active_bytes

    @property
    def dead_detail_rows(self) -> int:
        return self.dead_rows

    @property
    def dead_detail_bytes(self) -> int:
        return self.dead_bytes


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
