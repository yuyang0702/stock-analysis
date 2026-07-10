from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_store import SignalRecord, StrategyRunRecord, TradingStore


class TradingStoreTest(unittest.TestCase):
    def test_signal_insert_is_immutable_and_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord(
                run_id="run-1", trade_date="2026-07-11", started_at="2026-07-11 09:30:00",
                strategy_version="git:abc", parameter_version="risk-observe-v1",
            )
            signal = SignalRecord(
                signal_id="sig-1", run_id="run-1", trade_date="2026-07-11",
                code="600000", jq_code="600000.XSHG", action="buy",
                position_pct=10.0, generated_at="2026-07-11 09:31:00",
                expires_at="2026-07-11 09:51:00", raw_json='{"id":"sig-1"}',
            )
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                self.assertTrue(store.record_signal(conn, signal))
                self.assertFalse(store.record_signal(conn, signal))
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM signals WHERE signal_id='sig-1'").fetchone()[0]
            self.assertEqual(count, 1)

    def test_system_state_records_latest_value_and_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "0", "ledger unavailable")
            self.assertEqual(store.get_system_state("buy_enabled"), "0")
            with store.connect() as conn:
                reason = conn.execute(
                    "SELECT reason FROM system_state WHERE key = ?", ("buy_enabled",)
                ).fetchone()[0]
            self.assertEqual(reason, "ledger unavailable")

    def test_initialize_creates_version_one_schema_and_pragmas(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(version, 1)
            self.assertTrue({"strategy_runs", "signals", "risk_decisions", "system_state"}.issubset(tables))
            self.assertEqual(foreign_keys, 1)
            self.assertEqual(busy_timeout, 5000)

    def test_transaction_rolls_back_all_rows_on_error(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with store.transaction() as conn:
                    conn.execute(
                        "INSERT INTO system_state(key, value, updated_at) VALUES (?, ?, datetime('now'))",
                        ("buy_enabled", "1"),
                    )
                    raise RuntimeError("boom")
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM system_state").fetchone()[0]
            self.assertEqual(count, 0)

    def test_initialize_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            store.initialize()
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(count, 1)
