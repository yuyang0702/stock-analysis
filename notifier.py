from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import config as app_config

WECOM_MARKDOWN_MAX_BYTES = 4000


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _sent_at_text(sent_at: datetime | str) -> str:
    value = sent_at
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError("sent_at must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sent_at must include a timezone")
    value = value.astimezone(ZoneInfo("Asia/Shanghai"))
    zone = getattr(value.tzinfo, "key", None) or value.tzname() or "local"
    return f"{value.strftime('%Y-%m-%d %H:%M:%S')} {zone}"


def _render_markdown(title: str, body: str, sent_at: datetime | str) -> str:
    title = _truncate_utf8(str(title), 512)
    sent_text = _truncate_utf8(_sent_at_text(sent_at), 128)
    prefix = f"### {title}\n> 实际发送时间：{sent_text}\n"
    body = str(body)
    rendered = prefix + body
    if len(rendered.encode("utf-8")) <= WECOM_MARKDOWN_MAX_BYTES:
        return rendered
    suffix = "\n> 内容已截断"
    available = (
        WECOM_MARKDOWN_MAX_BYTES
        - len(prefix.encode("utf-8"))
        - len(suffix.encode("utf-8"))
    )
    return prefix + _truncate_utf8(body, available) + suffix


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    error: str = ""
    error_kind: str = ""
    error_code: str = ""
    permanent: bool = False
    ambiguous: bool = False
    already_sent: bool = False


class WeComNotifier:
    """Stateless Enterprise WeChat markdown transport."""

    def __init__(
        self,
        webhook_url: str | None,
        state_file: Path | None = None,
        cooldown_sec: int = app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
        timeout_sec: int = app_config.WECOM_TIMEOUT_SEC,
        retry_queue_file: Path | None = None,
    ):
        self.webhook_url = webhook_url.strip() if webhook_url else ""
        self.timeout_sec = timeout_sec
        # Compatibility-only arguments. SQLite owns delivery state in Batch B.
        _ = state_file, cooldown_sec, retry_queue_file

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    @staticmethod
    def _http_failure(exc: Exception) -> DeliveryResult:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None:
            return DeliveryResult(
                False,
                type(exc).__name__,
                "temporary",
                "TRANSPORT_ERROR",
                ambiguous=True,
            )
        status = int(status)
        permanent = 400 <= status < 500 and status not in {408, 429}
        return DeliveryResult(
            False,
            f"http status {status}",
            "permanent" if permanent else "temporary",
            f"HTTP_{status}",
            permanent=permanent,
        )

    def deliver_markdown(
        self,
        title: str,
        body: str,
        sent_at: datetime | str,
    ) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(
                False,
                "notifier disabled",
                "disabled",
                "NOTIFIER_DISABLED",
            )
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": _render_markdown(title, body, sent_at)},
        }
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return self._http_failure(exc)
        except Exception as exc:
            return DeliveryResult(
                False,
                type(exc).__name__,
                "temporary",
                "TRANSPORT_ERROR",
                ambiguous=True,
            )

        try:
            data = response.json()
            errcode = data.get("errcode", 1)
        except Exception:
            return DeliveryResult(
                False,
                "invalid response",
                "temporary",
                "INVALID_RESPONSE",
                ambiguous=True,
            )
        if errcode == 0:
            return DeliveryResult(True)
        code = _truncate_utf8(str(errcode), 64)
        error_code = f"WECOM_{code}"
        permanent = code == "40058"
        return DeliveryResult(
            False,
            f"errcode={code}",
            "permanent" if permanent else "wecom",
            error_code,
            permanent=permanent,
        )

    def send_markdown(
        self,
        title: str,
        content: str,
        dedupe_key: str | None = None,
    ) -> bool:
        warnings.warn(
            "send_markdown() is deprecated; enqueue a notification event instead",
            DeprecationWarning,
            stacklevel=2,
        )
        _ = dedupe_key
        return self.deliver_markdown(
            title,
            content,
            datetime.now().astimezone(),
        ).ok


def retry_failed_notifications(
    webhook_url: str | None = None,
    queue_file: Path | None = None,
    state_file: Path | None = None,
    *,
    cooldown_sec: int = app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
    timeout_sec: int = app_config.WECOM_TIMEOUT_SEC,
    now: datetime | None = None,
) -> int:
    """Compatibility no-op: legacy JSON rows must never be replayed."""
    warnings.warn(
        "legacy JSON notification retry is disabled; use explicit legacy audit",
        DeprecationWarning,
        stacklevel=2,
    )
    _ = webhook_url, queue_file, state_file, cooldown_sec, timeout_sec, now
    return 0
