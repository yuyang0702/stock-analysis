import json
import tempfile
import unittest
from pathlib import Path

import joinquant_readiness_report


class JoinQuantReadinessReportTest(unittest.TestCase):
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
