import json
import tempfile
import unittest
from pathlib import Path

import joinquant_readiness_report
from trading_store import TradingStore


class JoinQuantReadinessReportTest(unittest.TestCase):
    def test_missing_ledger_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            snapshot_file.write_text(json.dumps({"schema_version": 1, "positions": []}), encoding="utf-8")

            result = joinquant_readiness_report.build_report(signal_file, snapshot_file, base / "report.md", db_file=base / "missing" / "trading.db")

            self.assertFalse(result["ledger_ok"])
            self.assertIn("SQLite 交易账本: 未就绪", (base / "report.md").read_text(encoding="utf-8"))

    def test_initialized_v1_ledger_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
            snapshot_file.write_text(json.dumps({"schema_version": 1, "positions": []}), encoding="utf-8")
            db_file = base / "trading.db"
            TradingStore(db_file).initialize()

            result = joinquant_readiness_report.build_report(signal_file, snapshot_file, base / "report.md", db_file=db_file)

            self.assertTrue(result["ledger_ok"])
            self.assertIn("SQLite 交易账本: 正常", (base / "report.md").read_text(encoding="utf-8"))
    def test_populated_json_with_empty_ledger_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            signal_file = base / "signals.json"
            snapshot_file = base / "account.json"
            signal_file.write_text(json.dumps({"schema_version": 1, "signals": [{"id": "s1", "action": "buy"}]}), encoding="utf-8")
            snapshot_file.write_text(json.dumps({"schema_version": 1, "positions": []}), encoding="utf-8")
            db_file = base / "trading.db"
            TradingStore(db_file).initialize()
            result = joinquant_readiness_report.build_report(signal_file, snapshot_file, base / "report.md", db_file=db_file)
            self.assertFalse(result["ledger_json_parity"])
            self.assertEqual(result["conclusion"], "keep_dry_run")

    def test_reports_basic_readiness_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal_file = Path(tmp) / "signals.json"
            snapshot_file = Path(tmp) / "account.json"
            report_file = Path(tmp) / "report.md"
            signal_file.write_text(
                json.dumps({"schema_version": 1, "signals": [{"id": "s1", "position_pct": 20}]}),
                encoding="utf-8",
            )
            snapshot_file.write_text(
                json.dumps({"schema_version": 1, "positions": [{"code": "600000"}]}),
                encoding="utf-8",
            )

            result = joinquant_readiness_report.build_report(signal_file, snapshot_file, report_file)

            self.assertEqual(result["signal_count"], 1)
            self.assertTrue(result["snapshot_ok"])
            self.assertIn("JoinQuant Readiness", report_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
