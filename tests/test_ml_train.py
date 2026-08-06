from __future__ import annotations

import hashlib
import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import joblib
import pandas as pd

from ml_contracts import canonical_hash
from ml_runtime import (
    LoadedBundle,
    ModelVerificationError,
    load_active_bundle,
    predict_candidate_frame,
)
from ml_training_data import TrainingFrame, build_ml_splits, build_training_frame
from ml_train import (
    TrainingConfig,
    evaluate_performance_gates,
    train_challenger,
)


FEATURES = ("feature_x", "feature_y", "market_regime")


def _business_dates(start: date, count: int) -> list[str]:
    result: list[str] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _frame(*, constant_d5: bool = False) -> TrainingFrame:
    rows: list[dict[str, object]] = []
    dates = _business_dates(date(2025, 1, 2), 120)
    regimes = ("NORMAL", "CAUTION", "RISK_OFF")
    for day_index, trade_date in enumerate(dates):
        for stock_index in range(8):
            feature_x = (stock_index - 3.5) / 3.5
            feature_y = math.sin((day_index + 1) / 9.0) + stock_index * 0.03
            fill = int((stock_index + day_index) % 4 != 0)
            signal = 0.025 * feature_x + 0.018 * feature_y
            nonlinear = 0.012 * feature_x * feature_x * (1 if feature_x > 0 else -1)
            ret_5d = 0.0 if constant_d5 else signal + nonlinear
            rows.append(
                {
                    "sample_id": f"sample-{day_index:03d}-{stock_index:02d}",
                    "source": "strict",
                    "dataset_id": "strict-fixture-v1",
                    "trade_date": trade_date,
                    "decision_at": f"{trade_date}T10:00:00+08:00",
                    "code": f"{stock_index:06d}",
                    "strategy_version": "strategy-v1",
                    "parameter_version": "params-v1",
                    "feature_schema_version": "features-v1",
                    "label_version": "labels-v2",
                    "label_source": "strict_counterfactual_v2",
                    "cost_version": "fees-v2",
                    "cost_sha256": "c" * 64,
                    "policy_version": "labels-policy-v2",
                    "policy_sha256": "p" * 64,
                    "feature_x": feature_x,
                    "feature_y": feature_y,
                    "market_regime": regimes[day_index % len(regimes)],
                    "fill_label": fill,
                    "ret_3d_net": 0.75 * signal + 0.004 * feature_x,
                    "ret_5d_net": ret_5d,
                    "ret_10d_net": 1.2 * signal + 0.006 * feature_y,
                    "downside_loss": max(
                        0.001,
                        0.035 - 0.011 * feature_x + 0.003 * abs(feature_y),
                    ),
                    "hit_stop": int(feature_x < -0.25),
                    "fill_matured_at": f"{trade_date}T10:15:00+08:00",
                    "matured_3d_at": f"{trade_date}T16:00:00+08:00",
                    "matured_5d_at": f"{trade_date}T16:00:00+08:00",
                    "matured_10d_at": f"{trade_date}T16:00:00+08:00",
                    "downside_matured_at": f"{trade_date}T16:00:00+08:00",
                    "sample_weight": 1.0,
                    # These are audit-only columns, never model features.  They make
                    # the counterfactual gate evaluable in this focused fixture.
                    "rule_score": 100.0 - stock_index,
                    "rule_order": stock_index,
                    "rule_eligible": 1,
                    "rule_selected": int(stock_index < 5),
                    "rule_rejection_stage": "" if stock_index < 5 else "score",
                    "rule_rejection_code": "" if stock_index < 5 else "OUTSIDE_TOP5",
                    "rule_final_action": (
                        "buy_published" if stock_index < 5 else "rule_rejected"
                    ),
                    "rule_target_qty": 100,
                    "rule_slot_count": 5,
                }
            )
    return TrainingFrame(
        rows=pd.DataFrame.from_records(rows),
        feature_names=FEATURES,
        metadata={
            "source": "strict",
            "dataset_id": "strict-fixture-v1",
            "strategy_version": "strategy-v1",
            "parameter_version": "params-v1",
            "feature_schema_version": "features-v1",
            "label_version": "labels-v2",
            "label_source": "strict_counterfactual_v2",
            "cost_version": "fees-v2",
            "cost_sha256": "c" * 64,
            "policy_version": "labels-policy-v2",
            "policy_sha256": "p" * 64,
        },
    )


