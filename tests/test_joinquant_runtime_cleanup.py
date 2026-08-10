import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import joinquant_runtime_cleanup
import joinquant_sync
from trading_store import TradingStore


class JoinQuantRuntimeCleanupTest(unittest.TestCase):
    @staticmethod
    def _paths(root: Path) -> dict[str, Path]:
        project = root / "project"
        joinquant = project / "cache" / "joinquant"
        portfolio = project / "cache" / "portfolio_web"
        joinquant.mkdir(parents=True)
        portfolio.mkdir(parents=True)
        return {
            "project": project,
            "backup": root / "backups",
            "db": project / "cache" / "trading" / "trading.db",
            "account": joinquant / "account_snapshot.json",
            "positions": portfolio / "positions.json",
            "history": joinquant / "account_snapshot_history.jsonl",
            "api": joinquant / "api_events.jsonl",
            "portfolio_events": portfolio / "events.jsonl",
        }

    @staticmethod
    def _incident_snapshot() -> dict:
        return {
            "schema_version": 1,
            "trade_date": "2026-08-08",
            "generated_at": "2026-08-08 03:20:00",
            "received_at": "2026-08-08 03:20:01",
            "source": "joinquant",
            "strategy_template_version": "2026-07-18.1-gap-reentry",
            "cash": 98_824.076,
            "available_cash": 98_824.076,
            "total_value": 98_824.076,
            "positions": [],
            "orders": [],
            "trades": [],
        }

    def _seed_incident(self, paths: dict[str, Path]) -> None:
        store = TradingStore(paths["db"])
        payload = self._incident_snapshot()
        joinquant_sync.ingest_snapshot_payload(
            payload, store, payload["received_at"], mode="full",
        )
        paths["account"].write_text(json.dumps(payload), encoding="utf-8")
        paths["positions"].write_text(json.dumps({
            "updated_at": "2026-08-10 12:00:00",
            "source": "joinquant",
            "account": {"trade_date": "2026-08-08"},
            "positions": [],
        }), encoding="utf-8")
        safe_snapshot = dict(payload)
        safe_snapshot.update({
            "trade_date": "2026-08-07",
            "generated_at": "2026-08-07 15:05:00",
            "received_at": "2026-08-07 15:05:01",
        })
        paths["history"].write_text(
            json.dumps(payload) + "\n" + json.dumps(safe_snapshot) + "\n",
            encoding="utf-8",
        )
        paths["api"].write_text(
            json.dumps({
                "received_at": "2026-08-08 03:20:01",
                "endpoint": "signals", "status_code": 200,
            }) + "\n" + json.dumps({
                "received_at": "2026-08-10 10:00:00",
                "endpoint": "health", "status_code": 200,
            }) + "\n",
            encoding="utf-8",
        )
        paths["portfolio_events"].write_text(
            json.dumps({
                "ts": "2026-08-08 03:20:02", "action": "joinquant_sync", "count": 0,
            }) + "\n" + json.dumps({
                "ts": "2026-08-10 10:00:00", "action": "manual_note", "count": 0,
            }) + "\n",
            encoding="utf-8",
        )
        with store.transaction() as conn:
            conn.execute(
                """INSERT INTO strategy_runs(
                   run_id, trade_date, started_at, finished_at, git_commit,
                   strategy_version, parameters_version, data_status, result,
                   error_message, created_at, updated_at
                   ) VALUES ('real-scan', '2026-08-10', '2026-08-10 09:30:00',
                   '2026-08-10 09:31:00', '', '', '', 'ok', 'success', '',
                   '2026-08-10 09:30:00', '2026-08-10 09:31:00')"""
            )

    @staticmethod
    def _inspect(paths: dict[str, Path]) -> dict:
        return joinquant_runtime_cleanup.inspect_incident(
            paths["db"], paths["account"], paths["positions"],
            paths["history"], paths["api"], paths["portfolio_events"],
            cleanup_end="2026-08-10 23:59:59",
        )

    def test_inspection_identifies_only_incident_chain_and_preserves_real_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._seed_incident(paths)

            result = self._inspect(paths)

            self.assertEqual(result["counts"]["account_snapshots"], 1)
            self.assertEqual(result["counts"]["reconciliation_runs"], 1)
            self.assertEqual(result["counts"]["daily_equity"], 1)
            self.assertEqual(result["counts"]["api_event_lines"], 1)
            self.assertEqual(result["counts"]["derived_portfolio_event_lines"], 1)
            self.assertEqual(result["period_summary"]["strategy_runs"]["2026-08-10"], 1)
            self.assertFalse(any(result["blockers"].values()))

    def test_retention_pruned_payload_is_audited_but_independent_material_evidence_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._seed_incident(paths)
            with closing(sqlite3.connect(paths["db"])) as conn:
                conn.execute("UPDATE account_snapshots SET raw_json=NULL")
                conn.commit()

            result = self._inspect(paths)

            self.assertEqual(
                result["warnings"]["retention_pruned_snapshot_payloads"], 1,
            )
            self.assertFalse(any(result["blockers"].values()))

            with closing(sqlite3.connect(paths["db"])) as conn:
                conn.execute(
                    "UPDATE account_snapshots SET position_market_value=100"
                )
                conn.commit()
            blocked = self._inspect(paths)
            self.assertEqual(
                blocked["blockers"]["nonzero_position_market_value_rows"], 1,
            )

    def test_apply_creates_verified_quarantine_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._seed_incident(paths)

            result = joinquant_runtime_cleanup.apply_incident_cleanup(
                paths["db"], paths["account"], paths["positions"],
                paths["history"], paths["api"], paths["portfolio_events"],
                paths["backup"], paths["project"],
                cleanup_end="2026-08-10 23:59:59",
            )

            self.assertEqual(result["status"], "completed")
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["pre_cleanup_backup"]["integrity_check"], "ok")
            self.assertTrue(Path(manifest["pre_cleanup_backup"]["backup_file"]).is_file())
            self.assertFalse(paths["account"].exists())
            self.assertFalse(paths["positions"].exists())
            self.assertEqual(len(paths["history"].read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(len(paths["api"].read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(
                json.loads(paths["portfolio_events"].read_text(encoding="utf-8"))["action"],
                "manual_note",
            )
            with closing(sqlite3.connect(paths["db"])) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM account_snapshots WHERE trade_date='2026-08-08'"
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM strategy_runs WHERE run_id='real-scan'"
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

            repeated = joinquant_runtime_cleanup.apply_incident_cleanup(
                paths["db"], paths["account"], paths["positions"],
                paths["history"], paths["api"], paths["portfolio_events"],
                paths["backup"], paths["project"],
                cleanup_end="2026-08-10 23:59:59",
            )
            self.assertEqual(repeated["status"], "already_completed")

    def test_apply_refuses_referenced_incident_before_backup_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            self._seed_incident(paths)
            store = TradingStore(paths["db"])
            with store.transaction() as conn:
                reconciliation_id = conn.execute(
                    "SELECT reconciliation_id FROM reconciliation_runs LIMIT 1"
                ).fetchone()[0]
                conn.execute(
                    """INSERT INTO control_events(
                       event_id, action, operator, old_value, new_value, reason,
                       reconciliation_id, created_at
                       ) VALUES ('control-1', 'stop_buy', 'test', '1', '0',
                       'material reference', ?, '2026-08-08 03:20:02')""",
                    (reconciliation_id,),
                )

            with self.assertRaises(joinquant_runtime_cleanup.CleanupRefused):
                joinquant_runtime_cleanup.apply_incident_cleanup(
                    paths["db"], paths["account"], paths["positions"],
                    paths["history"], paths["api"], paths["portfolio_events"],
                    paths["backup"], paths["project"],
                    cleanup_end="2026-08-10 23:59:59",
                )

            self.assertFalse((paths["backup"] / "quarantine").exists())
            self.assertTrue(paths["account"].exists())


if __name__ == "__main__":
    unittest.main()
