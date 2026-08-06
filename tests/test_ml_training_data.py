from __future__ import annotations

import math
import unittest
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

from ml_training_data import (
    FORBIDDEN_MODEL_FEATURES,
    DataReadiness,
    TrainingFrame,
    assign_stock_day_weights,
    build_ml_splits,
    build_training_frame,
    validate_training_data,
)


ALLOWLIST = ("feature_x", "market_regime")
LABEL_FIELDS = {
    "label_version": "labels-v2",
    "label_source": "strict_counterfactual_v2",
    "cost_version": "fees-v2",
    "cost_sha256": "c" * 64,
    "policy_version": "policy-v2",
    "policy_sha256": "p" * 64,
    "fill_label": 1,
    "fill_status": "filled",
    "ret_3d_net": 0.01,
    "ret_5d_net": 0.02,
    "ret_10d_net": 0.03,
    "downside_loss": 0.01,
    "fill_matured_at": "2025-12-31T10:15:00+08:00",
    "matured_3d_at": "2025-12-31T16:00:00+08:00",
    "matured_5d_at": "2025-12-31T16:00:00+08:00",
    "matured_10d_at": "2025-12-31T16:00:00+08:00",
    "downside_matured_at": "2025-12-31T16:00:00+08:00",
}


def _candidate(
    sample_id: str,
    trade_date: str,
    code: str,
    *,
    decision_at: str | None = None,
    feature_x: object = 1.0,
    feature_available_at: str | None = None,
    regime: str = "NORMAL",
    strategy_version: str = "strategy-v1",
    parameter_version: str = "params-v1",
    feature_schema_version: str = "features-v1",
    extra_features: dict[str, object] | None = None,
) -> dict[str, object]:
    decision_at = decision_at or f"{trade_date}T10:00:00+08:00"
    feature_available_at = feature_available_at or decision_at
    features: dict[str, object] = {
        "feature_x": {
            "value": feature_x,
            "available_at": feature_available_at,
        },
        "market_regime": {
            "value": regime,
            "available_at": decision_at,
        },
    }
    features.update(extra_features or {})
    return {
        "sample_id": sample_id,
        "source": "strict",
        "dataset_id": "strict-year-v1",
        "trade_date": trade_date,
        "decision_at": decision_at,
        "code": code,
        "strategy_version": strategy_version,
        "parameter_version": parameter_version,
        "feature_schema_version": feature_schema_version,
        "features": features,
        "selected": True,
        "rejection_stage": "selected",
        "rejection_code": "",
        "final_action": "selected",
        "rule_score": 80.0,
        "rule_order": 0,
        "rule_target_qty": 100,
        "rule_slot_count": 3,
    }


