from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

import config as app_config

WECOM_MARKDOWN_MAX_BYTES = 4000
RETRY_MAX_ATTEMPTS = 5
RETRY_MAX_ITEMS = 100
RETRY_RETENTION_DAYS = 30
RETRY_BACKOFF_SECONDS = (300, 900, 1800, 3600)


def _server_time_text() -> str:
    now = datetime.now().astimezone()
    zone = getattr(now.tzinfo, "key", None) or now.tzname() or "local"
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {zone}"


def _render_timed_content(content: str) -> str:
    return f"> 服务器时间：{_server_time_text()}\n{content}"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _render_markdown(title: str, content: str) -> str:
    prefix = f"### {title}\n> 服务器时间：{_server_time_text()}\n"
    rendered = prefix + content
    if len(rendered.encode("utf-8")) <= WECOM_MARKDOWN_MAX_BYTES:
        return rendered
    suffix = "\n> 内容已截断"
    available = (
        WECOM_MARKDOWN_MAX_BYTES
        - len(prefix.encode("utf-8"))
        - len(suffix.encode("utf-8"))
    )
    return prefix + _truncate_utf8(content, available) + suffix


@dataclass
class NotifyState:
    sent: dict[str, float]


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    error: str = ""
    error_kind: str = ""
    permanent: bool = False
    already_sent: bool = False


class WeComNotifier:
    """企业微信机器人通知封装，主流程失败时不受影响。"""

    def __init__(
        self,
        webhook_url: str | None,
        state_file: Path,
        cooldown_sec: int = app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
        timeout_sec: int = app_config.WECOM_TIMEOUT_SEC,
        retry_queue_file: Path | None = None,
    ):
        self.webhook_url = webhook_url.strip() if webhook_url else ""
        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.retry_queue_file = retry_queue_file or app_config.CACHE_DIR / "notify_failed_queue.jsonl"
        self.cooldown_sec = cooldown_sec
        self.timeout_sec = timeout_sec
        self.state = self._load_state()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def _load_state(self) -> NotifyState:
        if not self.state_file.exists():
            return NotifyState(sent={})
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            sent = raw.get("sent", {})
            return NotifyState(sent={str(k): float(v) for k, v in sent.items()})
        except Exception:
            return NotifyState(sent={})

    def _save_state(self) -> None:
        payload = {"sent": self.state.sent}
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def _expired(self, last_ts: float) -> bool:
        return (time.time() - last_ts) >= self.cooldown_sec

    def should_send(self, key: str | None) -> bool:
        if not key:
            return True
        last_ts = self.state.sent.get(key)
        if last_ts is None:
            return True
        return self._expired(last_ts)

    def mark_sent(self, key: str | None) -> None:
        if not key:
            return
        self.state.sent[key] = time.time()
        self._save_state()

    def _queue_failed(
        self,
        title: str,
        content: str,
        dedupe_key: str | None,
        result: DeliveryResult,
    ) -> None:
        if not self.webhook_url:
            return
        self.retry_queue_file.parent.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "created_at": created_at,
            "title": title,
            "content": content,
            "dedupe_key": dedupe_key,
            "state": "dead" if result.permanent else "pending",
            "attempt_count": 1,
            "next_retry_at": created_at,
            "last_attempt_at": created_at,
            "error_kind": result.error_kind or "temporary",
            "error": result.error[:160],
        }
        rows = _bounded_retry_rows(
            _read_retry_queue(self.retry_queue_file) + [payload],
            datetime.now(),
        )
        _write_retry_queue(self.retry_queue_file, rows)

    def _deliver(
        self, title: str, content: str, dedupe_key: str | None = None
    ) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(False, "notifier disabled", "disabled")
        if not self.should_send(dedupe_key):
            return DeliveryResult(False, already_sent=True)

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": _render_markdown(title, content),
            },
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout_sec)
            resp.raise_for_status()
            data: Any = resp.json()
            if data.get("errcode", 1) == 0:
                self.mark_sent(dedupe_key)
                return DeliveryResult(True)
            errcode = data.get("errcode")
            permanent = errcode == 40058
            return DeliveryResult(
                False,
                f"errcode={errcode}",
                "permanent" if permanent else "wecom",
                permanent=permanent,
            )
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            permanent = bool(status and 400 <= int(status) < 500 and int(status) not in {408, 429})
            return DeliveryResult(
                False,
                str(exc),
                "permanent" if permanent else "temporary",
                permanent=permanent,
            )

    def send_markdown(self, title: str, content: str, dedupe_key: str | None = None) -> bool:
        result = self._deliver(title, content, dedupe_key)
        if not result.ok and not result.already_sent:
            self._queue_failed(title, content, dedupe_key, result)
        return result.ok


