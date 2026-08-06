import unittest

from ml_admin import (
    MlAdmin,
    PermissionDenied,
    PermissionEvidence,
    block_bootstrap_improvement,
    evaluate_permission_gate,
)


MODEL = {
    "model_id": "model-v1",
    "artifact_sha256": "a" * 64,
    "strategy_version": "strategy-v1",
    "status": "approvable_l0",
}


def evidence(**overrides):
    values = {
        "model_id": "model-v1",
        "artifact_sha256": "a" * 64,
        "strategy_version": "strategy-v1",
        "evidence_start": "2026-01-01",
        "evidence_end": "2026-08-01",
        "valid_days": 65,
        "prediction_count": 1000,
        "mature_d5": 500,
        "mature_d10": 500,
        "mature_filters": 100,
        "closed_cycles": 50,
        "prediction_availability": 0.995,
        "runtime_fault_rate": 0.005,
        "max_psi": 0.05,
        "behavior_equivalence": 1.0,
        "mean_improvement_pct_points": 0.20,
        "improvement_ci_lower": 0.02,
        "max_drawdown_not_worse": True,
        "versions_match": True,
        "hashes_match": True,
        "permission_violations": 0,
        "l0_trading_equivalence": 1.0,
        "l1_set_quantity_equivalence": 1.0,
        "l2_added_candidates": 0,
        "l2_increased_quantity": 0,
        "l3_multiplier_min": 0.8,
        "l3_multiplier_max": 1.1,
        "sell_equivalence": 1.0,
        "hard_rejection_equivalence": 1.0,
    }
    values.update(overrides)
    return PermissionEvidence(**values)


class FakeStore:
    def __init__(self):
        self.runtime = {
            "active_model_id": None,
            "permission_level": 0,
            "updated_at": "2026-08-01T00:00:00+08:00",
        }
        self.models = {
            "model-v1": dict(MODEL),
            "model-parent": {
                **MODEL,
                "model_id": "model-parent",
                "artifact_sha256": "b" * 64,
                "parent_model_id": None,
            },
        }
        self.events = []

    def model_record(self, model_id):
        return self.models.get(model_id)

    def approved_model_event(self, model_id, artifact_sha256):
        for event in reversed(self.events):
            if (
                event["model_id"] == model_id
                and event["artifact_sha256"] == artifact_sha256
                and event["action"] == "approve"
            ):
                return event
        return None

    def record_model_event(self, **event):
        self.events.append(dict(event))
        return True

    def runtime_state(self):
        return dict(self.runtime)

    def compare_and_swap_runtime(
        self, *, expected_model_id, expected_permission_level,
        new_model_id, new_permission_level, updated_at,
    ):
        if (
            self.runtime["active_model_id"] != expected_model_id
            or self.runtime["permission_level"] != expected_permission_level
        ):
            return False
        self.runtime = {
            "active_model_id": new_model_id,
            "permission_level": new_permission_level,
            "updated_at": updated_at,
        }
        return True


