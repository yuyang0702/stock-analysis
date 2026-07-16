import os
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

from ml_contracts import (
    CANDIDATE_FINAL_ACTIONS,
    CandidateSample,
    LabelRecord,
    ModelManifest,
    PredictionRecord,
    TimedFeature,
    candidate_sample_id,
    canonical_hash,
)


class MlContractsTest(unittest.TestCase):
    def _sample(self, **overrides: object) -> CandidateSample:
        values = {
            "source": "strict",
            "dataset_id": "d1",
            "decision_at": "2025-01-02T10:00:00+08:00",
            "code": "600000",
            "strategy_version": "s1",
            "parameter_version": "p1",
            "feature_schema_version": "f1",
            "features": {
                "price": TimedFeature(
                    10.0, "2025-01-02T09:59:00+08:00"
                )
            },
            "selected": False,
            "rejection_stage": "score",
            "rejection_code": "buy_low_score",
            "final_action": "score_rejected",
            "universe_hash": "universe",
            "market_data_version": "market-v1",
            "code_hash": "code",
            "generator_hash": "generator",
        }
        values.update(overrides)
        return CandidateSample.from_values(**values)

    def _manifest(self, **overrides: object) -> ModelManifest:
        values = {
            "model_id": "m1",
            "parent_model_id": None,
            "feature_names": ("price",),
            "train_start": "2024-01-01",
            "train_end": "2024-06-30",
            "validation_start": "2024-07-01",
            "validation_end": "2024-08-31",
            "holdout_start": "2024-09-01",
            "holdout_end": "2024-10-31",
            "dataset_sha256": "data",
            "code_sha256": "code",
            "config_sha256": "config",
            "artifact_sha256": "artifact",
            "parameter_version": "p1",
            "cost_version": "c1",
            "dependency_versions": {"python": "3.12"},
            "metrics": {"rank_correlation": 0.1},
            "created_at": "2025-01-02T11:00:00+08:00",
            "split_sha256": "split",
            "search_inputs_hash": "search",
            "holdout_metrics": {"rank_correlation": 0.2},
        }
        values.update(overrides)
        return ModelManifest(**values)

    def test_candidate_hash_is_stable_and_future_feature_is_rejected(self) -> None:
        sample = self._sample()

        self.assertEqual(candidate_sample_id(sample), candidate_sample_id(sample))
        self.assertEqual(sample.sample_id, candidate_sample_id(sample))
        with self.assertRaisesRegex(ValueError, "FEATURE_FROM_FUTURE"):
            self._sample(
                features={
                    "price": TimedFeature(
                        10.0, "2025-01-02T10:01:00+08:00"
                    )
                }
            )

    def test_identity_normalizes_code_and_excludes_feature_values(self) -> None:
        first = self._sample(code="1")
        second = self._sample(
            code="000001",
            features={
                "price": TimedFeature(
                    999.0, "2025-01-02T09:58:00+08:00"
                ),
                "name": TimedFeature(
                    "不进入身份", "2025-01-02T09:58:00+08:00"
                ),
                "account_value": TimedFeature(
                    123.0, "2025-01-02T09:58:00+08:00"
                ),
            },
        )

        self.assertEqual(first.code, "000001")
        self.assertEqual(first.sample_id, second.sample_id)
        self.assertEqual(
            canonical_hash({"b": 2, "a": 1}),
            canonical_hash({"a": 1, "b": 2}),
        )

    def test_decision_and_feature_timestamps_must_be_timezone_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "TIMEZONE_AWARE_TIMESTAMP_REQUIRED"):
            self._sample(decision_at="2025-01-02T10:00:00")
        with self.assertRaisesRegex(ValueError, "TIMEZONE_AWARE_TIMESTAMP_REQUIRED"):
            self._sample(
                features={
                    "price": TimedFeature(10.0, "2025-01-02T09:59:00")
                }
            )

    def test_candidate_requires_provenance_and_consistent_decision(self) -> None:
        for field in (
            "final_action",
            "universe_hash",
            "market_data_version",
            "code_hash",
            "generator_hash",
        ):
            for value in (" ", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, f"REQUIRED_FIELD: {field}"):
                        self._sample(**{field: value})

        selected = self._sample(
            selected=True,
            rejection_stage="selected",
            rejection_code="",
            final_action="buy_blocked_disabled",
        )
        self.assertTrue(selected.selected)
        self.assertEqual(selected.final_action, "buy_blocked_disabled")
        for overrides in (
            {"selected": True, "rejection_stage": "score", "rejection_code": "buy_low_score"},
            {"rejection_code": ""},
            {"rejection_stage": ""},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, "CANDIDATE_DECISION_MISMATCH"):
                    self._sample(**overrides)

    def test_candidate_final_action_uses_the_shared_stable_enum(self) -> None:
        self.assertEqual(CANDIDATE_FINAL_ACTIONS, frozenset({
            "selected", "score_rejected", "risk_rejected",
            "tradability_rejected", "execution_rejected",
            "buy_published", "rule_rejected", "sell_published",
            "sell_rejected_no_holding", "sell_blocked_disabled",
            "sell_blocked_kill_switch", "buy_blocked_disabled",
            "buy_blocked_kill_switch",
        }))
        with self.assertRaisesRegex(ValueError, "UNKNOWN_FINAL_ACTION"):
            self._sample(final_action="future_action")

    def test_canonical_hash_supports_contracts_and_is_stable_across_processes(self) -> None:
        feature = TimedFeature(
            {"levels": [1, MappingProxyType({"value": 2})], "tags": {"b", "a"}},
            "2025-01-02T09:59:00+08:00",
        )
        expected = canonical_hash(MappingProxyType({"feature": feature}))
        self.assertEqual(
            expected,
            canonical_hash(
                {
                    "feature": {
                        "available_at": "2025-01-02T09:59:00+08:00",
                        "value": {"levels": [1, {"value": 2}], "tags": {"a", "b"}},
                    }
                }
            ),
        )

        script = """
from types import MappingProxyType
from ml_contracts import TimedFeature, canonical_hash
value = MappingProxyType({"feature": TimedFeature(
    {"levels": [1, MappingProxyType({"value": 2})], "tags": {"b", "a"}},
    "2025-01-02T09:59:00+08:00",
)})
print(canonical_hash(value))
print(hash(value["feature"]))
"""
        outputs = []
        for seed in ("1", "2"):
            environment = os.environ.copy()
            environment["PYTHONHASHSEED"] = seed
            outputs.append(
                subprocess.check_output(
                    [sys.executable, "-c", script],
                    text=True,
                    env=environment,
                ).splitlines()
            )
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0][0], expected)

    def test_same_identity_has_distinct_content_hash_for_provenance_or_features(self) -> None:
        original = self._sample()
        provenance_changed = self._sample(generator_hash="generator-v2")
        feature_changed = self._sample(
            features={
                "price": TimedFeature(11.0, "2025-01-02T09:59:00+08:00")
            }
        )

        self.assertEqual(
            {original.sample_id, provenance_changed.sample_id, feature_changed.sample_id},
            {original.sample_id},
        )
        self.assertEqual(
            len(
                {
                    canonical_hash(original),
                    canonical_hash(provenance_changed),
                    canonical_hash(feature_changed),
                }
            ),
            3,
        )

    def test_optional_maturity_and_required_creation_times_are_aware(self) -> None:
        label = LabelRecord(
            sample_id="sample",
            label_version="l1",
            label_source="strict",
            cost_version="c1",
        )
        self.assertIsNone(label.matured_at)
        with self.assertRaisesRegex(ValueError, "TIMEZONE_AWARE_TIMESTAMP_REQUIRED"):
            replace(label, matured_at="2025-01-02T11:00:00")

        with self.assertRaises(TypeError):
            PredictionRecord(sample_id="sample", model_id="m1")
        prediction = PredictionRecord(
            sample_id="sample",
            model_id="m1",
            created_at="2025-01-02T11:00:00+08:00",
        )
        self.assertEqual(prediction.created_at, "2025-01-02T11:00:00+08:00")
        with self.assertRaisesRegex(ValueError, "TIMEZONE_AWARE_TIMESTAMP_REQUIRED"):
            replace(prediction, created_at="2025-01-02T11:00:00")
        with self.assertRaisesRegex(ValueError, "TIMEZONE_AWARE_TIMESTAMP_REQUIRED"):
            self._manifest(created_at="2025-01-02T11:00:00")

    def test_ml_records_are_frozen_contracts(self) -> None:
        label = LabelRecord(
            sample_id="sample",
            label_version="l1",
            label_source="strict",
            cost_version="c1",
        )
        prediction = PredictionRecord(
            sample_id="sample",
            model_id="m1",
            created_at="2025-01-02T11:00:00+08:00",
        )
        manifest = self._manifest()

        for record in (label, prediction, manifest):
            with self.assertRaises(FrozenInstanceError):
                record.created_at = "changed"

    def test_contracts_recursively_freeze_nested_values(self) -> None:
        feature = TimedFeature(
            {"levels": [1, {"value": 2}], "tags": {"b", "a"}},
            "2025-01-02T09:59:00+08:00",
        )
        sample = self._sample(features={"nested": feature})
        manifest = self._manifest(
            feature_names=("nested",),
            metrics={"folds": [{"score": 0.1}]},
            holdout_metrics={"scores": [0.2]},
        )

        with self.assertRaises(TypeError):
            sample.features["new"] = feature
        with self.assertRaises(TypeError):
            feature.value["new"] = 3
        with self.assertRaises(TypeError):
            manifest.metrics["new"] = 3
        self.assertIsInstance(feature.value["levels"], tuple)
        self.assertIsInstance(feature.value["tags"], frozenset)
        self.assertIsInstance(manifest.metrics["folds"], tuple)
        self.assertEqual(
            canonical_hash({"items": (1, 2), "tags": frozenset({"b", "a"})}),
            canonical_hash({"tags": frozenset({"a", "b"}), "items": [1, 2]}),
        )

    def test_direct_candidate_construction_enforces_factory_invariants(self) -> None:
        sample = self._sample()

        with self.assertRaisesRegex(ValueError, "SAMPLE_ID_MISMATCH"):
            replace(sample, sample_id="wrong")
        with self.assertRaisesRegex(ValueError, "TIMEZONE_AWARE_TIMESTAMP_REQUIRED"):
            replace(sample, sample_id="", decision_at="2025-01-02T10:00:00")
        with self.assertRaisesRegex(ValueError, "FEATURE_FROM_FUTURE"):
            replace(
                sample,
                sample_id="",
                features={
                    "price": TimedFeature(
                        10.0, "2025-01-02T10:01:00+08:00"
                    )
                },
            )
        normalized = replace(sample, sample_id="", code="1")
        self.assertEqual(normalized.code, "000001")
        self.assertEqual(normalized.sample_id, candidate_sample_id(normalized))
        normalized_source = replace(sample, sample_id="", source=123)
        self.assertEqual(normalized_source.source, "123")
        self.assertEqual(
            normalized_source.sample_id, candidate_sample_id(normalized_source)
        )

    def test_candidate_provenance_is_content_not_identity(self) -> None:
        first = self._sample()
        second = self._sample(
            rejection_stage="risk",
            rejection_code="buy_risk_disallowed",
            final_action="risk_rejected",
            generator_hash="generator-v2",
        )

        self.assertEqual(first.sample_id, second.sample_id)
        self.assertEqual(second.final_action, "risk_rejected")
        self.assertEqual(second.universe_hash, "universe")
        self.assertEqual(second.market_data_version, "market-v1")
        self.assertEqual(second.code_hash, "code")
        self.assertEqual(second.generator_hash, "generator-v2")


if __name__ == "__main__":
    unittest.main()
