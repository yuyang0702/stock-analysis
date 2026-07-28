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
    buy_minimum_commission_yuan=D("5"),
    sell_minimum_commission_yuan=D("5"),
    stamp_tax_rate=D("0.0005"),
    transfer_fee_rate=D("0.00001"),
    other_fee_rate=D("0"),
    buy_slippage_rate=D("0.001"),
    sell_slippage_rate=D("0.001"),
)
OPEN_ORDER = {
    "client_order_id": "order-1",
    "broker_order_id": "broker-order-1",
    "stock_code": "600000",
    "side": "buy",
    "target_qty": 100,
    "filled_qty": 0,
    "status": "submitted",
    "updated_at": "2026-07-28T10:00:00+08:00",
}
FILL = {
    "broker_fill_id": "broker-fill-1",
    "client_order_id": "order-0",
    "broker_order_id": "broker-order-0",
    "stock_code": "600001",
    "side": "sell",
    "qty": 100,
    "price": "9.90",
    "commission_yuan": "5",
    "stamp_tax_yuan": "0.50",
    "transfer_fee_yuan": "0",
    "other_fee_yuan": "0",
    "fee_data_status": "reported",
    "filled_at": "2026-07-28T09:59:00+08:00",
}


def make_candidate(*, target_price: str = "11") -> StrategyOrderCandidate:
    logical = logical_signal_id(
        "scope-uuid", "2026-07-28", "main", "s1", "600000", "buy", "breakout"
    )
    return StrategyOrderCandidate(
        candidate_id="candidate-1", logical_signal_id=logical,
        account_scope_id="scope-uuid", source_signal_id="signal-run-1",
        source_run_id="run-1", strategy_id="main", strategy_version="s1",
        parameter_version="p1", model_version="disabled",
        fee_schedule_version="sim-v1", code="600000", side="buy",
        setup_type="breakout", suggested_entry_price=D("10"), stop_price=D("9.5"),
        target_price=D(target_price), signal_time="2026-07-28T09:55:00+08:00",
        frozen_valid_until="2026-07-28T10:05:00+08:00",
    )


def intent_values(candidate: StrategyOrderCandidate) -> dict[str, object]:
    return {
        "client_order_id": "order-1", "pre_trade_result_id": "risk-1",
        "submission_attempt_id": candidate.candidate_id, "account_scope_id": "scope-uuid",
        "adapter": "joinquant", "logical_signal_id": candidate.logical_signal_id,
        "source_signal_id": candidate.source_signal_id, "strategy_id": "main",
        "strategy_version": "s1", "parameter_version": "p1", "model_version": "disabled",
        "fee_schedule_version": "sim-v1", "code": "600000", "side": "buy",
        "order_qty": 100, "expected_current_qty": 0, "target_position_qty": 100,
        "limit_price": D("10"), "price_cap": D("10.10"), "stop_price": D("9.5"),
        "signal_time": "2026-07-28T09:55:00+08:00",
        "expires_at": "2026-07-28T10:01:00+08:00",
        "broker_snapshot_id": "broker-1", "broker_snapshot_sha256": "a" * 64,
        "quote_snapshot_id": "quote-1", "quote_snapshot_sha256": "b" * 64,
        "instrument_rules_sha256": "c" * 64,
    }


