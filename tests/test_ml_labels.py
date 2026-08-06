from __future__ import annotations

import unittest
from decimal import Decimal

from execution_contracts import FeeSchedule
from ml_contracts import CandidateSample, TimedFeature
from ml_labels import LabelPolicy, label_path, label_sample


def D(value: str) -> Decimal:
    return Decimal(value)


FEES = FeeSchedule.simulation(
    version="fees-v2",
    commission_rate=D("0.0003"),
    minimum_commission_yuan=D("5"),
    stamp_tax_rate=D("0.0005"),
    transfer_fee_rate=D("0.00001"),
    other_fee_rate=D("0.00002"),
    slippage_rate=D("0.001"),
)


def sample(*, planned_price: float = 10.0) -> CandidateSample:
    return CandidateSample.from_values(
        source="strict_history",
        dataset_id="strict-1",
        decision_at="2025-01-02T10:00:00+08:00",
        code="600000",
        strategy_version="strategy-v1",
        parameter_version="params-v1",
        feature_schema_version="features-v1",
        features={
            "entry_price": TimedFeature(
                planned_price, "2025-01-02T10:00:00+08:00"
            ),
            "stop_loss": TimedFeature(9.0, "2025-01-02T10:00:00+08:00"),
            "take_profit": TimedFeature(12.0, "2025-01-02T10:00:00+08:00"),
        },
        selected=True,
        rejection_stage="selected",
        rejection_code="",
        final_action="selected",
        universe_hash="universe-sha",
        market_data_version="market-v1",
        code_hash="code-sha",
        generator_hash="generator-sha",
    )


def price(
    at: str,
    *,
    open_price: float | None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    paused: int = 0,
    limit_up: float | None = None,
    limit_down: float | None = None,
) -> dict[str, object]:
    return {
        "dataset_id": "strict-1",
        "code": "600000",
        "bar_at": at,
        "available_at": at,
        "open": open_price,
        "high": open_price if high is None else high,
        "low": open_price if low is None else low,
        "close": open_price if close is None else close,
        "volume": 100_000.0,
        "amount": 1_000_000.0,
        "paused": paused,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "adjustment_version": "raw-v1",
    }


