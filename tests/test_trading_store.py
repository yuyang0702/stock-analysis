from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_store import SignalConflictError, SignalRecord, StrategyRunRecord, TradingStore


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

    def test_signal_replay_with_changed_payload_raises_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord("run-1", "2026-07-11", "2026-07-11 09:30:00", "v1", "p1")
            original = SignalRecord("sig-1", "run-1", "2026-07-11", "600000", "600000.XSHG", "buy", 10.0, "2026-07-11 09:31:00", "", '{"id":"sig-1","price":10}')
            changed = SignalRecord("sig-1", "run-1", "2026-07-11", "600000", "600000.XSHG", "buy", 10.0, "2026-07-11 09:31:00", "", '{"price":11,"id":"sig-1"}')
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                self.assertTrue(store.record_signal(conn, original))
                with self.assertRaises(SignalConflictError):
                    store.record_signal(conn, changed)

    def test_signal_replay_with_equivalent_canonical_json_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord("run-1", "2026-07-11", "2026-07-11 09:30:00", "v1", "p1")
            first = SignalRecord("sig-1", "run-1", "2026-07-11", "600000", "600000.XSHG", "buy", 10.0, "2026-07-11 09:31:00", "", '{"id":"sig-1","price":10}')
            repeat = SignalRecord("sig-1", "run-1", "2026-07-11", "600000", "600000.XSHG", "buy", 10.0, "2026-07-11 09:31:00", "", '{ "price": 10, "id": "sig-1" }')
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                self.assertTrue(store.record_signal(conn, first))
                self.assertFalse(store.record_signal(conn, repeat))

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

    def test_initialize_creates_version_five_schema_and_pragmas(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(version, 5)
            self.assertTrue({"strategy_runs", "signals", "risk_decisions", "system_state", "position_cycles", "order_events", "exit_intents", "trade_cooldowns"}.issubset(tables))
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
            self.assertEqual(count, 5)

    def test_position_cycle_freezes_initial_risk_and_tracks_high_watermark(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10.0,
                    "current_price": 10.5, "stop_price": 9.2, "mode": "short", "atr14": 0.4,
                }], "2026-07-13 09:31:00")
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10.1,
                    "current_price": 12.0, "stop_price": 8.0, "mode": "mid", "atr14": 0.8,
                }], "2026-07-14 09:31:00")
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["initial_stop_price"], 9.2)
            self.assertEqual(cycle["initial_r"], 0.8)
            self.assertEqual(cycle["highest_price"], 12.0)
            self.assertEqual(cycle["mode"], "short")

    def test_position_cycle_closes_and_reopen_gets_new_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            position = {"code": "600000", "qty": 1000, "cost_price": 10, "current_price": 10, "stop_price": 9}
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [position], "2026-07-13 09:31:00")
            first = store.get_active_position_cycles()["600000"]["position_cycle_id"]
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [], "2026-07-14 09:31:00")
                store.reconcile_position_cycles(conn, [position], "2026-07-15 09:31:00")
            second = store.get_active_position_cycles()["600000"]["position_cycle_id"]
            self.assertNotEqual(first, second)

    def test_position_cycle_marks_first_take_profit_after_quantity_reduces(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10,
                    "current_price": 10, "stop_price": 9,
                }], "2026-07-13 09:31:00")
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 500, "cost_price": 10,
                    "current_price": 12, "stop_price": 9,
                }], "2026-07-14 09:31:00")
            self.assertEqual(store.get_active_position_cycles()["600000"]["take_profit_stage"], 1)

    def test_add_position_updates_weighted_cost_without_lowering_frozen_stop(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10,
                    "current_price": 10, "stop_price": 9.2,
                }], "2026-07-13 09:31:00")
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1500, "cost_price": 10.5,
                    "current_price": 10.8, "stop_price": 8.8,
                }], "2026-07-14 09:31:00")
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["entry_price"], 10.5)
            self.assertEqual(cycle["initial_stop_price"], 9.2)
            self.assertEqual(cycle["initial_r"], 1.3)

    def test_new_position_cycle_uses_latest_buy_signal_risk_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord("run-1", "2026-07-13", "2026-07-13 09:30:00", "v1", "p1")
            signal = SignalRecord(
                "buy-1", "run-1", "2026-07-13", "600000", "600000.XSHG", "buy", 10,
                "2026-07-13 09:31:00", "",
                '{"id":"buy-1","stop_loss":9.1,"signal_type":"short","atr14":0.4,"market_regime":"NORMAL"}',
            )
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                store.record_signal(conn, signal)
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10,
                    "current_price": 10.2, "stop_price": 9.65,
                }], "2026-07-13 09:32:00")
            cycle = store.get_active_position_cycles()["600000"]
            self.assertEqual(cycle["entry_signal_id"], "buy-1")
            self.assertEqual(cycle["initial_stop_price"], 9.1)
            self.assertEqual(cycle["mode"], "short")
            self.assertEqual(cycle["atr14"], 0.4)

    def test_online_backup_restores_schema_and_position_cycles(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.reconcile_position_cycles(conn, [{
                    "code": "600000", "qty": 1000, "cost_price": 10,
                    "current_price": 10.2, "stop_price": 9.4,
                }], "2026-07-13 09:31:00")
            backup_path = Path(tmp) / "backups" / "trading.db"

            store.backup_to(backup_path)

            restored = TradingStore(backup_path)
            self.assertTrue(restored.health().ok)
            self.assertEqual(restored.integrity_check(), "ok")
            self.assertIn("600000", restored.get_active_position_cycles())

    def test_online_backup_closes_target_before_returning(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            backup_path = Path(tmp) / "backup.db"

            store.backup_to(backup_path)
            moved_path = Path(tmp) / "moved.db"
            backup_path.replace(moved_path)

            self.assertTrue(moved_path.exists())

    def test_schema_three_reconciles_order_events_idempotently(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            event = {"id": "exit-1", "order_id": "o-1", "code": "600000", "action": "sell",
                     "target_qty": 0, "amount": -1000, "filled": 400, "status": "partial",
                     "reason": "", "datetime": "2026-07-13 10:00:00"}
            with store.transaction() as conn:
                store.reconcile_order_events(conn, [event], "2026-07-13 10:00:01")
                store.reconcile_order_events(conn, [event], "2026-07-13 10:00:02")
            with store.connect() as conn:
                row = conn.execute("SELECT status, filled_qty FROM order_events").fetchone()
                count = conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
            self.assertEqual((row[0], row[1], count), ("partial", 400, 1))

    def test_exit_intent_tracks_partial_fill_and_completes_on_position_target(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.upsert_exit_intent(conn, "exit-1", "600000", 0, "hard_stop", "2026-07-13 10:00:00")
                store.reconcile_order_events(conn, [{"id": "exit-1", "order_id": "o-1", "code": "600000",
                    "action": "sell", "target_qty": 0, "amount": -1000, "filled": 400,
                    "status": "partial", "datetime": "2026-07-13 10:01:00"}], "2026-07-13 10:01:01")
                store.reconcile_exit_intents(conn, [{"code": "600000", "qty": 600}], "2026-07-13 10:02:00")
            self.assertEqual(store.get_open_exit_intents()["600000"]["remaining_qty"], 600)
            with store.transaction() as conn:
                store.reconcile_exit_intents(conn, [], "2026-07-13 10:03:00")
            self.assertEqual(store.get_open_exit_intents(), {})

    def test_higher_priority_exit_supersedes_prior_intent_for_same_stock(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.upsert_exit_intent(conn, "take-1", "600000", 500, "take_profit_1", "2026-07-13 10:00:00")
                store.upsert_exit_intent(conn, "stop-1", "600000", 0, "hard_stop", "2026-07-13 10:01:00")
            self.assertEqual(store.get_open_exit_intents()["600000"]["signal_id"], "stop-1")

    def test_market_regime_confirmation_persists_across_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            self.assertEqual(store.confirm_market_regime("RISK_OFF"), "NORMAL")
            self.assertEqual(store.confirm_market_regime("RISK_OFF"), "RISK_OFF")
            self.assertEqual(TradingStore(Path(tmp) / "trading.db").confirm_market_regime("NORMAL"), "RISK_OFF")

    def test_completed_hard_stop_creates_rebuy_cooldown(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.upsert_exit_intent(conn, "cycle-hard_stop-0", "600000", 0, "hard_stop", "2026-07-13 10:00:00")
                store.reconcile_exit_intents(conn, [], "2026-07-13 10:03:00")
            self.assertTrue(store.is_in_cooldown("600000", "2026-07-14"))
