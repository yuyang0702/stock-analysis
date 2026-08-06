from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config as app_config
from notification_outbox import (
    NotificationEvent,
    OutboxRecord,
    body_sha256,
    critical_trading_minutes,
    event_payload_sha256,
    notification_event_key,
)
from trading_control import reconcile_notification_capacity


RETRY_BACKOFF_SECONDS = (300, 900, 1800, 3600)
LEGACY_AUDIT_MARKER = "notification_legacy_audit_v1"
MAX_LEGACY_FILE_BYTES = 4 * 1024 * 1024
MAX_LEGACY_DETAIL_ROWS = 1000


@dataclass(frozen=True)
class WorkerResult:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    dead: int = 0
    cancelled: int = 0
    ambiguous: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class LegacyAuditResult:
    completed: bool
    code: str
    sent: int = 0
    dead: int = 0
    cancelled: int = 0
    imported: int = 0
    state_file_sha256: str = ""
    queue_file_sha256: str = ""


def _utc_text(value: str | datetime, name: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc,
    )


def _semantic_hash(record: OutboxRecord) -> str | None:
    try:
        if any(item is None for item in (
            record.title, record.body, record.body_sha256,
            record.payload, record.metadata,
        )):
            return None
        if body_sha256(record.body or "") != record.body_sha256:
            return None
        return event_payload_sha256({
            "account_scope_id": record.account_scope_id,
            "adapter": record.adapter,
            "event_type": record.event_type,
            "object_type": record.object_type,
            "object_id": record.object_id,
            "source_fact_id": record.source_fact_id,
            "priority": record.priority,
            "payload_version": record.payload_version,
            "occurred_at": record.occurred_at,
            "expires_at": record.expires_at,
            "title": record.title,
            "body": record.body,
            "payload": record.payload,
            "metadata": record.metadata,
        })
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def _buy_plan_is_current(store: Any, record: OutboxRecord) -> bool:
    if record.event_type != "buy-plan":
        return True
    parts = record.event_key.split(":")
    if len(parts) != 6 or parts[2] != "buy-plan":
        return False
    trade_date, logical_signal_id, version = parts[3:]
    plan = store.get_logical_signal_plan(
        record.account_scope_id, trade_date, logical_signal_id,
    )
    return bool(
        plan
        and str(plan.get("current_plan_version") or "") == version
    )


def _critical_reminder_is_current(store: Any, record: OutboxRecord) -> bool:
    if record.event_type != "issue_reminder":
        return True
    payload = record.payload or {}
    return bool(
        str(payload.get("incident_id") or "")
        and store.is_current_critical_incident(
            record.object_id, str(payload.get("incident_id")),
        )
    )


def _business_body(record: OutboxRecord) -> str:
    body = record.body or ""
    if "业务时间：" in body or "成交时间：" in body:
        return body
    occurred_at = _instant(record.occurred_at).astimezone(
        ZoneInfo("Asia/Shanghai")
    ).isoformat()
    return f"> 业务时间：{occurred_at}\n{body}"


def _cancellation_reason(
    store: Any,
    record: OutboxRecord,
    now: datetime,
) -> str:
    if record.cancel_requested_at:
        return record.cancel_reason or "CANCEL_REQUESTED"
    if record.expires_at and _instant(record.expires_at) <= now:
        return "NOTIFICATION_EXPIRED"
    if not _buy_plan_is_current(store, record):
        return "BUY_PLAN_REPLACED"
    if not _critical_reminder_is_current(store, record):
        return "CRITICAL_INCIDENT_NOT_ACTIVE"
    return ""


def _safe_error_code(result: Any) -> str:
    value = str(getattr(result, "error_code", "") or "DELIVERY_FAILED")
    if not value or len(value.encode("utf-8")) > 128:
        return "DELIVERY_FAILED"
    if any(not (character.isalnum() or character in "_-") for character in value):
        return "DELIVERY_FAILED"
    return value


def _elapsed_clock(base: str) -> Any:
    base_instant = _instant(base)
    started = time.monotonic()

    def current() -> str:
        elapsed = max(0.0, time.monotonic() - started)
        return (base_instant + timedelta(seconds=elapsed)).isoformat()

    return current