class MlLabelsTest(unittest.TestCase):
    def test_horizons_mature_independently_and_keep_fee_components(self) -> None:
        rows = [
            price("2025-01-02T10:05:00+08:00", open_price=10.0),
            price("2025-01-03T15:00:00+08:00", open_price=10.1, close=10.1),
            price("2025-01-06T15:00:00+08:00", open_price=10.2, close=10.2),
            price("2025-01-07T15:00:00+08:00", open_price=10.3, close=10.3),
            price("2025-01-08T15:00:00+08:00", open_price=10.4, close=10.4),
            price("2025-01-09T15:00:00+08:00", open_price=10.5, close=10.5),
        ]

        outcome = label_sample(
            sample(),
            rows,
            as_of="2025-01-09T16:00:00+08:00",
            fee_schedule=FEES,
            policy=LabelPolicy(reference_quantity=100),
        )

        self.assertIsNotNone(outcome.ret_3d_net)
        self.assertIsNotNone(outcome.ret_5d_net)
        self.assertIsNone(outcome.ret_10d_net)
        self.assertEqual(outcome.cost_version, FEES.version)
        self.assertAlmostEqual(
            outcome.net_cost,
            outcome.buy_cost + outcome.sell_cost + outcome.slippage_cost,
            places=8,
        )
        self.assertIsNotNone(outcome.matured_3d_at)
        self.assertIsNotNone(outcome.matured_5d_at)
        self.assertIsNone(outcome.matured_10d_at)

    def test_downside_is_positive_loss_and_marks_blocked_exit(self) -> None:
        outcome = label_path(
            entry_ref=D("10"),
            lows=[D("9"), D("8")],
            fee_schedule=FEES,
            qty=100,
            limit_down_blocked=True,
        )

        self.assertGreater(outcome.downside_loss, 0)
        self.assertEqual(outcome.exit_blocked, 1)

    def test_locked_limit_up_window_is_a_no_fill_not_zero_return(self) -> None:
        rows = [
            price(
                "2025-01-02T10:05:00+08:00",
                open_price=11.0,
                high=11.0,
                low=11.0,
                close=11.0,
                limit_up=11.0,
            )
        ]

        outcome = label_sample(
            sample(planned_price=11.0),
            rows,
            as_of="2025-01-02T16:00:00+08:00",
            fee_schedule=FEES,
            policy=LabelPolicy(fill_window_bars=1),
        )

        self.assertEqual(outcome.fill_label, 0)
        self.assertEqual(outcome.fill_status, "not_filled")
        self.assertEqual(
            outcome.fill_matured_at,
            "2025-01-02T10:05:00+08:00",
        )
        self.assertIsNone(outcome.ret_3d_net)
        self.assertIn("OPENING_LIMIT_UP", outcome.quality_reasons)

    def test_paused_path_carries_last_official_close_and_records_quality_flag(self) -> None:
        rows = [
            price("2025-01-02T10:05:00+08:00", open_price=10.0, close=10.0),
            price("2025-01-03T15:00:00+08:00", open_price=10.1, close=10.1),
            price("2025-01-06T15:00:00+08:00", open_price=10.2, close=10.2),
            price(
                "2025-01-07T15:00:00+08:00",
                open_price=None,
                high=None,
                low=None,
                close=None,
                paused=1,
            ),
        ]

        outcome = label_sample(
            sample(),
            rows,
            as_of="2025-01-07T16:00:00+08:00",
            fee_schedule=FEES,
        )

        self.assertEqual(outcome.paused_path, 1)
        self.assertIsNotNone(outcome.ret_3d_net)
        self.assertIn("PAUSED_PATH", outcome.quality_reasons)

    def test_bar_may_be_published_after_bar_time_but_not_used_before_available(self) -> None:
        row = {
            **price("2025-01-02T10:05:00+08:00", open_price=10.0),
            "available_at": "2025-01-02T10:05:01+08:00",
        }

        pending = label_sample(
            sample(),
            [row],
            as_of="2025-01-02T10:05:00+08:00",
            fee_schedule=FEES,
        )
        visible = label_sample(
            sample(),
            [row],
            as_of="2025-01-02T16:00:00+08:00",
            fee_schedule=FEES,
        )

        self.assertIsNone(pending.fill_label)
        self.assertEqual(pending.fill_status, "pending")
        self.assertEqual(visible.fill_label, 1)
        self.assertEqual(
            visible.fill_matured_at,
            "2025-01-02T10:05:01+08:00",
        )

    def test_partial_fill_window_stays_pending_until_deadline(self) -> None:
        row = price(
            "2025-01-02T10:05:00+08:00",
            open_price=10.5,
            low=10.4,
        )
        policy = LabelPolicy(fill_window_bars=2, max_fill_delay_sec=15 * 60)

        pending = label_sample(
            sample(),
            [row],
            as_of="2025-01-02T10:06:00+08:00",
            fee_schedule=FEES,
            policy=policy,
        )
        mature = label_sample(
            sample(),
            [row],
            as_of="2025-01-02T10:16:00+08:00",
            fee_schedule=FEES,
            policy=policy,
        )

        self.assertIsNone(pending.fill_label)
        self.assertIsNone(pending.fill_matured_at)
        self.assertEqual(mature.fill_label, 0)
        self.assertEqual(mature.fill_status, "not_filled")
        self.assertEqual(
            mature.fill_matured_at,
            "2025-01-02T10:15:00+08:00",
        )

    def test_missing_official_path_matures_as_failure_at_deadline(self) -> None:
        outcome = label_sample(
            sample(),
            [],
            as_of="2025-01-02T10:16:00+08:00",
            fee_schedule=FEES,
            policy=LabelPolicy(max_fill_delay_sec=15 * 60),
        )
        self.assertIsNone(outcome.fill_label)
        self.assertEqual(outcome.fill_status, "failed")
        self.assertEqual(
            outcome.fill_matured_at,
            "2025-01-02T10:15:00+08:00",
        )


if __name__ == "__main__":
    unittest.main()
