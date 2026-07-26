import json
import tempfile
import unittest
from datetime import datetime
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
    def test_utf8_markdown_payload_is_bounded_and_truncated_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notifier = WeComNotifier(
                "https://example.invalid/webhook",
                Path(tmp) / "state.json",
                cooldown_sec=0,
            )
            with patch("notifier.requests.post", return_value=FakeResponse({"errcode": 0})) as post:
                self.assertTrue(notifier.send_markdown("长消息", "测试内容" * 2000))

            content = post.call_args.kwargs["json"]["markdown"]["content"]
            self.assertLessEqual(len(content.encode("utf-8")), 4000)
            self.assertIn("内容已截断", content)
            content.encode("utf-8").decode("utf-8")

    def test_wecom_40058_is_dead_and_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = base / "queue.jsonl"
            notifier = WeComNotifier(
                "https://example.invalid/webhook",
                base / "state.json",
                retry_queue_file=queue,
                cooldown_sec=0,
            )
            with patch("notifier.requests.post", return_value=FakeResponse({"errcode": 40058})):
                self.assertFalse(notifier.send_markdown("Title", "Body", "bad-content"))
            row = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["state"], "dead")
            self.assertEqual(row["error_kind"], "permanent")

            with patch("notifier.requests.post") as post:
                self.assertEqual(retry_failed_notifications(
                    "https://example.invalid/webhook", queue, base / "state.json", cooldown_sec=0,
                ), 0)
            post.assert_not_called()

    def test_legacy_40058_queue_row_is_upgraded_to_dead_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = base / "queue.jsonl"
            queue.write_text(json.dumps({
                "created_at": "2026-07-21 15:30:00",
                "title": "Old report",
                "content": "Old content",
                "dedupe_key": "old-40058",
                "error": "errcode=40058",
            }) + "\n", encoding="utf-8")

            with patch("notifier.requests.post") as post:
                retry_failed_notifications(
                    "https://example.invalid/webhook",
                    queue,
                    base / "state.json",
                    now=datetime(2026, 7, 26, 10, 0),
                )

            post.assert_not_called()
            row = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["state"], "dead")
            self.assertEqual(row["error_kind"], "permanent")

    def test_fifth_temporary_failure_moves_item_to_dead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = base / "queue.jsonl"
            queue.write_text(json.dumps({
                "created_at": "2026-07-26 09:30:00",
                "title": "Title",
                "content": "Body",
                "dedupe_key": "retry-5",
                "state": "pending",
                "attempt_count": 4,
                "next_retry_at": "2026-07-26 09:30:00",
                "error": "offline",
            }) + "\n", encoding="utf-8")
            with patch("notifier.requests.post", side_effect=requests.RequestException("offline")):
                retry_failed_notifications(
                    "https://example.invalid/webhook",
                    queue,
                    base / "state.json",
                    cooldown_sec=0,
                    now=datetime(2026, 7, 26, 10, 0),
                )
            row = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["state"], "dead")
            self.assertEqual(row["attempt_count"], 5)

    def test_queue_is_bounded_to_recent_one_hundred_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            queue = base / "queue.jsonl"
            rows = [
                {
                    "created_at": f"2026-07-{(index % 20) + 1:02d} 09:30:00",
                    "title": str(index),
                    "content": "Body",
                    "state": "dead",
                    "attempt_count": 5,
                }
                for index in range(130)
            ]
            queue.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )

            retry_failed_notifications(
                "https://example.invalid/webhook",
                queue,
                base / "state.json",
                cooldown_sec=0,
                now=datetime(2026, 7, 26, 10, 0),
            )

            retained = queue.read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(len(retained), 100)

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
