from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

import config as app_config


def _server_time_text() -> str:
    now = datetime.now().astimezone()
    zone = getattr(now.tzinfo, "key", None) or now.tzname() or "local"
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {zone}"


def _render_timed_content(content: str) -> str:
    return f"> 服务器时间：{_server_time_text()}\n{content}"


@dataclass
class NotifyState:
    sent: dict[str, float]


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

    def _queue_failed(self, title: str, content: str, dedupe_key: str | None, error: str) -> None:
        if not self.webhook_url:
            return
        self.retry_queue_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": title,
            "content": content,
            "dedupe_key": dedupe_key,
            "error": error[:160],
        }
        with self.retry_queue_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def send_markdown(self, title: str, content: str, dedupe_key: str | None = None) -> bool:
        if not self.enabled:
            return False
        if not self.should_send(dedupe_key):
            return False

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### {title}\n{_render_timed_content(content)}",
            },
        }

        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout_sec)
            resp.raise_for_status()
            data: Any = resp.json()
            if data.get("errcode", 1) == 0:
                self.mark_sent(dedupe_key)
                return True
            self._queue_failed(title, content, dedupe_key, f"errcode={data.get('errcode')}")
            return False
        except Exception as exc:
            self._queue_failed(title, content, dedupe_key, str(exc))
            return False


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


def retry_failed_notifications(
    webhook_url: str | None = None,
    queue_file: Path | None = None,
    state_file: Path | None = None,
    *,
    cooldown_sec: int = app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
    timeout_sec: int = app_config.WECOM_TIMEOUT_SEC,
) -> int:
    queue_file = queue_file or app_config.CACHE_DIR / "notify_failed_queue.jsonl"
    state_file = state_file or app_config.CACHE_DIR / "wecom_notify_state.json"
    notifier = WeComNotifier(
        webhook_url or app_config.WECOM_WEBHOOK_URL,
        state_file,
        cooldown_sec=cooldown_sec,
        timeout_sec=timeout_sec,
        retry_queue_file=queue_file,
    )
    pending = _read_retry_queue(queue_file)
    remaining: list[dict[str, Any]] = []
    sent = 0
    for item in pending:
        if notifier.send_markdown(str(item.get("title") or ""), str(item.get("content") or ""), item.get("dedupe_key")):
            sent += 1
        else:
            remaining.append(item)
    _write_retry_queue(queue_file, remaining)
    return sent
