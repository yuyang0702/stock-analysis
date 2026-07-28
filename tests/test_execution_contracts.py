import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from execution_contracts import (
    BrokerPosition,
    BrokerSnapshot,
    ExecutionIntent,
    FeeSchedule,
    InstrumentRules,
    PreTradeResult,
    QuoteSnapshot,
    StrategyOrderCandidate,
    canonical_json,
    canonical_sha256,
    client_order_id,
    logical_signal_id,
)


D = Decimal
FEES = FeeSchedule(
    version="sim-v1",
    effective_from="2026-01-01",
    buy_commission_rate=D("0.0003"),
    sell_commission_rate=D("0.0003"),
    minimum_commission_yuan=D("5"),
    stamp_tax_rate=D("0.0005"),
    transfer_fee_rate=D("0.00001"),
    other_fee_rate=D("0"),
    buy_slippage_rate=D("0.001"),
    sell_slippage_rate=D("0.001"),
)


class ExecutionContractsTest(unittest.TestCase):
    def test_round_trip_applies_each_minimum_commission_and_sell_tax(self) -> None:
        result = FEES.estimate_round_trip(D("10"), D("11"), 100)

        self.assertEqual(result.buy.commission_yuan, D("5.00"))
        self.assertEqual(result.sell.commission_yuan, D("5.00"))
        self.assertEqual(result.sell.stamp_tax_yuan, D("0.55"))
        self.assertEqual(result.buy.transfer_fee_yuan, D("0.01"))
        self.assertEqual(result.buy.slippage_yuan, D("1.00"))
        self.assertEqual(result.total_yuan, D("12.67"))
        self.assertEqual(len(result.content_sha256), 64)

    def test_fee_schedule_rejects_missing_invalid_or_non_finite_inputs(self) -> None:
        values = dict(FEES.to_dict())
        for field, value in (
            ("version", ""),
            ("effective_from", "2026/01/01"),
            ("stamp_tax_rate", "-0.1"),
            ("other_fee_rate", "NaN"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    FeeSchedule.from_dict({**values, field: value})

        with self.assertRaises(ValueError):
            FEES.estimate("buy", D("Infinity"), 100)
        with self.assertRaises(ValueError):
            FEES.estimate("buy", D("10"), -100)

    def test_fee_breakdown_and_pre_trade_cost_round_trip_keep_decimal_semantics(self) -> None:
        costs = FEES.estimate_round_trip(D("10"), D("9.5"), 100)
        result = PreTradeResult(
            pre_trade_result_id="risk-with-costs",
            candidate_id="candidate-1",
            allowed=True,
            hard_blocks=(),
            warnings=(),
            approved_qty=100,
            target_position_qty=100,
            fee_schedule_version="sim-v1",
            checked_at="2026-07-28T09:56:00+08:00",
            valid_until="2026-07-28T10:01:00+08:00",
            round_trip_cost=costs,
        )

        restored = PreTradeResult.from_dict(result.to_dict())

        self.assertEqual(restored.round_trip_cost.total_yuan, costs.total_yuan)
        tampered = costs.buy.to_dict()
        tampered["commission_yuan"] = "4.99"
        with self.assertRaisesRegex(ValueError, "content hash conflict"):
            type(costs.buy).from_dict(tampered)

    def test_instrument_rules_validate_board_lots_price_ticks_and_odd_lot_sells(self) -> None:
        rules = InstrumentRules.a_share(
            "600000",
            buy_min_qty=100,
            buy_qty_step=100,
            price_tick=D("0.01"),
            as_of="2026-07-28T09:25:00+08:00",
            valid_until="2026-07-28T15:00:00+08:00",
        )

        self.assertEqual(rules.validate_order("buy", 150, D("10")), ("BUY_QTY_STEP_INVALID",))
        self.assertEqual(rules.validate_order("buy", 100, D("10.001")), ("PRICE_TICK_INVALID",))
        self.assertEqual(rules.validate_order("sell", 0, D("10")), ("SELL_QTY_ZERO",))
        self.assertEqual(rules.validate_order("sell", 37, D("10")), ())
        self.assertTrue(rules.is_fresh("2026-07-28T10:00:00+08:00"))
        self.assertFalse(rules.is_fresh("2026-07-28T15:00:01+08:00"))
        self.assertEqual(rules.rules_sha256, InstrumentRules.from_dict(rules.to_dict()).rules_sha256)

    def test_canonical_json_is_stable_and_rejects_non_finite_numbers(self) -> None:
        left = {"b": [D("1.00"), 2], "a": "text"}
        right = {"a": "text", "b": (D("1"), 2)}

        self.assertEqual(canonical_json(left), '{"a":"text","b":["1",2]}')
        self.assertEqual(canonical_sha256(left), canonical_sha256(right))
        self.assertNotEqual(canonical_sha256(left), canonical_sha256({"a": "text", "b": ["2", 2]}))
        with self.assertRaises(ValueError):
            canonical_json({"bad": float("nan")})

    def test_logical_and_client_order_identities_are_stable(self) -> None:
        logical = logical_signal_id(
            "scope-uuid", "2026-07-28", "main", "s1", "000001", "buy", "breakout"
        )
        exact_order = {"code": "000001", "side": "buy", "target_qty": 100, "limit_price": D("10")}
        first = client_order_id("scope", "joinquant", logical, "risk-1", exact_order, "candidate-1")

        self.assertEqual(
            logical,
            logical_signal_id(
                "scope-uuid", "2026-07-28", "main", "s1", "000001", "buy", "breakout"
            ),
        )
        self.assertEqual(len(logical), 20)
        self.assertEqual(
            first,
            client_order_id("scope", "joinquant", logical, "risk-1", exact_order, "candidate-1"),
        )
        self.assertNotEqual(
            first,
            client_order_id(
                "scope", "joinquant", logical, "risk-2", exact_order, "manual-reissue-event-1"
            ),
        )

    def test_quote_and_broker_snapshot_round_trip_is_stable_and_deeply_immutable(self) -> None:
        quote = QuoteSnapshot.from_values(
            code="600000",
            quote_time="2026-07-28T10:00:00+08:00",
            last_price="10.20",
            bid_price="10.19",
            ask_price="10.20",
            limit_up_price="11.00",
            limit_down_price="9.00",
        )
        position = BrokerPosition.from_values(
            code="600000", total_qty=100, sellable_qty=0, today_buy_qty=100, last_price="10.20"
        )
        snapshot = BrokerSnapshot.from_values(
            account_scope_id="scope-uuid",
            trade_date="2026-07-28",
            broker_time="2026-07-28T10:00:00+08:00",
            total_equity=50_000,
            cash=20_000,
            available_cash=19_500,
            frozen_cash=500,
            positions=[position],
            open_orders=[{"client_order_id": "order-1", "target_qty": 100}],
            fills=[],
            adapter_version="sim-1",
            node_version="node-1",
            session_id="session-1",
            capabilities_version="cap-1",
        )

        self.assertEqual(quote.quote_sha256, QuoteSnapshot.from_dict(quote.to_dict()).quote_sha256)
        self.assertEqual(snapshot.snapshot_sha256, BrokerSnapshot.from_dict(snapshot.to_dict()).snapshot_sha256)
        self.assertEqual(snapshot.positions, (position,))
        with self.assertRaises(TypeError):
            snapshot.open_orders[0]["target_qty"] = 200
        with self.assertRaises(FrozenInstanceError):
            snapshot.cash = D("1")

    def test_candidate_result_and_intent_hashes_expose_content_conflicts(self) -> None:
        logical = logical_signal_id(
            "scope-uuid", "2026-07-28", "main", "s1", "600000", "buy", "breakout"
        )
        candidate = StrategyOrderCandidate(
            candidate_id="candidate-1",
            logical_signal_id=logical,
            account_scope_id="scope-uuid",
            source_signal_id="signal-run-1",
            source_run_id="run-1",
            strategy_id="main",
            strategy_version="s1",
            parameter_version="p1",
            model_version="disabled",
            fee_schedule_version="sim-v1",
            code="600000",
            side="buy",
            setup_type="breakout",
            suggested_entry_price=D("10"),
            stop_price=D("9.5"),
            target_price=D("11"),
            signal_time="2026-07-28T09:55:00+08:00",
            frozen_valid_until="2026-07-28T10:05:00+08:00",
        )
        result = PreTradeResult(
            pre_trade_result_id="risk-1",
            candidate_id=candidate.candidate_id,
            allowed=True,
            hard_blocks=(),
            warnings=(),
            approved_qty=100,
            target_position_qty=100,
            fee_schedule_version="sim-v1",
            checked_at="2026-07-28T09:56:00+08:00",
            valid_until="2026-07-28T10:01:00+08:00",
        )
        common = dict(
            client_order_id="order-1",
            pre_trade_result_id=result.pre_trade_result_id,
            submission_attempt_id=candidate.candidate_id,
            account_scope_id="scope-uuid",
            adapter="joinquant",
            logical_signal_id=logical,
            source_signal_id=candidate.source_signal_id,
            strategy_id="main",
            strategy_version="s1",
            parameter_version="p1",
            model_version="disabled",
            fee_schedule_version="sim-v1",
            code="600000",
            side="buy",
            expected_current_qty=0,
            target_position_qty=100,
            limit_price=D("10"),
            price_cap=D("10.10"),
            stop_price=D("9.5"),
            signal_time="2026-07-28T09:55:00+08:00",
            expires_at="2026-07-28T10:01:00+08:00",
            broker_snapshot_id="broker-1",
            broker_snapshot_sha256="a" * 64,
            quote_snapshot_id="quote-1",
            quote_snapshot_sha256="b" * 64,
            instrument_rules_sha256="c" * 64,
        )
        first = ExecutionIntent(order_qty=100, **common)
        conflicting = ExecutionIntent(order_qty=200, **common)

        self.assertEqual(candidate.payload_sha256, StrategyOrderCandidate.from_dict(candidate.to_dict()).payload_sha256)
        self.assertEqual(result.result_sha256, PreTradeResult.from_dict(result.to_dict()).result_sha256)
        self.assertNotEqual(first.intent_sha256, conflicting.intent_sha256)
        with self.assertRaises(FrozenInstanceError):
            first.order_qty = 200


if __name__ == "__main__":
    unittest.main()