class MlAdminTest(unittest.TestCase):
    def test_days_alone_cannot_promote_l1(self) -> None:
        result = evaluate_permission_gate(
            1, MODEL, evidence(valid_days=25, mature_d5=80)
        )
        self.assertFalse(result.allowed)
        self.assertIn("MATURE_D5_200_REQUIRED", result.reasons)

    def test_l0_gate_is_bootstrappable_without_live_observation(self) -> None:
        result = evaluate_permission_gate(
            0,
            MODEL,
            evidence(
                evidence_start="2026-08-01",
                evidence_end="2026-08-01",
                valid_days=0,
                prediction_count=0,
                mature_d5=0,
                mature_d10=0,
                mature_filters=0,
                closed_cycles=0,
            ),
        )
        self.assertTrue(result.allowed)
        promoted = evaluate_permission_gate(
            1,
            MODEL,
            evidence(
                evidence_start="2026-08-01",
                evidence_end="2026-08-01",
                valid_days=0,
                prediction_count=0,
                mature_d5=0,
            ),
        )
        self.assertFalse(promoted.allowed)
        self.assertIn("L0_PREDICTIONS_500_REQUIRED", promoted.reasons)

    def test_evidence_rejects_invalid_ratio_and_window_values(self) -> None:
        with self.assertRaises(ValueError):
            evidence(prediction_availability=1.1)
        with self.assertRaises(ValueError):
            evidence(max_psi=-0.01)
        with self.assertRaises(ValueError):
            evidence(
                evidence_start="2026-08-01",
                evidence_end="2026-08-01",
                valid_days=2,
            )

    def test_all_permission_gates_are_conjunctive(self) -> None:
        self.assertTrue(evaluate_permission_gate(0, MODEL, evidence()).allowed)
        self.assertTrue(evaluate_permission_gate(1, MODEL, evidence()).allowed)
        self.assertTrue(evaluate_permission_gate(2, MODEL, evidence()).allowed)
        self.assertTrue(evaluate_permission_gate(3, MODEL, evidence()).allowed)
        failed = evaluate_permission_gate(
            3,
            MODEL,
            evidence(
                runtime_fault_rate=0.02,
                max_psi=0.11,
                l3_multiplier_max=1.2,
                sell_equivalence=0.999,
            ),
        )
        self.assertFalse(failed.allowed)
        self.assertTrue({
            "RUNTIME_FAULT_RATE_EXCEEDED",
            "PSI_LIMIT_EXCEEDED",
            "L3_MULTIPLIER_OUT_OF_RANGE",
            "SELL_EQUIVALENCE_REQUIRED",
        }.issubset(failed.reasons))

    def test_model_and_evidence_identity_must_match(self) -> None:
        result = evaluate_permission_gate(
            0, MODEL, evidence(artifact_sha256="b" * 64)
        )
        self.assertFalse(result.allowed)
        self.assertIn("EVIDENCE_ARTIFACT_MISMATCH", result.reasons)

    def test_block_bootstrap_is_deterministic_and_one_sided(self) -> None:
        daily = [0.20, 0.15, 0.10, 0.25, 0.05] * 10
        first = block_bootstrap_improvement(daily)
        second = block_bootstrap_improvement(daily)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first.mean, 0.10)
        self.assertGreaterEqual(first.lower_90, 0.0)

    def test_approve_does_not_activate_and_activation_requires_approval(self) -> None:
        store = FakeStore()
        admin = MlAdmin(store)
        with self.assertRaises(PermissionDenied):
            admin.activate(
                model_id="model-v1",
                level=0,
                expected_model_id=None,
                expected_level=0,
                reason="activate",
                operator="human",
                now="2026-08-06T10:00:00+08:00",
            )
        admin.approve(
            model_id="model-v1",
            level=0,
            evidence=evidence(),
            expected_model_id=None,
            expected_level=0,
            reason="reviewed evidence",
            operator="human",
            now="2026-08-06T10:01:00+08:00",
        )
        self.assertIsNone(store.runtime["active_model_id"])
        self.assertTrue(admin.activate(
            model_id="model-v1",
            level=0,
            expected_model_id=None,
            expected_level=0,
            reason="activate approved L0",
            operator="human",
            now="2026-08-06T10:02:00+08:00",
        ))
        self.assertEqual(store.runtime["active_model_id"], "model-v1")

    def test_automatic_process_can_downgrade_but_not_promote(self) -> None:
        store = FakeStore()
        store.runtime.update(active_model_id="model-v1", permission_level=2)
        admin = MlAdmin(store)
        self.assertTrue(admin.auto_downgrade(
            model_id="model-v1",
            reason="HASH_MISMATCH",
            now="2026-08-06T10:00:00+08:00",
        ))
        with self.assertRaises(PermissionDenied):
            admin.auto_promote(model_id="model-v1", level=1)

    def test_approval_refuses_stale_runtime_evidence(self) -> None:
        store = FakeStore()
        store.runtime.update(active_model_id="model-parent", permission_level=0)
        admin = MlAdmin(store)
        with self.assertRaisesRegex(PermissionDenied, "ML_RUNTIME_STATE_CHANGED"):
            admin.approve(
                model_id="model-v1",
                level=0,
                evidence=evidence(),
                expected_model_id=None,
                expected_level=0,
                reason="stale review",
                operator="human",
                now="2026-08-06T10:03:00+08:00",
            )
        self.assertEqual(store.events, [])

    def test_human_downgrade_only_reduces_permission_with_cas(self) -> None:
        store = FakeStore()
        store.runtime.update(active_model_id="model-v1", permission_level=3)
        admin = MlAdmin(store)
        self.assertTrue(admin.downgrade(
            model_id="model-v1",
            to_level=1,
            expected_model_id="model-v1",
            expected_level=3,
            reason="reduce model authority",
            operator="human",
            now="2026-08-06T10:04:00+08:00",
        ))
        self.assertEqual(store.runtime["permission_level"], 1)
        with self.assertRaisesRegex(PermissionDenied, "DOWNGRADE_MUST_REDUCE_PERMISSION"):
            admin.downgrade(
                model_id="model-v1",
                to_level=2,
                expected_model_id="model-v1",
                expected_level=1,
                reason="invalid increase",
                operator="human",
                now="2026-08-06T10:05:00+08:00",
            )

    def test_rollback_requires_registered_parent_and_resets_to_l0(self) -> None:
        store = FakeStore()
        store.models["model-v1"]["parent_model_id"] = "model-parent"
        store.runtime.update(active_model_id="model-v1", permission_level=2)
        store.events.append({
            "model_id": "model-parent",
            "artifact_sha256": "b" * 64,
            "action": "approve",
            "new_level": 0,
        })
        admin = MlAdmin(store)
        self.assertTrue(admin.rollback(
            to_model_id="model-parent",
            expected_model_id="model-v1",
            expected_level=2,
            reason="restore parent",
            operator="human",
            now="2026-08-06T10:06:00+08:00",
        ))
        self.assertEqual(store.runtime["active_model_id"], "model-parent")
        self.assertEqual(store.runtime["permission_level"], 0)

    def test_rollback_rejects_non_parent_target(self) -> None:
        store = FakeStore()
        store.runtime.update(active_model_id="model-v1", permission_level=1)
        admin = MlAdmin(store)
        with self.assertRaisesRegex(PermissionDenied, "ROLLBACK_TARGET_NOT_PARENT"):
            admin.rollback(
                to_model_id="model-parent",
                expected_model_id="model-v1",
                expected_level=1,
                reason="wrong lineage",
                operator="human",
                now="2026-08-06T10:07:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()