def _in_trading_minute(
    value: str,
    calendar: object,
    paused_intervals: object,
) -> bool:
    shanghai = _instant(value).astimezone(ZoneInfo("Asia/Shanghai"))
    minute_start = shanghai.replace(second=0, microsecond=0)
    return critical_trading_minutes(
        minute_start,
        minute_start + timedelta(minutes=1),
        calendar,
        paused_intervals=paused_intervals,
    ) == 1


def run_once(
    store: Any,
    transport: Any,
    worker_id: str,
    now: str | datetime,
    limit: int = 50,
    lease_seconds: int = 120,
    calendar: object | None = None,
    paused_intervals: object = (),
    cycle_id: str | None = None,
) -> WorkerResult:
    now_text = _utc_text(now, "now")
    active_calendar = (
        app_config.A_SHARE_HOLIDAYS_DEFAULT
        if calendar is None else calendar
    )
    pauses = tuple(paused_intervals or ())
    with store.transaction() as conn:
        store.probe_notification_write_failure(conn, now_text)
        store.enqueue_due_critical_reminders(
            conn,
            now_text,
            active_calendar,
            pauses,
        )
        store.cleanup_notifications(conn, now_text)
        reconcile_notification_capacity(
            store,
            conn,
            now=now_text,
            cycle_id=str(cycle_id or f"{worker_id}:{now_text}"),
        )
    cycle_now = _elapsed_clock(now_text)
    in_trading_minute = _in_trading_minute(
        now_text, active_calendar, pauses,
    )
    claimed = store.claim_notifications(
        worker_id=worker_id,
        now=now_text,
        limit=limit,
        lease_seconds=lease_seconds,
        skip_issue_reminders=not in_trading_minute,
    )
    counts = {
        "claimed": len(claimed), "sent": 0, "retried": 0, "dead": 0,
        "cancelled": 0, "ambiguous": 0, "skipped": 0,
    }
    for lease in claimed:
        row_now = cycle_now()
        row_instant = _instant(row_now)
        try:
            current = store.get_notification(lease.event_key)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            current = None
            if store.fail_notification(
                lease.event_key,
                worker_id,
                row_now,
                "CONTENT_DECODE_ERROR",
                "stored notification content could not be decoded",
                expected_lease_until=lease.lease_until,
                dead=True,
            ):
                counts["dead"] += 1
            else:
                counts["skipped"] += 1
            continue
        if not current or (
            current.state != "leased"
            or current.lease_owner != worker_id
            or current.lease_until != lease.lease_until
        ):
            counts["skipped"] += 1
            continue
        if _instant(current.lease_until) <= row_instant:
            counts["skipped"] += 1
            continue
        try:
            cancel_reason = _cancellation_reason(store, current, row_instant)
        except (TypeError, ValueError, OverflowError):
            if store.fail_notification(
                current.event_key,
                worker_id,
                row_now,
                "CONTENT_TIME_INVALID",
                "stored notification time could not be decoded",
                expected_lease_until=current.lease_until,
                dead=True,
            ):
                counts["dead"] += 1
            else:
                counts["skipped"] += 1
            continue
        if cancel_reason:
            if store.cancel_notification(
                current.event_key,
                row_now,
                cancel_reason,
                worker_id=worker_id,
                expected_lease_until=current.lease_until,
            ):
                counts["cancelled"] += 1
            else:
                counts["skipped"] += 1
            continue

        if _semantic_hash(current) != current.payload_sha256:
            if store.fail_notification(
                current.event_key,
                worker_id,
                row_now,
                "CONTENT_HASH_MISMATCH",
                "stored notification content failed integrity verification",
                expected_lease_until=current.lease_until,
                dead=True,
            ):
                counts["dead"] += 1
            else:
                counts["skipped"] += 1
            continue

        if current.attempt_count >= 5:
            if store.fail_notification(
                current.event_key,
                worker_id,
                row_now,
                "MAX_ATTEMPTS",
                "notification retry limit already reached",
                expected_lease_until=current.lease_until,
                dead=True,
            ):
                counts["dead"] += 1
            else:
                counts["skipped"] += 1
            continue

        begin_now = cycle_now()
        attempt = store.begin_notification_attempt(
            current.event_key,
            worker_id,
            current.lease_until,
            begin_now,
        )
        if attempt is None:
            try:
                refreshed = store.get_notification(current.event_key)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                refreshed = None
            refreshed_now = cycle_now()
            try:
                refreshed_reason = (
                    _cancellation_reason(
                        store, refreshed, _instant(refreshed_now),
                    )
                    if refreshed else ""
                )
            except (TypeError, ValueError, OverflowError):
                refreshed_reason = ""
                if refreshed and store.fail_notification(
                    refreshed.event_key,
                    worker_id,
                    refreshed_now,
                    "CONTENT_TIME_INVALID",
                    "stored notification time could not be decoded",
                    expected_lease_until=refreshed.lease_until,
                    dead=True,
                ):
                    counts["dead"] += 1
                    continue
            if refreshed and refreshed_reason and (
                refreshed.state == "leased"
                and refreshed.lease_owner == worker_id
                and refreshed.lease_until == current.lease_until
            ) and store.cancel_notification(
                refreshed.event_key,
                refreshed_now,
                refreshed_reason,
                worker_id=worker_id,
                expected_lease_until=refreshed.lease_until,
            ):
                counts["cancelled"] += 1
            else:
                counts["skipped"] += 1
            continue

        before_now = cycle_now()
        before_instant = _instant(before_now)
        try:
            before_http = store.get_notification(current.event_key)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            before_http = None
            if store.fail_notification(
                current.event_key,
                worker_id,
                before_now,
                "CONTENT_DECODE_ERROR",
                "stored notification content could not be decoded",
                expected_lease_until=current.lease_until,
                dead=True,
            ):
                counts["dead"] += 1
            else:
                counts["skipped"] += 1
            continue
        if not before_http or (
            before_http.state != "leased"
            or before_http.lease_owner != worker_id
            or before_http.lease_until != current.lease_until
        ):
            counts["skipped"] += 1
            continue
        if _instant(before_http.lease_until) <= before_instant:
            counts["skipped"] += 1
            continue
        try:
            before_cancel_reason = _cancellation_reason(
                store, before_http, before_instant,
            )
        except (TypeError, ValueError, OverflowError):
            if store.fail_notification(
                before_http.event_key,
                worker_id,
                before_now,
                "CONTENT_TIME_INVALID",
                "stored notification time could not be decoded",
                expected_lease_until=before_http.lease_until,
                dead=True,
            ):
                counts["dead"] += 1
            else:
                counts["skipped"] += 1
            continue
        if before_cancel_reason:
            if store.cancel_notification(
                before_http.event_key,
                before_now,
                before_cancel_reason,
                worker_id=worker_id,
                expected_lease_until=before_http.lease_until,
            ):
                counts["cancelled"] += 1
            else:
                counts["skipped"] += 1
            continue
        if _semantic_hash(before_http) != before_http.payload_sha256:
            if store.fail_notification(
                before_http.event_key,
                worker_id,
                before_now,
                "CONTENT_HASH_MISMATCH",
                "stored notification content failed integrity verification",
                expected_lease_until=before_http.lease_until,
                dead=True,
            ):
                counts["dead"] += 1
            else:
                counts["skipped"] += 1
            continue

        send_now = cycle_now()
        send_instant = _instant(send_now)
        if _instant(before_http.lease_until) <= send_instant:
            counts["skipped"] += 1
            continue
        try:
            send_cancel_reason = _cancellation_reason(
                store, before_http, send_instant,
            )
        except (TypeError, ValueError, OverflowError):
            send_cancel_reason = "CONTENT_TIME_INVALID"
        if send_cancel_reason:
            if send_cancel_reason == "CONTENT_TIME_INVALID":
                changed = store.fail_notification(
                    before_http.event_key,
                    worker_id,
                    send_now,
                    send_cancel_reason,
                    "stored notification time could not be decoded",
                    expected_lease_until=before_http.lease_until,
                    dead=True,
                )
                counts["dead" if changed else "skipped"] += 1
            elif store.cancel_notification(
                before_http.event_key,
                send_now,
                send_cancel_reason,
                worker_id=worker_id,
                expected_lease_until=before_http.lease_until,
            ):
                counts["cancelled"] += 1
            else:
                counts["skipped"] += 1
            continue

        if (
            before_http.event_type == "issue_reminder"
            and not _in_trading_minute(send_now, active_calendar, pauses)
        ):
            store.defer_notification_before_send(
                before_http.event_key,
                worker_id,
                send_now,
                expected_lease_until=before_http.lease_until,
                attempt_no=attempt,
            )
            counts["skipped"] += 1
            continue

        try:
            result = transport.deliver_markdown(
                before_http.title,
                _business_body(before_http),
                send_now,
            )
        except Exception:
            result = _UnexpectedDeliveryFailure()

        completed_now = cycle_now()
        completed_instant = _instant(completed_now)
        after_http_unreadable = False
        try:
            after_http = store.get_notification(current.event_key)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            after_http = None
            after_http_unreadable = True
        race_reason = ""
        if after_http_unreadable:
            race_reason = "POST_HTTP_STATE_UNREADABLE"
        elif _instant(current.lease_until) <= completed_instant:
            race_reason = "LEASE_EXPIRED_AFTER_HTTP"
        elif after_http:
            if (
                after_http.state != "leased"
                or after_http.lease_owner != worker_id
                or after_http.lease_until != current.lease_until
            ):
                race_reason = "LEASE_CHANGED_AFTER_HTTP"
            else:
                try:
                    after_reason = _cancellation_reason(
                        store, after_http, completed_instant,
                    )
                except (TypeError, ValueError, OverflowError):
                    after_reason = "POST_HTTP_STATE_INVALID"
                race_reason = {
                    "CANCEL_REQUESTED": "CANCEL_RACE_AFTER_HTTP",
                    "NOTIFICATION_EXPIRED": "EXPIRY_RACE_AFTER_HTTP",
                    "BUY_PLAN_REPLACED": "PLAN_RACE_AFTER_HTTP",
                    "POST_HTTP_STATE_INVALID": "POST_HTTP_STATE_INVALID",
                }.get(after_reason, "")
                if after_reason not in {
                    "", "CANCEL_REQUESTED", "NOTIFICATION_EXPIRED",
                    "BUY_PLAN_REPLACED", "POST_HTTP_STATE_INVALID",
                }:
                    race_reason = "CANCEL_RACE_AFTER_HTTP"
        ambiguous_result = bool(getattr(result, "ambiguous", False))
        ambiguity_reason = race_reason or (
            "RESPONSE_LOST" if ambiguous_result else ""
        )
        ambiguity_recorded = False
        if ambiguity_reason:
            ambiguity_recorded = store.record_notification_ambiguity(
                current.event_key,
                completed_now,
                ambiguity_reason,
                attempt,
            )
            counts["ambiguous"] += 1

        if bool(getattr(result, "ok", False)):
            if store.complete_notification(
                current.event_key,
                worker_id,
                send_now,
                expected_lease_until=current.lease_until,
                completed_at=completed_now,
            ):
                counts["sent"] += 1
            else:
                if not ambiguity_recorded:
                    store.record_notification_ambiguity(
                        current.event_key,
                        completed_now,
                        "SUCCESS_COMPLETION_CAS_LOST",
                        attempt,
                    )
                    counts["ambiguous"] += 1
            continue

        ambiguous = ambiguous_result
        permanent = bool(getattr(result, "permanent", False))
        error_code = _safe_error_code(result)
        if ambiguous:
            error_code = "AMBIGUOUS_DELIVERY"
        dead = permanent or attempt >= 5
        retry_at = None
        if not dead:
            delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, 3)]
            retry_at = (completed_instant + timedelta(seconds=delay)).isoformat()
        changed = store.fail_notification(
            current.event_key,
            worker_id,
            completed_now,
            error_code,
            f"notification delivery failed ({error_code})",
            expected_lease_until=current.lease_until,
            retry_at=retry_at,
            dead=dead,
        )
        if changed:
            counts["dead" if dead else "retried"] += 1
        else:
            if ambiguous and not ambiguity_recorded:
                store.record_notification_ambiguity(
                    current.event_key,
                    completed_now,
                    "FAILURE_COMPLETION_CAS_LOST",
                    attempt,
                )
            counts["skipped"] += 1
    return WorkerResult(**counts)


