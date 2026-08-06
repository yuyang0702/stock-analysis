import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from holdings_web import PositionStore
from pre_trade_check import (
    AdoptedPositionCapacityEvidence,
    CapacityReservationEvidence,
    PositionCapacityEvidence,
    ReservationView,
    pre_trade_check,
)
from tests.test_execution_contracts import D, FEES, make_candidate
from tests.test_pre_trade_check import (
    SYSTEM_STATE,
    make_broker,
    make_active_reservation,
    make_policy,
    make_position_evidence,
    make_quote,
    make_reservation_view,
    make_rules,
    open_order,
    position,
)


class PortfolioValidationTest(unittest.TestCase):
    def make_store(self, tmpdir: str) -> PositionStore:
        base = Path(tmpdir)
        return PositionStore(base / "positions.json", base / "events.jsonl")

    def test_rejects_negative_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            with self.assertRaisesRegex(ValueError, "持仓数量"):
                store.upsert(
                    {
                        "code": "600000",
                        "qty": "-100",
                        "cost_price": "10",
                        "current_price": "10",
                    },
                    source="manual",
                )

    def test_rejects_empty_stock_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            with self.assertRaisesRegex(ValueError, "股票代码"):
                store.upsert(
                    {
                        "code": "",
                        "qty": "100",
                        "cost_price": "10",
                        "current_price": "10",
                    },
                    source="manual",
                )

    def test_rejects_inverted_stop_and_take_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            with self.assertRaisesRegex(ValueError, "止损价"):
                store.upsert(
                    {
                        "code": "600000",
                        "qty": "100",
                        "cost_price": "10",
                        "current_price": "10",
                        "stop_price": "10.5",
                        "take_price": "11",
                    },
                    source="manual",
                )

    def test_reservation_view_is_validated_and_defensively_immutable(self) -> None:
        broker = make_broker(positions=(position(),))
        evidence = make_position_evidence(broker)[0].to_dict()
        view = make_reservation_view(
            broker,
            positions=(evidence,),
        )
        evidence["effective_stop_price"] = "999"

        self.assertEqual(view.positions[0].industry, "technology")
        with self.assertRaises(FrozenInstanceError):
            view.daily_orders = 2
        with self.assertRaises(ValueError):
            replace(
                view,
                positions=(replace(
                    view.positions[0],
                    effective_stop_price=Decimal("-1"),
                ),),
            )

    def test_adopted_legacy_position_keeps_conservative_gap_risk(self) -> None:
        broker = make_broker(positions=(position(code="600001"),))
        adopted = AdoptedPositionCapacityEvidence(
            position_cycle_id="legacy-cycle-1",
            account_scope_id=broker.account_scope_id,
            adapter=broker.adapter,
            code="600001",
            industry="technology",
            theme="artificial-intelligence",
            effective_stop_price=D("9.50"),
            gap_price=D("8.50"),
            initial_qty=100,
            adopted_at="2026-07-27T15:00:00+08:00",
            source_sha256="a" * 64,
        )
        result = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(max_open_risk_fraction=D("0.50")),
            make_reservation_view(broker, positions=(adopted.to_dict(),)),
        )
        expected_gap_risk = (
            D("10") - D("8.50")
        ) * 100 + FEES.estimate_round_trip(D("10"), D("8.50"), 100).total_yuan

        self.assertTrue(result.allowed)
        self.assertEqual(
            result.projected_open_risk_yuan - result.per_trade_risk_yuan,
            expected_gap_risk,
        )
        self.assertEqual(
            make_reservation_view(
                broker, positions=(adopted.to_dict(),),
            ).positions[0].to_dict(),
            adopted.to_dict(),
        )

    def test_adopted_position_quantity_growth_fails_closed(self) -> None:
        broker = make_broker(positions=(position(
            code="600001", total_qty=200, sellable_qty=200,
        ),))
        adopted = AdoptedPositionCapacityEvidence(
            position_cycle_id="legacy-cycle-1",
            account_scope_id=broker.account_scope_id,
            adapter=broker.adapter,
            code="600001",
            industry="technology",
            theme="artificial-intelligence",
            effective_stop_price=D("9.50"),
            gap_price=D("8.50"),
            initial_qty=100,
            adopted_at="2026-07-27T15:00:00+08:00",
            source_sha256="a" * 64,
        )
        result = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(), make_reservation_view(broker, positions=(adopted,)),
        )

        self.assertFalse(result.allowed)
        self.assertIn("RESERVATION_EVIDENCE_INCOMPLETE", result.hard_blocks)

    def test_filled_position_retains_signed_gap_risk(self) -> None:
        broker = make_broker(positions=(position(code="600001"),))
        evidence = make_position_evidence(broker)[0]
        signed_gap_risk = evidence.entry_intent.pre_trade_result.gap_loss_yuan
        result = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(max_open_risk_fraction=D("0.50")),
            make_reservation_view(broker, positions=(evidence,)),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(
            result.projected_open_risk_yuan - result.per_trade_risk_yuan,
            signed_gap_risk,
        )

    def test_existing_position_cannot_be_bought_again_in_either_mode(self) -> None:
        broker = make_broker(positions=(position(code="600000"),))
        for mode in ("observe", "enforce"):
            with self.subTest(mode=mode):
                result = pre_trade_check(
                    make_candidate(), broker, make_quote(), make_rules(),
                    SYSTEM_STATE, make_policy(mode=mode),
                    make_reservation_view(broker),
                )
                self.assertFalse(result.allowed)
                self.assertIn("POSITION_ALREADY_HELD", result.hard_blocks)

    def test_projected_turnover_reduces_quantity_and_honors_exact_boundary(self) -> None:
        broker = make_broker()
        reduced = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(), make_reservation_view(
                broker, daily_turnover_fraction=D("1.98"),
            ),
        )
        exact_one_lot = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(), make_reservation_view(
                broker, daily_turnover_fraction=D("1.9899"),
            ),
        )
        below_one_lot = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(), make_reservation_view(
                broker, daily_turnover_fraction=D("1.99"),
            ),
        )

        self.assertTrue(reduced.allowed)
        self.assertEqual(reduced.approved_qty, 100)
        self.assertTrue(exact_one_lot.allowed)
        self.assertEqual(exact_one_lot.approved_qty, 100)
        self.assertFalse(below_one_lot.allowed)
        self.assertIn("MAX_DAILY_TURNOVER_EXCEEDED", below_one_lot.hard_blocks)

    def test_partial_classification_enforces_named_and_uncategorized_caps(self) -> None:
        broker = make_broker(positions=(position(
            code="600001", last_price=D("95"), market_value=D("9500"),
        ),))
        result = pre_trade_check(
            make_candidate(industry="technology", theme=""),
            broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(
                max_industry_fraction=D("0.10"),
                max_uncategorized_fraction=D("0.10"),
            ),
            make_reservation_view(
                broker,
                positions=make_position_evidence(
                    broker, industry="technology", theme="",
                    stop_price=D("94.5"),
                ),
            ),
        )

        self.assertIn("INDUSTRY_EXPOSURE_EXCEEDED", result.hard_blocks)
        self.assertIn("UNCATEGORIZED_EXPOSURE_EXCEEDED", result.hard_blocks)

    def test_terminal_and_partial_reservations_keep_exact_remaining_capacity(self) -> None:
        terminal = make_active_reservation(
            code="600002", status="filled", remaining_qty=100,
        )
        terminal_broker = make_broker()
        terminal_result = pre_trade_check(
            make_candidate(), terminal_broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(), make_reservation_view(
                terminal_broker, active_reservations=(terminal,),
            ),
        )
        self.assertTrue(terminal_result.allowed)
        self.assertGreater(terminal.position_value_yuan, D("0"))

        partial = make_active_reservation(
            code="600002", status="partially_filled", remaining_qty=50,
        )
        fee = partial.intent.pre_trade_result.execution_fee
        expected_cash = (
            (fee.notional_yuan + fee.total_yuan) * D("0.5")
        ).quantize(D("0.01"), rounding=ROUND_CEILING)
        pending = {
            **open_order(code="600002"),
            "client_order_id": partial.client_order_id,
            "status": "partially_filled",
            "filled_qty": 50,
        }
        partial_broker = make_broker(open_orders=(pending,))
        partial_result = pre_trade_check(
            make_candidate(), partial_broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(), make_reservation_view(
                partial_broker, active_reservations=(partial,),
            ),
        )
        self.assertEqual(partial.cash_yuan, expected_cash)
        self.assertTrue(partial_result.allowed)

    def test_capacity_evidence_identity_and_market_value_fail_closed(self) -> None:
        broker = make_broker()
        wrong_view = make_reservation_view(broker)
        mismatched_snapshot = replace(
            wrong_view, broker_snapshot_id="different-snapshot",
        )
        bad_value_broker = make_broker(positions=(position(
            code="600001", last_price=D("10"), market_value=D("999"),
        ),))
        target_mismatch_reservation = make_active_reservation(code="600002")
        target_mismatch_order = {
            **open_order(code="600002"),
            "client_order_id": target_mismatch_reservation.client_order_id,
            "target_qty": 200,
        }
        target_mismatch_broker = make_broker(
            open_orders=(target_mismatch_order,),
        )
        wrong_adapter_broker = make_broker(positions=(position(code="600001"),))
        wrong_adapter = AdoptedPositionCapacityEvidence(
            position_cycle_id="legacy-cycle-1",
            account_scope_id=wrong_adapter_broker.account_scope_id,
            adapter="qmt",
            code="600001",
            industry="technology",
            theme="artificial-intelligence",
            effective_stop_price=D("9.50"),
            gap_price=D("8.50"),
            initial_qty=100,
            adopted_at="2026-07-27T15:00:00+08:00",
            source_sha256="a" * 64,
        )
        extra_evidence = replace(
            wrong_adapter, adapter="joinquant",
        )
        cases = (
            (broker, mismatched_snapshot),
            (bad_value_broker, make_reservation_view(bad_value_broker)),
            (
                target_mismatch_broker,
                make_reservation_view(
                    target_mismatch_broker,
                    active_reservations=(target_mismatch_reservation,),
                ),
            ),
            (
                wrong_adapter_broker,
                make_reservation_view(
                    wrong_adapter_broker, positions=(wrong_adapter,),
                ),
            ),
            (broker, make_reservation_view(broker, positions=(extra_evidence,))),
        )
        for current_broker, view in cases:
            with self.subTest(snapshot=current_broker.snapshot_id):
                result = pre_trade_check(
                    make_candidate(), current_broker, make_quote(), make_rules(),
                    SYSTEM_STATE, make_policy(), view,
                )
                self.assertFalse(result.allowed)
                self.assertIn(
                    "RESERVATION_EVIDENCE_INCOMPLETE", result.hard_blocks,
                )

        with self.assertRaisesRegex(ValueError, "scope"):
            make_reservation_view(
                broker,
                positions=(replace(
                    extra_evidence, account_scope_id="other-scope",
                ),),
            )

    def test_open_order_status_and_remaining_quantity_must_match_reservation(self) -> None:
        status_reservation = make_active_reservation(code="600002")
        status_order = {
            **open_order(code="600002"),
            "client_order_id": status_reservation.client_order_id,
            "status": "pending_cancel",
        }
        remaining_reservation = make_active_reservation(
            code="600003", status="partially_filled", remaining_qty=100,
        )
        remaining_order = {
            **open_order(code="600003"),
            "client_order_id": remaining_reservation.client_order_id,
            "status": "partially_filled",
            "filled_qty": 50,
        }
        for reservation, order in (
            (status_reservation, status_order),
            (remaining_reservation, remaining_order),
        ):
            broker = make_broker(open_orders=(order,))
            result = pre_trade_check(
                make_candidate(), broker, make_quote(), make_rules(),
                SYSTEM_STATE, make_policy(), make_reservation_view(
                    broker, active_reservations=(reservation,),
                ),
            )
            self.assertFalse(result.allowed)
            self.assertIn("RESERVATION_EVIDENCE_INCOMPLETE", result.hard_blocks)

    def test_buy_portfolio_and_daily_capacity_matrix(self) -> None:
        candidate = make_candidate()
        base_broker = make_broker()
        five_positions = tuple(
            position(code=f"60000{index}") for index in range(1, 6)
        )
        slots_broker = make_broker(positions=five_positions)
        total_broker = make_broker(positions=(position(
            code="600001", last_price=Decimal("795"),
            market_value=Decimal("79500"),
        ),))
        industry_broker = make_broker(positions=(position(
            code="600001", last_price=Decimal("245"),
            market_value=Decimal("24500"),
        ),))
        theme_broker = make_broker(positions=(position(
            code="600001", last_price=Decimal("195"),
            market_value=Decimal("19500"),
        ),))
        risk_broker = make_broker(positions=(position(
            code="600001", last_price=Decimal("10"),
            market_value=Decimal("1000"),
        ),))
        cases = (
            (
                "slots", slots_broker,
                make_reservation_view(slots_broker),
                make_policy(), "MAX_POSITIONS_EXCEEDED",
            ),
            (
                "single", base_broker, make_reservation_view(base_broker),
                make_policy(max_single_position_fraction=Decimal("0.005")),
                "MAX_SINGLE_POSITION_EXCEEDED",
            ),
            (
                "total", total_broker,
                make_reservation_view(
                    total_broker,
                    positions=make_position_evidence(
                        total_broker,
                        industry="other",
                        theme="other",
                        stop_price=Decimal("790"),
                    ),
                ),
                make_policy(), "MAX_TOTAL_POSITION_EXCEEDED",
            ),
            (
                "industry", industry_broker,
                make_reservation_view(
                    industry_broker,
                    positions=make_position_evidence(
                        industry_broker,
                        industry="technology",
                        theme="other",
                        stop_price=Decimal("240"),
                    ),
                ),
                make_policy(), "INDUSTRY_EXPOSURE_EXCEEDED",
            ),
            (
                "theme", theme_broker,
                make_reservation_view(
                    theme_broker,
                    positions=make_position_evidence(
                        theme_broker,
                        industry="other",
                        theme="artificial-intelligence",
                        stop_price=Decimal("190"),
                    ),
                ),
                make_policy(), "THEME_EXPOSURE_EXCEEDED",
            ),
            (
                "open risk", risk_broker,
                make_reservation_view(
                    risk_broker,
                    positions=make_position_evidence(
                        risk_broker,
                        industry="other",
                        theme="other",
                        stop_price=Decimal("9.5"),
                    ),
                ),
                make_policy(max_open_risk_fraction=Decimal("0.0005")),
                "PORTFOLIO_OPEN_RISK_EXCEEDED",
            ),
            (
                "cash", make_broker(cash=Decimal("500"), available_cash=Decimal("500")),
                make_reservation_view(make_broker(
                    cash=Decimal("500"), available_cash=Decimal("500"),
                )),
                make_policy(), "CASH_CAPACITY_EXCEEDED",
            ),
            (
                "new positions", base_broker,
                replace(
                    make_reservation_view(base_broker),
                    daily_new_positions=10,
                ),
                make_policy(), "MAX_NEW_POSITIONS_EXCEEDED",
            ),
            (
                "orders", base_broker,
                replace(make_reservation_view(base_broker), daily_orders=50),
                make_policy(), "MAX_DAILY_ORDERS_EXCEEDED",
            ),
            (
                "turnover", base_broker,
                replace(
                    make_reservation_view(base_broker),
                    daily_turnover_fraction=Decimal("2"),
                ),
                make_policy(), "MAX_DAILY_TURNOVER_EXCEEDED",
            ),
            (
                "daily loss",
                make_broker(intraday_pnl=Decimal("-5000")),
                make_reservation_view(make_broker(
                    intraday_pnl=Decimal("-5000"),
                )),
                make_policy(), "DAILY_LOSS_LIMIT_EXCEEDED",
            ),
            (
                "drawdown",
                make_broker(account_drawdown_pct=Decimal("-15")),
                make_reservation_view(make_broker(
                    account_drawdown_pct=Decimal("-15"),
                )),
                make_policy(), "ACCOUNT_DRAWDOWN_LIMIT_EXCEEDED",
            ),
            (
                "loss streak", base_broker,
                replace(
                    make_reservation_view(base_broker),
                    consecutive_losses=3,
                ),
                make_policy(), "CONSECUTIVE_LOSS_LIMIT_EXCEEDED",
            ),
        )
        for label, broker, view, policy, reason in cases:
            for mode in ("observe", "enforce"):
                with self.subTest(label=label, mode=mode):
                    result = pre_trade_check(
                        candidate, broker, make_quote(), make_rules(),
                        SYSTEM_STATE, replace(policy, mode=mode), view,
                    )
                    self.assertFalse(result.allowed)
                    self.assertIn(reason, result.hard_blocks)

    def test_uncategorized_capacity_is_aggregated(self) -> None:
        broker = make_broker(positions=(position(
            code="600001", last_price=Decimal("95"),
            market_value=Decimal("9500"),
        ),))
        result = pre_trade_check(
            make_candidate(industry="", theme=""),
            broker,
            make_quote(),
            make_rules(),
            SYSTEM_STATE,
            make_policy(),
            make_reservation_view(
                broker,
                positions=make_position_evidence(
                    broker,
                    industry="",
                    theme="",
                    stop_price=Decimal("94.5"),
                ),
            ),
        )
        self.assertFalse(result.allowed)
        self.assertIn("UNCATEGORIZED_EXPOSURE_EXCEEDED", result.hard_blocks)

    def test_missing_position_capacity_evidence_fails_closed(self) -> None:
        broker = make_broker(positions=(position(
            code="600001", market_value=Decimal("1000"),
        ),))
        result = pre_trade_check(
            make_candidate(),
            broker,
            make_quote(),
            make_rules(),
            SYSTEM_STATE,
            make_policy(),
            make_reservation_view(
                broker,
                positions=(),
            ),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.approved_qty, 0)
        self.assertIn(
            "RESERVATION_EVIDENCE_INCOMPLETE", result.hard_blocks,
        )

    def test_zero_capacity_policy_is_valid_and_blocks_new_buy(self) -> None:
        result = pre_trade_check(
            make_candidate(),
            make_broker(),
            make_quote(),
            make_rules(),
            SYSTEM_STATE,
            make_policy(
                max_positions=0,
                max_single_position_fraction=Decimal("0"),
                max_total_position_fraction=Decimal("0"),
                max_industry_fraction=Decimal("0"),
                max_theme_fraction=Decimal("0"),
                max_uncategorized_fraction=Decimal("0"),
                max_open_risk_fraction=Decimal("0"),
                max_new_positions_per_day=0,
                max_orders_per_day=0,
                max_daily_turnover_fraction=Decimal("0"),
                max_daily_loss_fraction=Decimal("0"),
                max_account_drawdown_fraction=Decimal("0"),
                max_consecutive_losses=0,
            ),
            make_reservation_view(),
        )

        self.assertFalse(result.allowed)
        self.assertIn("MAX_POSITIONS_EXCEEDED", result.hard_blocks)
        self.assertIn("MAX_NEW_POSITIONS_EXCEEDED", result.hard_blocks)
        self.assertIn("MAX_DAILY_ORDERS_EXCEEDED", result.hard_blocks)


if __name__ == "__main__":
    unittest.main()
