import json
import tempfile
import unittest
import unittest.mock
from decimal import Decimal
from pathlib import Path

import pandas as pd

import joinquant_exporter
import joinquant_signal_server
import config as app_config
from execution_contracts import BrokerSnapshot
from trading_store import TradingStore


class JoinQuantSignalServerTest(unittest.TestCase):
    @staticmethod
    def _runtime_headers(run_type: str = "sim_trade") -> dict[str, str]:
        return {
            "Authorization": "Bearer secret",
            "X-JoinQuant-Run-Type": run_type,
            "X-JoinQuant-Template-Version": app_config.JOINQUANT_TEMPLATE_VERSION,
            "X-JoinQuant-Protocol-Version": "1",
        }

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

    def test_strict_runtime_rejects_missing_and_backtest_identity_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            signal_file = root / "signals.json"
            account_file = root / "account.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8"
            )
            app = joinquant_signal_server.create_app(
                token="secret", signal_file=signal_file,
                account_file=account_file, api_event_file=root / "events.jsonl",
                require_runtime_identity=True,
            )
            client = app.test_client()

            missing = client.get(
                "/joinquant/signals", headers={"Authorization": "Bearer secret"},
            )
            backtest = client.post(
                "/joinquant/account_snapshot",
                headers=self._runtime_headers("full_backtest"),
                json={"schema_version": 1, "positions": [], "orders": [], "trades": []},
            )

            self.assertEqual(missing.status_code, 409)
            self.assertEqual(backtest.status_code, 409)
            self.assertFalse(account_file.exists())

    def test_strict_runtime_rejects_payload_identity_mismatch_and_accepts_exact_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            signal_file = root / "signals.json"
            account_file = root / "account.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8"
            )
            app = joinquant_signal_server.create_app(
                token="secret", signal_file=signal_file,
                account_file=account_file, api_event_file=root / "events.jsonl",
                require_runtime_identity=True,
            )
            client = app.test_client()
            payload = {
                "schema_version": 1,
                "trade_date": "2026-08-11",
                "generated_at": "2026-08-11 15:05:00",
                "runtime_mode": "full_backtest",
                "runtime_protocol_version": "1",
                "strategy_template_version": app_config.JOINQUANT_TEMPLATE_VERSION,
                "cash": 100_000,
                "available_cash": 100_000,
                "total_value": 100_000,
                "positions": [], "orders": [], "trades": [],
            }

            rejected = client.post(
                "/joinquant/account_snapshot",
                headers=self._runtime_headers(), json=payload,
            )
            self.assertEqual(rejected.status_code, 409)
            self.assertFalse(account_file.exists())

            payload["runtime_mode"] = "sim_trade"
            accepted = client.post(
                "/joinquant/account_snapshot",
                headers=self._runtime_headers(), json=payload,
            )
            self.assertEqual(accepted.status_code, 200)
            stored = json.loads(account_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["runtime_mode"], "sim_trade")

    @staticmethod
    def _write_exact_signal(base: Path, store: TradingStore) -> Path:
        store.initialize()
        with store.transaction() as conn:
            scope = store.get_or_create_account_scope(conn, "joinquant", "primary")
            for key, value in {
                "buy_enabled": "1", "sell_enabled": "1", "kill_switch": "0",
                "market_regime": "NORMAL",
            }.items():
                store.set_system_state(conn, key, value, "test")
            store.replace_current_broker_snapshot(conn, BrokerSnapshot.from_values(
                account_scope_id=scope, trade_date="2099-07-28",
                broker_time="2099-07-28T09:59:40+08:00",
                generated_at="2099-07-28T09:59:41+08:00",
                total_equity=Decimal("100000"), cash=Decimal("100000"),
                available_cash=Decimal("100000"), frozen_cash=Decimal("0"),
                positions=(), open_orders=(), fills=(),
                adapter_version="joinquant-v1", node_version="server-v1",
                session_id="session-1", capabilities_version="cap-v1",
                daily_risk_evidence_status="reported", intraday_pnl=Decimal("0"),
                account_drawdown_pct=Decimal("0"),
                daily_turnover_fraction=Decimal("0"), consecutive_losses=0,
            ))
        row = pd.DataFrame([{
            "code": "600000", "name": "PF Bank", "price": 10.0,
            "entry_price": 10.0, "stop_loss": 9.8, "take_profit": 10.4,
            "position_pct": 5.0, "final_score": 95,
            "signal_action": "continue",
            "execution_plan_version": joinquant_exporter.EXECUTION_PLAN_VERSION,
            "execution_allowed": True, "market_state": "NORMAL",
            "market_regime": "NORMAL", "board_type": "main_active",
            "atr14": 0.2, "prev_close": 10.0,
            "quote_time": "2099-07-28T09:59:50+08:00",
            "industry": "bank", "theme_label": "dividend",
        }])
        with unittest.mock.patch.object(
            joinquant_exporter,
            "_execution_now",
            return_value="2099-07-28T10:00:00+08:00",
        ):
            return joinquant_exporter.export_signals(
                row, run_id="exact-server-filter", trade_date="2099-07-28",
                output_path=base / "signals.json", store=store,
                account_total_value=100000, available_cash=100000,
                enforce_execution_contract=True,
            )

    def test_signal_pull_rechecks_ready_intent_and_current_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = TradingStore(base / "trading.db")
            signal_file = self._write_exact_signal(base, store)
            payload = json.loads(signal_file.read_text(encoding="utf-8"))
            payload["signals"].insert(0, {
                "id": "protective-sell", "action": "sell", "code": "000001",
                "jq_code": "000001.XSHE", "target_qty": 0,
            })
            signal_file.write_text(json.dumps(payload), encoding="utf-8")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, base / "account.json", store=store,
            )
            client = app.test_client()

            ready = client.get("/joinquant/signals?token=secret").get_json()
            self.assertEqual(
                [item["action"] for item in ready["signals"]], ["sell", "buy"],
            )

            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "0", "test stop")
            stopped = client.get("/joinquant/signals?token=secret").get_json()
            self.assertEqual(
                [item["action"] for item in stopped["signals"]], ["sell"],
            )

            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "1", "test resume")
                conn.execute("UPDATE orders SET status='rejected' WHERE action='buy'")
            terminal = client.get("/joinquant/signals?token=secret").get_json()
            self.assertEqual(
                [item["action"] for item in terminal["signals"]], ["sell"],
            )

            with store.transaction() as conn:
                store.set_system_state(conn, "kill_switch", "1", "test kill")
            killed = client.get("/joinquant/signals?token=secret").get_json()
            self.assertEqual(killed["signals"], [])

    def test_signal_pull_fails_closed_for_invalid_control_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = TradingStore(base / "trading.db")
            signal_file = self._write_exact_signal(base, store)
            payload = json.loads(signal_file.read_text(encoding="utf-8"))
            payload["signals"].insert(0, {
                "id": "protective-sell", "action": "sell", "code": "000001",
                "jq_code": "000001.XSHE", "target_qty": 0,
            })
            signal_file.write_text(json.dumps(payload), encoding="utf-8")
            client = joinquant_signal_server.create_app(
                "secret", signal_file, base / "account.json", store=store,
            ).test_client()

            with store.transaction() as conn:
                store.set_system_state(conn, "kill_switch", "corrupt", "test")
            self.assertEqual(
                client.get("/joinquant/signals?token=secret").get_json()["signals"],
                [],
            )

            with store.transaction() as conn:
                store.set_system_state(conn, "kill_switch", "0", "test")
                store.set_system_state(conn, "buy_enabled", "corrupt", "test")
            self.assertEqual(
                [item["action"] for item in client.get(
                    "/joinquant/signals?token=secret"
                ).get_json()["signals"]],
                ["sell"],
            )

            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "1", "test")
                store.set_system_state(conn, "sell_enabled", "corrupt", "test")
            self.assertEqual(
                [item["action"] for item in client.get(
                    "/joinquant/signals?token=secret"
                ).get_json()["signals"]],
                ["buy"],
            )

    def test_signal_pull_rejects_tampered_execution_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = TradingStore(base / "trading.db")
            signal_file = self._write_exact_signal(base, store)
            payload = json.loads(signal_file.read_text(encoding="utf-8"))
            buy = payload["signals"][0]
            buy.update({
                "jq_code": "000001.XSHE",
                "price_cap": 999,
                "required_cash_yuan": 1,
            })
            payload["signals"].insert(0, {
                "id": "protective-sell", "action": "sell", "code": "000001",
                "jq_code": "000001.XSHE", "target_qty": 0,
            })
            signal_file.write_text(json.dumps(payload), encoding="utf-8")
            response = joinquant_signal_server.create_app(
                "secret", signal_file, base / "account.json", store=store,
            ).test_client().get("/joinquant/signals?token=secret").get_json()

            self.assertEqual(
                [item["action"] for item in response["signals"]], ["sell"],
            )
            self.assertEqual(response["diagnostics"]["execution_filter_removed"], 1)

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
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store,
            )
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
            with unittest.mock.patch(
                "joinquant_signal_server.ingest_snapshot_payload",
                side_effect=RuntimeError("database is locked"),
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
                issue_notification = conn.execute(
                    """SELECT source_fact_id FROM notification_outbox
                       WHERE object_type='execution_issue'"""
                ).fetchone()

            self.assertEqual(reconciliation_scope, scope)
            self.assertEqual(issue_key, f"scope:{scope}:ledger:sqlite")
            self.assertIsNotNone(issue_notification)

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
                    "joinquant_signal_server._notify_execution"
                ) as execution_notify,
                unittest.mock.patch("ml_dataset.update_order_labels") as labels,
            ):
                response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret", json=payload,
                )

            self.assertEqual(response.status_code, 503)
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
                    expected = 1 if table == "system_state" else 0
                    self.assertEqual(
                        conn.execute(
                            f'SELECT count(*) FROM "{table}"'
                        ).fetchone()[0],
                        expected,
                        table,
                    )

    def test_event_only_fill_conflict_creates_sticky_critical_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}),
                encoding="utf-8",
            )
            store = TradingStore(base / "trading.db")
            client = joinquant_signal_server.create_app(
                "secret", signal_file, base / "account.json", store=store,
            ).test_client()
            payload = {
                "schema_version": 1,
                "positions": [],
                "orders": [{
                    "order_id": "order-conflict", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 100,
                    "avg_price": 10, "status": "filled",
                    "datetime": "2026-07-14 10:00:00",
                }],
                "trades": [{
                    "trade_id": "fill-conflict", "order_id": "order-conflict",
                    "action": "buy", "code": "600000", "amount": 100,
                    "price": 10, "datetime": "2026-07-14 10:00:00",
                }],
            }
            self.assertEqual(client.post(
                "/joinquant/account_snapshot?token=secret", json=payload,
            ).status_code, 200)
            changed = json.loads(json.dumps(payload))
            changed["trades"][0]["price"] = 11

            response = client.post(
                "/joinquant/account_snapshot?token=secret", json=changed,
            )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()["error"], "immutable_conflict")
            with store.connect() as conn:
                issue = conn.execute(
                    """SELECT state, severity, recovered_at
                       FROM execution_issue_state"""
                ).fetchone()
                controls = {
                    str(row["key"]): str(row["value"])
                    for row in conn.execute(
                        """SELECT key, value FROM system_state
                           WHERE key IN ('buy_enabled','kill_switch')"""
                    )
                }
                notice_count = conn.execute(
                    """SELECT COUNT(*) FROM notification_outbox
                       WHERE object_type IN ('execution_issue','control_event')"""
                ).fetchone()[0]
            self.assertEqual(
                (issue["state"], issue["severity"], issue["recovered_at"]),
                ("IMMUTABLE_FILL_CONFLICT", "CRITICAL", None),
            )
            self.assertEqual(controls, {"buy_enabled": "0", "kill_switch": "1"})
            self.assertGreaterEqual(notice_count, 3)

    def test_event_only_order_identity_conflict_creates_sticky_critical_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": []}),
                encoding="utf-8",
            )
            store = TradingStore(base / "trading.db")
            client = joinquant_signal_server.create_app(
                "secret", signal_file, base / "account.json", store=store,
            ).test_client()
            payload = {
                "schema_version": 1,
                "positions": [],
                "orders": [{
                    "client_order_id": "client-a",
                    "order_id": "broker-same", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 0,
                    "status": "submitted",
                    "datetime": "2026-07-14 10:00:00",
                }],
                "trades": [],
            }
            self.assertEqual(client.post(
                "/joinquant/account_snapshot?token=secret", json=payload,
            ).status_code, 200)
            changed = json.loads(json.dumps(payload))
            changed["orders"][0]["client_order_id"] = "client-b"

            response = client.post(
                "/joinquant/account_snapshot?token=secret", json=changed,
            )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()["error"], "immutable_conflict")
            with store.connect() as conn:
                issue = conn.execute(
                    """SELECT state, severity, recovered_at
                       FROM execution_issue_state"""
                ).fetchone()
                controls = {
                    str(row["key"]): str(row["value"])
                    for row in conn.execute(
                        """SELECT key, value FROM system_state
                           WHERE key IN ('buy_enabled','kill_switch')"""
                    )
                }
            self.assertEqual(
                (issue["state"], issue["severity"], issue["recovered_at"]),
                ("LEDGER_INTEGRITY_FAILURE", "CRITICAL", None),
            )
            self.assertEqual(controls, {"buy_enabled": "0", "kill_switch": "1"})

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
                "orders": [],
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
                json.dumps({
                    "schema_version": 1,
                    "generated_at": "2026-07-09 09:40:00",
                    "signals": [{"id": "s1", "action": "sell", "code": "600000"}],
                }),
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
            self.assertEqual(rows[0]["signal_count"], 0)
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
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store,
            )
            client = app.test_client()

            with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
                response = client.post(
                    "/joinquant/account_snapshot?token=secret",
                    json={"schema_version": 1, "positions": [], "trades": [], "orders": []},
                )

            self.assertEqual(response.status_code, 200)
            notify.assert_not_called()

    def test_snapshot_endpoint_uses_full_mode_only_for_complete_account_state(self) -> None:
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
            event = {
                "schema_version": 1,
                "positions": [],
                "orders": [{
                    "order_id": "event-order", "action": "buy",
                    "code": "600000", "amount": 100, "filled": 0,
                    "status": "submitted", "datetime": "2026-07-14 10:00:00",
                }],
                "trades": [],
            }
            with unittest.mock.patch(
                "joinquant_signal_server.ingest_snapshot_payload",
                side_effect=(
                    {"event_only": False},
                    {"event_only": True},
                ),
            ) as ingest:
                full_response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret",
                    json=self._snapshot(),
                )
                event_response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret", json=event,
                )

            self.assertEqual(full_response.status_code, 200)
            self.assertEqual(event_response.status_code, 200)
            self.assertEqual(
                [call.kwargs["mode"] for call in ingest.call_args_list],
                ["full", "incremental"],
            )

    def test_complete_account_snapshot_requires_all_three_collections(self) -> None:
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
            for missing in ("positions", "orders", "trades"):
                with self.subTest(missing=missing), unittest.mock.patch(
                    "joinquant_signal_server.ingest_snapshot_payload",
                ) as ingest:
                    payload = self._snapshot()
                    payload.pop(missing)
                    response = app.test_client().post(
                        "/joinquant/account_snapshot?token=secret", json=payload,
                    )
                    self.assertEqual(response.status_code, 400)
                    ingest.assert_not_called()

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
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store,
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
            with unittest.mock.patch(
                "ml_dataset.update_order_labels"
            ) as labels:
                response = app.test_client().post(
                    "/joinquant/account_snapshot?token=secret", json=payload,
                )
            self.assertEqual(response.status_code, 200)
            labels.assert_not_called()
            with store.connect() as conn:
                event_types = [
                    str(row[0]) for row in conn.execute(
                        "SELECT event_type FROM notification_outbox ORDER BY event_type"
                    )
                ]
            self.assertEqual(event_types, ["fill", "order_terminal"])
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
            payload["generated_at"] = "2026-07-14 10:00:01"
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
                        expected = 1 if table == "system_state" else 0
                        self.assertEqual(
                            conn.execute(
                                f'SELECT count(*) FROM "{table}"'
                            ).fetchone()[0],
                            expected,
                            table,
                        )

    def test_repeated_filled_snapshot_notifies_execution_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store,
            )
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
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)
            payload["generated_at"] = "2026-07-14 13:40:10"
            payload["total_value"] = 99482.757
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)

            with store.connect() as conn:
                rows = conn.execute(
                    """SELECT event_type, object_id FROM notification_outbox
                       ORDER BY event_type, object_id"""
                ).fetchall()
            self.assertEqual(
                [(row["event_type"], row["object_id"]) for row in rows],
                [
                    ("fill", "trade-1783991771"),
                    ("order_terminal", "manual:1783991771"),
                ],
            )
            self.assertFalse(account_file.exists())

    def test_second_partial_fill_notifies_only_the_new_trade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store,
            )
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

            with store.connect() as conn:
                fills = [
                    str(row[0]) for row in conn.execute(
                        """SELECT object_id FROM notification_outbox
                           WHERE event_type='fill' ORDER BY object_id"""
                    )
                ]
                terminal_count = conn.execute(
                    """SELECT COUNT(*) FROM notification_outbox
                       WHERE event_type='order_terminal'"""
                ).fetchone()[0]
            self.assertEqual(fills, ["trade-1", "trade-2"])
            self.assertEqual(terminal_count, 1)

    def test_zero_filled_orders_do_not_notify_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            account_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store,
            )
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
            store = TradingStore(base / "trading.db")
            app = joinquant_signal_server.create_app(
                "secret", signal_file, account_file, store=store,
            )
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
            response = client.post("/joinquant/account_snapshot?token=secret", json=payload)

            self.assertEqual(response.status_code, 200)
            with store.connect() as conn:
                fills = conn.execute(
                    """SELECT payload_json FROM notification_outbox
                       WHERE event_type='fill' ORDER BY created_at, event_key"""
                ).fetchall()
            self.assertEqual(
                sorted(json.loads(row[0])["action"] for row in fills),
                ["buy", "sell"],
            )
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
