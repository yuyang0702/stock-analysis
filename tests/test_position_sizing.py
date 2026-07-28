from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal

from execution_contracts import FeeSchedule, InstrumentRules, RoundTripCost
from position_sizing import (
    CapacityBudget,
    SizingDecision,
    SizingPolicy,
    allocate_buy_quantity,
)


D = Decimal
FEES = FeeSchedule(
    version="sim-v1",
    effective_from="2026-01-01",
    buy_commission_rate=D("0.0003"),
    sell_commission_rate=D("0.0003"),
    buy_minimum_commission_yuan=D("5"),
    sell_minimum_commission_yuan=D("5"),
    stamp_tax_rate=D("0.0005"),
    transfer_fee_rate=D("0.00001"),
    other_fee_rate=D("0"),
    buy_slippage_rate=D("0.001"),
    sell_slippage_rate=D("0.001"),
)
RULES_100 = InstrumentRules.a_share("600000")
RULES_200 = InstrumentRules.a_share("920001", buy_min_qty=200, buy_qty_step=200)


def allocate(**changes: object):
    values: dict[str, object] = {
        "entry_price": D("10"),
        "stop_price": D("9.5"),
        "gap_price": D("9"),
        "rules": RULES_100,
        "fees": FEES,
        "equity": D("50000"),
        "available_cash": D("12000"),
        "risk_pct": D("0.01"),
        "risk_cap_yuan": D("300"),
        "capacity": CapacityBudget(
            max_qty=1300,
            remaining_open_risk_yuan=D("500"),
        ),
        "expected_gross_return": D("0.03"),
        "max_cost_edge_ratio": D("0.35"),
    }
    values.update(changes)
    return allocate_buy_quantity(**values)


