import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import joinquant_signal_server
from trading_store import TradingStore


class JoinQuantSignalServerTest(unittest.TestCase):
    def test_accepts_bearer_token_without_query_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            signal_file = root / "signals.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app(
                token="secret", signal_file=signal_file,
                account_file=root / "account.json", api_event_file=root / "events.jsonl",
            )
            response = app.test_client().get("/joinquant/signals", headers={"Authorization": "Bearer secret"})
            self.assertEqual(response.status_code, 200)

    @staticmethod
    def _snapshot() -> dict:
        return {
            "schema_version": 1, "trade_date": "2026-07-14",
            "generated_at": "2026-07-14 10:00:00", "cash": 100000,
            "available_cash": 100000, "total_value": 100000,
            "positions": [], "orders": [], "trades": [],
        }

    def test_ledger_commits_before_compatible_json_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store
            )

            original = joinquant_signal_server._write_json
            def assert_ledger_first(path, payload):
                with store.connect() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0], 1)
                original(path, payload)

            with unittest.mock.patch("joinquant_signal_server._write_json", side_effect=assert_ledger_first):
                response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret", json=self._snapshot()
                )
            self.assertEqual(response.status_code, 200)

    def test_ledger_failure_returns_503_and_preserves_old_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            account_file.write_text(json.dumps({"old": True}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            with unittest.mock.patch(
                "joinquant_signal_server.ingest_snapshot_payload", side_effect=RuntimeError("database is locked")
            ):
                response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret", json=self._snapshot()
                )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(json.loads(account_file.read_text(encoding="utf-8")), {"old": True})

    def test_snapshot_replay_is_idempotent_in_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file, store=store)
            client = app.test_client()
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=self._snapshot()).status_code, 200)
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=self._snapshot()).status_code, 200)
            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM reconciliation_runs").fetchone()[0], 1)

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

    def test_builds_mobile_execution_markdown_from_new_executions(self) -> None:
        payload = {
            "generated_at": "2026-07-07 15:05:00",
            "cash": 1000,
            "total_value": 2000,
            "positions": [{"code": "600000"}],
        }
        executions = [{
            "event_id": "fill:20", "action": "buy", "stock_code": "600000",
            "qty": 100, "cumulative_qty": 100, "price": 10.0,
            "status": "filled", "filled_at": "2026-07-07 15:04:58", "order_id": "10",
        }]

        md = joinquant_signal_server.build_execution_markdown(payload, executions)

        self.assertIn("JoinQuant 模拟盘", md)
        self.assertIn("执行回报", md)
        self.assertIn("本次新增成交：1", md)
        self.assertIn("买入 600000", md)
        self.assertIn("本次 100股 @ 10.00", md)
        self.assertIn("成交时间：2026-07-07 15:04:58", md)


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

    def test_repeated_filled_snapshot_notifies_execution_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            client = app.test_client()

            payload = {
                "schema_version": 1,
                "trade_date": "2026-07-14",
                "generated_at": "2026-07-14 13:39:10",
                "positions": [],
                "orders": [{
                    "order_id": "1783991771", "action": "sell", "code": "000021",
                    "amount": 100, "filled": 100, "avg_price": 52.43,
                    "status": "filled", "datetime": "2026-07-14 09:52:10",
                }],
                "trades": [{
                    "trade_id": "trade-1783991771", "order_id": "1783991771",
                    "action": "sell", "code": "000021", "amount": 100,
                    "price": 52.43, "datetime": "2026-07-14 09:52:10",
                }],
            }
            with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
                self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)
                payload["generated_at"] = "2026-07-14 13:40:10"
                payload["total_value"] = 99482.757
                self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)

            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[1][0]["event_id"], "fill:trade-1783991771")

    def test_second_partial_fill_notifies_only_the_new_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            app = joinquant_signal_server.create_app("secret", signal_file, account_file)
            client = app.test_client()
            first_trade = {
                "trade_id": "trade-1", "order_id": "order-1", "action": "buy",
                "code": "600000", "amount": 50, "price": 10.0,
                "datetime": "2026-07-14 10:00:00",
            }
            payload = {
                "schema_version": 1,
                "trade_date": "2026-07-14",
                "generated_at": "2026-07-14 10:00:10",
                "positions": [],
                "orders": [{
                    "order_id": "order-1", "action": "buy", "code": "600000",
                    "amount": 100, "filled": 50, "avg_price": 10.0,
                    "status": "partial", "datetime": "2026-07-14 10:00:00",
                }],
                "trades": [first_trade],
            }
            with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
                self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)
                payload["generated_at"] = "2026-07-14 10:01:10"
                payload["orders"][0]["filled"] = 100
                payload["orders"][0]["status"] = "filled"
                payload["trades"] = [
                    first_trade,
                    {
                        "trade_id": "trade-2", "order_id": "order-1", "action": "buy",
                        "code": "600000", "amount": 50, "price": 10.1,
                        "datetime": "2026-07-14 10:01:00",
                    },
                ]
                self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)

            self.assertEqual(notify.call_count, 2)
            self.assertEqual([row["event_id"] for row in notify.call_args_list[0].args[1]], ["fill:trade-1"])
            self.assertEqual([row["event_id"] for row in notify.call_args_list[1].args[1]], ["fill:trade-2"])

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

    def test_legacy_mixed_orders_notify_only_positive_filled_buy_and_sell(self) -> None:
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
            executions = notify.call_args.args[1]
            self.assertEqual([event["action"] for event in executions], ["buy", "sell"])
            saved = json.loads(account_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["orders"], payload["orders"])


if __name__ == "__main__":
    unittest.main()
