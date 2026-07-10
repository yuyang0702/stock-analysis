import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import joinquant_signal_server


class JoinQuantSignalServerTest(unittest.TestCase):
    def test_rejects_bad_token_and_serves_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal_file = Path(tmp) / "signals.json"
            account_file = Path(tmp) / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            client = app.test_client()

            self.assertEqual(client.get("/joinquant/signals?token=bad").status_code, 403)

            response = client.get("/joinquant/signals?token=secret")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["schema_version"], 1)

    def test_accepts_valid_account_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal_file = Path(tmp) / "signals.json"
            account_file = Path(tmp) / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            client = app.test_client()
            payload = {
                "schema_version": 1,
                "trade_date": "2026-07-07",
                "generated_at": "2026-07-07 15:05:00",
                "source": "joinquant",
                "cash": 1000,
                "total_value": 2000,
                "positions": [{"code": "600000", "jq_code": "600000.XSHG", "qty": 100}],
                "trades": [],
            }

            response = client.post("/joinquant/account_snapshot?token=secret", json=payload)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(account_file.read_text(encoding="utf-8"))["source"], "joinquant")

    def test_writes_api_event_log_for_signal_pull_and_snapshot_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            event_file = base / "api_events.jsonl"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "generated_at": "2026-07-09 09:40:00", "signals": [{"id": "s1"}]}),
                encoding="utf-8",
            )
            app = joinquant_signal_server.create_app("secret", signal_file, account_file, event_file)
            client = app.test_client()

            self.assertEqual(client.get("/joinquant/signals?token=secret").status_code, 200)
            self.assertEqual(
                client.post(
                    "/joinquant/account_snapshot?token=secret",
                    json={"schema_version": 1, "positions": [], "trades": [], "orders": []},
                ).status_code,
                200,
            )

            rows = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["endpoint"] for row in rows], ["signals", "account_snapshot"])
            self.assertEqual(rows[0]["signal_count"], 1)
            self.assertEqual(rows[1]["status_code"], 200)

    def test_writes_api_error_event_for_bad_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            event_file = base / "api_events.jsonl"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file, event_file)
            client = app.test_client()

            self.assertEqual(client.get("/joinquant/signals?token=bad").status_code, 403)

            rows = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["endpoint"], "signals")
            self.assertEqual(rows[0]["status_code"], 403)

    def test_builds_mobile_execution_markdown_from_orders(self) -> None:
        payload = {
            "generated_at": "2026-07-07 15:05:00",
            "cash": 1000,
            "total_value": 2000,
            "positions": [{"code": "600000"}],
            "orders": [
                {
                    "action": "buy",
                    "jq_code": "600000.XSHG",
                    "name": "PF Bank",
                    "status": "held",
                    "filled": 100,
                    "amount": 100,
                    "target_pct": 12.5,
                },
                {
                    "action": "buy",
                    "jq_code": "000001.XSHE",
                    "name": "PA Bank",
                    "status": "failed",
                    "reason": "limit_up_or_suspended",
                },
            ],
        }

        md = joinquant_signal_server.build_execution_markdown(payload)

        self.assertIn("JoinQuant 模拟盘", md)
        self.assertIn("执行回报", md)
        self.assertIn("委托 2", md)
        self.assertIn("成功 1", md)
        self.assertIn("失败 1", md)
        self.assertIn("600000.XSHG", md)
        self.assertIn("limit_up_or_suspended", md)


    def test_empty_periodic_snapshot_does_not_notify_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            client = app.test_client()

            with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
                response = client.post(
                    "/joinquant/account_snapshot?token=secret",
                    json={"schema_version": 1, "positions": [], "trades": [], "orders": []},
                )

            self.assertEqual(response.status_code, 200)
            notify.assert_not_called()

    def test_snapshot_with_orders_notifies_execution_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            client = app.test_client()

            payload = {
                "schema_version": 1,
                "positions": [{"code": "600000", "qty": 100}],
                "trades": [],
                "orders": [{"action": "buy", "code": "600000", "status": "held", "filled": 100}],
            }
            with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
                response = client.post("/joinquant/account_snapshot?token=secret", json=payload)

            self.assertEqual(response.status_code, 200)
            notify.assert_called_once()
            notified = notify.call_args.args[0]
            self.assertEqual(notified["orders"], payload["orders"])
            self.assertEqual(notified["positions"], payload["positions"])
            self.assertEqual(notified["source"], "joinquant")
            self.assertTrue(notified["received_at"])

    def test_zero_filled_orders_do_not_notify_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            client = app.test_client()

            payload = {
                "schema_version": 1,
                "positions": [],
                "trades": [],
                "orders": [
                    {"action": "buy", "code": "600000", "status": "submitted", "filled": 0},
                    {"action": "sell", "code": "000001", "status": "failed", "filled": 0},
                    {"action": "buy", "code": "000002", "status": "skipped"},
                ],
            }
            with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
                response = client.post("/joinquant/account_snapshot?token=secret", json=payload)

            self.assertEqual(response.status_code, 200)
            notify.assert_not_called()

    def test_mixed_orders_notify_only_positive_filled_buy_and_sell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            client = app.test_client()

            filled_buy = {"action": "buy", "code": "600000", "status": "held", "filled": 100}
            filled_sell = {"action": "sell", "code": "000001", "status": "filled", "filled": "50"}
            payload = {
                "schema_version": 1,
                "positions": [],
                "trades": [],
                "orders": [
                    filled_buy,
                    {"action": "buy", "code": "000002", "status": "submitted", "filled": 0},
                    {"action": "sell", "code": "000003", "status": "failed", "filled": 0},
                    {"action": "hold", "code": "000004", "status": "filled", "filled": 100},
                    filled_sell,
                ],
            }
            with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
                response = client.post("/joinquant/account_snapshot?token=secret", json=payload)

            self.assertEqual(response.status_code, 200)
            notify.assert_called_once()
            notified = notify.call_args.args[0]
            self.assertEqual(notified["orders"], [filled_buy, filled_sell])
            saved = json.loads(account_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["orders"], payload["orders"])


if __name__ == "__main__":
    unittest.main()
