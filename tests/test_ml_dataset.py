import json
import tempfile
import unittest
from pathlib import Path

import ml_dataset


class MlDatasetTest(unittest.TestCase):
    def test_updates_order_labels_from_joinquant_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sample_path = Path(tmp) / "signal_samples.jsonl"
            sample = {
                "sample_version": 1,
                "sample_id": "run-1-600000-buy-0000",
                "signal": {"id": "run-1-600000-buy-0000", "action": "buy", "code": "600000"},
                "features": {"final_score": 90.0},
                "labels": {"order_status": ""},
            }
            sample_path.write_text(json.dumps(sample, ensure_ascii=False) + "\n", encoding="utf-8")
            snapshot = {
                "schema_version": 1,
                "orders": [
                    {
                        "id": "run-1-600000-buy-0000",
                        "status": "filled",
                        "reason": "submitted",
                        "order_id": "jq-123",
                        "amount": 1000,
                        "filled": 1000,
                        "price": 10.23,
                    }
                ],
            }

            updated = ml_dataset.update_order_labels(sample_path, snapshot)

            rows = [json.loads(line) for line in sample_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(updated, 1)
            self.assertEqual(rows[0]["labels"]["order_status"], "filled")
            self.assertEqual(rows[0]["labels"]["order_id"], "jq-123")
            self.assertEqual(rows[0]["labels"]["filled"], 1000.0)
            self.assertEqual(rows[0]["labels"]["order_price"], 10.23)

    def test_builds_basic_review_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sample_path = Path(tmp) / "signal_samples.jsonl"
            report_path = Path(tmp) / "ml_report.md"
            rows = [
                {
                    "sample_version": 1,
                    "signal": {"action": "buy", "code": "600000"},
                    "features": {"final_score": 91.0, "theme_heat_level": "高", "market_state": "强势进攻"},
                    "labels": {"order_status": "filled"},
                },
                {
                    "sample_version": 1,
                    "signal": {"action": "sell", "code": "000001"},
                    "features": {"final_score": 78.0, "theme_heat_level": "中", "market_state": "弱势震荡"},
                    "labels": {"order_status": "failed"},
                },
            ]
            sample_path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )

            md = ml_dataset.build_review_report(sample_path, report_path)

            self.assertIn("ML 样本复盘", md)
            self.assertIn("样本 2", md)
            self.assertIn("买入 1", md)
            self.assertIn("成交/提交 1", md)
            self.assertIn("失败/跳过 1", md)
            self.assertTrue(report_path.exists())


if __name__ == "__main__":
    unittest.main()
