import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from historical_backtest import (
    HistoricalBacktestConfig,
    _implementation_hash,
    _implementation_paths,
    _publish_atomic,
    _run_id,
    main,
)
from historical_data import HistoricalStore, strict_table_hash
from ml_contracts import CandidateSample, TimedFeature


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

    def test_import_accepts_strict_candidate_and_price_payloads_with_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "history.db"
            candidate = CandidateSample.from_values(
                source="strict_history",
                dataset_id="strict-1",
                decision_at="2025-01-02T10:00:00+08:00",
                code="600000",
                strategy_version="strategy-v1",
                parameter_version="parameter-v1",
                feature_schema_version="feature-v1",
                features={
                    "score": TimedFeature(80.0, "2025-01-02T10:00:00+08:00"),
                    "market_regime": TimedFeature("NORMAL", "2025-01-02T10:00:00+08:00"),
                },
                selected=True,
                rejection_stage="selected",
                rejection_code="",
                final_action="selected",
                universe_hash="universe-hash",
                market_data_version="market-v1",
                code_hash="code-hash",
                generator_hash="generator-hash",
            )
            candidate_payload = {
                **candidate.__dict__,
                "features": {
                    name: {
                        "value": feature.value,
                        "available_at": feature.available_at,
                    }
                    for name, feature in candidate.features.items()
                },
            }
            prices = [{
                "dataset_id": "strict-1",
                "code": "600000",
                "bar_at": "2025-01-02T10:05:00+08:00",
                "available_at": "2025-01-02T10:05:01+08:00",
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000,
                "amount": 10100,
                "paused": 0,
                "limit_up": 11.0,
                "limit_down": 9.0,
                "adjustment_version": "raw-v1",
            }]
            manifest = {
                "dataset_id": "strict-1",
                "source": "strict_history",
                "strategy_version": "strategy-v1",
                "parameter_version": "parameter-v1",
                "feature_schema_version": "feature-v1",
                "market_data_version": "market-v1",
                "code_hash": "code-hash",
                "generator_hash": "generator-hash",
                "adjustment_version": "raw-v1",
                "cohorts": {
                    candidate.decision_at: {
                        "codes": [candidate.code],
                        "universe_hash": candidate.universe_hash,
                    },
                },
                "table_hashes": {
                    "decision_candidates": strict_table_hash(
                        "decision_candidates", [candidate]
                    ),
                    "candidate_prices": strict_table_hash(
                        "candidate_prices", prices
                    ),
                },
            }
            candidate_file = root / "candidates.json"
            price_file = root / "prices.json"
            manifest_file = root / "manifest.json"
            candidate_file.write_text(
                json.dumps([candidate_payload], ensure_ascii=False), encoding="utf-8"
            )
            price_file.write_text(
                json.dumps(prices, ensure_ascii=False), encoding="utf-8"
            )
            manifest_file.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

            candidate_code = main([
                "import", "--db", str(db), "--dataset", "strict-1",
                "--kind", "decision_candidates", "--file", str(candidate_file),
                "--manifest", str(manifest_file),
            ])
            price_code = main([
                "import", "--db", str(db), "--dataset", "strict-1",
                "--kind", "candidate_prices", "--file", str(price_file),
                "--manifest", str(manifest_file),
            ])

            self.assertEqual(candidate_code, 0)
            self.assertEqual(price_code, 0)
            store = HistoricalStore(db)
            self.assertEqual(store.candidate_cohort("strict-1", candidate.decision_at), [candidate])
            self.assertEqual(
                len(store.candidate_price_path(
                    "strict-1", "600000",
                    "2025-01-02T10:05:00+08:00",
                    "2025-01-02T10:05:01+08:00",
                )),
                1,
            )

    def test_replay_command_writes_hash_bound_decision_time_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "history.db"
            output = root / "output"
            store = HistoricalStore(db)
            store.initialize()
            candidate = CandidateSample.from_values(
                source="strict_history",
                dataset_id="strict-1",
                decision_at="2025-01-02T10:00:00+08:00",
                code="600000",
                strategy_version="strategy-v1",
                parameter_version="parameter-v1",
                feature_schema_version="feature-v1",
                features={
                    "score": TimedFeature(80.0, "2025-01-02T10:00:00+08:00"),
                    "market_regime": TimedFeature(
                        "NORMAL", "2025-01-02T10:00:00+08:00"
                    ),
                },
                selected=True,
                rejection_stage="selected",
                rejection_code="",
                final_action="selected",
                universe_hash="universe-hash",
                market_data_version="market-v1",
                code_hash="code-hash",
                generator_hash="generator-hash",
            )
            manifest = {
                "dataset_id": "strict-1",
                "source": "strict_history",
                "strategy_version": "strategy-v1",
                "parameter_version": "parameter-v1",
                "feature_schema_version": "feature-v1",
                "market_data_version": "market-v1",
                "code_hash": "code-hash",
                "generator_hash": "generator-hash",
                "adjustment_version": "raw-v1",
                "cohorts": {
                    candidate.decision_at: {
                        "codes": [candidate.code],
                        "universe_hash": candidate.universe_hash,
                    }
                },
                "table_hashes": {
                    "decision_candidates": strict_table_hash(
                        "decision_candidates", [candidate]
                    ),
                    "candidate_prices": "",
                },
            }
            store.import_candidate_cohorts([candidate], manifest=manifest)
            dataset_hash = store.dataset_hash("strict-1")

            args = [
                "replay",
                "--db", str(db),
                "--dataset", "strict-1",
                "--start-at", "2025-01-02T09:55:00+08:00",
                "--end-at", "2025-01-02T10:05:00+08:00",
                "--output-dir", str(output),
                "--expected-dataset-hash", dataset_hash,
                "--strategy-version", "strategy-v1",
                "--parameter-version", "parameter-v1",
                "--feature-schema-version", "feature-v1",
                "--market-data-version", "market-v1",
                "--code-hash", "code-hash",
                "--generator-hash", "generator-hash",
            ]
            with patch(
                "historical_strategy.fetch_live_quotes",
                side_effect=AssertionError("network"),
            ):
                code = main(args)

            self.assertEqual(code, 0)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"historical_decision_replay_latest.json"},
            )
            evidence_file = output / "historical_decision_replay_latest.json"
            first_bytes = evidence_file.read_bytes()
            self.assertEqual(main(args), 0)
            self.assertEqual(evidence_file.read_bytes(), first_bytes)
            payload = json.loads(first_bytes)
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["replay_mode"], "decision_time")
            self.assertEqual(payload["dataset_sha256"], dataset_hash)
            self.assertEqual(payload["batch_count"], 1)
            self.assertEqual(payload["candidate_count"], 1)
            self.assertEqual(payload["selected_count"], 1)
            self.assertEqual(
                payload["batches"][0]["decision_at"], candidate.decision_at
            )
            with store.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0],
                    0,
                )

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

    def test_walk_forward_rejects_insufficient_dates_without_fabricating_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._database(root)
            output = root / "output"

            code = main([
                "walk-forward",
                "--db", str(db),
                "--dataset", "d1",
                "--start", "2025-01-02",
                "--end", "2025-01-02",
                "--mode", "price_core",
                "--output-dir", str(output),
                "--folds", "3",
                "--holdout-days", "0",
            ])

            self.assertEqual(code, 2)
            payload = json.loads(
                (output / "historical_walk_forward_latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "rejected")
            self.assertIn("INSUFFICIENT_WALK_FORWARD_DATES", payload["reason"])

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

    def test_run_id_changes_when_position_or_score_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._database(root)
            args = SimpleNamespace(
                dataset="d1", start="2025-01-02", end="2025-01-02",
                mode="price_core", strategy_version="historical-v1",
            )
            base = HistoricalBacktestConfig()
            positions = HistoricalBacktestConfig(max_positions=4)
            score = HistoricalBacktestConfig(min_score=80.0)
            first = _run_id(HistoricalStore(db), args, base)
            self.assertNotEqual(first, _run_id(HistoricalStore(db), args, positions))
            self.assertNotEqual(first, _run_id(HistoricalStore(db), args, score))

    def test_implementation_hash_covers_strict_candidate_and_execution_contract(self) -> None:
        names = {path.name for path in _implementation_paths()}
        self.assertTrue(
            {
                "candidate_core.py",
                "exit_policy.py",
                "trade_safety.py",
                "historical_data.py",
                "historical_strategy.py",
                "historical_backtest.py",
                "ml_contracts.py",
            }.issubset(names)
        )

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

    def test_failed_run_can_be_retried_and_replaced_by_complete_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = self._database(root)
            output = root / "output"
            args = [
                "run", "--db", str(db), "--dataset", "d1", "--start", "2025-01-02",
                "--end", "2025-01-02", "--mode", "price_core", "--output-dir", str(output),
            ]
            with patch(
                "historical_backtest.run_historical_backtest",
                side_effect=RuntimeError("transient"),
            ):
                self.assertNotEqual(main(args), 0)
            self.assertEqual(main(args), 0)
            with HistoricalStore(db).connect() as connection:
                status, error = connection.execute(
                    "SELECT status, error FROM backtest_runs"
                ).fetchone()
            self.assertEqual(status, "complete")
            self.assertEqual(error, "")

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