@dataclass(frozen=True)
class _UnexpectedDeliveryFailure:
    ok: bool = False
    permanent: bool = False
    ambiguous: bool = True
    error_code: str = "TRANSPORT_EXCEPTION"


def _file_snapshot(path: Path) -> tuple[bytes, str, bool]:
    digest = hashlib.sha256()
    retained = bytearray()
    oversized = False
    try:
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
                if not oversized and len(retained) + len(chunk) <= MAX_LEGACY_FILE_BYTES:
                    retained.extend(chunk)
                else:
                    oversized = True
                    retained.clear()
    except FileNotFoundError:
        pass
    return bytes(retained), digest.hexdigest(), oversized


def _legacy_entries(
    state_raw: bytes,
    queue_raw: bytes,
    *,
    state_sha: str,
    queue_sha: str,
    state_oversized: bool,
    queue_oversized: bool,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    if state_oversized:
        entries.append(_legacy_overflow_entry("state", state_sha))
    elif state_raw:
        try:
            state = json.loads(state_raw.decode("utf-8"))
            sent = state.get("sent", {}) if isinstance(state, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            sent = None
        if isinstance(sent, dict):
            for index, key in enumerate(sorted(str(item) for item in sent)):
                digest = hashlib.sha256(
                    f"state:{index}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}".encode()
                ).hexdigest()
                entries.append({
                    "source": "state", "digest": digest,
                    "state": "sent", "code": "LEGACY_SENT_TOMBSTONE",
                })
        else:
            entries.append(_legacy_entry("state", 0, state_raw, "cancelled", "LEGACY_INVALID_STATE"))
    if queue_oversized:
        entries.append(_legacy_overflow_entry("queue", queue_sha))
    for index, line in enumerate(() if queue_oversized else queue_raw.splitlines()):
        state = "cancelled"
        code = "LEGACY_UNPROVABLE"
        try:
            item = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            item = None
        if isinstance(item, dict):
            try:
                attempts = int(item.get("attempt_count") or 0)
            except (TypeError, ValueError):
                attempts = 0
            if (
                str(item.get("state") or "").lower() == "dead"
                or attempts >= 5
                or "errcode=40058" in str(item.get("error") or "")
            ):
                state = "dead"
                code = "LEGACY_DEAD"
        entries.append(_legacy_entry("queue", index, line, state, code))
    if len(entries) > MAX_LEGACY_DETAIL_ROWS:
        omitted = len(entries) - (MAX_LEGACY_DETAIL_ROWS - 1)
        entries = entries[:MAX_LEGACY_DETAIL_ROWS - 1]
        digest = hashlib.sha256(
            f"bounded:{state_sha}:{queue_sha}:{omitted}".encode()
        ).hexdigest()
        entries.append({
            "source": "bounded", "digest": digest,
            "state": "cancelled", "code": "LEGACY_DETAIL_LIMIT",
        })
    return entries


def _legacy_overflow_entry(source: str, digest: str) -> dict[str, str]:
    return {
        "source": source,
        "digest": hashlib.sha256(f"oversized:{source}:{digest}".encode()).hexdigest(),
        "state": "cancelled",
        "code": "LEGACY_FILE_TOO_LARGE",
    }


def _legacy_entry(
    source: str,
    index: int,
    raw: bytes,
    state: str,
    code: str,
) -> dict[str, str]:
    digest = hashlib.sha256(
        f"{source}:{index}:{hashlib.sha256(raw).hexdigest()}".encode()
    ).hexdigest()
    return {"source": source, "digest": digest, "state": state, "code": code}


def legacy_audit(
    store: Any,
    state_file: Path,
    queue_file: Path,
    now: str | datetime,
    *,
    dry_run: bool = False,
) -> LegacyAuditResult:
    now_text = _utc_text(now, "now")
    state_raw, state_sha, state_oversized = _file_snapshot(Path(state_file))
    queue_raw, queue_sha, queue_oversized = _file_snapshot(Path(queue_file))
    entries = _legacy_entries(
        state_raw,
        queue_raw,
        state_sha=state_sha,
        queue_sha=queue_sha,
        state_oversized=state_oversized,
        queue_oversized=queue_oversized,
    )
    counts = {
        state: sum(item["state"] == state for item in entries)
        for state in ("sent", "dead", "cancelled")
    }
    base = {
        "sent": counts["sent"],
        "dead": counts["dead"],
        "cancelled": counts["cancelled"],
        "imported": 0,
        "state_file_sha256": state_sha,
        "queue_file_sha256": queue_sha,
    }
    if dry_run:
        return LegacyAuditResult(
            completed=False, code="LEGACY_AUDIT_DRY_RUN", **base,
        )

    with store.transaction() as conn:
        marker = conn.execute(
            "SELECT value FROM system_state WHERE key=?",
            (LEGACY_AUDIT_MARKER,),
        ).fetchone()
        if marker is not None:
            return LegacyAuditResult(
                completed=False,
                code="LEGACY_AUDIT_ALREADY_COMPLETED",
                **base,
            )
        scope = conn.execute(
            """SELECT account_scope_id FROM account_scopes
               WHERE adapter='joinquant' AND scope_alias='primary'"""
        ).fetchone()
        if scope is None:
            raise RuntimeError("primary JoinQuant account scope is required")
        account_scope_id = str(scope[0])
        for item in entries:
            digest = item["digest"]
            safe_payload = {
                "audit_version": 1,
                "source": item["source"],
                "source_digest": digest,
                "classification": item["code"],
            }
            event_key = notification_event_key(
                "joinquant",
                account_scope_id,
                "control",
                control_event_id=(
                    f"legacy-notification-{item['source']}-{digest[:32]}"
                ),
            )
            state = item["state"]
            event = NotificationEvent(
                event_key=event_key,
                account_scope_id=account_scope_id,
                adapter="joinquant",
                event_type="legacy-audit",
                object_type="legacy_notification_audit",
                object_id=digest,
                source_fact_id=f"legacy:{digest}",
                priority="normal",
                payload_version=1,
                occurred_at=now_text,
                expires_at=None,
                title="Legacy notification audit",
                body=f"Classification: {item['code']}",
                payload=safe_payload,
                metadata={"audit_only": True},
            )
            store.enqueue_notification(conn, event, now_text)
            conn.execute(
                """UPDATE notification_outbox
                   SET state=?, payload_json=NULL, title=NULL, body=NULL,
                       body_sha256=NULL, metadata_json=NULL,
                       sent_at=?, terminal_at=?, last_error_code=?
                   WHERE event_key=? AND state='pending'""",
                (
                    state,
                    now_text if state == "sent" else None,
                    now_text,
                    None if state == "sent" else item["code"],
                    event_key,
                ),
            )
        # The final state rewrite can create a new dead detail after the
        # per-event enqueue cleanup.  Re-run the shared bounded cleanup before
        # the one-time audit marker is committed.
        store.cleanup_notifications(conn, now_text)
        marker_value = json.dumps({
            "version": 1,
            "completed_at": now_text,
            "state_file_sha256": state_sha,
            "queue_file_sha256": queue_sha,
            **counts,
            "imported": 0,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        store.set_system_state(
            conn,
            LEGACY_AUDIT_MARKER,
            marker_value,
            "explicit one-time notification legacy audit",
        )
    return LegacyAuditResult(
        completed=True, code="LEGACY_AUDIT_COMPLETED", **base,
    )


def notification_status(store: Any, conn: Any | None = None) -> dict[str, object]:
    owned = conn is None
    if conn is None:
        conn = store.connect()
    try:
        states = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT state, COUNT(*) FROM notification_outbox GROUP BY state"
            )
        }
        capacity = store.notification_capacity(conn)
        marker_row = conn.execute(
            "SELECT value FROM system_state WHERE key=?",
            ("notification_outbox_write_failure",),
        ).fetchone()
        marker_text = str(marker_row[0] or "") if marker_row is not None else ""
        marker: dict[str, object] = {}
        if marker_text:
            try:
                parsed = json.loads(marker_text)
            except (TypeError, ValueError, RecursionError):
                parsed = {}
            if isinstance(parsed, dict):
                marker = parsed
        return {
            "pending": states.get("pending", 0),
            "leased": states.get("leased", 0),
            "sent": states.get("sent", 0),
            "dead": states.get("dead", 0),
            "cancelled": states.get("cancelled", 0),
            "unresolved_gaps": capacity.unresolved_gap_rows,
            "high_unresolved_gaps": capacity.high_unresolved_gap_rows,
            "tombstones": capacity.tombstone_rows,
            "normal_active_rows": capacity.normal_active_rows,
            "normal_active_bytes": capacity.normal_active_bytes,
            "high_active_rows": capacity.high_active_rows,
            "high_active_bytes": capacity.high_active_bytes,
            "dead_detail_rows": capacity.dead_detail_rows,
            "dead_detail_bytes": capacity.dead_detail_bytes,
            "high_dead_detail_rows": capacity.high_dead_rows,
            "high_dead_total_rows": capacity.high_dead_total_rows,
            "write_failure_marker": bool(marker_text),
            "write_failure_requires_manual_resolution": bool(
                marker.get("requires_manual_resolution")
            ),
        }
    finally:
        if owned:
            conn.close()


def compact_notifications(
    store: Any,
    now: str | datetime,
    *,
    apply: bool = False,
) -> dict[str, object]:
    now_text = _utc_text(now, "now")
    with store.transaction() as conn:
        before = notification_status(store, conn)
        if not apply:
            conn.execute("SAVEPOINT notification_compact_preview")
        changes = store.cleanup_notifications(conn, now_text)
        after = notification_status(store, conn)
        if not apply:
            conn.execute("ROLLBACK TO notification_compact_preview")
            conn.execute("RELEASE notification_compact_preview")
    return {
        "applied": apply,
        "changes": changes,
        "before": before,
        "after": after,
    }


def _json_value(value: Any) -> object:
    if is_dataclass(value):
        return asdict(value)
    return value


def _require_current_store(store: Any) -> None:
    health = store.health()
    if not health.ok:
        raise RuntimeError(
            f"trading database schema {health.schema_version} is unavailable"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deliver and maintain the transactional notification outbox",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--legacy-audit", action="store_true")
    mode.add_argument("--compact-dry-run", action="store_true")
    mode.add_argument("--compact-apply", action="store_true")
    mode.add_argument("--resolve-write-failure", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db-file", type=Path, default=app_config.TRADING_DB_FILE)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=app_config.CACHE_DIR / "wecom_notify_state.json",
    )
    parser.add_argument(
        "--queue-file",
        type=Path,
        default=app_config.CACHE_DIR / "notify_failed_queue.jsonl",
    )
    parser.add_argument("--worker-id")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--expected-event-key")
    parser.add_argument("--reason")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.apply and not args.legacy_audit:
        parser.error("--apply is only valid with --legacy-audit")
    if args.resolve_write_failure and (
        not args.expected_event_key or not args.reason
    ):
        parser.error(
            "--resolve-write-failure requires --expected-event-key and --reason"
        )
    if not args.resolve_write_failure and (
        args.expected_event_key or args.reason
    ):
        parser.error(
            "--expected-event-key and --reason are only valid with "
            "--resolve-write-failure"
        )

    from trading_store import TradingStore

    now = datetime.now(timezone.utc)
    store = TradingStore(args.db_file)
    if args.legacy_audit and not args.apply:
        result: object = legacy_audit(
            store,
            args.state_file,
            args.queue_file,
            now,
            dry_run=True,
        )
    else:
        _require_current_store(store)
        if args.resolve_write_failure:
            with store.transaction() as conn:
                resolved = store.resolve_notification_write_failure(
                    conn,
                    args.expected_event_key,
                    args.reason,
                )
            result = {
                "event_key": args.expected_event_key,
                "resolved": resolved,
            }
        elif args.once:
            from notifier import WeComNotifier

            worker_id = args.worker_id or f"{socket.gethostname()}:{os.getpid()}"
            result = run_once(
                store,
                WeComNotifier(app_config.WECOM_WEBHOOK_URL),
                worker_id,
                now,
                limit=args.limit,
                lease_seconds=args.lease_seconds,
            )
        elif args.status:
            result = notification_status(store)
        elif args.legacy_audit:
            result = legacy_audit(
                store,
                args.state_file,
                args.queue_file,
                now,
            )
        else:
            result = compact_notifications(
                store,
                now,
                apply=args.compact_apply,
            )
    print(json.dumps(_json_value(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
