import math
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import pandas as pd

import ml_runtime
from ml_runtime import (
    LoadedBundle,
    apply_model_policy,
    compute_counterfactual_economics,
    compute_confidence,
    compute_feature_psi,
    frozen_midrank_pct,
    load_active_bundle,
    observe_candidate_frame,
    predict_candidate_frame,
    runtime_dependency_versions,
)


class ConstantRegressor:
    def __init__(self, value):
        self.value = value

    def predict(self, rows):
        return [self.value for _ in range(len(rows))]


class ConstantClassifier:
    def __init__(self, value):
        self.value = value
        self.classes_ = [0, 1]

    def predict_proba(self, rows):
        return [[1 - self.value, self.value] for _ in range(len(rows))]


def bundle() -> LoadedBundle:
    manifest = {
        "model_id": "model-v1",
        "required_features": ["score", "turnover"],
        "oof_references": {
            "ret_5d": [-0.02, 0.00, 0.01, 0.02],
            "downside": [0.01, 0.02, 0.03, 0.04],
        },
        "residual_q80": {
            "ret_5d": 0.005,
            "downside": 0.005,
            "fill": 0.05,
        },
        "downside_oof_prediction_p80": 0.035,
        "d5_difference_p90": 0.02,
        "drift_reference": {
            "score": {
                "kind": "numeric",
                "bins": [70.0, 80.0, 90.0],
                "counts": [20, 30, 30, 20, 0],
            },
            "turnover": {
                "kind": "categorical",
                "categories": ["LOW", "HIGH", "OTHER", "MISSING"],
                "counts": [40, 40, 10, 10],
            },
        },
    }
    return LoadedBundle(
        model_id="model-v1",
        manifest=manifest,
        models={
            "ret_3d": ConstantRegressor(0.01),
            "ret_5d": ConstantRegressor(0.015),
            "ret_10d": ConstantRegressor(0.02),
            "downside": ConstantRegressor(0.02),
            "fill": ConstantClassifier(0.80),
            "ridge_ret_5d": ConstantRegressor(0.014),
        },
        artifact_dir=Path("."),
        artifact_sha256="a" * 64,
    )


class FakeRuntimeStore:
    def __init__(self, *, artifact_path: str, artifact_sha256: str):
        self.runtime = {
            "active_model_id": "model-v1",
            "permission_level": 0,
            "updated_at": "2026-08-06T10:00:00+08:00",
        }
        self.record = {
            "model_id": "model-v1",
            "parent_model_id": None,
            "status": "approvable_l0",
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha256,
            "strategy_version": "strategy-v1",
        }
        self.predictions = []
        self.health = []
        self.recent_rows = []

    def runtime_state(self):
        return dict(self.runtime)

    def model_record(self, model_id):
        return dict(self.record) if model_id == "model-v1" else None

    def approved_model_event(self, model_id, artifact_sha256):
        if model_id == "model-v1" and artifact_sha256 == self.record["artifact_sha256"]:
            return {"new_level": 0}
        return None

    def record_predictions(self, records):
        self.predictions.extend(records)
        return len(records)

    def recent_prediction_candidate_rows(self, model_id, *, batch_limit=20, row_limit=1000):
        del model_id, batch_limit, row_limit
        return list(self.recent_rows)

    def record_runtime_health(self, **values):
        self.health.append(dict(values))


