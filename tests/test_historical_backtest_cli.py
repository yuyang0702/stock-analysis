import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from historical_backtest import _implementation_hash, _publish_atomic, main
from historical_data import HistoricalStore


class HistoricalBacktestCliTest(unittest.TestCase):
    def _database(self, root: Path) -> Path:
        db = root / "history.db"
        store = HistoricalStore(db)
        store.initialize()
        with store.connect() as connection:
            connection.execute(
                "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("d1", "2025-01-02", "600000", 10, 11, 9, 10.5, 10, 100, 1000, 1),
            )
            connection.execute(
                "INSERT INTO daily_status VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("d1", "2025-01-02", "600000", 1, 0, 0, 11, 9),
            )
            connection.execute(
                "INSERT INTO daily_universe VALUES (?, ?, ?)",
                ("d1", "2025-01-02", "600000"),
            )
        return db

    def test_strict_validation_failure_writes_only_quality_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._database(root)
            output = root / "output"

            code = main(["validate", "--db", str(db), "--dataset", "d1", "--start", "2025-01-02", "--end", "2025-01-02", "--mode", "strict", "--output-dir", str(output)])

            self.assertNotEqual(code, 0)
            self.assertEqual([path.name for path in output.iterdir()], ["historical_backtest_quality.json"])
            self.assertFalse(json.loads((output / "historical_backtest_quality.json").read_text(encoding="utf-8"))["accepted"])

    def test_proxy_run_is_labeled_and_reuses_deterministic_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._database(root)
            output = root / "output"
            args = ["run", "--db", str(db), "--dataset", "d1", "--start", "2025-01-02", "--end", "2025-01-02", "--mode", "price_core", "--output-dir", str(output)]

            self.assertEqual(main(args), 0)
            with HistoricalStore(db).connect() as connection:
                first = connection.execute("SELECT run_id FROM backtest_runs").fetchone()[0]
            self.assertEqual(main(args), 0)
            with HistoricalStore(db).connect() as connection:
                runs = connection.execute("SELECT run_id FROM backtest_runs").fetchall()

            self.assertEqual([row[0] for row in runs], [first])
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"historical_backtest_latest.md", "historical_backtest_quality.json", "historical_backtest_equity.csv", "historical_backtest_trades.csv"},
            )
            self.assertTrue(json.loads((output / "historical_backtest_quality.json").read_text(encoding="utf-8"))["proxy_only"])

    def test_atomic_publication_restores_previous_outputs_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            one = output / "one.txt"
            two = output / "two.txt"
            one.write_text("old-one", encoding="utf-8")
            two.write_text("old-two", encoding="utf-8")
            original = Path.replace
            calls = 0
            def failing_replace(path, target):
                nonlocal calls
                if path.name.endswith(".tmp"):
                    calls += 1
                    if calls == 2:
                        raise OSError("simulated")
                return original(path, target)

            with patch.object(Path, "replace", failing_replace), self.assertRaises(OSError):
                _publish_atomic(output, {"one.txt": "new-one", "two.txt": "new-two"})

            self.assertEqual(one.read_text(encoding="utf-8"), "old-one")
            self.assertEqual(two.read_text(encoding="utf-8"), "old-two")

    def test_prune_runs_keeps_latest_complete_pinned_and_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            with store.connect() as connection:
                for index in range(22):
                    connection.execute(
                        "INSERT INTO backtest_runs VALUES (?, 'd1', 'h', '2025-01-01', '2025-01-02', 'strict', '{}', 'complete', '', 0, '{}', ?, ?)",
                        (f"run-{index:02d}", f"2025-01-{index + 1:02d}", f"2025-01-{index + 1:02d}"),
                    )
                connection.execute(
                    "INSERT INTO backtest_runs VALUES ('pinned', 'd1', 'h', '2025-01-01', '2025-01-02', 'strict', '{}', 'complete', '', 1, '{}', '2024-01-01', '2024-01-01')"
                )
                connection.execute(
                    "INSERT INTO backtest_runs VALUES ('failed', 'd1', 'h', '2025-01-01', '2025-01-02', 'strict', '{}', 'failed', 'x', 0, '{}', '2024-01-01', NULL)"
                )

            deleted = store.prune_runs(20)

            self.assertEqual(deleted, 2)
            with store.connect() as connection:
                ids = {row[0] for row in connection.execute("SELECT run_id FROM backtest_runs")}
            self.assertNotIn("run-00", ids)
            self.assertNotIn("run-01", ids)
            self.assertTrue({"pinned", "failed", "run-21"}.issubset(ids))

    def test_implementation_hash_changes_with_code_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp) / "module.py"
            code.write_text("one", encoding="utf-8")
            first = _implementation_hash([code])
            code.write_text("two", encoding="utf-8")
            self.assertNotEqual(first, _implementation_hash([code]))

    def test_run_failure_is_bounded_and_persisted_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._database(root)
            output = root / "output"
            args = ["run", "--db", str(db), "--dataset", "d1", "--start", "2025-01-02", "--end", "2025-01-02", "--mode", "price_core", "--output-dir", str(output)]
            with patch("historical_backtest.run_historical_backtest", side_effect=RuntimeError("secret\n" + "x" * 400)):
                code = main(args)

            self.assertNotEqual(code, 0)
            with HistoricalStore(db).connect() as connection:
                status, error = connection.execute("SELECT status, error FROM backtest_runs").fetchone()
            self.assertEqual(status, "failed")
            self.assertLessEqual(len(error), 240)
            self.assertNotIn("\n", error)
            self.assertEqual({path.name for path in output.iterdir()}, {"historical_backtest_quality.json"})

    def test_compare_rejects_different_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "history.db"
            store = HistoricalStore(db)
            store.initialize()
            base = {"initial_cash": 100000, "commission_rate": 0.0003, "slippage_bps": 10, "strategy_version": "v1", "code_hash": "h1"}
            changed = {**base, "initial_cash": 200000}
            with store.connect() as connection:
                for run_id, config in (("base", base), ("candidate", changed)):
                    connection.execute(
                        "INSERT INTO backtest_runs VALUES (?, 'd1', 'hash', '2025-01-01', '2025-01-31', 'strict', ?, 'complete', '', 0, '{}', '2025-02-01', '2025-02-01')",
                        (run_id, json.dumps(config)),
                    )
            output = root / "output"

            code = main(["compare", "--db", str(db), "--baseline", "base", "--candidate", "candidate", "--output-dir", str(output)])

            payload = json.loads((output / "historical_backtest_compare.json").read_text(encoding="utf-8"))
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["status"], "COMPARISON_CONTRACT_MISMATCH")
            self.assertIn("config:initial_cash", payload["mismatches"])


if __name__ == "__main__":
    unittest.main()
