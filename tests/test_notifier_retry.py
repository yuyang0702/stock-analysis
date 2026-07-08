import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from notifier import WeComNotifier, retry_failed_notifications


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class NotifierRetryTest(unittest.TestCase):
    def test_failed_markdown_send_is_queued_and_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue_file = base / "notify_failed_queue.jsonl"
            state_file = base / "state.json"
            notifier = WeComNotifier(
                "https://example.invalid/webhook",
                state_file,
                retry_queue_file=queue_file,
                cooldown_sec=0,
            )

            with patch("notifier.requests.post", side_effect=requests.RequestException("offline")):
                self.assertFalse(notifier.send_markdown("Title", "Content", dedupe_key="k1"))

            queued = [json.loads(line) for line in queue_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(queued[0]["title"], "Title")
            self.assertEqual(queued[0]["dedupe_key"], "k1")

            with patch("notifier.requests.post", return_value=FakeResponse({"errcode": 0})):
                sent = retry_failed_notifications(
                    "https://example.invalid/webhook",
                    queue_file,
                    state_file,
                    cooldown_sec=0,
                )

            self.assertEqual(sent, 1)
            self.assertFalse(queue_file.exists())


if __name__ == "__main__":
    unittest.main()