def write_bundle(root: Path) -> FakeRuntimeStore:
    directory = root / "model-v1"
    directory.mkdir(parents=True)
    joblib.dump(
        {
            "generated_by": "stock-analysis",
            "bundle_schema_version": "five-head-bundle-v1",
            "manifest_schema_version": "five-head-bundle-v1",
            "model_id": "model-v1",
            "models": dict(bundle().models),
        },
        directory / "bundle.joblib",
        compress=0,
        protocol=5,
    )
    manifest = {
        **dict(bundle().manifest),
        "generated_by": "stock-analysis",
        "manifest_schema_version": "five-head-bundle-v1",
        "status": "approvable_l0",
        "strategy_version": "strategy-v1",
        "dependency_versions": runtime_dependency_versions(),
        "files": {
            "bundle.joblib": ml_runtime._file_sha256(directory / "bundle.joblib"),
        },
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return FakeRuntimeStore(
        artifact_path="model-v1",
        artifact_sha256=ml_runtime._bundle_sha256(directory),
    )


class MlRuntimeTest(unittest.TestCase):
    def test_frozen_midrank_handles_ties_and_out_of_range(self) -> None:
        reference = [1.0, 2.0, 2.0, 4.0]
        self.assertEqual(frozen_midrank_pct(0.0, reference), 0.0)
        self.assertEqual(frozen_midrank_pct(5.0, reference), 100.0)
        self.assertEqual(frozen_midrank_pct(2.0, reference), 50.0)
        self.assertIsNone(frozen_midrank_pct(2.0, []))
        self.assertIsNone(frozen_midrank_pct(math.nan, reference))

    def test_numeric_and_categorical_psi_use_frozen_buckets(self) -> None:
        frame = pd.DataFrame({
            "score": [65.0, 75.0, 85.0, 95.0, None],
            "turnover": ["LOW", "HIGH", "NEW", None, "LOW"],
        })
        result = compute_feature_psi(frame, bundle().manifest["drift_reference"])
        self.assertEqual(set(result), {"score", "turnover"})
        self.assertTrue(all(value >= 0 for value in result.values()))

    def test_confidence_is_minimum_of_coverage_disagreement_and_drift(self) -> None:
        result = compute_confidence(
            required_features=10,
            provided_features=9,
            challenger_d5=0.02,
            ridge_d5=0.01,
            d5_difference_p90=0.02,
            max_feature_psi=0.05,
            drift_sufficient=True,
        )
        self.assertAlmostEqual(result.coverage, 0.9)
        self.assertAlmostEqual(result.disagreement, 0.5)
        self.assertAlmostEqual(result.drift, 0.8)
        self.assertAlmostEqual(result.value, 0.5)

    def test_prediction_score_is_independent_of_current_batch_members(self) -> None:
        one = pd.DataFrame([{"sample_id": "s1", "score": 85.0, "turnover": "HIGH"}])
        many = pd.concat([
            one,
            pd.DataFrame([
                {"sample_id": "s2", "score": 70.0, "turnover": "LOW"},
                {"sample_id": "s3", "score": 95.0, "turnover": "HIGH"},
            ]),
        ], ignore_index=True)
        first = predict_candidate_frame(one, bundle()).iloc[0]
        second = predict_candidate_frame(many, bundle()).iloc[0]
        self.assertEqual(first["ml_score"], second["ml_score"])

    def test_l0_is_field_for_field_trading_equivalent(self) -> None:
        rules = pd.DataFrame([
            {"code": "600001", "target_qty": 100, "final_score": 80.0},
            {"code": "600002", "target_qty": 200, "final_score": 79.0},
        ])
        predictions = pd.DataFrame([
            {"code": "600001", "ml_score": 10.0, "ml_filter": 1, "position_multiplier": 0.8},
            {"code": "600002", "ml_score": 90.0, "ml_filter": 0, "position_multiplier": 1.1},
        ])
        result = apply_model_policy(rules, predictions, level=0)
        pd.testing.assert_frame_equal(result, rules)

    def test_l1_sorts_only_l2_deletes_only_and_l3_never_expands_set(self) -> None:
        rules = pd.DataFrame([
            {"code": "600001", "target_qty": 500, "final_score": 90.0},
            {"code": "600002", "target_qty": 200, "final_score": 80.0},
            {"code": "600003", "target_qty": 300, "final_score": 70.0},
        ])
        predictions = pd.DataFrame([
            {"code": "600001", "ml_score": 30.0, "ml_filter": 0, "position_multiplier": 0.8},
            {"code": "600002", "ml_score": 90.0, "ml_filter": 1, "position_multiplier": 1.1},
            {"code": "600003", "ml_score": 60.0, "ml_filter": 0, "position_multiplier": 1.0},
        ])
        l1 = apply_model_policy(rules, predictions, level=1)
        self.assertEqual(l1["code"].tolist(), ["600002", "600003", "600001"])
        self.assertEqual(sorted(l1["target_qty"]), [200, 300, 500])
        l2 = apply_model_policy(rules, predictions, level=2)
        self.assertEqual(set(l2["code"]), {"600001", "600003"})
        l3 = apply_model_policy(rules, predictions, level=3)
        self.assertEqual(set(l3["code"]), {"600001", "600003"})
        self.assertEqual(l3.set_index("code").loc["600001", "target_qty"], 400)
        self.assertEqual(l3.set_index("code").loc["600003", "target_qty"], 300)

    def test_policy_never_reorders_or_filters_sells_and_ineligible_rows(self) -> None:
        rules = pd.DataFrame([
            {
                "code": "600001", "action": "sell", "target_qty": 0,
                "rule_eligible": True, "final_score": 10.0,
            },
            {
                "code": "600002", "action": "buy", "target_qty": 500,
                "rule_eligible": True, "final_score": 90.0,
            },
            {
                "code": "600003", "action": "buy", "target_qty": 300,
                "rule_eligible": False, "final_score": 80.0,
            },
        ])
        predictions = pd.DataFrame([
            {"code": "600001", "ml_score": 100.0, "ml_filter": 1, "position_multiplier": 0.8},
            {"code": "600002", "ml_score": 10.0, "ml_filter": 1, "position_multiplier": 0.8},
            {"code": "600003", "ml_score": 99.0, "ml_filter": 1, "position_multiplier": 0.8},
        ])
        l1 = apply_model_policy(rules, predictions, level=1)
        self.assertEqual(l1["code"].tolist(), rules["code"].tolist())
        l2 = apply_model_policy(rules, predictions, level=2)
        self.assertEqual(l2["code"].tolist(), ["600001", "600003"])
        l3 = apply_model_policy(rules, predictions, level=3)
        self.assertEqual(l3["code"].tolist(), ["600001", "600003"])
        self.assertEqual(l3.iloc[0]["target_qty"], 0)
        self.assertEqual(l3.iloc[1]["target_qty"], 300)

    def test_verified_bundle_loads_and_l0_predictions_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = write_bundle(root)
            loaded = load_active_bundle(
                store,
                root,
                {"strategy_version": "strategy-v1", **runtime_dependency_versions()},
            )
            self.assertEqual(loaded.model_id, "model-v1")
            frame = pd.DataFrame([
                {"sample_id": "s1", "code": "600001", "score": 85.0, "turnover": "HIGH"},
            ])
            drift = pd.concat([frame] * 200, ignore_index=True)
            result = observe_candidate_frame(
                store,
                root,
                frame,
                expected_versions={
                    "strategy_version": "strategy-v1",
                    **runtime_dependency_versions(),
                },
                created_at="2026-08-06T10:05:00+08:00",
                timeout_sec=1.0,
                max_permission_level=0,
                drift_frame=drift,
                drift_batch_count=20,
            )
            self.assertEqual(result.status, "observed_l0")
            self.assertEqual(result.prediction_count, 1)
            self.assertEqual(result.recorded_count, 1)
            self.assertEqual(len(store.predictions), 1)

    def test_runtime_uses_bounded_store_drift_history_and_records_health_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = write_bundle(root)
            store.recent_rows = [
                {
                    "sample_id": f"old-{index}",
                    "decision_at": f"2026-08-{index // 10 + 1:02d}T09:35:00+08:00",
                    "score": 85.0,
                    "turnover": "HIGH",
                }
                for index in range(200)
            ]
            frame = pd.DataFrame([
                {"sample_id": "s1", "code": "600001", "score": 85.0, "turnover": "HIGH"},
            ])

            result = observe_candidate_frame(
                store,
                root,
                frame,
                expected_versions={"strategy_version": "strategy-v1", **runtime_dependency_versions()},
                created_at="2026-08-06T10:05:00+08:00",
                timeout_sec=1.0,
                max_permission_level=0,
            )

            self.assertEqual(result.status, "observed_l0")
            self.assertEqual(len(store.health), 1)
            self.assertEqual(store.health[0]["status"], "ok")
            self.assertTrue(store.health[0]["trading_equivalent"])
            self.assertEqual(store.predictions[0].drift_status, "ready")
            self.assertEqual(store.predictions[0].feature_coverage, 1.0)

    def test_bundle_path_escape_and_file_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = write_bundle(root)
            store.record["artifact_path"] = "../outside"
            with self.assertRaisesRegex(ml_runtime.ModelVerificationError, "MODEL_PATH_ESCAPE"):
                load_active_bundle(store, root, {})

            store = write_bundle(root / "second")
            bundle_path = root / "second" / "model-v1" / "bundle.joblib"
            bundle_path.write_bytes(bundle_path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                ml_runtime.ModelVerificationError,
                "MODEL_FILE_HASH_MISMATCH",
            ):
                load_active_bundle(store, root / "second", {})

    def test_timeout_falls_back_without_recording_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = write_bundle(root)
            frame = pd.DataFrame([
                {"sample_id": "s1", "code": "600001", "score": 85.0, "turnover": "HIGH"},
            ])

            def slow_predict(*args, **kwargs):
                time.sleep(0.02)
                return predict_candidate_frame(*args, **kwargs)

            with patch("ml_runtime.predict_candidate_frame", side_effect=slow_predict):
                result = observe_candidate_frame(
                    store,
                    root,
                    frame,
                    expected_versions={},
                    created_at="2026-08-06T10:05:00+08:00",
                    timeout_sec=0.001,
                )
            self.assertEqual(result.status, "fallback_rules")
            self.assertIn("ML_INFERENCE_TIMEOUT", result.reasons)
            self.assertEqual(store.predictions, [])

    def test_counterfactual_economics_never_discounts_hard_risk(self) -> None:
        result = compute_counterfactual_economics(
            order_notional_yuan=10_000,
            predicted_gross_return=0.03,
            fill_probability=0.50,
            confidence=0.50,
            actual_round_trip_cost_yuan=20,
            predicted_downside_loss=0.04,
            downside_residual_q80=0.01,
            reference_round_trip_cost_rate=0.005,
            rule_stop_loss_yuan=300,
        )
        self.assertAlmostEqual(result.conditional_net_pnl_yuan, 280.0)
        self.assertAlmostEqual(result.unconditional_expected_net_pnl_yuan, 140.0)
        self.assertAlmostEqual(result.conservative_edge_yuan, 70.0)
        self.assertAlmostEqual(result.filled_downside_yuan, 470.0)
        self.assertAlmostEqual(result.hard_risk_yuan, 470.0)


if __name__ == "__main__":
    unittest.main()