class ExecutionContractsTest(unittest.TestCase):
    def test_buy_and_sell_minimum_commissions_are_independent_and_hashed(self) -> None:
        fees = FeeSchedule(
            version="asymmetric-v1",
            effective_from="2026-01-01",
            buy_commission_rate=D("0.0003"),
            sell_commission_rate=D("0.0003"),
            buy_minimum_commission_yuan=D("3"),
            sell_minimum_commission_yuan=D("7"),
            stamp_tax_rate=D("0.0005"),
            transfer_fee_rate=D("0"),
            other_fee_rate=D("0"),
            buy_slippage_rate=D("0"),
            sell_slippage_rate=D("0"),
        )

        self.assertEqual(fees.estimate("buy", D("10"), 100).commission_yuan, D("3.00"))
        self.assertEqual(fees.estimate("sell", D("10"), 100).commission_yuan, D("7.00"))
        self.assertEqual(len(fees.contract_sha256), 64)
        self.assertNotEqual(
            fees.contract_sha256,
            FeeSchedule.from_dict(
                {**fees.to_dict(), "sell_minimum_commission_yuan": "8"}
            ).contract_sha256,
        )

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
            candidate=make_candidate(),
            allowed=True,
            hard_blocks=(),
            warnings=(),
            approved_qty=100,
            target_position_qty=100,
            fee_schedule_version="sim-v1",
            strategy_version="s1",
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
            open_orders=[OPEN_ORDER],
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

    def test_snapshot_allows_negative_drawdown_and_canonicalizes_equivalent_facts(self) -> None:
        first_quote = QuoteSnapshot.from_values(
            code="600000", quote_time="2026-07-28T10:00:00+08:00",
            last_price=10, bid_price=None, ask_price=None,
            limit_up_price=None, limit_down_price=None,
        )
        second_quote = QuoteSnapshot.from_values(
            code="600000", quote_time="2026-07-28T02:00:00Z",
            last_price="10.0", bid_price=None, ask_price=None,
            limit_up_price=None, limit_down_price=None,
        )
        positions = [
            BrokerPosition.from_values(code="600001", total_qty=100, sellable_qty=100),
            BrokerPosition.from_values(code="600000", total_qty=100, sellable_qty=0,
                                       today_buy_qty=100, last_price=10),
        ]
        common = dict(
            account_scope_id="scope-uuid", trade_date="2026-07-28",
            total_equity=50_000, cash=20_000, available_cash=20_000, frozen_cash=0,
            adapter_version="sim-1", node_version="node-1", session_id="session-1",
            capabilities_version="cap-1", account_drawdown_pct="-2.5",
        )
        first = BrokerSnapshot.from_values(
            broker_time="2026-07-28T10:00:00+08:00",
            positions=positions, open_orders=[OPEN_ORDER], fills=[FILL], **common,
        )
        second = BrokerSnapshot.from_values(
            broker_time="2026-07-28T02:00:00Z",
            positions=list(reversed(positions)), open_orders=[dict(OPEN_ORDER)],
            fills=[dict(FILL)], **common,
        )

        self.assertEqual(first_quote.snapshot_id, second_quote.snapshot_id)
        self.assertEqual(first_quote.quote_sha256, second_quote.quote_sha256)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.snapshot_sha256, second.snapshot_sha256)
        self.assertEqual(first.account_drawdown_pct, D("-2.5"))

    def test_broker_snapshot_rejects_incomplete_or_arbitrary_order_and_fill_records(self) -> None:
        common = dict(
            account_scope_id="scope-uuid", trade_date="2026-07-28",
            broker_time="2026-07-28T10:00:00+08:00", total_equity=50_000,
            cash=20_000, available_cash=20_000, frozen_cash=0, positions=[],
            adapter_version="sim-1", node_version="node-1", session_id="session-1",
            capabilities_version="cap-1",
        )
        for field, records in (
            ("open_orders", ["order-1"]),
            ("open_orders", [{"client_order_id": "order-1"}]),
            ("fills", [object()]),
            ("fills", [{"broker_fill_id": "fill-1"}]),
        ):
            with self.subTest(field=field, records=records):
                values = {"open_orders": [], "fills": [], **common, field: records}
                with self.assertRaises(ValueError):
                    BrokerSnapshot.from_values(**values)

    def test_broker_snapshot_normalizes_mapping_and_future_typed_to_dict_records(self) -> None:
        class FutureOrder:
            def to_dict(self):
                return dict(OPEN_ORDER)

        source_fill = dict(FILL)
        snapshot = BrokerSnapshot.from_values(
            account_scope_id="scope-uuid", trade_date="2026-07-28",
            broker_time="2026-07-28T10:00:00+08:00", total_equity=50_000,
            cash=20_000, available_cash=20_000, frozen_cash=0, positions=[],
            open_orders=[FutureOrder()], fills=[source_fill], adapter_version="sim-1",
            node_version="node-1", session_id="session-1", capabilities_version="cap-1",
        )
        source_fill["qty"] = 999

        self.assertEqual(snapshot.open_orders[0]["client_order_id"], "order-1")
        self.assertEqual(snapshot.fills[0]["qty"], 100)
        with self.assertRaises(TypeError):
            snapshot.fills[0]["qty"] = 200

    def test_submit_unknown_order_allows_broker_id_to_be_absent(self) -> None:
        unknown = {
            **OPEN_ORDER, "status": "submit_unknown", "broker_order_id": None
        }
        common = dict(
            account_scope_id="scope-uuid", trade_date="2026-07-28",
            broker_time="2026-07-28T10:00:00+08:00", total_equity=50_000,
            cash=20_000, available_cash=20_000, frozen_cash=0, positions=[], fills=[],
            adapter_version="sim-1", node_version="node-1", session_id="session-1",
            capabilities_version="cap-1",
        )

        snapshot = BrokerSnapshot.from_values(open_orders=[unknown], **common)

        self.assertIsNone(snapshot.open_orders[0]["broker_order_id"])
        with self.assertRaises(ValueError):
            BrokerSnapshot.from_values(
                open_orders=[{**unknown, "status": "submitted"}], **common
            )

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
            candidate=candidate,
            allowed=True,
            hard_blocks=(),
            warnings=(),
            approved_qty=100,
            target_position_qty=100,
            fee_schedule_version="sim-v1",
            strategy_version="s1",
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

    def test_pre_trade_result_binds_the_full_normalized_candidate_payload(self) -> None:
        first_candidate = make_candidate(target_price="11")
        second_candidate = make_candidate(target_price="12")
        common = dict(
            pre_trade_result_id="risk-1", candidate_id="candidate-1", allowed=True,
            hard_blocks=(), warnings=(), approved_qty=100, target_position_qty=100,
            fee_schedule_version="sim-v1", strategy_version="s1",
            checked_at="2026-07-28T09:56:00+08:00",
            valid_until="2026-07-28T10:01:00+08:00",
        )

        first = PreTradeResult(candidate=first_candidate, **common)
        second = PreTradeResult(candidate=second_candidate, **common)

        self.assertNotEqual(first.result_sha256, second.result_sha256)
        self.assertEqual(
            PreTradeResult.from_dict(first.to_dict()).candidate.payload_sha256,
            first_candidate.payload_sha256,
        )
        with self.assertRaisesRegex(ValueError, "candidate_id"):
            PreTradeResult(candidate=first_candidate, **{**common, "candidate_id": "other"})

    def test_pre_trade_result_rejects_candidate_version_conflicts(self) -> None:
        candidate = make_candidate()
        common = dict(
            pre_trade_result_id="risk-1", candidate_id=candidate.candidate_id,
            candidate=candidate, allowed=True, hard_blocks=(), warnings=(), approved_qty=100,
            target_position_qty=100, fee_schedule_version="sim-v1", strategy_version="s1",
            checked_at="2026-07-28T09:56:00+08:00",
            valid_until="2026-07-28T10:01:00+08:00",
        )
        for field in ("fee_schedule_version", "strategy_version"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    PreTradeResult(**{**common, field: "different"})

    def test_execution_intent_rejects_malformed_digest_references(self) -> None:
        candidate = make_candidate()
        for field in (
            "broker_snapshot_sha256", "quote_snapshot_sha256", "instrument_rules_sha256"
        ):
            with self.subTest(field=field):
                values = {**intent_values(candidate), field: "x"}
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    ExecutionIntent(**values)


if __name__ == "__main__":
    unittest.main()