def _strict_builder_frame() -> TrainingFrame:
    direct = _frame()
    candidates: list[dict[str, object]] = []
    labels: list[dict[str, object]] = []
    for row in direct.rows.to_dict(orient="records"):
        decision_at = str(row["decision_at"])
        candidates.append(
            {
                "sample_id": row["sample_id"],
                "source": row["source"],
                "dataset_id": row["dataset_id"],
                "trade_date": row["trade_date"],
                "decision_at": decision_at,
                "code": row["code"],
                "strategy_version": row["strategy_version"],
                "parameter_version": row["parameter_version"],
                "feature_schema_version": row["feature_schema_version"],
                "features": {
                    name: {"value": row[name], "available_at": decision_at}
                    for name in FEATURES
                },
                "selected": bool(row["rule_selected"]),
                "rejection_stage": row["rule_rejection_stage"],
                "rejection_code": row["rule_rejection_code"],
                "final_action": row["rule_final_action"],
                "rule_score": row["rule_score"],
                "rule_order": row["rule_order"],
                "rule_target_qty": row["rule_target_qty"],
                "rule_slot_count": row["rule_slot_count"],
            }
        )
        labels.append(
            {
                key: row[key]
                for key in (
                    "sample_id",
                    "label_version",
                    "label_source",
                    "cost_version",
                    "cost_sha256",
                    "policy_version",
                    "policy_sha256",
                    "fill_label",
                    "fill_matured_at",
                    "ret_3d_net",
                    "matured_3d_at",
                    "ret_5d_net",
                    "matured_5d_at",
                    "ret_10d_net",
                    "matured_10d_at",
                    "downside_loss",
                    "downside_matured_at",
                    "hit_stop",
                )
            }
        )
    return build_training_frame(candidates, labels, FEATURES)


def _config(**overrides: object) -> TrainingConfig:
    values: dict[str, object] = {
        "parameter_configs": (
            {
                "learning_rate": 0.08,
                "max_iter": 35,
                "max_leaf_nodes": 15,
                "min_samples_leaf": 8,
                "l2_regularization": 0.2,
            },
        ),
    }
    values.update(overrides)
    return TrainingConfig(**values)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _passing_gate_metrics() -> dict[str, object]:
    return {
        "rank": {
            "ret_3d": {"oof": 0.10, "folds": [0.10, 0.08, -0.01]},
            "ret_5d": {
                "oof": 0.15,
                "folds": [0.10, 0.08, -0.01],
                "holdout": 0.06,
            },
            "ret_10d": {"oof": 0.08, "folds": [0.10, 0.04, -0.01]},
        },
        "top20": {
            "challenger_mean": 0.03,
            "pool_mean": 0.01,
            "ridge_mean": 0.02,
        },
        "monotonicity": {
            "downside": True,
            "downside_holdout": True,
            "downside_stop_rate": True,
            "downside_stop_rate_holdout": True,
            "fill": True,
            "fill_holdout": True,
        },
        "counterfactual": {
            "evaluable": True,
            "return_improvement": 0.01,
            "max_drawdown_delta": 0.0,
        },
        "errors": {
            "ret_3d": {
                "challenger_mae": 0.01,
                "baseline_mae": 0.02,
                "holdout_challenger_mae": 0.01,
                "holdout_baseline_mae": 0.02,
            },
            "ret_5d": {
                "challenger_mae": 0.01,
                "baseline_mae": 0.02,
                "holdout_challenger_mae": 0.01,
                "holdout_baseline_mae": 0.02,
            },
            "ret_10d": {
                "challenger_mae": 0.01,
                "baseline_mae": 0.02,
                "holdout_challenger_mae": 0.01,
                "holdout_baseline_mae": 0.02,
            },
            "downside": {
                "challenger_pinball": 0.01,
                "baseline_pinball": 0.02,
                "holdout_challenger_pinball": 0.01,
                "holdout_baseline_pinball": 0.02,
            },
            "fill": {
                "challenger_brier": 0.12,
                "baseline_brier": 0.13,
                "holdout_challenger_brier": 0.12,
                "holdout_baseline_brier": 0.13,
                "max_calibration_error": 0.08,
                "holdout_max_calibration_error": 0.08,
            },
        },
        "interval_coverage": {
            head: {"folds": [0.79, 0.80, 0.81], "holdout": 0.80}
            for head in ("ret_3d", "ret_5d", "ret_10d", "downside")
        },
    }


