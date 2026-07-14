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

            with patch("notifier._server_time_text", return_value="2026-07-14 18:32:00 Asia/Shanghai"):
                with patch("notifier.requests.post", return_value=FakeResponse({"errcode": 0})) as post:
                    sent = retry_failed_notifications(
                        "https://example.invalid/webhook",
                        queue_file,
                        state_file,
                        cooldown_sec=0,
                    )

            self.assertEqual(sent, 1)
            self.assertFalse(queue_file.exists())
            retry_content = post.call_args.kwargs["json"]["markdown"]["content"]
            self.assertEqual(retry_content.count("服务器时间："), 1)
            self.assertIn("服务器时间：2026-07-14 18:32:00 Asia/Shanghai", retry_content)

    def test_send_adds_exactly_one_current_server_time_without_changing_queue_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue_file = base / "queue.jsonl"
            notifier = WeComNotifier(
                "https://example.invalid/webhook",
                base / "state.json",
                retry_queue_file=queue_file,
                cooldown_sec=0,
            )
            with patch("notifier._server_time_text", return_value="2026-07-14 18:30:00 Asia/Shanghai"):
                with patch("notifier.requests.post", return_value=FakeResponse({"errcode": 0})) as post:
                    self.assertTrue(notifier.send_markdown("Title", "Body", dedupe_key="time-1"))
            content = post.call_args.kwargs["json"]["markdown"]["content"]
            self.assertEqual(content.count("服务器时间："), 1)
            self.assertIn("服务器时间：2026-07-14 18:30:00 Asia/Shanghai", content)

            with patch("notifier._server_time_text", return_value="2026-07-14 18:31:00 Asia/Shanghai"):
                with patch("notifier.requests.post", side_effect=requests.RequestException("offline")):
                    self.assertFalse(notifier.send_markdown("Title", "Body", dedupe_key="time-2"))
            queued = json.loads(queue_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(queued["content"], "Body")


if __name__ == "__main__":
    unittest.main()