def _read_retry_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _write_retry_queue(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    tmp.replace(path)


def _parse_queue_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _positive_int(value: object, default: int = 1) -> int:
    try:
        return max(1, int(value or default))
    except (TypeError, ValueError):
        return default


def _normalize_retry_row(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    item = dict(row)
    created_at = str(item.get("created_at") or now.strftime("%Y-%m-%d %H:%M:%S"))
    legacy_40058 = "errcode=40058" in str(item.get("error") or "")
    item.update({
        "created_at": created_at,
        "state": str(item.get("state") or ("dead" if legacy_40058 else "pending")),
        "attempt_count": _positive_int(item.get("attempt_count")),
        "next_retry_at": str(item.get("next_retry_at") or created_at),
        "last_attempt_at": str(item.get("last_attempt_at") or created_at),
        "error_kind": str(item.get("error_kind") or ("permanent" if legacy_40058 else "temporary")),
        "error": str(item.get("error") or "")[:160],
    })
    return item


def _bounded_retry_rows(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=RETRY_RETENTION_DAYS)
    normalized = [
        _normalize_retry_row(row, now)
        for row in rows
        if (_parse_queue_time(row.get("created_at")) or now) >= cutoff
    ]
    return normalized[-RETRY_MAX_ITEMS:]


def _next_retry_at(now: datetime, attempt_count: int) -> str:
    index = min(max(attempt_count - 2, 0), len(RETRY_BACKOFF_SECONDS) - 1)
    return (now + timedelta(seconds=RETRY_BACKOFF_SECONDS[index])).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def retry_failed_notifications(
    webhook_url: str | None = None,
    queue_file: Path | None = None,
    state_file: Path | None = None,
    *,
    cooldown_sec: int = app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
    timeout_sec: int = app_config.WECOM_TIMEOUT_SEC,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now()
    queue_file = queue_file or app_config.CACHE_DIR / "notify_failed_queue.jsonl"
    state_file = state_file or app_config.CACHE_DIR / "wecom_notify_state.json"
    notifier = WeComNotifier(
        webhook_url or app_config.WECOM_WEBHOOK_URL,
        state_file,
        cooldown_sec=cooldown_sec,
        timeout_sec=timeout_sec,
        retry_queue_file=queue_file,
    )
    pending = _bounded_retry_rows(_read_retry_queue(queue_file), now)
    remaining: list[dict[str, Any]] = []
    sent = 0
    for item in pending:
        if item.get("state") == "dead":
            remaining.append(item)
            continue
        next_retry = _parse_queue_time(item.get("next_retry_at"))
        if next_retry is not None and next_retry > now:
            remaining.append(item)
            continue
        result = notifier._deliver(
            str(item.get("title") or ""),
            str(item.get("content") or ""),
            item.get("dedupe_key"),
        )
        if result.ok:
            sent += 1
            continue
        if result.already_sent:
            continue
        attempts = _positive_int(item.get("attempt_count")) + 1
        item.update({
            "attempt_count": attempts,
            "last_attempt_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "error": result.error[:160],
            "error_kind": result.error_kind or "temporary",
        })
        if result.permanent or attempts >= RETRY_MAX_ATTEMPTS:
            item["state"] = "dead"
            item["error_kind"] = "permanent" if result.permanent else "max_attempts"
        else:
            item["state"] = "pending"
            item["next_retry_at"] = _next_retry_at(now, attempts)
        remaining.append(item)
    _write_retry_queue(queue_file, _bounded_retry_rows(remaining, now))
    return sent
