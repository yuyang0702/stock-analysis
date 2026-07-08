from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

import config as app_config


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
    ):
        self.webhook_url = webhook_url.strip() if webhook_url else ""
        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
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

    def send_markdown(self, title: str, content: str, dedupe_key: str | None = None) -> bool:
        if not self.enabled:
            return False
        if not self.should_send(dedupe_key):
            return False

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### {title}\n{content}",
            },
        }

        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout_sec)
            resp.raise_for_status()
            data: Any = resp.json()
            if data.get("errcode", 1) == 0:
                self.mark_sent(dedupe_key)
                return True
            return False
        except Exception:
            return False
