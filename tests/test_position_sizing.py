from __future__ import annotations

import inspect
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, Inexact, ROUND_DOWN, ROUND_UP, Rounded, localcontext

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
    def test_allocator_keeps_its_public_signature(self) -> None:
        self.assertEqual(allocate_buy_quantity.__name__, "allocate_buy_quantity")
        self.assertIn("entry_price", inspect.signature(allocate_buy_quantity).parameters)

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
        self.assertEqual(result.available_cash_yuan, D("12000"))
        self.assertEqual(result.capacity_max_qty, 1300)
        self.assertEqual(result.entry_price, D("10"))
        self.assertEqual(result.planned_stop_price, D("9.5"))
        self.assertEqual(result.gap_scenario_price, D("9"))
        self.assertEqual(result.equity_yuan, D("50000"))
        self.assertEqual(result.risk_pct, D("0.01"))
        self.assertEqual(result.expected_gross_return, D("0.03"))
        self.assertEqual(result.fee_schedule, FEES)
        self.assertEqual(result.instrument_rules, RULES_100)

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

    def test_decision_recomputes_cash_and_board_lot_rejections(self) -> None:
        allowed = allocate()
        cash_rejected = allocate(available_cash=D("1"))
        no_board_lot = allocate(capacity=CapacityBudget(50, D("500")))

        for value, changes in (
            (cash_rejected, {"reasons": ("NO_BOARD_LOT",)}),
            (
                allowed,
                {
                    "allowed": False,
                    "target_qty": 0,
                    "reasons": ("CASH_CAPACITY_EXCEEDED",),
                },
            ),
            (
                allowed,
                {"available_cash_yuan": allowed.buy_cash_required_yuan - D("0.01")},
            ),
            (allowed, {"capacity_max_qty": allowed.target_qty - 1}),
            (no_board_lot, {"capacity_max_qty": 100}),
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(value, **changes)

        for changes in (
            {"available_cash_yuan": D("NaN")},
            {"capacity_max_qty": True},
            {"instrument_rules": None},
            {
                "instrument_rules": InstrumentRules.a_share(
                    "600000", buy_min_qty=300, buy_qty_step=100,
                ),
            },
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(allowed, **changes)

    def test_decision_replays_the_full_descending_quantity_search(self) -> None:
        one_lot = allocate(
            risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.10"),
            max_cost_edge_ratio=D("0.5"),
        )
        uneconomic_one_lot = allocate(
            risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(100, D("1000")),
        )

        self.assertTrue(one_lot.allowed)
        self.assertEqual(one_lot.target_qty, 100)
        self.assertEqual(
            uneconomic_one_lot.reasons,
            ("ECONOMIC_EDGE_INSUFFICIENT",),
        )
        with self.assertRaises(ValueError):
            replace(one_lot, capacity_max_qty=1300)
        with self.assertRaises(ValueError):
            replace(uneconomic_one_lot, capacity_max_qty=1300)

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
        self.assertEqual(
            allocate(
                stop_price=D("-1"),
                expected_gross_return=D("0.10"),
                max_cost_edge_ratio=D("0.5"),
            ).reasons,
            ("INVALID_STOP_DISTANCE",),
        )
        self.assertEqual(
            allocate(
                gap_price=D("-1"),
                expected_gross_return=D("0.10"),
                max_cost_edge_ratio=D("0.5"),
            ).reasons,
            ("INVALID_STOP_DISTANCE",),
        )
        self.assertEqual(
            allocate(stop_price=D("-1"), fees=None).reasons,
            ("FEE_SCHEDULE_REQUIRED", "INVALID_STOP_DISTANCE"),
        )

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

    def test_rule_target_price_floors_to_the_instrument_tick(self) -> None:
        result = allocate(entry_price=D("10.01"))

        self.assertTrue(result.allowed)
        self.assertEqual(result.rule_target_price, D("10.31"))
        self.assertEqual(result.target_cost.sell.price, D("10.31"))

    def test_allocator_and_decision_ignore_external_decimal_context(self) -> None:
        decisions = []
        for precision in (8, 12, 28):
            for rounding in (ROUND_DOWN, ROUND_UP):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    decisions.append(allocate(
                        expected_gross_return=D("0.0319999999"),
                        max_cost_edge_ratio=D("0.24"),
                    ))
        self.assertTrue(all(decision == decisions[0] for decision in decisions))
        with localcontext() as context:
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            self.assertEqual(
                allocate(
                    expected_gross_return=D("0.0319999999"),
                    max_cost_edge_ratio=D("0.24"),
                ),
                decisions[0],
            )

        precise = allocate(
            stop_price=D("9.987654321"),
            gap_price=D("9.87654321"),
            risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(1100, D("1000")),
            expected_gross_return=D("0.10"),
            max_cost_edge_ratio=D("0.5"),
        )
        with localcontext() as context:
            context.prec = 5
            context.rounding = ROUND_DOWN
            self.assertEqual(replace(precise), precise)

    def test_future_target_is_not_limited_by_todays_limit_up(self) -> None:
        limited_rules = InstrumentRules.a_share("600000", limit_up_price=D("10.20"))
        result = allocate(
            rules=limited_rules, risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.03"), max_cost_edge_ratio=D("0.5"),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.rule_target_price, D("10.30"))

    def test_missing_fee_schedule_rejects_without_fabricated_costs(self) -> None:
        result = allocate(fees=None)

        self.assertEqual(result.reasons, ("FEE_SCHEDULE_REQUIRED",))
        self.assertIsNone(result.buy_fee)
        self.assertIsNone(result.planned_stop_cost)
        self.assertEqual(result.fee_schedule_version, "")
        with self.assertRaises(ValueError):
            replace(result, target_rule_valid=True)

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
            "cost_to_expected_edge_ratio", "max_cost_edge_ratio",
            "available_cash_yuan", "entry_price", "planned_stop_price",
            "gap_scenario_price", "equity_yuan", "risk_pct",
        )
        bad_changes = (
            {"allowed": "yes"},
            {"reasons": ["NO_BOARD_LOT"]},
            {"reasons": ("PER_TRADE_RISK_EXCEEDED",)},
            {"target_qty": -1},
            {"evaluated_qty": 0},
            {"target_rule_valid": "yes"},
            {"economic_trade_allowed": False},
            {"expected_gross_return": D("NaN")},
            {"fee_schedule": None},
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
            {"target_rule_valid": False},
        )
        for changes in bad_changes:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(result, **changes)

        cash_rejected = allocate(
            available_cash=D("0"),
            expected_gross_return=D("0.10"),
            max_cost_edge_ratio=D("0.5"),
        )
        with self.assertRaises(ValueError):
            replace(
                cash_rejected,
                reasons=("ECONOMIC_EDGE_INSUFFICIENT",),
                available_cash_yuan=D("12000"),
                economic_trade_allowed=False,
            )

        economic_rejected = allocate(
            risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.005"),
            max_cost_edge_ratio=D("0.35"),
        )
        richer_target = allocate(
            risk_cap_yuan=D("1000"),
            capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.05"),
            max_cost_edge_ratio=D("0.35"),
        )
        with self.assertRaises(ValueError):
            replace(
                economic_rejected,
                allowed=True,
                reasons=(),
                target_qty=economic_rejected.evaluated_qty,
                rule_target_price=richer_target.rule_target_price,
                target_cost=richer_target.target_cost,
                gross_edge_yuan=richer_target.gross_edge_yuan,
                expected_net_pnl_yuan=richer_target.expected_net_pnl_yuan,
                fee_erosion_ratio=richer_target.fee_erosion_ratio,
                cost_to_expected_edge_ratio=richer_target.cost_to_expected_edge_ratio,
                target_rule_valid=True,
                economic_trade_allowed=True,
            )

        risk_rejected = allocate(
            risk_cap_yuan=D("80"),
            capacity=CapacityBudget(100, D("200")),
            expected_gross_return=D("0.10"),
            max_cost_edge_ratio=D("0.5"),
        )
        tighter_stops = allocate(
            stop_price=D("9.9"),
            gap_price=D("9.85"),
            risk_cap_yuan=D("80"),
            capacity=CapacityBudget(100, D("200")),
            expected_gross_return=D("0.10"),
            max_cost_edge_ratio=D("0.5"),
        )
        with self.assertRaises(ValueError):
            replace(
                risk_rejected,
                allowed=True,
                reasons=(),
                target_qty=risk_rejected.evaluated_qty,
                planned_stop_cost=tighter_stops.planned_stop_cost,
                gap_cost=tighter_stops.gap_cost,
                planned_stop_loss_yuan=tighter_stops.planned_stop_loss_yuan,
                gap_loss_yuan=tighter_stops.gap_loss_yuan,
                worst_case_loss_yuan=tighter_stops.worst_case_loss_yuan,
            )

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

    def test_economic_rejection_cannot_be_flipped_above_frozen_ratio_limit(self) -> None:
        result = allocate(
            risk_cap_yuan=D("1000"), capacity=CapacityBudget(100, D("1000")),
            expected_gross_return=D("0.02"), max_cost_edge_ratio=D("0.35"),
        )

        self.assertGreater(result.expected_net_pnl_yuan, D("0"))
        self.assertGreater(result.cost_to_expected_edge_ratio, D("0.35"))
        with self.assertRaises(ValueError):
            replace(
                result,
                allowed=True,
                reasons=(),
                target_qty=result.evaluated_qty,
                economic_trade_allowed=True,
            )

    def test_invalid_stop_rejection_cannot_claim_scenario_risk_reasons(self) -> None:
        result = allocate(
            stop_price=D("10"), expected_gross_return=D("0.10"),
            max_cost_edge_ratio=D("0.5"),
        )

        with self.assertRaises(ValueError):
            replace(
                result,
                reasons=("INVALID_STOP_DISTANCE", "PER_TRADE_RISK_EXCEEDED"),
            )
        with self.assertRaises(ValueError):
            replace(
                result,
                reasons=("INVALID_STOP_DISTANCE", "PORTFOLIO_OPEN_RISK_EXCEEDED"),
            )

    def test_economics_can_be_observed_without_changing_hard_risk_search(self) -> None:
        enforced = allocate(
            expected_gross_return=D("0.002"),
            max_cost_edge_ratio=D("0.01"),
        )
        observed = allocate(
            expected_gross_return=D("0.002"),
            max_cost_edge_ratio=D("0.01"),
            economic_required=False,
        )

        self.assertFalse(enforced.allowed)
        self.assertIn("ECONOMIC_EDGE_INSUFFICIENT", enforced.reasons)
        self.assertTrue(observed.allowed)
        self.assertFalse(observed.economic_trade_allowed)
        self.assertGreater(observed.target_qty, enforced.evaluated_qty)

    def test_future_target_above_todays_limit_is_valid_economic_evidence(self) -> None:
        rules = InstrumentRules.a_share(
            "600000", limit_up_price=D("10.50"), limit_down_price=D("9"),
        )
        result = allocate(
            rules=rules,
            expected_gross_return=D("0.10"),
            max_cost_edge_ratio=D("0.5"),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.rule_target_price, D("11.00"))


if __name__ == "__main__":
    unittest.main()