class MlTrainTest(unittest.TestCase):
    def test_dependency_is_pinned_without_requiring_local_install(self) -> None:
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("scikit-learn==1.9.0", requirements.splitlines())

    def test_config_fixes_seed_and_limits_search_to_three_configs(self) -> None:
        with self.assertRaisesRegex(ValueError, "TRAINING_SEED_MUST_BE_7"):
            _config(seed=8)
        with self.assertRaisesRegex(ValueError, "PARAMETER_CONFIG_LIMIT_EXCEEDED"):
            _config(parameter_configs=({}, {}, {}, {}))

    def test_performance_gate_contract_has_pass_and_stable_reject_reasons(self) -> None:
        passing = _passing_gate_metrics()
        self.assertEqual(evaluate_performance_gates(passing), ())

        failing = json.loads(json.dumps(passing))
        failing["rank"]["ret_5d"]["oof"] = -0.01
        failing["rank"]["ret_5d"]["folds"] = [-0.1, -0.1, 0.01]
        failing["counterfactual"]["evaluable"] = False
        reasons = evaluate_performance_gates(failing)

        self.assertIn("D5_OOF_RANK_GATE", reasons)
        self.assertIn("COUNTERFACTUAL_RULE_BASELINE_NOT_EVALUABLE", reasons)

    def test_same_data_config_and_seed_reuse_identical_immutable_bundle(self) -> None:
        frame = _frame()
        splits = build_ml_splits(frame)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "models"
            first = train_challenger(frame, splits, _config(), root)
            manifest_mtime = (first.artifact_dir / "manifest.json").stat().st_mtime_ns
            second = train_challenger(frame, splits, _config(), root)

            self.assertEqual(first.model_id, second.model_id)
            self.assertEqual(first.manifest_sha256, second.manifest_sha256)
            self.assertEqual(first.artifact_sha256, second.artifact_sha256)
            self.assertTrue(second.reused_existing)
            self.assertEqual(
                manifest_mtime,
                (second.artifact_dir / "manifest.json").stat().st_mtime_ns,
            )
            self.assertEqual(
                first.manifest_sha256,
                _sha256(first.artifact_dir / "manifest.json"),
            )
            loaded = joblib.load(first.artifact_dir / "bundle.joblib")
            self.assertEqual(
                set(loaded["models"]),
                {
                    "ret_3d",
                    "ret_5d",
                    "ret_10d",
                    "downside",
                    "fill",
                    "ridge_ret_3d",
                    "ridge_ret_5d",
                    "ridge_ret_10d",
                    "logistic_fill",
                },
            )
            self.assertEqual(
                loaded["models"]["downside"].named_steps["model"].loss,
                "quantile",
            )
            self.assertEqual(
                loaded["models"]["downside"].named_steps["model"].quantile,
                0.8,
            )
            references = first.manifest.oof_references
            for values in references.values():
                self.assertEqual(list(values), sorted(values))
            manifest_payload = json.loads(
                (first.artifact_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("artifact_sha256", manifest_payload)
            self.assertFalse(first.approvable_l0)
            self.assertEqual(first.status, "rejected")
            self.assertIn("L0_STRICT_BUILDER_PROVENANCE_REQUIRED", first.failed_gates)

            class Store:
                def runtime_state(self) -> dict[str, object]:
                    return {"active_model_id": first.model_id, "permission_level": 0}

                def model_record(self, model_id: str) -> dict[str, object] | None:
                    if model_id != first.model_id:
                        return None
                    return {
                        "artifact_path": first.model_id,
                        "artifact_sha256": first.artifact_sha256,
                    }

            with self.assertRaisesRegex(
                ModelVerificationError, "MODEL_NOT_APPROVABLE"
            ):
                load_active_bundle(
                    Store(),
                    root,
                    {
                        "strategy_version": "strategy-v1",
                        "parameter_version": "params-v1",
                        "feature_schema_version": "features-v1",
                        "label_version": "labels-v2",
                        "cost_version": "fees-v2",
                    },
                )
            runtime_bundle = LoadedBundle(
                model_id=first.model_id,
                manifest=manifest_payload,
                models=loaded["models"],
                artifact_dir=first.artifact_dir,
                artifact_sha256=first.artifact_sha256,
            )
            predictions = predict_candidate_frame(
                frame.rows.iloc[:3].copy(deep=True),
                runtime_bundle,
                drift_min_rows=1,
            )
            self.assertEqual(len(predictions), 3)
            self.assertTrue(predictions["pred_ret_5d"].map(math.isfinite).all())

    def test_holdout_is_not_used_by_search_and_unseen_category_is_supported(self) -> None:
        frame = _frame()
        splits = build_ml_splits(frame)
        rows = frame.rows.copy(deep=True)
        rows.loc[list(splits.holdout_indices), "market_regime"] = "UNSEEN_HOLDOUT"
        metadata = {
            key: value
            for key, value in frame.metadata.items()
            if key != "dataset_sha256"
        }
        isolated = TrainingFrame(rows=rows, feature_names=FEATURES, metadata=metadata)
        with tempfile.TemporaryDirectory() as temp:
            result = train_challenger(isolated, splits, _config(), Path(temp) / "models")

            self.assertTrue(result.manifest.holdout_evaluated_after_freeze)
            self.assertTrue(
                set(result.manifest.search_indices).isdisjoint(splits.holdout_indices)
            )
            self.assertEqual(
                set(result.manifest.holdout_indices), set(splits.holdout_indices)
            )
            expected_search_hash = canonical_hash(
                isolated.rows.iloc[list(result.manifest.search_indices)].to_dict(
                    orient="records"
                )
            )
            self.assertEqual(result.manifest.search_inputs_hash, expected_search_hash)
            categories = result.manifest.drift_reference["market_regime"]["categories"]
            self.assertNotIn("UNSEEN_HOLDOUT", categories)
            self.assertIn("OTHER", categories)
            self.assertIn("MISSING", categories)
            loaded = joblib.load(result.artifact_dir / "bundle.joblib")
            fill = loaded["models"]["fill"]
            self.assertLess(
                max(fill.base_fit_indices_), min(fill.calibration_indices_)
            )

    def test_constant_d5_is_published_as_rejected_evidence(self) -> None:
        frame = _frame(constant_d5=True)
        splits = build_ml_splits(frame)
        with tempfile.TemporaryDirectory() as temp:
            result = train_challenger(frame, splits, _config(), Path(temp) / "models")

            self.assertEqual(result.status, "rejected")
            self.assertFalse(result.approvable_l0)
            self.assertIn("D5_OOF_RANK_GATE", result.failed_gates)
            self.assertTrue((result.artifact_dir / "manifest.json").is_file())

    def test_strict_builder_provenance_flows_into_manifest_and_gates(self) -> None:
        frame = _strict_builder_frame()
        splits = build_ml_splits(frame)
        with tempfile.TemporaryDirectory() as temp:
            result = train_challenger(frame, splits, _config(), Path(temp) / "models")

            self.assertEqual(
                result.manifest.strict_provenance_sha256,
                frame.metadata["strict_provenance_sha256"],
            )
            self.assertNotIn(
                "L0_STRICT_BUILDER_PROVENANCE_REQUIRED", result.failed_gates
            )
            self.assertNotIn("COST_HASH_MISSING", result.failed_gates)
            self.assertNotIn("LABEL_POLICY_HASH_MISSING", result.failed_gates)

    def test_publish_failure_removes_temporary_directory_and_never_exposes_final(self) -> None:
        frame = _frame()
        splits = build_ml_splits(frame)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "models"
            with patch("ml_train.os.replace", side_effect=OSError("publish failed")):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    train_challenger(frame, splits, _config(), root)

            self.assertEqual(list(root.glob(".tmp-*")), [])
            self.assertEqual(
                [path for path in root.iterdir() if path.is_dir()],
                [],
            )


if __name__ == "__main__":
    unittest.main()