class PositionSizingTest(unittest.TestCase):
    def test_allocator_searches_down_by_lot_and_freezes_exact_fee_evidence(self) -> None:
        result = allocate()

        self.assertTrue(result.allowed)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.target_qty, 200)
        self.assertEqual(result.position_value_yuan, D("2000"))
        self.assertEqual(result.buy_cash_required_yuan, D("2007.02"))
        self.assertEqual(result.percentage_risk_yuan, D("500.00"))
        self.assertEqual(result.effective_per_trade_risk_yuan, D("300"))
        self.assertEqual(result.planned_stop_loss_yuan, D("114.89"))
        self.assertEqual(result.gap_loss_yuan, D("214.74"))
        self.assertEqual(result.worst_case_loss_yuan, D("214.74"))
        self.assertEqual(result.rule_target_price, D("10.30"))
        self.assertEqual(result.gross_edge_yuan, D("60.00"))
        self.assertEqual(result.expected_net_pnl_yuan, D("44.87"))
        self.assertEqual(result.fee_erosion_ratio, D("0.00756500"))
        self.assertEqual(result.cost_to_expected_edge_ratio, D("0.25216667"))
        self.assertIsInstance(result.planned_stop_cost, RoundTripCost)
        self.assertEqual(result.buy_fee, result.planned_stop_cost.buy)
        self.assertEqual(result.planned_stop_cost.buy.commission_yuan, D("5.00"))
        self.assertEqual(result.planned_stop_cost.sell.commission_yuan, D("5.00"))
        self.assertEqual(result.planned_stop_cost.sell.stamp_tax_yuan, D("0.95"))
        self.assertEqual(result.planned_stop_cost.buy.slippage_yuan, D("2.00"))
        self.assertEqual(result.planned_stop_cost.sell.slippage_yuan, D("1.90"))
        self.assertEqual(result.target_cost.sell.stamp_tax_yuan, D("1.03"))
        self.assertEqual(result.fee_schedule_version, FEES.version)
        self.assertEqual(result.fee_schedule_sha256, FEES.contract_sha256)

    def test_planned_stop_can_be_the_worst_loss_scenario(self) -> None:
        result = allocate(gap_price=D("9.8"))

        self.assertGreater(result.planned_stop_loss_yuan, result.gap_loss_yuan)
        self.assertEqual(result.worst_case_loss_yuan, result.planned_stop_loss_yuan)

    def test_allocator_respects_200_share_step_at_low_and_high_prices(self) -> None:
        low = allocate(
            entry_price=D("2"), stop_price=D("1.9"), gap_price=D("1.8"),
            rules=RULES_200, risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(550, D("1000")),
            expected_gross_return=D("0.10"), max_cost_edge_ratio=D("0.5"),
        )
        high = allocate(
            entry_price=D("100"), stop_price=D("99.5"), gap_price=D("99"),
            rules=RULES_200, available_cash=D("19999"), risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(400, D("1000")),
            expected_gross_return=D("0.03"), max_cost_edge_ratio=D("0.5"),
        )

        self.assertEqual(low.target_qty, 400)
        self.assertFalse(high.allowed)
        self.assertEqual(high.reasons, ("CASH_CAPACITY_EXCEEDED",))

    def test_allocator_uses_minimum_valid_qty_when_minimum_and_step_differ(self) -> None:
        rules = InstrumentRules.a_share("920001", buy_min_qty=100, buy_qty_step=200)
        allowed = allocate(rules=rules, capacity=CapacityBudget(300, D("500")))
        rejected = allocate(rules=rules, capacity=CapacityBudget(199, D("500")))

        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.target_qty, 200)
        self.assertEqual(allowed.evaluated_qty, 200)
        self.assertEqual(rules.validate_order("buy", allowed.target_qty, D("10")), ())
        self.assertEqual(rejected.reasons, ("NO_BOARD_LOT",))
        self.assertEqual(rejected.evaluated_qty, 200)
        self.assertEqual(rejected.buy_fee.qty, 200)

    def test_capacity_value_and_cash_are_separate_at_one_lot_boundary(self) -> None:
        result = allocate(
            equity=D("10000"), available_cash=D("1007"), risk_pct=D("0.10"),
            risk_cap_yuan=D("1000"), capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.10"), max_cost_edge_ratio=D("0.5"),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.position_value_yuan, D("1000"))
        self.assertEqual(result.buy_cash_required_yuan, D("1006.01"))

        one_cent_short = allocate(
            equity=D("10000"), available_cash=D("1006"), risk_pct=D("0.10"),
            risk_cap_yuan=D("1000"), capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.10"), max_cost_edge_ratio=D("0.5"),
        )
        self.assertEqual(one_cent_short.reasons, ("CASH_CAPACITY_EXCEEDED",))

    def test_rejection_reasons_are_complete_and_in_fixed_order(self) -> None:
        result = allocate(
            stop_price=D("9.5"),
            capacity=CapacityBudget(50, D("0")),
            available_cash=D("0"),
            risk_cap_yuan=D("0"),
            expected_gross_return=D("0"),
            max_cost_edge_ratio=D("0"),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(
            result.reasons,
            (
                "NO_BOARD_LOT",
                "PER_TRADE_RISK_EXCEEDED",
                "PORTFOLIO_OPEN_RISK_EXCEEDED",
                "CASH_CAPACITY_EXCEEDED",
                "ECONOMIC_EDGE_INSUFFICIENT",
            ),
        )

    def test_invalid_stop_distance_is_reported_independently(self) -> None:
        result = allocate(
            stop_price=D("10"), expected_gross_return=D("0.10"),
            max_cost_edge_ratio=D("0.5"),
        )

        self.assertEqual(result.reasons, ("INVALID_STOP_DISTANCE",))

    def test_trade_and_portfolio_risk_reasons_are_independent(self) -> None:
        trade = allocate(
            risk_cap_yuan=D("80"),
            capacity=CapacityBudget(100, D("200")),
            expected_gross_return=D("0.10"), max_cost_edge_ratio=D("0.5"),
        )
        portfolio = allocate(
            risk_cap_yuan=D("200"),
            capacity=CapacityBudget(100, D("80")),
            expected_gross_return=D("0.10"), max_cost_edge_ratio=D("0.5"),
        )

        self.assertEqual(trade.reasons, ("PER_TRADE_RISK_EXCEEDED",))
        self.assertEqual(portfolio.reasons, ("PORTFOLIO_OPEN_RISK_EXCEEDED",))

    def test_rejected_evidence_names_its_audited_quantity(self) -> None:
        allowed = allocate()
        rejected = allocate(
            risk_cap_yuan=D("80"),
            capacity=CapacityBudget(100, D("200")),
            expected_gross_return=D("0.10"), max_cost_edge_ratio=D("0.5"),
        )

        self.assertEqual(allowed.evaluated_qty, allowed.target_qty)
        self.assertEqual(rejected.target_qty, 0)
        self.assertEqual(rejected.evaluated_qty, 100)
        self.assertEqual(rejected.position_value_yuan, D("10") * rejected.evaluated_qty)
        self.assertEqual(rejected.buy_fee.qty, rejected.evaluated_qty)
        for cost in (
            rejected.planned_stop_cost,
            rejected.gap_cost,
            rejected.target_cost,
        ):
            self.assertEqual(cost.buy.qty, rejected.evaluated_qty)
            self.assertEqual(cost.sell.qty, rejected.evaluated_qty)

    def test_economic_gate_uses_target_fees_and_can_only_reject(self) -> None:
        result = allocate(
            risk_cap_yuan=D("1000"), capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.005"), max_cost_edge_ratio=D("0.35"),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.target_qty, 0)
        self.assertEqual(result.reasons, ("ECONOMIC_EDGE_INSUFFICIENT",))
        self.assertFalse(result.economic_trade_allowed)
        self.assertEqual(result.target_cost.sell.price, D("10.050"))

    def test_rule_target_price_must_pass_instrument_price_rules(self) -> None:
        limited_rules = InstrumentRules.a_share("600000", limit_up_price=D("10.20"))
        result = allocate(
            rules=limited_rules, risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.03"), max_cost_edge_ratio=D("0.5"),
        )

        self.assertEqual(result.reasons, ("ECONOMIC_EDGE_INSUFFICIENT",))

    def test_missing_fee_schedule_rejects_without_fabricated_costs(self) -> None:
        result = allocate(fees=None)

        self.assertEqual(result.reasons, ("FEE_SCHEDULE_REQUIRED",))
        self.assertIsNone(result.buy_fee)
        self.assertIsNone(result.planned_stop_cost)
        self.assertEqual(result.fee_schedule_version, "")

    def test_non_finite_stop_rejects_and_other_non_finite_inputs_raise(self) -> None:
        self.assertEqual(
            allocate(
                stop_price=D("NaN"), expected_gross_return=D("0.10"),
                max_cost_edge_ratio=D("0.5"),
            ).reasons,
            ("INVALID_STOP_DISTANCE",),
        )
        with self.assertRaises(ValueError):
            allocate(available_cash=D("Infinity"))

    def test_policy_and_capacity_are_immutable_validated_decimals(self) -> None:
        policy = SizingPolicy(D("0.01"), D("300"), D("0.03"), D("0.35"))
        capacity = CapacityBudget(100, D("200"))

        self.assertEqual(policy.risk_cap_yuan, D("300"))
        with self.assertRaises(FrozenInstanceError):
            capacity.max_qty = 200
        with self.assertRaises(ValueError):
            CapacityBudget(True, D("200"))

    def test_sizing_decision_rejects_invalid_shape_reasons_and_decimals(self) -> None:
        result = allocate()
        values = {field.name: getattr(result, field.name) for field in fields(result)}
        decimal_fields = (
            "position_value_yuan", "buy_cash_required_yuan", "percentage_risk_yuan",
            "risk_cap_yuan", "effective_per_trade_risk_yuan",
            "remaining_open_risk_yuan", "planned_stop_loss_yuan", "gap_loss_yuan",
            "worst_case_loss_yuan", "rule_target_price", "gross_edge_yuan",
            "expected_net_pnl_yuan", "fee_erosion_ratio",
            "cost_to_expected_edge_ratio",
        )
        bad_changes = (
            {"allowed": "yes"},
            {"reasons": ["NO_BOARD_LOT"]},
            {"reasons": ("PER_TRADE_RISK_EXCEEDED",)},
            {"target_qty": -1},
            {"evaluated_qty": 0},
            {"economic_trade_allowed": False},
        )
        for changes in bad_changes:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    SizingDecision(**{**values, **changes})
        for name in decimal_fields:
            for value in (D("NaN"), 0):
                with self.subTest(field=name, value=value):
                    with self.assertRaises(ValueError):
                        replace(result, **{name: value})

        rejected = allocate(
            capacity=CapacityBudget(50, D("0")), available_cash=D("0"),
            risk_cap_yuan=D("0"), expected_gross_return=D("0"),
            max_cost_edge_ratio=D("0"),
        )
        with self.assertRaises(ValueError):
            replace(rejected, target_qty=rejected.evaluated_qty)
        with self.assertRaises(ValueError):
            replace(rejected, reasons=tuple(reversed(rejected.reasons)))
        with self.assertRaises(ValueError):
            replace(rejected, reasons=("UNKNOWN_REASON",))

        missing_fees = allocate(fees=None)
        with self.assertRaises(ValueError):
            replace(missing_fees, fee_schedule_version=0)
        with self.assertRaises(ValueError):
            replace(missing_fees, fee_schedule_sha256=None)

    def test_sizing_decision_recomputes_fee_scenario_and_economic_evidence(self) -> None:
        result = allocate()
        wrong_qty_cost = FEES.estimate_round_trip(D("10"), D("9.5"), 100)
        wrong_price_cost = FEES.estimate_round_trip(D("11"), D("9.5"), 200)
        other_fees = FEES.derive_variant("other", buy_slippage_rate=D("0.002"))
        wrong_fee_cost = other_fees.estimate_round_trip(D("10"), D("9.5"), 200)
        bad_changes = (
            {"position_value_yuan": result.position_value_yuan + D("0.01")},
            {"buy_cash_required_yuan": result.buy_cash_required_yuan + D("0.01")},
            {"buy_fee": FEES.estimate("buy", D("10"), 100)},
            {"planned_stop_cost": None},
            {"gap_cost": None},
            {"target_cost": None},
            {"planned_stop_cost": wrong_qty_cost},
            {"planned_stop_cost": wrong_price_cost},
            {"planned_stop_cost": wrong_fee_cost},
            {"planned_stop_loss_yuan": result.planned_stop_loss_yuan + D("0.01")},
            {"gap_loss_yuan": result.gap_loss_yuan + D("0.01")},
            {"worst_case_loss_yuan": result.worst_case_loss_yuan + D("0.01")},
            {"rule_target_price": result.rule_target_price + D("0.01")},
            {"gross_edge_yuan": result.gross_edge_yuan + D("0.01")},
            {"expected_net_pnl_yuan": result.expected_net_pnl_yuan + D("0.01")},
            {"fee_erosion_ratio": result.fee_erosion_ratio + D("0.00000001")},
            {
                "cost_to_expected_edge_ratio":
                    result.cost_to_expected_edge_ratio + D("0.00000001")
            },
            {"fee_schedule_version": "other"},
            {"fee_schedule_sha256": "0" * 64},
        )
        for changes in bad_changes:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(result, **changes)

    def test_rejected_sizing_decision_enforces_audited_evidence_identity(self) -> None:
        result = allocate(
            risk_cap_yuan=D("80"), capacity=CapacityBudget(100, D("200")),
            expected_gross_return=D("0.10"), max_cost_edge_ratio=D("0.5"),
        )

        with self.assertRaises(ValueError):
            replace(result, evaluated_qty=200)
        with self.assertRaises(ValueError):
            replace(result, position_value_yuan=D("0"))
        with self.assertRaises(ValueError):
            replace(result, reasons=())


if __name__ == "__main__":
    unittest.main()
