import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import ml_dataset
from ml_store import MlDataConflict, MlStore


class MlDatasetTest(unittest.TestCase):
    def _context(self, **overrides):
        context = {
            "source": "joinquant_live",
            "dataset_id": "run-1",
            "decision_at": "2026-07-15T10:05:00+08:00",
            "strategy_version": "strategy-v1",
            "parameter_version": "params-v1",
            "feature_schema_version": "features-v1",
            "cohort_mode": "intraday",
            "cohort_interval_sec": 300,
            "parameter_snapshot": {"min_score": 75.0},
            "universe_hash": "universe-v1",
            "market_data_version": "market-v1",
            "code_hash": "code-v1",
            "generator_hash": "generator-v1",
        }
        context.update(overrides)
        return context

    def test_builds_complete_candidate_samples_with_strict_runtime_time(self) -> None:
        rows = pd.DataFrame([
            {"code": "600000", "price": 10.0, "final_score": 90.0},
            {"code": "000001", "price": 12.0, "final_score": 70.0},
        ])
        decisions = [
            {"code": "600000", "selected": True, "rejection_stage": "selected", "rejection_code": "", "final_action": "buy_published", "training_eligible": True},
            {"code": "000001", "selected": False, "rejection_stage": "score", "rejection_code": "buy_low_score", "final_action": "rule_rejected", "training_eligible": True},
        ]

        samples = ml_dataset.build_candidate_samples(rows, decisions, self._context())

        self.assertEqual([sample.code for sample in samples], ["600000", "000001"])
        self.assertEqual([sample.selected for sample in samples], [True, False])
        self.assertEqual(samples[0].decision_at, "2026-07-15T10:05:00+08:00")
        self.assertEqual(samples[0].features["price"].available_at, samples[0].decision_at)
        self.assertTrue(samples[0].features["training_eligible"].value)
        self.assertEqual(samples[0].features["cohort_mode"].value, "intraday")
        self.assertEqual(samples[0].final_action, "buy_published")
        self.assertEqual(samples[1].final_action, "rule_rejected")

    def test_non_intraday_candidate_is_auditable_but_not_training_eligible(self) -> None:
        samples = ml_dataset.build_candidate_samples(
            pd.DataFrame([{"code": "600000", "price": 10.0}]),
            [{"code": "600000", "selected": False, "rejection_stage": "risk", "rejection_code": "buy_disabled", "final_action": "rule_rejected", "training_eligible": True}],
            self._context(cohort_mode="after"),
        )

        self.assertFalse(samples[0].features["training_eligible"].value)
        self.assertEqual(samples[0].features["cohort_mode"].value, "after")

    def test_non_five_minute_intraday_candidate_is_not_training_eligible(self) -> None:
        samples = ml_dataset.build_candidate_samples(
            pd.DataFrame([{"code": "600000", "price": 10.0}]),
            [{"code": "600000", "selected": True, "rejection_stage": "selected", "rejection_code": "", "final_action": "buy_published", "training_eligible": True}],
            self._context(cohort_interval_sec=60),
        )

        self.assertFalse(samples[0].features["training_eligible"].value)
        self.assertEqual(samples[0].features["cohort_interval_sec"].value, 60)

    def test_record_candidate_batch_is_idempotent_and_conflicts_on_changed_content(self) -> None:
        rows = pd.DataFrame([{"code": "600000", "price": 10.0, "final_score": 90.0}])
        decisions = [{"code": "600000", "selected": True, "rejection_stage": "selected", "rejection_code": "", "final_action": "buy_published", "training_eligible": True}]
        with tempfile.TemporaryDirectory() as tmp:
            store = MlStore(Path(tmp) / "ml.db")
            store.initialize()
            self.assertEqual(ml_dataset.record_candidate_batch(rows, decisions, self._context(), store), 1)
            self.assertEqual(ml_dataset.record_candidate_batch(rows, decisions, self._context(), store), 0)
            changed = rows.copy()
            changed.loc[0, "price"] = 10.1
            with self.assertRaises(MlDataConflict):
                ml_dataset.record_candidate_batch(changed, decisions, self._context(), store)

    def test_candidate_decisions_must_match_rows_exactly(self) -> None:
        rows = pd.DataFrame([{"code": "600000", "price": 10.0}])
        with self.assertRaisesRegex(ValueError, "CANDIDATE_DECISION_SET_MISMATCH"):
            ml_dataset.build_candidate_samples(rows, [], self._context())

    def test_rule_selected_but_control_blocked_candidate_is_audit_only(self) -> None:
        sample = ml_dataset.build_candidate_samples(
            pd.DataFrame([{"code": "600000", "price": 10.0}]),
            [{"code": "600000", "selected": True, "rejection_stage": "selected", "rejection_code": "", "final_action": "buy_blocked_kill_switch", "training_eligible": False}],
            self._context(),
        )[0]

        self.assertTrue(sample.selected)
        self.assertEqual(sample.final_action, "buy_blocked_kill_switch")
        self.assertFalse(sample.features["training_eligible"].value)

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
                    "features": {
                        "final_score": 91.0,
                        "enhanced_score": 94.0,
                        "theme_heat_level": "高",
                        "market_state": "强势进攻",
                    },
                    "labels": {"order_status": "filled"},
                },
                {
                    "sample_version": 1,
                    "signal": {"action": "sell", "code": "000001"},
                    "features": {
                        "final_score": 78.0,
                        "enhanced_score": 70.0,
                        "theme_heat_level": "中",
                        "market_state": "弱势震荡",
                    },
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
            self.assertIn("## 影子评分分布", md)
            self.assertIn("90+: 1", md)
            self.assertTrue(report_path.exists())


if __name__ == "__main__":
    unittest.main()
