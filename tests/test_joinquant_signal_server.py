import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import joinquant_signal_server
from trading_store import TradingStore


class JoinQuantSignalServerTest(unittest.TestCase):
    @staticmethod
    def _normalized_keys(value) -> set[str]:
        keys: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add("".join(
                    character for character in str(key).lower()
                    if character.isalnum()
                ))
                keys.update(JoinQuantSignalServerTest._normalized_keys(item))
        elif isinstance(value, list):
            for item in value:
                keys.update(JoinQuantSignalServerTest._normalized_keys(item))
        return keys

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

    def test_ledger_failure_issue_is_bound_to_primary_account_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}),
                encoding="utf-8",
            )
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store,
            )
            with (
                unittest.mock.patch(
                    "joinquant_signal_server.ingest_snapshot_payload",
                    side_effect=RuntimeError("database is locked"),
                ),
                unittest.mock.patch(
                    "joinquant_signal_server.notify_reconciliation",
                ),
            ):
                response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret",
                    json=self._snapshot(),
                )
            self.assertEqual(response.status_code, 503)
            with store.connect() as conn:
                scope = conn.execute(
                    """SELECT account_scope_id FROM account_scopes
                       WHERE adapter='joinquant' AND scope_alias='primary'"""
                ).fetchone()[0]
                reconciliation_scope = conn.execute(
                    """SELECT account_scope_id FROM reconciliation_runs"""
                ).fetchone()[0]
                issue_key = conn.execute(
                    """SELECT issue_key FROM execution_issue_state"""
                ).fetchone()[0]

            self.assertEqual(reconciliation_scope, scope)
            self.assertEqual(issue_key, f"scope:{scope}:ledger:sqlite")

    def test_event_only_failure_is_audited_without_account_control_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            history_file = base / "account_snapshot_history.jsonl"
            event_file = base / "api_events.jsonl"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}),
                encoding="utf-8",
            )
            account_file.write_text(
                json.dumps({"existing": "account"}),
                encoding="utf-8",
            )
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, event_file, store,
            )
            payload = {
                "schema_version": 1,
                "positions": [],
                "orders": [{
                    "order_id": "rolled-back-order", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 0,
                    "status": "submitted",
                    "datetime": "2026-07-14 10:00:00",
                }],
                "trades": [{
                    "trade_id": "orphan-fill", "order_id": "missing-order",
                    "action": "buy", "code": "600000", "amount": 100,
                    "price": 10, "datetime": "2026-07-14 10:00:01",
                }],
            }
            with (
                unittest.mock.patch(
                    "joinquant_signal_server.notify_reconciliation"
                ) as reconcile_notify,
                unittest.mock.patch(
                    "joinquant_signal_server._notify_execution"
                ) as execution_notify,
                unittest.mock.patch("ml_dataset.update_order_labels") as labels,
            ):
                response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret", json=payload,
                )

            self.assertEqual(response.status_code, 503)
            reconcile_notify.assert_not_called()
            execution_notify.assert_not_called()
            labels.assert_not_called()
            self.assertEqual(
                json.loads(account_file.read_text(encoding="utf-8")),
                {"existing": "account"},
            )
            self.assertFalse(history_file.exists())
            events = [
                json.loads(line)
                for line in event_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [(row["endpoint"], row["status_code"]) for row in events],
                [("account_snapshot", 503)],
            )
            with store.connect() as conn:
                for table in (
                    "orders", "fills", "account_scopes",
                    "broker_snapshot_current", "account_snapshots",
                    "position_snapshots", "daily_equity",
                    "reconciliation_runs", "reconciliation_items",
                    "execution_issue_state", "control_events", "system_state",
                ):
                    self.assertEqual(
                        conn.execute(
                            f'SELECT count(*) FROM "{table}"'
                        ).fetchone()[0],
                        0,
                        table,
                    )

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
                "available_cash": 1000,
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
            "available_cash": 1000,
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

    def test_event_only_markdown_does_not_fabricate_zero_account_values(self) -> None:
        md = joinquant_signal_server.build_execution_markdown(
            {"generated_at": "2026-07-07 15:05:00", "positions": []},
            [{
                "event_id": "fill:20", "action": "buy",
                "stock_code": "600000", "qty": 100, "price": 10,
                "filled_at": "2026-07-07 15:04:58",
            }],
        )
        self.assertIn("事件包未提供账户快照", md)
        self.assertNotIn("总资产：0.00", md)
        self.assertNotIn("现金：0.00", md)


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

    def test_event_only_does_not_publish_account_or_run_ml_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            history_file = base / "account_snapshot_history.jsonl"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}),
                encoding="utf-8",
            )
            account_file.write_text(
                json.dumps({"existing": "account"}),
                encoding="utf-8",
            )
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file,
            )
            payload = {
                "schema_version": 1,
                "trade_date": "2026-07-14",
                "generated_at": "2026-07-14 10:00:00",
                "positions": [],
                "orders": [{
                    "order_id": "event-order", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 100,
                    "avg_price": 10, "status": "filled",
                    "datetime": "2026-07-14 10:00:00",
                }],
                "trades": [],
            }
            with (
                unittest.mock.patch(
                    "joinquant_signal_server._notify_execution"
                ) as notify,
                unittest.mock.patch("ml_dataset.update_order_labels") as labels,
            ):
                response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret", json=payload,
                )
            self.assertEqual(response.status_code, 200)
            notify.assert_called_once()
            labels.assert_not_called()
            self.assertEqual(
                json.loads(account_file.read_text(encoding="utf-8")),
                {"existing": "account"},
            )
            self.assertFalse(history_file.exists())

    def test_complete_snapshot_is_sanitized_before_account_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}),
                encoding="utf-8",
            )
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file,
            )
            payload = self._snapshot()
            payload["nested"] = {
                "access_token": "publish-access-secret",
                "webhookUrl": "publish-webhook-secret",
                "broker_account_id": "broker-account-secret",
                "brokeraccountid": "compact-broker-account-secret",
                "user_id": "user-id-secret",
                "userid": "compact-user-id-secret",
                "username": "username-secret",
                "passphrase": "passphrase-secret",
                "credential": "credential-secret",
                "credentials": {"safe_note": "must-remain"},
                "broker_auth": "auth-secret",
                "auth_header": "auth-header-secret",
                "authorization_header": "authorization-header-secret",
                "qmt_access_key": "access-key-secret",
                "qmt_access_key_id": "access-key-id-secret",
                "request_signing_key": "signing-key-secret",
                "request_signing_key_pem": "signing-key-pem-secret",
                "ssh_key": "ssh-key-secret",
                "ssh_key_path": "ssh-key-path-secret",
                "raw_account_id": "raw-account-secret",
                "account_no": "account-number-secret",
                "qmt_account": "qmt-account-secret",
                "oauth_bearer": "bearer-secret",
                "X-API-Key": "header-api-key-secret",
                "api_secret_value": "api-secret-value",
            }
            payload["orders"] = [{
                "order_id": "privacy-order", "action": "buy",
                "code": "600000", "amount": 100, "filled": 100,
                "avg_price": 10, "status": "filled",
                "datetime": "2026-07-14 10:00:00",
                "transport": {"auth_key": "order-auth-secret"},
            }]
            payload["trades"] = [{
                "trade_id": "privacy-fill", "order_id": "privacy-order",
                "action": "buy", "code": "600000", "amount": 100,
                "price": 10, "datetime": "2026-07-14 10:00:01",
                "transport": {"ssh_private_key": "fill-ssh-secret"},
            }]
            response = app.test_client().post(
                "/joinquant/account_snapshot?token=secret", json=payload,
            )
            self.assertEqual(response.status_code, 200)
            published = json.loads(account_file.read_text(encoding="utf-8"))
            history = json.loads(
                (base / "account_snapshot_history.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            store = TradingStore(base / "trading.db")
            with store.connect() as conn:
                sqlite_payloads = [
                    json.loads(conn.execute(
                        """SELECT raw_json FROM account_snapshots
                           WHERE raw_json IS NOT NULL"""
                    ).fetchone()[0]),
                    json.loads(conn.execute(
                        "SELECT raw_json FROM orders"
                    ).fetchone()[0]),
                    json.loads(conn.execute(
                        "SELECT raw_json FROM fills"
                    ).fetchone()[0]),
                ]
            forbidden = {
                "accesstoken", "webhookurl", "brokeraccountid",
                "userid", "username", "passphrase", "credential",
                "credentials", "brokerauth", "qmtaccesskey",
                "authheader", "authorizationheader", "qmtaccesskeyid",
                "requestsigningkey", "requestsigningkeypem", "sshkey",
                "sshkeypath", "authkey", "sshprivatekey",
                "rawaccountid", "accountno", "qmtaccount", "oauthbearer",
                "xapikey", "apisecretvalue",
            }
            for sink in (published, history, *sqlite_payloads):
                self.assertTrue(
                    forbidden.isdisjoint(self._normalized_keys(sink)),
                    self._normalized_keys(sink) & forbidden,
                )

    def test_partial_account_tuple_is_not_event_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}),
                encoding="utf-8",
            )
            app = joinquant_signal_server.create_app(
                "secret", signal_file, base / "account.json",
            )
            response = app.test_client().post(
                "/joinquant/account_snapshot?token=secret",
                json={
                    "schema_version": 1, "positions": [], "orders": [],
                    "trades": [], "total_value": 1000,
                },
            )
            self.assertEqual(response.status_code, 503)

    def test_partial_account_values_with_events_remain_event_only(self) -> None:
        for account_field in ("cash", "total_value"):
            with (
                self.subTest(account_field=account_field),
                tempfile.TemporaryDirectory() as tmp,
            ):
                base = Path(tmp)
                signal_file = base / "signals.json"
                account_file = base / "account.json"
                store = TradingStore(base / "trading.db")
                signal_file.write_text(
                    json.dumps({"schema_version": 1, "signals": []}),
                    encoding="utf-8",
                )
                app = joinquant_signal_server.create_app(
                    "secret", signal_file, account_file, store=store,
                )
                response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret",
                    json={
                        "schema_version": 1, "positions": [],
                        account_field: 999999,
                        "orders": [{
                            "order_id": f"partial-{account_field}",
                            "action": "buy", "code": "600000",
                            "amount": 100, "filled": 100,
                            "avg_price": 10, "status": "filled",
                            "datetime": "2026-07-14 10:00:00",
                        }],
                        "trades": [],
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertFalse(account_file.exists())
                with store.connect() as conn:
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM orders").fetchone()[0],
                        1,
                    )
                    for table in (
                        "account_snapshots", "daily_equity",
                        "reconciliation_runs", "reconciliation_items",
                        "control_events", "system_state",
                    ):
                        self.assertEqual(
                            conn.execute(
                                f'SELECT count(*) FROM "{table}"'
                            ).fetchone()[0],
                            0,
                            table,
                        )

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
            self.assertFalse(account_file.exists())

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

            filled_buy = {
                "action": "buy", "code": "600000", "status": "held",
                "amount": 100, "filled": 100, "avg_price": 10,
            }
            filled_sell = {
                "action": "sell", "code": "000001", "status": "filled",
                "amount": 50, "filled": "50", "avg_price": 9,
            }
            payload = {
                "schema_version": 1,
                "positions": [],
                "trades": [],
                "orders": [
                    filled_buy,
                    {"action": "buy", "code": "000002", "status": "submitted", "filled": 0},
                    {"action": "sell", "code": "000003", "status": "failed", "filled": 0},
                    filled_sell,
                ],
            }
            with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
                response = client.post("/joinquant/account_snapshot?token=secret", json=payload)

            self.assertEqual(response.status_code, 200)
            notify.assert_called_once()
            executions = notify.call_args.args[1]
            self.assertEqual([event["action"] for event in executions], ["buy", "sell"])
            self.assertFalse(account_file.exists())

    def test_event_only_invalid_action_fails_without_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}),
                encoding="utf-8",
            )
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file,
            )
            client = app.test_client()
            with unittest.mock.patch(
                "joinquant_signal_server._notify_execution"
            ) as notify:
                response = client.post(
                    "/joinquant/account_snapshot?token=secret",
                    json={
                        "schema_version": 1, "positions": [], "trades": [],
                        "orders": [{
                            "action": "hold", "code": "000004",
                            "status": "filled", "amount": 100,
                            "filled": 100,
                        }],
                    },
                )
            self.assertEqual(response.status_code, 503)
            notify.assert_not_called()
            self.assertFalse(account_file.exists())


if __name__ == "__main__":
    unittest.main()
