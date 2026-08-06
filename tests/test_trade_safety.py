import unittest
import math
from dataclasses import replace

from pre_trade_check import ReservationView, pre_trade_check
from tests.test_execution_contracts import D, make_candidate
from tests.test_pre_trade_check import (
    SYSTEM_STATE,
    make_broker,
    make_active_reservation,
    make_policy,
    make_position_evidence,
    make_quote,
    make_rules,
    open_order,
    position,
)
from trade_safety import MarketRegimeState, attainable_sell_target, tradability_reject_reason


class TradeSafetyTest(unittest.TestCase):
    def sell_view(self, owner="cycle-1"):
        return ReservationView(
            account_scope_id="scope-uuid",
            position_exit_owner_ids={"600000": owner},
            position_exit_target_qtys={"600000": 0},
        )

    def test_valid_stop_sell_ignores_buy_only_gates(self):
        broker = make_broker(
            positions=(position(
                code="600000", total_qty=500, sellable_qty=500,
            ),),
        )
        overloaded = replace(
            self.sell_view(),
            daily_new_positions=999,
            daily_orders=999,
            daily_turnover_fraction=D("9.99"),
            consecutive_losses=999,
        )
        result = pre_trade_check(
            make_candidate(side="sell"),
            broker,
            make_quote(),
            make_rules(),
            {**SYSTEM_STATE, "buy_enabled": "0", "market_regime": "RISK_OFF"},
            make_policy(max_cost_edge_ratio=D("0")),
            overloaded,
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.approved_qty, 500)
        self.assertEqual(result.target_position_qty, 0)

        killed = pre_trade_check(
            make_candidate(side="sell"), broker, make_quote(), make_rules(),
            {**SYSTEM_STATE, "kill_switch": "1"},
            make_policy(), self.sell_view(),
        )
        self.assertFalse(killed.allowed)
        self.assertIn("KILL_SWITCH_ACTIVE", killed.hard_blocks)

    def test_sell_t1_partial_and_zero_sellable(self):
        candidate = make_candidate(side="sell")
        partial = pre_trade_check(
            candidate,
            make_broker(positions=(position(
                code="600000", total_qty=500, sellable_qty=300,
                today_buy_qty=200,
            ),)),
            make_quote(), make_rules(), SYSTEM_STATE, make_policy(),
            self.sell_view(),
        )
        blocked = pre_trade_check(
            candidate,
            make_broker(positions=(position(
                code="600000", total_qty=500, sellable_qty=0,
                today_buy_qty=500,
            ),)),
            make_quote(), make_rules(), SYSTEM_STATE, make_policy(),
            self.sell_view(),
        )

        self.assertTrue(partial.allowed)
        self.assertEqual(partial.approved_qty, 300)
        self.assertEqual(partial.target_position_qty, 200)
        self.assertIn("SELL_PARTIAL_QUANTITY", partial.warnings)
        self.assertFalse(blocked.allowed)
        self.assertIn("T_PLUS_ONE_UNSELLABLE", blocked.hard_blocks)

    def test_sell_safety_and_evidence_matrix(self):
        candidate = make_candidate(side="sell")
        broker = make_broker(positions=(position(
            code="600000", total_qty=500, sellable_qty=500,
        ),))
        cases = (
            (
                "no position", make_broker(), make_quote(), make_rules(),
                self.sell_view(), candidate, "POSITION_NOT_FOUND",
            ),
            (
                "owner mismatch", broker, make_quote(), make_rules(),
                self.sell_view("other-cycle"), candidate, "EXIT_OWNER_MISMATCH",
            ),
            (
                "duplicate",
                make_broker(
                    positions=(position(
                        code="600000", total_qty=500, sellable_qty=500,
                    ),),
                    open_orders=(open_order(side="sell"),),
                ),
                make_quote(), make_rules(), self.sell_view(), candidate,
                "DUPLICATE_ORDER",
            ),
            (
                "suspended", broker, make_quote(suspended=True), make_rules(),
                self.sell_view(), candidate, "INSTRUMENT_SUSPENDED",
            ),
            (
                "limit down", broker,
                make_quote(last_price=D("9"), bid_price=D("9"), ask_price=D("9")),
                make_rules(), self.sell_view(), candidate, "SELL_LIMIT_DOWN",
            ),
            (
                "floor", broker,
                make_quote(last_price=D("9.40"), bid_price=D("9.39"), ask_price=D("9.40")),
                make_rules(), self.sell_view(),
                make_candidate(
                    side="sell", sell_limit_price=D("9.60"),
                    sell_price_floor=D("9.50"),
                ),
                "SELL_PRICE_FLOOR_BREACHED",
            ),
        )
        for label, current_broker, quote, rules, view, current_candidate, reason in cases:
            with self.subTest(label=label):
                result = pre_trade_check(
                    current_candidate, current_broker, quote, rules,
                    SYSTEM_STATE, make_policy(), view,
                )
                self.assertFalse(result.allowed)
                self.assertIn(reason, result.hard_blocks)

        missing_optional_evidence = pre_trade_check(
            candidate, broker, make_quote(), None, SYSTEM_STATE,
            make_policy(fee_schedule=None), self.sell_view(),
        )
        self.assertTrue(missing_optional_evidence.allowed)
        self.assertIn(
            "SELL_FEE_EVIDENCE_UNAVAILABLE",
            missing_optional_evidence.warnings,
        )
        self.assertIn(
            "SELL_RULE_EVIDENCE_UNAVAILABLE",
            missing_optional_evidence.warnings,
        )

        stale_rules = pre_trade_check(
            candidate, broker, make_quote(),
            make_rules(valid_until="2026-07-28T09:59:00+08:00"),
            SYSTEM_STATE, make_policy(), self.sell_view(),
        )
        self.assertTrue(stale_rules.allowed)
        self.assertIn(
            "SELL_RULE_EVIDENCE_UNAVAILABLE", stale_rules.warnings,
        )

    def test_sell_enabled_is_independent_from_buy_enabled(self):
        broker = make_broker(positions=(position(
            code="600000", total_qty=500, sellable_qty=500,
        ),))
        result = pre_trade_check(
            make_candidate(side="sell"), broker, make_quote(), make_rules(),
            {**SYSTEM_STATE, "sell_enabled": "0"},
            make_policy(), self.sell_view(),
        )
        self.assertFalse(result.allowed)
        self.assertIn("SELL_DISABLED", result.hard_blocks)

    def test_full_sell_removes_the_positions_open_risk_projection(self):
        broker = make_broker(positions=(position(
            code="600000", total_qty=100, sellable_qty=100,
        ),))
        view = ReservationView(
            account_scope_id=broker.account_scope_id,
            broker_snapshot_id=broker.snapshot_id,
            broker_snapshot_sha256=broker.snapshot_sha256,
            positions=make_position_evidence(broker),
            position_exit_owner_ids={"600000": "cycle-1"},
            position_exit_target_qtys={"600000": 0},
        )
        result = pre_trade_check(
            make_candidate(side="sell"), broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(), view,
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.target_position_qty, 0)
        self.assertEqual(result.projected_open_risk_yuan, D("0"))

    def test_qmt_observe_mode_cannot_submit_even_a_sell(self):
        broker = make_broker(
            adapter="qmt", adapter_version="qmt-v1",
            positions=(position(
                code="600000", total_qty=100, sellable_qty=100,
            ),),
        )
        result = pre_trade_check(
            make_candidate(side="sell"), broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(adapter="qmt", mode="observe"),
            self.sell_view(),
        )

        self.assertFalse(result.allowed)
        self.assertIn("QMT_ENFORCE_REQUIRED", result.hard_blocks)

    def test_terminal_buy_reservation_does_not_block_protective_sell(self):
        broker = make_broker(positions=(position(
            code="600000", total_qty=100, sellable_qty=100,
        ),))
        terminal_buy = make_active_reservation(
            code="600000", status="filled", remaining_qty=100,
        )
        view = replace(
            self.sell_view(), active_reservations=(terminal_buy,),
        )
        result = pre_trade_check(
            make_candidate(side="sell"), broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(), view,
        )

        self.assertTrue(result.allowed)

    def test_nonterminal_buy_order_blocks_sell_until_cancel_is_confirmed(self):
        broker_position = position(
            code="600000", total_qty=100, sellable_qty=100,
        )
        for status in ("ready", "submitted", "pending_cancel"):
            with self.subTest(status=status):
                reservation = make_active_reservation(
                    code="600000", status=status, remaining_qty=100,
                )
                orders = ()
                if status != "ready":
                    orders = ({
                        **open_order(side="buy", code="600000"),
                        "client_order_id": reservation.client_order_id,
                        "status": status,
                    },)
                broker = make_broker(
                    positions=(broker_position,), open_orders=orders,
                )
                result = pre_trade_check(
                    make_candidate(side="sell"), broker, make_quote(),
                    make_rules(), SYSTEM_STATE, make_policy(),
                    replace(
                        self.sell_view(), active_reservations=(reservation,),
                    ),
                )
                self.assertFalse(result.allowed)
                self.assertIn("DUPLICATE_ORDER", result.hard_blocks)

    def test_sell_target_is_limited_by_closeable_quantity(self):
        self.assertEqual(attainable_sell_target(1000, 0, 600), (400, "partial_sellable"))
        self.assertEqual(attainable_sell_target(1000, 500, 0), (None, "t_plus_one"))

    def test_market_regime_requires_confirmation_and_slower_recovery(self):
        state = MarketRegimeState("NORMAL", "", 0)
        state = state.advance("RISK_OFF")
        self.assertEqual(state.current, "NORMAL")
        state = state.advance("RISK_OFF")
        self.assertEqual(state.current, "RISK_OFF")
        state = state.advance("NORMAL").advance("NORMAL")
        self.assertEqual(state.current, "RISK_OFF")
        self.assertEqual(state.advance("NORMAL").current, "NORMAL")

    def test_tradability_rejects_unsafe_or_chased_buy(self):
        self.assertEqual(tradability_reject_reason({"is_st": True}), "buy_st")
        self.assertEqual(tradability_reject_reason({"paused": True}), "buy_suspended")
        self.assertEqual(tradability_reject_reason({"entry_price": 10, "price": 10.3, "atr14": 0.2}), "buy_chasing")
        self.assertEqual(tradability_reject_reason({"entry_price": 10, "price": 10.1, "atr14": 0.2, "amount": 1e8}), "")
        self.assertEqual(tradability_reject_reason({"paused": math.nan, "is_st": math.nan, "entry_price": 10, "price": 10}), "")

    def test_tradability_rejects_new_listing_and_stale_quote(self):
        self.assertEqual(tradability_reject_reason({"listing_days": 4}), "buy_special_listing_stage")
        self.assertEqual(tradability_reject_reason({"quote_age_sec": 121}), "buy_quote_stale")


if __name__ == "__main__":
    unittest.main()
