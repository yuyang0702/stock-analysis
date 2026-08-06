import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import strategy_compare_report


class StrategyCompareReportTest(unittest.TestCase):
    def test_trusted_rows_compare_rule_topn_with_model_topn_and_bounded_evidence(self) -> None:
        rows = []
        for batch in ("2026-08-01T09:35:00+08:00", "2026-08-01T09:40:00+08:00"):
            for idx in range(12):
                rows.append(
                    {
                        "sample_id": f"{batch}-{idx}",
                        "decision_at": batch,
                        "trade_date": "2026-08-01",
                        "rule_selected": True,
                        "rule_final_action": "buy",
                        "rule_score": 90 - idx,
                        "rule_order": idx,
                        "model_id": "ml-1",
                        "ml_score": idx,
                        "ret_3d_net": 0.01,
                        "ret_5d_net": 0.02 if idx < 5 else -0.01,
                        "ret_10d_net": 0.03,
                        "downside_loss": 0.01,
                        "fill_label": 1,
                        "confidence": 0.8,
                        "max_feature_psi": 0.05,
                        "drift_status": "ready",
                    }
                )

        result = strategy_compare_report.compare_trained_model_rows(
            rows, top_n=5, min_samples=5,
            runtime={"last_trading_equivalent": True},
        )

        self.assertTrue(result["model_available"])
        self.assertEqual(result["model_ids"], ["ml-1"])
        self.assertEqual(result["base"]["count"], 10)
        self.assertEqual(result["model"]["count"], 10)
        self.assertEqual(result["l0_trading_equivalence"], "observed_equal")
        self.assertEqual(result["label_coverage"]["d5"], 100.0)
        self.assertEqual(result["model"]["confidence_coverage"], 100.0)

    def test_aggregate_coverage_keeps_missing_predictions_in_denominator(self) -> None:
        rows = [{
            "sample_id": "selected-1",
            "decision_at": "2026-08-01T09:35:00+08:00",
            "trade_date": "2026-08-01",
            "rule_selected": True,
            "rule_final_action": "buy",
            "rule_score": 90,
            "model_id": "ml-1",
            "ml_score": 80,
            "ret_3d_net": 0.01,
            "ret_5d_net": 0.02,
            "downside_loss": 0.01,
            "fill_label": 1,
        }]
        result = strategy_compare_report.compare_trained_model_rows(
            rows,
            min_samples=1,
            coverage_evidence={
                "sample_count": 100,
                "prediction_count": 50,
                "fill_count": 40,
                "d3_count": 30,
                "d5_count": 20,
                "d10_count": 10,
                "downside_count": 15,
            },
        )
        self.assertEqual(result["sample_count"], 100)
        self.assertEqual(result["prediction_availability"], 50.0)
        self.assertEqual(result["label_coverage"]["d5"], 20.0)
        self.assertEqual(result["model"]["avg_ret_5d"], 2.0)

        result_with_evidence = strategy_compare_report.compare_trained_model_rows(
            rows,
            top_n=5,
            min_samples=5,
            runtime={
                "active_model_id": "ml-1",
                "permission_level": 0,
                "health_status": "ok",
                "health_reason": "healthy",
                "last_prediction_count": 24,
                "last_trading_equivalent": True,
            },
            model_evidence={
                "model_id": "ml-1",
                "status": "challenger",
                "permission_level": 0,
                "artifact_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
            },
        )
        markdown = strategy_compare_report.build_report_markdown(result_with_evidence)
        self.assertIn("artifact SHA-256", markdown)
        self.assertIn("manifest SHA-256", markdown)
        self.assertIn("健康状态：ok", markdown)
        self.assertIn("当前权限：L0", markdown)

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

    def test_legacy_rule_shadow_rows_report_trained_model_unavailable(self) -> None:
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

        result = strategy_compare_report.compare_strategies(rows, min_samples=5)
        md = strategy_compare_report.build_report_markdown(result)

        self.assertFalse(result["model_available"])
        self.assertIn("unavailable", result["conclusion"])
        self.assertIn("原规则策略 Top5", md)
        self.assertIn("训练模型 Top5", md)
        self.assertIn("unavailable", md)
        self.assertNotIn("影子", md)

    def test_untrusted_jsonl_prediction_cannot_claim_a_trained_model(self) -> None:
        rows = []
        for idx in range(6):
            rows.append({
                "sample_id": f"model-{idx}",
                "trade_date": "2026-07-02",
                "signal": {"action": "buy", "code": f"00000{idx}"},
                "features": {"final_score": 95 - idx},
                "prediction": {"model_id": "model-v1", "ml_score": 70 + idx},
                "labels": {
                    "ret_3d": 1.0 + idx,
                    "ret_5d": 1.5 + idx,
                    "max_adverse_excursion": -3.0 + idx * 0.2,
                },
            })

        result = strategy_compare_report.compare_strategies(rows, min_samples=5)
        md = strategy_compare_report.build_report_markdown(result)

        self.assertFalse(result["model_available"])
        self.assertEqual(result["model_ids"], [])
        self.assertIn("unavailable", result["conclusion"])
        self.assertNotIn("model-v1", md)
        self.assertNotIn("影子", md)


if __name__ == "__main__":
    unittest.main()
