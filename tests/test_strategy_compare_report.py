import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import strategy_compare_report


class StrategyCompareReportTest(unittest.TestCase):
    def test_updates_return_labels_from_price_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sample_path = Path(tmp) / "samples.jsonl"
            sample = {
                "sample_id": "s1",
                "trade_date": "2026-07-01",
                "code": "600000",
                "signal": {"action": "buy", "price": 10.0, "code": "600000"},
                "features": {"final_score": 90, "enhanced_score": 95},
                "labels": {},
            }
            sample_path.write_text(json.dumps(sample, ensure_ascii=False) + "\n", encoding="utf-8")

            def history_provider(code: str, start_date: str) -> pd.DataFrame:
                self.assertEqual(code, "600000")
                return pd.DataFrame(
                    [
                        {"date": "2026-07-01", "close": 10.0, "high": 10.2, "low": 9.8},
                        {"date": "2026-07-02", "close": 10.5, "high": 10.8, "low": 10.1},
                        {"date": "2026-07-06", "close": 11.0, "high": 11.3, "low": 10.4},
                        {"date": "2026-07-08", "close": 9.6, "high": 11.5, "low": 9.4},
                    ]
                )

            updated = strategy_compare_report.update_return_labels(sample_path, history_provider=history_provider)

            rows = [json.loads(line) for line in sample_path.read_text(encoding="utf-8").splitlines()]
            labels = rows[0]["labels"]
            self.assertEqual(updated, 1)
            self.assertAlmostEqual(labels["ret_1d"], 5.0)
            self.assertAlmostEqual(labels["ret_3d"], -4.0)
            self.assertAlmostEqual(labels["max_favorable_excursion"], 15.0)
            self.assertAlmostEqual(labels["max_adverse_excursion"], -6.0)

    def test_builds_compare_report_and_weekly_alert(self) -> None:
        rows = []
        for idx in range(6):
            rows.append(
                {
                    "sample_id": f"base-{idx}",
                    "trade_date": "2026-07-01",
                    "signal": {"action": "buy", "code": f"60000{idx}"},
                    "features": {"final_score": 95 - idx, "enhanced_score": 70 + idx},
                    "labels": {"ret_3d": 1.0, "ret_5d": 1.5, "max_adverse_excursion": -3.0},
                }
            )
        for idx in range(6):
            rows.append(
                {
                    "sample_id": f"shadow-{idx}",
                    "trade_date": "2026-07-02",
                    "signal": {"action": "buy", "code": f"00000{idx}"},
                    "features": {"final_score": 70 + idx, "enhanced_score": 95 - idx},
                    "labels": {"ret_3d": 3.0, "ret_5d": 4.0, "max_adverse_excursion": -1.0},
                }
            )

        result = strategy_compare_report.compare_strategies(rows, min_samples=5)
        md = strategy_compare_report.build_report_markdown(result)
        alert = strategy_compare_report.build_weekly_alert_markdown(result)

        self.assertEqual(result["conclusion"], "影子评分本期占优，但仍仅观察，不参与下单。")
        self.assertIn("原策略 Top5", md)
        self.assertIn("影子评分 Top5", md)
        self.assertIn("【策略对照】周度复盘", alert)
        self.assertIn("影子评分本期占优", alert)


if __name__ == "__main__":
    unittest.main()
