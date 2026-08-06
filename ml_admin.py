"""Evidence-bound governance for trained tabular models.

This module deliberately contains no timer or HTTP promotion surface.  Upward
permission changes require a human CLI action, an immutable model identity and
an evidence hash; automated callers can only downgrade to L0.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import config as app_config
from ml_contracts import canonical_hash
from ml_store import MlStore


class PermissionDenied(RuntimeError):
    """Raised when model governance refuses the requested transition."""


class StaleModelState(PermissionDenied):
    """Raised when a compare-and-swap runtime transition loses a race."""


@dataclass(frozen=True)
class BootstrapResult:
    mean: float
    lower_90: float
    samples: int
    seed: int


@dataclass(frozen=True)
class PermissionEvidence:
    model_id: str
    artifact_sha256: str
    strategy_version: str
    evidence_start: str
    evidence_end: str
    valid_days: int
    prediction_count: int
    mature_d5: int
    mature_d10: int
    mature_filters: int
    closed_cycles: int
    prediction_availability: float
    runtime_fault_rate: float
    max_psi: float
    behavior_equivalence: float
    mean_improvement_pct_points: float
    improvement_ci_lower: float
    max_drawdown_not_worse: bool
    versions_match: bool
    hashes_match: bool
    permission_violations: int
    l0_trading_equivalence: float
    l1_set_quantity_equivalence: float
    l2_added_candidates: int
    l2_increased_quantity: int
    l3_multiplier_min: float
    l3_multiplier_max: float
    sell_equivalence: float
    hard_rejection_equivalence: float

    def __post_init__(self) -> None:
        for field in ("model_id", "artifact_sha256", "strategy_version"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"{field} is required")
        start = date.fromisoformat(str(self.evidence_start))
        end = date.fromisoformat(str(self.evidence_end))
        if end < start:
            raise ValueError("evidence_end precedes evidence_start")
        for field in (
            "valid_days", "prediction_count", "mature_d5", "mature_d10",
            "mature_filters", "closed_cycles", "permission_violations",
            "l2_added_candidates", "l2_increased_quantity",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or int(value) < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        for field in (
            "prediction_availability", "runtime_fault_rate", "max_psi",
            "behavior_equivalence", "mean_improvement_pct_points",
            "improvement_ci_lower", "l0_trading_equivalence",
            "l1_set_quantity_equivalence", "l3_multiplier_min",
            "l3_multiplier_max", "sell_equivalence",
            "hard_rejection_equivalence",
        ):
            value = float(getattr(self, field))
            if not math.isfinite(value):
                raise ValueError(f"{field} must be finite")
        for field in (
            "prediction_availability", "runtime_fault_rate",
            "behavior_equivalence", "l0_trading_equivalence",
            "l1_set_quantity_equivalence", "sell_equivalence",
            "hard_rejection_equivalence",
        ):
            value = float(getattr(self, field))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{field} must be between 0 and 1")
        if float(self.max_psi) < 0.0:
            raise ValueError("max_psi must be non-negative")
        if float(self.l3_multiplier_min) < 0.0 or float(self.l3_multiplier_max) < 0.0:
            raise ValueError("l3 multipliers must be non-negative")
        if int(self.valid_days) > (end - start).days + 1:
            raise ValueError("valid_days exceeds evidence window")

    @property
    def evidence_sha256(self) -> str:
        return canonical_hash(asdict(self))


@dataclass(frozen=True)
class PermissionGateResult:
    level: int
    allowed: bool
    reasons: tuple[str, ...]
    evidence_sha256: str


def _model_value(model: Mapping[str, object] | object, name: str) -> object:
    if isinstance(model, Mapping):
        return model.get(name)
    return getattr(model, name, None)


def evaluate_permission_gate(
    level: int,
    model: Mapping[str, object] | object,
    evidence: PermissionEvidence,
) -> PermissionGateResult:
    if isinstance(level, bool) or int(level) not in {0, 1, 2, 3}:
        raise ValueError("permission level must be between 0 and 3")
    level = int(level)
    reasons: list[str] = []
    model_id = str(_model_value(model, "model_id") or "")
    artifact = str(_model_value(model, "artifact_sha256") or "")
    strategy = str(_model_value(model, "strategy_version") or "")
    status = str(_model_value(model, "status") or "")

    if evidence.model_id != model_id:
        reasons.append("EVIDENCE_MODEL_MISMATCH")
    if evidence.artifact_sha256 != artifact:
        reasons.append("EVIDENCE_ARTIFACT_MISMATCH")
    if strategy and evidence.strategy_version != strategy:
        reasons.append("EVIDENCE_STRATEGY_MISMATCH")
    if status not in {"approvable_l0", "approved", "active"}:
        reasons.append("MODEL_NOT_APPROVABLE")
    if not evidence.versions_match:
        reasons.append("VERSION_MISMATCH")
    if not evidence.hashes_match:
        reasons.append("HASH_MISMATCH")
    if evidence.permission_violations:
        reasons.append("PERMISSION_VIOLATION_PRESENT")
    if evidence.prediction_availability < 0.99:
        reasons.append("PREDICTION_AVAILABILITY_BELOW_99_PERCENT")
    if evidence.runtime_fault_rate > 0.01:
        reasons.append("RUNTIME_FAULT_RATE_EXCEEDED")
    if evidence.max_psi > 0.10:
        reasons.append("PSI_LIMIT_EXCEEDED")
    if evidence.behavior_equivalence < 1.0:
        reasons.append("BEHAVIOR_EQUIVALENCE_REQUIRED")

    if level >= 1:
        if evidence.valid_days < 5:
            reasons.append("L0_VALID_DAYS_5_REQUIRED")
        if evidence.prediction_count < 500:
            reasons.append("L0_PREDICTIONS_500_REQUIRED")
        if evidence.l0_trading_equivalence < 1.0:
            reasons.append("L0_TRADING_EQUIVALENCE_REQUIRED")
        if evidence.valid_days < 20:
            reasons.append("L1_VALID_DAYS_20_REQUIRED")
        if evidence.mature_d5 < 200:
            reasons.append("MATURE_D5_200_REQUIRED")
        if evidence.l1_set_quantity_equivalence < 1.0:
            reasons.append("L1_SET_QUANTITY_EQUIVALENCE_REQUIRED")
        _append_effect_gate_reasons(reasons, evidence)

    if level >= 2:
        if evidence.valid_days < 40:
            reasons.append("L2_VALID_DAYS_40_REQUIRED")
        if evidence.mature_d10 < 300:
            reasons.append("MATURE_D10_300_REQUIRED")
        if evidence.mature_filters < 30:
            reasons.append("MATURE_FILTERS_30_REQUIRED")
        if evidence.l2_added_candidates:
            reasons.append("L2_NEW_CANDIDATE_FORBIDDEN")
        if evidence.l2_increased_quantity:
            reasons.append("L2_QUANTITY_INCREASE_FORBIDDEN")
        if not evidence.max_drawdown_not_worse:
            reasons.append("MAX_DRAWDOWN_NON_DEGRADATION_REQUIRED")

    if level >= 3:
        if evidence.valid_days < 60:
            reasons.append("L3_VALID_DAYS_60_REQUIRED")
        if evidence.closed_cycles < 30:
            reasons.append("CLOSED_CYCLES_30_REQUIRED")
        if evidence.l3_multiplier_min < 0.8 or evidence.l3_multiplier_max > 1.1:
            reasons.append("L3_MULTIPLIER_OUT_OF_RANGE")
        if evidence.sell_equivalence < 1.0:
            reasons.append("SELL_EQUIVALENCE_REQUIRED")
        if evidence.hard_rejection_equivalence < 1.0:
            reasons.append("HARD_REJECTION_EQUIVALENCE_REQUIRED")
        if not evidence.max_drawdown_not_worse:
            reasons.append("MAX_DRAWDOWN_NON_DEGRADATION_REQUIRED")

    unique = tuple(dict.fromkeys(reasons))
    return PermissionGateResult(
        level=level,
        allowed=not unique,
        reasons=unique,
        evidence_sha256=evidence.evidence_sha256,
    )


def _append_effect_gate_reasons(
    reasons: list[str], evidence: PermissionEvidence
) -> None:
    if evidence.mean_improvement_pct_points < 0.10:
        reasons.append("MEAN_IMPROVEMENT_0_10PP_REQUIRED")
    if evidence.improvement_ci_lower < 0:
        reasons.append("IMPROVEMENT_CI_LOWER_NONNEGATIVE_REQUIRED")


def block_bootstrap_improvement(
    daily_improvements: Sequence[float],
    *,
    samples: int = 2000,
    seed: int = 7,
    block_days: int = 5,
) -> BootstrapResult:
    values = [float(value) for value in daily_improvements]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("finite daily improvements are required")
    if samples < 1 or block_days < 1:
        raise ValueError("samples and block_days must be positive")
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(samples):
        drawn: list[float] = []
        while len(drawn) < n:
            start = rng.randrange(n)
            drawn.extend(values[(start + offset) % n] for offset in range(block_days))
        means.append(sum(drawn[:n]) / n)
    means.sort()
    lower_index = max(0, math.ceil(0.10 * samples) - 1)
    return BootstrapResult(
        mean=sum(values) / n,
        lower_90=means[lower_index],
        samples=samples,
        seed=seed,
    )


class MlAdmin:
    def __init__(self, store: Any) -> None:
        self.store = store

    def approve(
        self,
        *,
        model_id: str,
        level: int,
        evidence: PermissionEvidence,
        expected_model_id: str | None,
        expected_level: int,
        reason: str,
        operator: str,
        now: str,
    ) -> str:
        self._require_human(operator)
        reason = self._required_text(reason, "reason")
        now = self._aware_time(now)
        model = self._require_model(model_id)
        gate = evaluate_permission_gate(level, model, evidence)
        if not gate.allowed:
            raise PermissionDenied(",".join(gate.reasons))
        runtime = self._require_runtime_state(
            expected_model_id=expected_model_id,
            expected_level=expected_level,
        )
        artifact = str(_model_value(model, "artifact_sha256") or "")
        event_id = canonical_hash({
            "action": "approve",
            "model_id": model_id,
            "artifact_sha256": artifact,
            "level": int(level),
            "evidence_sha256": gate.evidence_sha256,
            "operator": operator,
            "created_at": now,
        })[:32]
        bound_reason = (
            f"evidence_sha256={gate.evidence_sha256};"
            f"evidence_window={evidence.evidence_start}:{evidence.evidence_end};"
            f"strategy_version={evidence.strategy_version};reason={reason}"
        )
        self.store.record_model_event(
            event_id=event_id,
            model_id=model_id,
            action="approve",
            old_level=int(runtime["permission_level"]),
            new_level=int(level),
            artifact_sha256=artifact,
            reason=bound_reason,
            operator=operator,
            created_at=now,
        )
        return event_id

    def activate(
        self,
        *,
        model_id: str,
        level: int,
        expected_model_id: str | None,
        expected_level: int,
        reason: str,
        operator: str,
        now: str,
    ) -> bool:
        self._require_human(operator)
        reason = self._required_text(reason, "reason")
        now = self._aware_time(now)
        model = self._require_model(model_id)
        artifact = str(_model_value(model, "artifact_sha256") or "")
        approval = self.store.approved_model_event(model_id, artifact)
        if approval is None or int(_row_value(approval, "new_level", -1)) < int(level):
            raise PermissionDenied("MODEL_HASH_LEVEL_NOT_APPROVED")
        event_id = canonical_hash({
            "action": "activate", "model_id": model_id,
            "artifact_sha256": artifact, "old_model_id": expected_model_id,
            "old_level": int(expected_level), "new_level": int(level),
            "operator": operator, "created_at": now,
        })[:32]
        changed = self._transition_runtime_with_event(
            expected_model_id=expected_model_id,
            expected_level=int(expected_level),
            new_model_id=model_id,
            new_level=int(level),
            event_id=event_id,
            event_model_id=model_id,
            action="activate",
            old_level=int(expected_level),
            artifact_sha256=artifact,
            reason=reason,
            operator=operator,
            now=now,
        )
        if not changed:
            raise StaleModelState("ML_RUNTIME_STATE_CHANGED")
        return True

    def downgrade(
        self,
        *,
        model_id: str,
        to_level: int,
        expected_model_id: str | None,
        expected_level: int,
        reason: str,
        operator: str,
        now: str,
    ) -> bool:
        self._require_human(operator)
        model_id = self._required_text(model_id, "model_id")
        reason = self._required_text(reason, "reason")
        now = self._aware_time(now)
        if model_id != expected_model_id:
            raise PermissionDenied("DOWNGRADE_MODEL_MISMATCH")
        if int(to_level) not in {0, 1, 2, 3}:
            raise ValueError("permission level must be between 0 and 3")
        if int(to_level) >= int(expected_level):
            raise PermissionDenied("DOWNGRADE_MUST_REDUCE_PERMISSION")
        model = self._require_model(model_id)
        artifact = str(_model_value(model, "artifact_sha256") or "")
        event_id = canonical_hash({
            "action": "downgrade",
            "model_id": model_id,
            "artifact_sha256": artifact,
            "old_level": int(expected_level),
            "new_level": int(to_level),
            "operator": operator,
            "created_at": now,
        })[:32]
        changed = self._transition_runtime_with_event(
            expected_model_id=expected_model_id,
            expected_level=int(expected_level),
            new_model_id=model_id,
            new_level=int(to_level),
            event_id=event_id,
            event_model_id=model_id,
            action="downgrade",
            old_level=int(expected_level),
            artifact_sha256=artifact,
            reason=reason,
            operator=operator,
            now=now,
        )
        if not changed:
            raise StaleModelState("ML_RUNTIME_STATE_CHANGED")
        return True

    def rollback(
        self,
        *,
        to_model_id: str | None,
        expected_model_id: str | None,
        expected_level: int,
        reason: str,
        operator: str,
        now: str,
    ) -> bool:
        self._require_human(operator)
        reason = self._required_text(reason, "reason")
        now = self._aware_time(now)
        if expected_model_id is None:
            raise PermissionDenied("ROLLBACK_ACTIVE_MODEL_REQUIRED")
        current = self._require_model(expected_model_id)
        parent_model_id = _model_value(current, "parent_model_id")
        parent_model_id = str(parent_model_id) if parent_model_id else None
        if to_model_id is not None:
            to_model_id = self._required_text(to_model_id, "to_model_id")
        if to_model_id != parent_model_id:
            raise PermissionDenied("ROLLBACK_TARGET_NOT_PARENT")
        if to_model_id is not None:
            target = self._require_model(to_model_id)
            target_artifact = str(_model_value(target, "artifact_sha256") or "")
            approval = self.store.approved_model_event(to_model_id, target_artifact)
            if approval is None or int(_row_value(approval, "new_level", -1)) < 0:
                raise PermissionDenied("ROLLBACK_PARENT_NOT_APPROVED_L0")
        current_artifact = str(_model_value(current, "artifact_sha256") or "")
        event_id = canonical_hash({
            "action": "rollback",
            "model_id": expected_model_id,
            "artifact_sha256": current_artifact,
            "to_model_id": to_model_id,
            "old_level": int(expected_level),
            "operator": operator,
            "created_at": now,
        })[:32]
        changed = self._transition_runtime_with_event(
            expected_model_id=expected_model_id,
            expected_level=int(expected_level),
            new_model_id=to_model_id,
            new_level=0,
            event_id=event_id,
            event_model_id=expected_model_id,
            action="rollback",
            old_level=int(expected_level),
            artifact_sha256=current_artifact,
            reason=f"to_model_id={to_model_id or 'none'};reason={reason}",
            operator=operator,
            now=now,
        )
        if not changed:
            raise StaleModelState("ML_RUNTIME_STATE_CHANGED")
        return True

    def status(self) -> dict[str, object]:
        runtime = self.store.runtime_state()
        active_model_id = runtime.get("active_model_id")
        model = self.store.model_record(str(active_model_id)) if active_model_id else None
        result = dict(runtime)
        if model is not None:
            result["active_model"] = {
                "model_id": str(_model_value(model, "model_id") or active_model_id),
                "parent_model_id": _model_value(model, "parent_model_id"),
                "status": str(_model_value(model, "status") or ""),
                "artifact_sha256": str(_model_value(model, "artifact_sha256") or ""),
            }
        else:
            result["active_model"] = None
        return result

    def auto_downgrade(self, *, model_id: str, reason: str, now: str) -> bool:
        runtime = self.store.runtime_state()
        if runtime.get("active_model_id") != model_id:
            return False
        old_level = int(runtime["permission_level"])
        if old_level <= 0:
            return False
        now = self._aware_time(now)
        model = self._require_model(model_id)
        artifact = str(_model_value(model, "artifact_sha256") or "")
        event_id = canonical_hash({
            "action": "auto_downgrade", "model_id": model_id,
            "old_level": old_level, "reason": reason, "created_at": now,
        })[:32]
        changed = self._transition_runtime_with_event(
            expected_model_id=model_id,
            expected_level=old_level,
            new_model_id=model_id,
            new_level=0,
            event_id=event_id,
            event_model_id=model_id,
            action="auto_downgrade",
            old_level=old_level,
            artifact_sha256=artifact,
            reason=self._required_text(reason, "reason"),
            operator="system",
            now=now,
        )
        return bool(changed)

    @staticmethod
    def auto_promote(*, model_id: str, level: int) -> None:
        del model_id, level
        raise PermissionDenied("AUTOMATIC_PROMOTION_FORBIDDEN")

    def _require_model(self, model_id: str) -> Mapping[str, object] | object:
        model = self.store.model_record(self._required_text(model_id, "model_id"))
        if model is None:
            raise PermissionDenied("MODEL_NOT_REGISTERED")
        return model

    def _transition_runtime_with_event(
        self,
        *,
        expected_model_id: str | None,
        expected_level: int,
        new_model_id: str | None,
        new_level: int,
        event_id: str,
        event_model_id: str,
        action: str,
        old_level: int,
        artifact_sha256: str,
        reason: str,
        operator: str,
        now: str,
    ) -> bool:
        atomic = getattr(self.store, "transition_runtime_with_event", None)
        if callable(atomic):
            return bool(atomic(
                expected_model_id=expected_model_id,
                expected_permission_level=int(expected_level),
                new_model_id=new_model_id,
                new_permission_level=int(new_level),
                updated_at=now,
                event_id=event_id,
                event_model_id=event_model_id,
                action=action,
                event_old_level=int(old_level),
                event_new_level=int(new_level),
                artifact_sha256=artifact_sha256,
                reason=reason,
                operator=operator,
                created_at=now,
            ))
        changed = self.store.compare_and_swap_runtime(
            expected_model_id=expected_model_id,
            expected_permission_level=int(expected_level),
            new_model_id=new_model_id,
            new_permission_level=int(new_level),
            updated_at=now,
        )
        if not changed:
            return False
        self.store.record_model_event(
            event_id=event_id,
            model_id=event_model_id,
            action=action,
            old_level=int(old_level),
            new_level=int(new_level),
            artifact_sha256=artifact_sha256,
            reason=reason,
            operator=operator,
            created_at=now,
        )
        return True

    def _require_runtime_state(
        self,
        *,
        expected_model_id: str | None,
        expected_level: int,
    ) -> Mapping[str, object]:
        runtime = self.store.runtime_state()
        if (
            runtime.get("active_model_id") != expected_model_id
            or int(runtime.get("permission_level", -1)) != int(expected_level)
        ):
            raise StaleModelState("ML_RUNTIME_STATE_CHANGED")
        return runtime

    @staticmethod
    def _required_text(value: object, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} is required")
        if len(text) > 1024:
            raise ValueError(f"{field} is too long")
        return text

    @classmethod
    def _require_human(cls, operator: str) -> str:
        value = cls._required_text(operator, "operator")
        if value.lower() in {"auto", "system", "timer", "worker"}:
            raise PermissionDenied("HUMAN_OPERATOR_REQUIRED")
        return value

    @staticmethod
    def _aware_time(value: str) -> str:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        return parsed.isoformat()


def _row_value(row: object, name: str, default: object = None) -> object:
    if isinstance(row, Mapping):
        return row.get(name, default)
    try:
        return row[name]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, default)


def _load_evidence(path: Path) -> PermissionEvidence:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("evidence must be a JSON object")
    return PermissionEvidence(**value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual trained-model governance")
    sub = parser.add_subparsers(dest="command", required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("--model-id", required=True)
    approve.add_argument("--level", required=True, type=int)
    approve.add_argument("--evidence", required=True, type=Path)
    approve.add_argument("--expected-model-id", default="none")
    approve.add_argument("--expected-level", required=True, type=int)
    for child in (approve,):
        child.add_argument("--reason", required=True)
        child.add_argument("--operator", required=True)
        child.add_argument("--now", required=True)
    activate = sub.add_parser("activate")
    activate.add_argument("--model-id", required=True)
    activate.add_argument("--level", required=True, type=int)
    activate.add_argument("--expected-model-id")
    activate.add_argument("--expected-level", required=True, type=int)
    activate.add_argument("--reason", required=True)
    activate.add_argument("--operator", required=True)
    activate.add_argument("--now", required=True)
    downgrade = sub.add_parser("downgrade")
    downgrade.add_argument("--model-id", required=True)
    downgrade.add_argument("--to-level", required=True, type=int)
    downgrade.add_argument("--expected-model-id", required=True)
    downgrade.add_argument("--expected-level", required=True, type=int)
    downgrade.add_argument("--reason", required=True)
    downgrade.add_argument("--operator", required=True)
    downgrade.add_argument("--now", required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--to-model-id", default="none")
    rollback.add_argument("--expected-model-id", required=True)
    rollback.add_argument("--expected-level", required=True, type=int)
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--operator", required=True)
    rollback.add_argument("--now", required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)

    store = MlStore(app_config.ML_DB_FILE, app_config.ML_DB_MAX_BYTES)
    store.initialize()
    admin = MlAdmin(store)
    if args.command == "approve":
        event_id = admin.approve(
            model_id=args.model_id,
            level=args.level,
            evidence=_load_evidence(args.evidence),
            expected_model_id=_parse_model_id(args.expected_model_id),
            expected_level=args.expected_level,
            reason=args.reason,
            operator=args.operator,
            now=args.now,
        )
        print(json.dumps({"status": "approved", "event_id": event_id}))
        return 0
    if args.command == "activate":
        admin.activate(
            model_id=args.model_id,
            level=args.level,
            expected_model_id=_parse_model_id(args.expected_model_id),
            expected_level=args.expected_level,
            reason=args.reason,
            operator=args.operator,
            now=args.now,
        )
        print(json.dumps({"status": "activated", "model_id": args.model_id}))
        return 0
    if args.command == "downgrade":
        admin.downgrade(
            model_id=args.model_id,
            to_level=args.to_level,
            expected_model_id=_parse_model_id(args.expected_model_id),
            expected_level=args.expected_level,
            reason=args.reason,
            operator=args.operator,
            now=args.now,
        )
        print(json.dumps({
            "status": "downgraded",
            "model_id": args.model_id,
            "permission_level": args.to_level,
        }))
        return 0
    if args.command == "rollback":
        admin.rollback(
            to_model_id=_parse_model_id(args.to_model_id),
            expected_model_id=_parse_model_id(args.expected_model_id),
            expected_level=args.expected_level,
            reason=args.reason,
            operator=args.operator,
            now=args.now,
        )
        print(json.dumps({
            "status": "rolled_back",
            "active_model_id": _parse_model_id(args.to_model_id),
            "permission_level": 0,
        }))
        return 0
    print(json.dumps(admin.status(), ensure_ascii=False, sort_keys=True))
    return 0


def _parse_model_id(value: object) -> str | None:
    text = str(value or "").strip()
    if text.casefold() in {"", "none", "null", "off"}:
        return None
    return text


if __name__ == "__main__":
    raise SystemExit(main())
