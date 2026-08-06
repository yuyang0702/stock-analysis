import json
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import notify_retry
from notifier import WeComNotifier, retry_failed_notifications


class FakeResponse:
    def __init__(self, payload: dict | Exception, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}", response=self,
            )

    def json(self) -> dict:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class NotifierTransportTest(unittest.TestCase):
    def test_deliver_rejects_naive_send_time_before_http(self) -> None:
        notifier = WeComNotifier("https://example.invalid/webhook")
        with patch("notifier.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "timezone"):
                notifier.deliver_markdown(
                    "Title", "Body", "2026-07-28T10:00:00",
                )
        post.assert_not_called()

    def test_deliver_is_stateless_utf8_bounded_and_uses_explicit_send_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            notifier = WeComNotifier(
                "https://example.invalid/webhook",
                base / "missing" / "state.json",
                retry_queue_file=base / "missing" / "queue.jsonl",
            )
            sent_at = datetime(2026, 8, 2, 9, 31, tzinfo=timezone.utc)
            with patch(
                "notifier.requests.post",
                return_value=FakeResponse({"errcode": 0}),
            ) as post:
                result = notifier.deliver_markdown(
                    "长消息", "测试内容" * 2000, sent_at,
                )

            self.assertTrue(result.ok)
            content = post.call_args.kwargs["json"]["markdown"]["content"]
            self.assertLessEqual(len(content.encode("utf-8")), 4000)
            self.assertIn(
                "实际发送时间：2026-08-02 17:31:00 Asia/Shanghai",
                content,
            )
            self.assertIn("内容已截断", content)
            self.assertFalse((base / "missing").exists())

    def test_compatibility_wrapper_warns_and_does_not_dedupe_or_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            notifier = WeComNotifier(
                "https://example.invalid/webhook",
                base / "state.json",
                retry_queue_file=base / "queue.jsonl",
            )
            with patch(
                "notifier.requests.post",
                side_effect=[
                    FakeResponse({"errcode": 0}),
                    FakeResponse({"errcode": 0}),
                ],
            ) as post:
                with self.assertWarns(DeprecationWarning):
                    self.assertTrue(
                        notifier.send_markdown("Title", "Body", dedupe_key="same"),
                    )
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    self.assertTrue(
                        notifier.send_markdown("Title", "Body", dedupe_key="same"),
                    )

            self.assertEqual(post.call_count, 2)
            self.assertFalse((base / "state.json").exists())
            self.assertFalse((base / "queue.jsonl").exists())

    def test_wecom_40058_is_a_permanent_nonambiguous_failure(self) -> None:
        notifier = WeComNotifier("https://example.invalid/webhook", Path("unused"))
        with patch(
            "notifier.requests.post",
            return_value=FakeResponse({"errcode": 40058}),
        ):
            result = notifier.deliver_markdown(
                "Title", "Body", "2026-08-02T09:31:00+08:00",
            )

        self.assertFalse(result.ok)
        self.assertTrue(result.permanent)
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.error_code, "WECOM_40058")

    def test_http_4xx_is_permanent_except_408_and_429(self) -> None:
        notifier = WeComNotifier("https://example.invalid/webhook", Path("unused"))
        for status, permanent in ((400, True), (408, False), (429, False), (500, False)):
            with self.subTest(status=status):
                with patch(
                    "notifier.requests.post",
                    return_value=FakeResponse({}, status),
                ):
                    result = notifier.deliver_markdown(
                        "Title", "Body", "2026-08-02T09:31:00+08:00",
                    )
                self.assertEqual(result.permanent, permanent)
                self.assertFalse(result.ambiguous)
                self.assertEqual(result.error_code, f"HTTP_{status}")

    def test_missing_response_is_marked_ambiguous(self) -> None:
        notifier = WeComNotifier("https://example.invalid/webhook", Path("unused"))
        with patch(
            "notifier.requests.post",
            side_effect=requests.ConnectionError(
                "response lost https://example.invalid/webhook?key=secret",
            ),
        ):
            result = notifier.deliver_markdown(
                "Title", "Body", "2026-08-02T09:31:00+08:00",
            )

        self.assertFalse(result.ok)
        self.assertFalse(result.permanent)
        self.assertTrue(result.ambiguous)
        self.assertEqual(result.error_code, "TRANSPORT_ERROR")
        self.assertNotIn("secret", result.error)

    def test_invalid_success_response_is_marked_ambiguous(self) -> None:
        notifier = WeComNotifier("https://example.invalid/webhook", Path("unused"))
        with patch(
            "notifier.requests.post",
            return_value=FakeResponse(ValueError("not json")),
        ):
            result = notifier.deliver_markdown(
                "Title", "Body", "2026-08-02T09:31:00+08:00",
            )

        self.assertFalse(result.ok)
        self.assertTrue(result.ambiguous)
        self.assertEqual(result.error_code, "INVALID_RESPONSE")

    def test_legacy_retry_function_is_a_safe_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue.jsonl"
            original = json.dumps({"title": "Old", "content": "Never replay"}) + "\n"
            queue.write_text(original, encoding="utf-8")
            with patch("notifier.requests.post") as post:
                with self.assertWarns(DeprecationWarning):
                    sent = retry_failed_notifications(queue_file=queue)

            self.assertEqual(sent, 0)
            self.assertEqual(queue.read_text(encoding="utf-8"), original)
            post.assert_not_called()


class NotifyRetryCliTest(unittest.TestCase):
    def test_dry_run_does_not_create_or_migrate_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db_file = base / "missing" / "trading.db"
            result = notify_retry.run_legacy_audit(
                base / "state.json",
                base / "queue.jsonl",
                db_file,
                dry_run=True,
            )
            self.assertEqual(result.code, "LEGACY_AUDIT_DRY_RUN")
            self.assertFalse(db_file.exists())

    def test_default_command_is_a_safe_noop(self) -> None:
        with patch("builtins.print") as output:
            notify_retry.main([])

        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["code"], "LEGACY_JSON_REPLAY_DISABLED")
        self.assertEqual(payload["sent"], 0)

    def test_legacy_audit_requires_explicit_flag(self) -> None:
        result = SimpleNamespace(
            completed=True, code="LEGACY_AUDIT_COMPLETED",
        )
        with patch("notify_retry.run_legacy_audit", return_value=result) as audit:
            with patch("builtins.print") as output:
                notify_retry.main(["--legacy-audit", "--dry-run"])

        audit.assert_called_once()
        self.assertTrue(audit.call_args.kwargs["dry_run"])
        payload = json.loads(output.call_args.args[0])
        self.assertTrue(payload["completed"])
        self.assertEqual(payload["code"], "LEGACY_AUDIT_COMPLETED")


if __name__ == "__main__":
    unittest.main()