def _label(sample_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {"sample_id": sample_id, **LABEL_FIELDS}
    row.update(overrides)
    return row


def _business_dates(start: date, count: int) -> list[str]:
    values: list[str] = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _ready_frame(
    *,
    trading_days: int = 270,
    stocks_per_day: int = 60,
) -> TrainingFrame:
    dates = _business_dates(date(2024, 1, 2), trading_days)
    regimes = ("NORMAL", "CAUTION", "RISK_OFF")
    candidates: list[dict[str, object]] = []
    labels: list[dict[str, object]] = []
    for date_index, trade_date in enumerate(dates):
        regime = regimes[date_index % len(regimes)]
        for stock_index in range(stocks_per_day):
            sample_id = f"s-{date_index}-{stock_index}"
            candidate = _candidate(
                sample_id,
                trade_date,
                f"{stock_index:06d}",
                feature_x=float(stock_index),
                regime=regime,
            )
            candidate["rule_order"] = stock_index
            candidates.append(candidate)
            labels.append(_label(
                sample_id,
                fill_label=stock_index % 2,
                fill_matured_at=f"{trade_date}T10:15:00+08:00",
                matured_3d_at=f"{trade_date}T16:00:00+08:00",
                matured_5d_at=f"{trade_date}T16:00:00+08:00",
                matured_10d_at=f"{trade_date}T16:00:00+08:00",
                downside_matured_at=f"{trade_date}T16:00:00+08:00",
            ))
    return build_training_frame(candidates, labels, ALLOWLIST)


class MlTrainingDataTest(unittest.TestCase):
    def test_builds_allowlisted_frame_and_each_stock_day_weight_sums_to_one(self) -> None:
        candidates = [
            _candidate(
                f"s{i}",
                "2025-01-02",
                "600000",
                feature_x=float(i),
                extra_features={
                    "parameter_snapshot": {
                        "value": {"nested": [1, 2, 3]},
                        "available_at": "2025-01-02T10:00:00+08:00",
                    }
                },
            )
            for i in range(5)
        ]
        labels = [_label(f"s{i}") for i in range(5)]

        frame = build_training_frame(candidates, labels, ALLOWLIST)

        self.assertEqual(frame.feature_names, ALLOWLIST)
        self.assertEqual(list(frame.features.columns), list(ALLOWLIST))
        self.assertAlmostEqual(frame.rows["sample_weight"].sum(), 1.0)
        self.assertAlmostEqual(frame.weights.sum(), 1.0)
        self.assertNotIn("code", frame.features.columns)
        self.assertNotIn("name", frame.features.columns)
        self.assertNotIn("parameter_snapshot", frame.rows.columns)
        self.assertEqual(frame.metadata["feature_schema_version"], "features-v1")
        self.assertEqual(frame.rows["rule_selected"].tolist(), [True] * 5)
        self.assertEqual(frame.rows["rule_target_qty"].tolist(), [100] * 5)
        self.assertNotIn("rule_score", frame.features.columns)
        self.assertEqual(frame.metadata["construction_source"], "build_training_frame-v2")

    def test_missing_label_values_remain_in_quality_denominators(self) -> None:
        dates = _business_dates(date(2024, 1, 2), 250)
        candidates = [
            _candidate(f"s{i}", trade_date, f"{i % 30:06d}")
            for i, trade_date in enumerate(dates)
        ]
        labels = pd.DataFrame(
            [
                _label(
                    f"s{i}",
                    fill_label=i % 2,
                    ret_5d_net=math.nan if i % 10 == 1 else 0.02,
                )
                for i in range(len(dates))
            ]
        )

        frame = build_training_frame(candidates, labels, ALLOWLIST)
        result = validate_training_data(frame, build_ml_splits(frame))

        self.assertEqual(result.metrics["coverage_denominators"]["fill"], 250)
        self.assertEqual(result.metrics["coverage_numerators"]["fill"], 250)
        self.assertEqual(result.metrics["coverage_denominators"]["ret_5d"], 125)
        self.assertEqual(result.metrics["quality_failures"]["ret_5d"], 25)
        self.assertAlmostEqual(result.metrics["ret_5d_coverage"], 0.8)

    def test_assign_stock_day_weights_is_reciprocal_and_independent_by_stock_day(self) -> None:
        rows = pd.DataFrame(
            [
                {"sample_id": "a", "trade_date": "2025-01-02", "code": "600000"},
                {"sample_id": "b", "trade_date": "2025-01-02", "code": "600000"},
                {"sample_id": "c", "trade_date": "2025-01-02", "code": "000001"},
                {"sample_id": "d", "trade_date": "2025-01-03", "code": "600000"},
            ]
        )

        weights = assign_stock_day_weights(rows)

        self.assertEqual(weights.tolist(), [0.5, 0.5, 1.0, 1.0])
        totals = rows.assign(sample_weight=weights).groupby(
            ["trade_date", "code"]
        )["sample_weight"].sum()
        self.assertTrue((totals == 1.0).all())

    def test_rejects_future_timestamps_for_allowlisted_features(self) -> None:
        candidate = _candidate(
            "s1",
            "2025-01-02",
            "600000",
            feature_available_at="2025-01-02T10:00:01+08:00",
        )

        with self.assertRaisesRegex(ValueError, "FEATURE_FROM_FUTURE: feature_x"):
            build_training_frame([candidate], [_label("s1")], ALLOWLIST)

    def test_rejects_identity_shadow_and_future_result_model_features(self) -> None:
        expected = {
            "code",
            "name",
            "enhanced_score",
            "shadow_adjust_score",
            "shadow_rank",
            "shadow_rank_change",
            "shadow_reason",
            "future_result",
        }
        self.assertTrue(expected.issubset(FORBIDDEN_MODEL_FEATURES))
        for field in expected:
            with self.subTest(field=field):
                candidate = _candidate(
                    "s1",
                    "2025-01-02",
                    "600000",
                    extra_features={
                        field: {
                            "value": 1,
                            "available_at": "2025-01-02T10:00:00+08:00",
                        }
                    },
                )
                with self.assertRaisesRegex(
                    ValueError, f"FORBIDDEN_MODEL_FEATURE: {field}"
                ):
                    build_training_frame(
                        [candidate], [_label("s1")], (*ALLOWLIST, field)
                    )

    def test_rejects_unknown_allowlist_entries_mixed_versions_and_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "FEATURE_NOT_IN_FROZEN_ALLOWLIST"):
            build_training_frame(
                [_candidate("s1", "2025-01-02", "600000")],
                [_label("s1")],
                ("feature_x", "not_recorded"),
            )

        cases = (
            (
                [
                    _candidate("s1", "2025-01-02", "600000"),
                    _candidate(
                        "s2",
                        "2025-01-03",
                        "600001",
                        parameter_version="params-v2",
                    ),
                ],
                [_label("s1"), _label("s2")],
                "MIXED_PARAMETER_VERSION",
            ),
            (
                [
                    _candidate("s1", "2025-01-02", "600000"),
                    _candidate(
                        "s2",
                        "2025-01-03",
                        "600001",
                        feature_schema_version="features-v2",
                    ),
                ],
                [_label("s1"), _label("s2")],
                "MIXED_FEATURE_SCHEMA_VERSION",
            ),
            (
                [
                    _candidate("s1", "2025-01-02", "600000"),
                    _candidate("s2", "2025-01-03", "600001"),
                ],
                [_label("s1"), _label("s2", cost_version="fees-v3")],
                "MIXED_COST_VERSION",
            ),
            (
                [
                    _candidate("s1", "2025-01-02", "600000"),
                    _candidate("s2", "2025-01-03", "600001"),
                ],
                [_label("s1"), _label("s2", label_source="simulation")],
                "MIXED_LABEL_SOURCE",
            ),
        )
        for candidates, labels, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    build_training_frame(candidates, labels, ALLOWLIST)

    def test_same_cost_version_with_different_contract_hash_is_rejected(self) -> None:
        candidates = [
            _candidate("s1", "2025-01-02", "600000"),
            _candidate("s2", "2025-01-03", "600001"),
        ]
        labels = [
            _label("s1"),
            _label("s2", cost_sha256="d" * 64),
        ]
        with self.assertRaisesRegex(ValueError, "MIXED_COST_SHA256"):
            build_training_frame(candidates, labels, ALLOWLIST)

    def test_generic_matured_at_cannot_mature_independent_heads(self) -> None:
        dates = _business_dates(date(2024, 1, 2), 250)
        candidates = [
            _candidate(f"s{i}", trade_date, f"{i % 30:06d}")
            for i, trade_date in enumerate(dates)
        ]
        labels = [
            _label(
                f"s{i}",
                fill_matured_at=None,
                matured_3d_at=None,
                matured_5d_at=None,
                matured_10d_at=None,
                downside_matured_at=None,
                matured_at=f"{trade_date}T16:00:00+08:00",
            )
            for i, trade_date in enumerate(dates)
        ]
        frame = build_training_frame(candidates, labels, ALLOWLIST)
        result = validate_training_data(frame, build_ml_splits(frame))
        self.assertEqual(result.metrics["coverage_denominators"]["fill"], 0)
        self.assertEqual(result.metrics["coverage_denominators"]["ret_3d"], 0)
        self.assertEqual(result.metrics["coverage_denominators"]["ret_5d"], 0)
        self.assertEqual(result.metrics["coverage_denominators"]["ret_10d"], 0)
        self.assertEqual(result.metrics["coverage_denominators"]["downside"], 0)

    def test_rejects_non_finite_values_and_invalid_fill_labels(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "NON_FINITE_FEATURE: feature_x"):
                    build_training_frame(
                        [_candidate("s1", "2025-01-02", "600000", feature_x=value)],
                        [_label("s1")],
                        ALLOWLIST,
                    )

        with self.assertRaisesRegex(ValueError, "INVALID_FILL_LABEL"):
            build_training_frame(
                [_candidate("s1", "2025-01-02", "600000")],
                [_label("s1", fill_label=2)],
                ALLOWLIST,
            )

    def test_builds_three_expanding_folds_with_embargo_and_sealed_holdout(self) -> None:
        dates = _business_dates(date(2024, 1, 2), 250)
        rows = pd.DataFrame(
            {
                "sample_id": [f"s{i}" for i in range(len(dates))],
                "trade_date": dates,
                "code": ["600000"] * len(dates),
                "sample_weight": [1.0] * len(dates),
            }
        )
        frame = TrainingFrame(rows=rows, feature_names=(), metadata={})

        splits = build_ml_splits(frame, holdout_days=40, folds=3, embargo_days=10)

        self.assertEqual(len(splits.walk_forward), 3)
        self.assertEqual(splits.holdout_dates, tuple(dates[-40:]))
        self.assertEqual(splits.all_development_dates, tuple(dates[:-40]))
        self.assertTrue(
            set(splits.holdout_dates).isdisjoint(splits.all_development_dates)
        )
        previous_train_size = 0
        for fold in splits.walk_forward:
            self.assertGreater(len(fold.train_dates), previous_train_size)
            previous_train_size = len(fold.train_dates)
            self.assertEqual(fold.test_dates, fold.validation_dates)
            self.assertEqual(len(fold.embargo_dates), 10)
            train_end = dates.index(fold.train_dates[-1])
            test_start = dates.index(fold.test_dates[0])
            self.assertEqual(test_start - train_end, 11)
            self.assertTrue(set(fold.test_dates).isdisjoint(splits.holdout_dates))
        self.assertEqual(
            splits.split_sha256,
            build_ml_splits(dates, 40, 3, 10).split_sha256,
        )

    def test_split_builder_refuses_too_few_dates_or_less_than_three_folds(self) -> None:
        dates = _business_dates(date(2025, 1, 2), 70)
        with self.assertRaisesRegex(ValueError, "AT_LEAST_THREE_FOLDS_REQUIRED"):
            build_ml_splits(dates, holdout_days=40, folds=2, embargo_days=10)
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_DATES_FOR_SPLITS"):
            build_ml_splits(dates, holdout_days=40, folds=3, embargo_days=10)

    def test_readiness_separates_diagnostic_from_l0_and_never_approves(self) -> None:
        frame = _ready_frame(trading_days=120, stocks_per_day=42)
        splits = build_ml_splits(frame)

        result = validate_training_data(frame, splits, require_l0=False)

        self.assertIsInstance(result, DataReadiness)
        self.assertTrue(result.diagnostic_ready)
        self.assertTrue(result.linear_diagnostic_ready)
        self.assertFalse(result.gradient_diagnostic_ready)
        self.assertFalse(result.l0_ready)
        self.assertFalse(result.approvable)
        self.assertIn("L0_CALENDAR_SPAN_LT_365", result.l0_reasons)
        self.assertIn("GRADIENT_STOCK_DAYS_LT_15000", result.diagnostic_reasons)

    def test_complete_one_year_frame_passes_l0_data_readiness(self) -> None:
        frame = _ready_frame()
        splits = build_ml_splits(frame)

        result = validate_training_data(frame, splits, require_l0=True)

        self.assertTrue(result.linear_diagnostic_ready)
        self.assertTrue(result.gradient_diagnostic_ready)
        self.assertTrue(result.l0_ready)
        self.assertTrue(result.eligible)
        self.assertFalse(result.approvable)
        self.assertEqual(result.metrics["fill_coverage"], 1.0)
        self.assertEqual(result.metrics["ret_3d_coverage"], 1.0)
        self.assertEqual(result.metrics["ret_5d_coverage"], 1.0)
        self.assertEqual(result.metrics["ret_10d_coverage"], 1.0)
        self.assertEqual(result.metrics["downside_coverage"], 1.0)
        for regime in ("NORMAL", "CAUTION", "RISK_OFF"):
            self.assertGreaterEqual(result.metrics["regimes"][regime]["trading_days"], 10)
            self.assertGreaterEqual(result.metrics["regimes"][regime]["stock_days"], 500)

    def test_direct_frame_without_builder_provenance_cannot_pass_l0(self) -> None:
        verified = _ready_frame()
        direct = TrainingFrame(
            rows=verified.rows,
            feature_names=verified.feature_names,
            metadata={
                key: value
                for key, value in verified.metadata.items()
                if key not in {"construction_source", "strict_provenance_sha256"}
            },
        )
        result = validate_training_data(
            direct,
            build_ml_splits(direct),
            require_l0=True,
        )
        self.assertFalse(result.l0_ready)
        self.assertIn(
            "L0_STRICT_BUILDER_PROVENANCE_REQUIRED",
            result.l0_reasons,
        )

    def test_coverage_failures_keep_failed_rows_in_denominators(self) -> None:
        frame = _ready_frame()
        damaged = frame.rows.copy(deep=True)
        damaged.loc[damaged.index[:200], "fill_label"] = pd.NA
        damaged.loc[damaged.index[:2000], "ret_5d_net"] = pd.NA
        damaged.loc[damaged.index[:2000], "downside_loss"] = pd.NA
        damaged_frame = TrainingFrame(
            rows=damaged,
            feature_names=frame.feature_names,
            metadata=frame.metadata,
        )

        result = validate_training_data(
            damaged_frame,
            build_ml_splits(damaged_frame),
            require_l0=True,
        )

        total = len(damaged)
        filled_total = int((damaged["fill_label"] == 1).sum())
        self.assertEqual(result.metrics["coverage_denominators"]["fill"], total)
        self.assertEqual(
            result.metrics["coverage_denominators"]["ret_5d"], filled_total
        )
        self.assertEqual(result.metrics["quality_failures"]["fill"], 200)
        self.assertEqual(result.metrics["quality_failures"]["ret_5d"], 900)
        self.assertIn("FILL_COVERAGE_LT_0_99", result.l0_reasons)
        self.assertIn("RET_5D_COVERAGE_LT_0_90", result.l0_reasons)
        self.assertIn("DOWNSIDE_COVERAGE_LT_0_90", result.l0_reasons)
        self.assertFalse(result.l0_ready)

    def test_regime_coverage_is_a_hard_l0_gate(self) -> None:
        frame = _ready_frame()
        rows = frame.rows.copy(deep=True)
        rows.loc[rows["market_regime"] == "RISK_OFF", "market_regime"] = "NORMAL"
        damaged = TrainingFrame(
            rows=rows,
            feature_names=frame.feature_names,
            metadata=frame.metadata,
        )

        result = validate_training_data(
            damaged,
            build_ml_splits(damaged),
            require_l0=True,
        )

        self.assertIn("REGIME_RISK_OFF_TRADING_DAYS_LT_10", result.l0_reasons)
        self.assertIn("REGIME_RISK_OFF_STOCK_DAYS_LT_500", result.l0_reasons)
        self.assertFalse(result.l0_ready)


if __name__ == "__main__":
    unittest.main()
