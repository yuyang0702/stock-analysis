import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import execution_contracts as contracts
from execution_contracts import (
    BrokerPosition,
    BrokerSnapshot,
    ExecutionIntent,
    FeeSchedule,
    InstrumentRules,
    PreTradeResult,
    QuoteSnapshot,
    RoundTripCost,
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


def make_candidate(
    *,
    target_price: str = "11",
    side: str = "buy",
    stop_price: str = "9.5",
    signal_time: str = "2026-07-28T09:55:00+08:00",
    frozen_valid_until: str = "2026-07-28T10:05:00+08:00",
    industry: object = "technology",
    theme: object = "artificial-intelligence",
    buy_gap_price: Decimal | None = None,
    buy_price_cap: Decimal | None = None,
    requested_target_position_qty: object = None,
    exit_owner_id: object = None,
    exit_action: object = None,
    exit_priority: object = None,
    sell_limit_price: Decimal | None = None,
    sell_price_floor: Decimal | None = None,
) -> StrategyOrderCandidate:
    logical = logical_signal_id(
        "scope-uuid", "2026-07-28", "main", "s1", "600000", side, "breakout"
    )
    return StrategyOrderCandidate(
        candidate_id="candidate-1", logical_signal_id=logical,
        account_scope_id="scope-uuid", source_signal_id="signal-run-1",
        source_run_id="run-1", strategy_id="main", strategy_version="s1",
        parameter_version="p1", model_version="disabled",
        fee_schedule_version="sim-v1", code="600000", side=side,
        setup_type="breakout", suggested_entry_price=D("10"), stop_price=D(stop_price),
        target_price=D(target_price), signal_time=signal_time,
        frozen_valid_until=frozen_valid_until,
        industry=industry, theme=theme,
        uncategorized=not (str(industry or "").strip() and str(theme or "").strip()),
        buy_gap_price=D("9") if side == "buy" and buy_gap_price is None else buy_gap_price,
        buy_price_cap=D("10.10") if side == "buy" and buy_price_cap is None else buy_price_cap,
        requested_target_position_qty=(
            0 if side == "sell" and requested_target_position_qty is None
            else requested_target_position_qty
        ),
        exit_owner_id="cycle-1" if side == "sell" and exit_owner_id is None else exit_owner_id,
        exit_action="hard_stop" if side == "sell" and exit_action is None else exit_action,
        exit_priority=100 if side == "sell" and exit_priority is None else exit_priority,
        sell_limit_price=(
            D("9.40") if side == "sell" and sell_limit_price is None else sell_limit_price
        ),
        sell_price_floor=sell_price_floor,
    )


def pre_trade_values(
    candidate: StrategyOrderCandidate,
    **changes: object,
) -> dict[str, object]:
    is_buy = candidate.side == "buy"
    entry_price = D("10") if is_buy else candidate.sell_limit_price
    approved_qty = 100
    execution_fee = FEES.estimate(candidate.side, entry_price, approved_qty)
    planned_cost = (
        FEES.estimate_round_trip(entry_price, candidate.stop_price, approved_qty)
        if is_buy else None
    )
    target_cost = (
        FEES.estimate_round_trip(entry_price, candidate.target_price, approved_qty)
        if is_buy else None
    )
    gap_price = candidate.buy_gap_price if is_buy else None
    gap_cost = (
        FEES.estimate_round_trip(entry_price, gap_price, approved_qty)
        if is_buy else None
    )
    planned_loss = (
        (entry_price - candidate.stop_price) * approved_qty + planned_cost.total_yuan
        if is_buy else None
    )
    gap_loss = (
        (entry_price - gap_price) * approved_qty + gap_cost.total_yuan
        if is_buy else None
    )
    values: dict[str, object] = {
        "pre_trade_result_id": "risk-1", "candidate_id": candidate.candidate_id,
        "candidate": candidate, "allowed": True, "hard_blocks": (), "warnings": (),
        "approved_qty": approved_qty, "target_position_qty": approved_qty if is_buy else 0,
        "fee_schedule_version": FEES.version,
        "fee_schedule_sha256": FEES.contract_sha256,
        "fee_evidence_status": "available",
        "rule_evidence_status": "available",
        "strategy_version": "s1",
        "checked_at": "2026-07-28T09:56:00+08:00",
        "valid_until": "2026-07-28T10:01:00+08:00",
        "projected_available_cash_yuan": D("10993.99") if is_buy else D("12932.75"),
        "projected_single_position_value_yuan": D("1000") if is_buy else D("0"),
        "projected_total_position_value_yuan": D("31000") if is_buy else D("30000"),
        "projected_industry_value_yuan": D("9000") if is_buy else D("8000"),
        "projected_theme_value_yuan": D("7000") if is_buy else D("6000"),
        "projected_uncategorized_value_yuan": D("0"),
        "projected_open_risk_yuan": (
            D("500") + max(planned_loss, gap_loss) if is_buy else D("500")
        ),
        "actual_trade_risk_fraction": (
            max(planned_loss, gap_loss) / D("10000") if is_buy else D("0")
        ),
        "approved_limit_price": entry_price,
        "approved_price_cap": candidate.buy_price_cap if is_buy else candidate.sell_price_floor,
        "submission_attempt_id": candidate.candidate_id,
        "execution_fee": execution_fee,
        "round_trip_cost": planned_cost,
        "target_round_trip_cost": target_cost,
        "planned_stop_loss_yuan": planned_loss,
        "gap_price": gap_price,
        "gap_round_trip_cost": gap_cost,
        "gap_loss_yuan": gap_loss,
        "fee_erosion_ratio": (
            target_cost.total_yuan / execution_fee.notional_yuan if is_buy else None
        ),
        "cost_to_expected_edge_ratio": (
            target_cost.total_yuan
            / ((candidate.target_price - entry_price) * approved_qty)
            if is_buy else None
        ),
        "per_trade_risk_yuan": max(planned_loss, gap_loss) if is_buy else D("0"),
        "broker_snapshot_id": "broker-1", "broker_snapshot_sha256": "a" * 64,
        "quote_snapshot_id": "quote-1", "quote_snapshot_sha256": "b" * 64,
        "instrument_rules_sha256": "c" * 64,
    }
    values.update(changes)
    return values


def rejected_pre_trade_values(
    candidate: StrategyOrderCandidate,
    *,
    hard_blocks: tuple[str, ...] = ("NO_QUOTE",),
    checked_at: str = "2026-07-28T09:56:00+08:00",
    **changes: object,
) -> dict[str, object]:
    values = pre_trade_values(
        candidate,
        allowed=False,
        hard_blocks=hard_blocks,
        approved_qty=0,
        target_position_qty=0,
        fee_evidence_status="unavailable",
        rule_evidence_status="unavailable",
        fee_schedule_version="not-applicable",
        fee_schedule_sha256="not-applicable",
        execution_fee=None,
        round_trip_cost=None,
        target_round_trip_cost=None,
        planned_stop_loss_yuan=None,
        gap_price=None,
        gap_round_trip_cost=None,
        gap_loss_yuan=None,
        fee_erosion_ratio=None,
        cost_to_expected_edge_ratio=None,
        per_trade_risk_yuan=D("0"),
        actual_trade_risk_fraction=D("0"),
        approved_limit_price=None,
        approved_price_cap=None,
        submission_attempt_id="not-applicable",
        broker_snapshot_id="not-applicable",
        broker_snapshot_sha256="not-applicable",
        quote_snapshot_id="not-applicable",
        quote_snapshot_sha256="not-applicable",
        instrument_rules_sha256="not-applicable",
        checked_at=checked_at,
        valid_until=checked_at,
    )
    values.update(changes)
    return values


def intent_values(
    candidate: StrategyOrderCandidate,
    result: PreTradeResult | None = None,
) -> dict[str, object]:
    is_buy = candidate.side == "buy"
    result = result or PreTradeResult(**pre_trade_values(candidate))
    current_qty = (
        result.target_position_qty - result.approved_qty
        if is_buy else result.target_position_qty + result.approved_qty
    )
    values: dict[str, object] = {
        "pre_trade_result_id": result.pre_trade_result_id,
        "pre_trade_result": result,
        "pre_trade_result_sha256": result.result_sha256,
        "submission_attempt_id": candidate.candidate_id, "account_scope_id": "scope-uuid",
        "adapter": "joinquant", "logical_signal_id": candidate.logical_signal_id,
        "source_signal_id": candidate.source_signal_id, "strategy_id": "main",
        "strategy_version": "s1", "parameter_version": "p1", "model_version": "disabled",
        "fee_schedule_version": result.fee_schedule_version,
        "fee_schedule_sha256": result.fee_schedule_sha256,
        "fee_evidence_status": result.fee_evidence_status,
        "rule_evidence_status": result.rule_evidence_status,
        "code": "600000", "side": candidate.side,
        "order_qty": result.approved_qty, "expected_current_qty": current_qty,
        "target_position_qty": result.target_position_qty,
        "limit_price": result.approved_limit_price,
        "price_cap": result.approved_price_cap,
        "stop_price": D("9.5"),
        "signal_time": "2026-07-28T09:55:00+08:00",
        "expires_at": "2026-07-28T10:01:00+08:00",
        "broker_snapshot_id": result.broker_snapshot_id,
        "broker_snapshot_sha256": result.broker_snapshot_sha256,
        "quote_snapshot_id": result.quote_snapshot_id,
        "quote_snapshot_sha256": result.quote_snapshot_sha256,
        "instrument_rules_sha256": result.instrument_rules_sha256,
    }
    exact_order = {
        name: values[name]
        for name in (
            "code", "side", "order_qty", "expected_current_qty", "target_position_qty",
            "limit_price", "price_cap", "stop_price", "expires_at",
        )
    }
    values["client_order_id"] = client_order_id(
        values["account_scope_id"], values["adapter"], values["logical_signal_id"],
        values["pre_trade_result_id"], exact_order, values["submission_attempt_id"],
    )
    return values


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
        changed = fees.to_dict()
        changed.pop("contract_sha256")
        changed["sell_minimum_commission_yuan"] = "8"
        self.assertNotEqual(fees.contract_sha256, FeeSchedule(**changed).contract_sha256)

    def test_round_trip_applies_each_minimum_commission_and_sell_tax(self) -> None:
        result = FEES.estimate_round_trip(D("10"), D("11"), 100)

        self.assertEqual(result.buy.commission_yuan, D("5.00"))
        self.assertEqual(result.sell.commission_yuan, D("5.00"))
        self.assertEqual(result.sell.stamp_tax_yuan, D("0.55"))
        self.assertEqual(result.buy.transfer_fee_yuan, D("0.01"))
        self.assertEqual(result.buy.slippage_yuan, D("1.00"))
        self.assertEqual(result.total_yuan, D("12.67"))
        self.assertEqual(len(result.content_sha256), 64)

    def test_fee_input_identity_excludes_serialization_envelope(self) -> None:
        result = FEES.estimate("buy", D("10"), 100)
        schedule = FEES.to_dict()
        schedule.pop("contract_sha256")
        expected = canonical_sha256(
            {
                "schedule": schedule,
                "side": "buy",
                "price": D("10"),
                "qty": 100,
                "notional": D("1000"),
                "commission": D("5"),
                "stamp_tax": D("0"),
                "transfer_fee": D("0.01"),
                "other_fee": D("0"),
                "slippage": D("1"),
            }
        )

        self.assertEqual(result.input_sha256, expected)

    def test_signed_fee_records_reject_changed_totals_and_extra_fields(self) -> None:
        breakdown = FEES.estimate("buy", D("10"), 100)
        round_trip = FEES.estimate_round_trip(D("10"), D("9.5"), 100)
        for loader, payload in (
            (type(breakdown).from_dict, breakdown.to_dict()),
            (RoundTripCost.from_dict, round_trip.to_dict()),
        ):
            with self.subTest(loader=loader.__qualname__, field="total_yuan"):
                with self.assertRaisesRegex(ValueError, "total_yuan"):
                    loader({**payload, "total_yuan": "999.99"})
            with self.subTest(loader=loader.__qualname__, field="extra"):
                with self.assertRaisesRegex(ValueError, "fields"):
                    loader({**payload, "unexpected": "value"})

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

    def test_fee_schedule_minimum_field_schema_is_unambiguous(self) -> None:
        legacy = FEES.to_dict()
        legacy["minimum_commission_yuan"] = legacy.pop("buy_minimum_commission_yuan")
        legacy.pop("sell_minimum_commission_yuan")

        self.assertEqual(FeeSchedule.from_dict(legacy), FEES)

        with self.assertRaisesRegex(ValueError, "mixed"):
            FeeSchedule.from_dict({**FEES.to_dict(), "minimum_commission_yuan": "999"})

    def test_fee_breakdown_and_pre_trade_cost_round_trip_keep_decimal_semantics(self) -> None:
        costs = FEES.estimate_round_trip(D("10"), D("9.5"), 100)
        candidate = make_candidate()
        result = PreTradeResult(
            **pre_trade_values(
                candidate, pre_trade_result_id="risk-with-costs", round_trip_cost=costs
            )
        )

        restored = PreTradeResult.from_dict(result.to_dict())

        self.assertEqual(restored.round_trip_cost.total_yuan, costs.total_yuan)
        tampered = costs.buy.to_dict()
        tampered["commission_yuan"] = "4.99"
        with self.assertRaisesRegex(ValueError, "total_yuan|content hash conflict"):
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

    def test_instrument_rules_special_status_is_strict_immutable_text(self) -> None:
        rules = InstrumentRules.a_share("600000", special_status=" normal ")

        self.assertEqual(rules.special_status, "normal")
        for value in ("", ["normal"], object()):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "special_status"):
                    InstrumentRules.a_share("600000", special_status=value)

        source = rules.to_dict()
        restored = InstrumentRules.from_dict(source)
        original = restored.to_dict()
        source["special_status"] = "halted"
        self.assertEqual(restored.to_dict(), original)

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
        exact_order = {
            "code": "000001", "side": "buy", "order_qty": 100,
            "expected_current_qty": 0, "target_position_qty": 100,
            "limit_price": D("10"), "price_cap": D("10.10"),
            "stop_price": D("9.5"), "expires_at": "2026-07-28T10:01:00+08:00",
        }
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
        candidate = make_candidate()
        result = PreTradeResult(**pre_trade_values(candidate))
        conflicting_result = PreTradeResult(
            **pre_trade_values(
                candidate,
                approved_qty=200,
                target_position_qty=200,
                execution_fee=FEES.estimate("buy", D("10"), 200),
                round_trip_cost=FEES.estimate_round_trip(D("10"), D("9.5"), 200),
                target_round_trip_cost=FEES.estimate_round_trip(D("10"), D("11"), 200),
                planned_stop_loss_yuan=D("114.89"),
                gap_price=D("9"),
                gap_round_trip_cost=FEES.estimate_round_trip(D("10"), D("9"), 200),
                gap_loss_yuan=D("214.74"),
                fee_erosion_ratio=D("0.00767"),
                cost_to_expected_edge_ratio=D("0.0767"),
                per_trade_risk_yuan=D("214.74"),
            )
        )
        first = ExecutionIntent(**intent_values(candidate, result))
        conflicting = ExecutionIntent(**intent_values(candidate, conflicting_result))

        self.assertEqual(candidate.payload_sha256, StrategyOrderCandidate.from_dict(candidate.to_dict()).payload_sha256)
        self.assertEqual(result.result_sha256, PreTradeResult.from_dict(result.to_dict()).result_sha256)
        self.assertNotEqual(first.intent_sha256, conflicting.intent_sha256)
        with self.assertRaises(FrozenInstanceError):
            first.order_qty = 200

    def test_candidate_freezes_admission_facts(self) -> None:
        buy = make_candidate(industry="", theme="")

        self.assertEqual(buy.industry, "__UNCATEGORIZED__")
        self.assertEqual(buy.theme, "__UNCATEGORIZED__")
        self.assertTrue(buy.uncategorized)
        self.assertEqual(StrategyOrderCandidate.from_dict(buy.to_dict()), buy)

        changed = replace(buy, buy_price_cap=D("10.01"), payload_sha256="")
        self.assertNotEqual(changed.payload_sha256, buy.payload_sha256)
        tampered = {**buy.to_dict(), "buy_price_cap": "10.01"}
        with self.assertRaisesRegex(ValueError, "conflict"):
            StrategyOrderCandidate.from_dict(tampered)

        sell = make_candidate(
            side="sell",
            requested_target_position_qty=0,
            exit_owner_id="cycle-1",
            exit_action="hard_stop",
            exit_priority=100,
            sell_limit_price=D("9.40"),
            sell_price_floor=D("9.30"),
        )
        self.assertEqual(StrategyOrderCandidate.from_dict(sell.to_dict()), sell)

        invalid = (
            (buy, {"buy_gap_price": None}, "buy_gap_price"),
            (buy, {"sell_limit_price": D("9")}, "side-inapplicable"),
            (buy, {"uncategorized": False}, "uncategorized"),
            (sell, {"exit_owner_id": None}, "exit_owner_id"),
            (sell, {"exit_priority": True}, "exit_priority"),
            (sell, {"sell_price_floor": D("9.50")}, "sell_price_floor"),
            (sell, {"buy_price_cap": D("10")}, "side-inapplicable"),
        )
        for original, changes, message in invalid:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, message):
                    replace(original, **changes, payload_sha256="")

    def test_pre_trade_result_binds_the_full_normalized_candidate_payload(self) -> None:
        first_candidate = make_candidate(target_price="11")
        second_candidate = make_candidate(target_price="12")
        common = pre_trade_values(first_candidate)
        common.pop("candidate")

        first = PreTradeResult(candidate=first_candidate, **common)
        second = PreTradeResult(
            candidate=second_candidate,
            **{
                **common,
                "target_round_trip_cost": FEES.estimate_round_trip(D("10"), D("12"), 100),
                "fee_erosion_ratio": D("0.01282"),
                "cost_to_expected_edge_ratio": D("0.0641"),
            },
        )

        self.assertNotEqual(first.result_sha256, second.result_sha256)
        self.assertEqual(
            PreTradeResult.from_dict(first.to_dict()).candidate.payload_sha256,
            first_candidate.payload_sha256,
        )
        with self.assertRaisesRegex(ValueError, "candidate_id"):
            PreTradeResult(candidate=first_candidate, **{**common, "candidate_id": "other"})

    def test_result_evidence_statuses_and_complete_projections_are_signed(self) -> None:
        candidate = make_candidate()
        values = pre_trade_values(candidate)

        result = PreTradeResult(**values)

        self.assertEqual(result.fee_evidence_status, "available")
        self.assertEqual(result.rule_evidence_status, "available")
        self.assertEqual(result.projected_available_cash_yuan, D("10993.99"))
        self.assertEqual(result.projected_single_position_value_yuan, D("1000"))
        self.assertEqual(result.projected_total_position_value_yuan, D("31000"))
        self.assertEqual(result.projected_uncategorized_value_yuan, D("0"))
        self.assertEqual(
            PreTradeResult.from_dict(result.to_dict()).result_sha256,
            result.result_sha256,
        )
        changed = PreTradeResult(
            **{**values, "projected_total_position_value_yuan": D("31000.01")}
        )
        self.assertNotEqual(changed.result_sha256, result.result_sha256)
        for broken in (
            {key: value for key, value in result.to_dict().items() if key != "fee_evidence_status"},
            {**result.to_dict(), "unexpected": "value"},
        ):
            with self.assertRaisesRegex(ValueError, "fields"):
                PreTradeResult.from_dict(broken)

        for field, value in (
            ("fee_evidence_status", "missing"),
            ("rule_evidence_status", "missing"),
            ("projected_available_cash_yuan", D("-0.01")),
            ("projected_single_position_value_yuan", D("31000.01")),
            ("projected_industry_value_yuan", D("31000.01")),
            ("projected_theme_value_yuan", D("31000.01")),
            ("projected_uncategorized_value_yuan", D("31000.01")),
            ("projected_industry_value_yuan", D("999.99")),
            ("projected_theme_value_yuan", D("999.99")),
            (
                "projected_open_risk_yuan",
                values["per_trade_risk_yuan"] - D("0.01"),
            ),
            ("actual_trade_risk_fraction", D("1.01")),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    PreTradeResult(**{**values, field: value})

        uncategorized = make_candidate(industry="", theme="")
        uncategorized_values = pre_trade_values(
            uncategorized,
            projected_uncategorized_value_yuan=D("1000"),
        )
        PreTradeResult(**uncategorized_values)
        with self.assertRaisesRegex(ValueError, "projected_uncategorized_value_yuan"):
            PreTradeResult(
                **{
                    **uncategorized_values,
                    "projected_uncategorized_value_yuan": D("999.99"),
                }
            )

    def test_buy_fails_closed_but_protective_sell_can_sign_unavailable_evidence(self) -> None:
        buy = make_candidate()
        buy_values = pre_trade_values(buy)
        for changes in (
            {
                "fee_evidence_status": "unavailable",
                "fee_schedule_version": "not-applicable",
                "fee_schedule_sha256": "not-applicable",
                "execution_fee": None,
            },
            {
                "rule_evidence_status": "unavailable",
                "instrument_rules_sha256": "not-applicable",
            },
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "buy|available"):
                    PreTradeResult(**{**buy_values, **changes})

        candidate = make_candidate(side="sell")
        values = pre_trade_values(
            candidate,
            fee_evidence_status="unavailable",
            rule_evidence_status="unavailable",
            fee_schedule_version="not-applicable",
            fee_schedule_sha256="not-applicable",
            instrument_rules_sha256="not-applicable",
            execution_fee=None,
            warnings=(
                "SELL_FEE_EVIDENCE_UNAVAILABLE",
                "SELL_RULE_EVIDENCE_UNAVAILABLE",
            ),
        )
        result = PreTradeResult(**values)
        intent = ExecutionIntent(**intent_values(candidate, result))

        self.assertTrue(result.allowed)
        self.assertIsNone(result.execution_fee)
        self.assertEqual(PreTradeResult.from_dict(result.to_dict()), result)
        self.assertEqual(ExecutionIntent.from_dict(intent.to_dict()), intent)
        for broken in (
            {key: value for key, value in intent.to_dict().items() if key != "rule_evidence_status"},
            {**intent.to_dict(), "unexpected": "value"},
        ):
            with self.assertRaisesRegex(ValueError, "fields"):
                ExecutionIntent.from_dict(broken)
        for changes, message in (
            ({"warnings": ("SELL_RULE_EVIDENCE_UNAVAILABLE",)}, "fee_evidence_status"),
            (
                {
                    "rule_evidence_status": "available",
                    "instrument_rules_sha256": "d" * 64,
                },
                "SELL_RULE_EVIDENCE_UNAVAILABLE",
            ),
        ):
            with self.assertRaisesRegex(ValueError, message):
                PreTradeResult(**{**values, **changes})
        for field, changes in (
            (
                "fee_evidence_status",
                {
                    "fee_evidence_status": "available",
                    "fee_schedule_version": FEES.version,
                    "fee_schedule_sha256": FEES.contract_sha256,
                },
            ),
            (
                "rule_evidence_status",
                {
                    "rule_evidence_status": "available",
                    "instrument_rules_sha256": "d" * 64,
                },
            ),
            ("fee_schedule_sha256", {"fee_schedule_sha256": "d" * 64}),
            ("instrument_rules_sha256", {"instrument_rules_sha256": "d" * 64}),
        ):
            with self.subTest(intent_mismatch=field):
                with self.assertRaisesRegex(ValueError, field):
                    ExecutionIntent(**{**intent_values(candidate, result), **changes})

    def test_result_price_protection_matches_the_signed_candidate(self) -> None:
        buy = make_candidate()
        buy_values = pre_trade_values(buy)
        with self.assertRaisesRegex(ValueError, "approved_price_cap"):
            PreTradeResult(**{**buy_values, "approved_price_cap": D("10.09")})
        changed_gap = D("9.01")
        changed_gap_cost = FEES.estimate_round_trip(D("10"), changed_gap, 100)
        changed_gap_loss = (D("10") - changed_gap) * 100 + changed_gap_cost.total_yuan
        with self.assertRaisesRegex(ValueError, "gap_price"):
            PreTradeResult(
                **{
                    **buy_values,
                    "gap_price": changed_gap,
                    "gap_round_trip_cost": changed_gap_cost,
                    "gap_loss_yuan": changed_gap_loss,
                    "per_trade_risk_yuan": changed_gap_loss,
                }
            )

        sell = make_candidate(side="sell", sell_price_floor=D("9.30"))
        sell_values = pre_trade_values(sell)
        for field, value in (
            ("approved_limit_price", D("9.39")),
            ("approved_price_cap", D("9.29")),
        ):
            with self.subTest(side="sell", field=field):
                with self.assertRaisesRegex(ValueError, field):
                    PreTradeResult(**{**sell_values, field: value})

    def test_rejected_result_does_not_fabricate_missing_evidence_or_lot_costs(self) -> None:
        candidate = make_candidate()
        values = pre_trade_values(
            candidate,
            allowed=False,
            hard_blocks=("FEE_SCHEDULE_REQUIRED",),
            approved_qty=0,
            target_position_qty=0,
            fee_evidence_status="unavailable",
            rule_evidence_status="unavailable",
            fee_schedule_version="not-applicable",
            fee_schedule_sha256="not-applicable",
            instrument_rules_sha256="not-applicable",
            execution_fee=None,
            round_trip_cost=None,
            target_round_trip_cost=None,
            planned_stop_loss_yuan=None,
            gap_price=None,
            gap_round_trip_cost=None,
            gap_loss_yuan=None,
            fee_erosion_ratio=None,
            cost_to_expected_edge_ratio=None,
            per_trade_risk_yuan=D("0"),
            actual_trade_risk_fraction=D("0"),
            approved_limit_price=None,
            approved_price_cap=None,
            submission_attempt_id="not-applicable",
            broker_snapshot_id="not-applicable",
            broker_snapshot_sha256="not-applicable",
            quote_snapshot_id="not-applicable",
            quote_snapshot_sha256="not-applicable",
            checked_at="2026-07-28T09:56:00+08:00",
            valid_until="2026-07-28T09:56:00+08:00",
        )

        rejected = PreTradeResult(**values)

        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.fee_schedule_sha256, "not-applicable")
        self.assertIsNone(rejected.round_trip_cost)
        for field, value in (
            ("execution_fee", FEES.estimate("buy", D("10"), 100)),
            (
                "round_trip_cost",
                FEES.estimate_round_trip(D("10"), candidate.stop_price, 100),
            ),
            ("planned_stop_loss_yuan", D("62.45")),
            ("actual_trade_risk_fraction", D("0.01")),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    PreTradeResult(**{**values, field: value})

    def test_pre_trade_result_rejects_candidate_version_conflicts(self) -> None:
        candidate = make_candidate()
        common = pre_trade_values(candidate)
        for field in ("fee_schedule_version", "strategy_version"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    PreTradeResult(**{**common, field: "different"})

    def test_round_trip_and_pre_trade_result_bind_one_fee_contract_hash(self) -> None:
        first = FeeSchedule.simulation(version="same", minimum_commission_yuan=3)
        second = FeeSchedule.simulation(version="same", minimum_commission_yuan=9)
        buy = first.estimate("buy", D("10"), 100)
        sell = second.estimate("sell", D("9.5"), 100)

        self.assertEqual(buy.fee_schedule_sha256, first.contract_sha256)
        with self.assertRaisesRegex(ValueError, "contract"):
            RoundTripCost(buy, sell)

        candidate = make_candidate()
        costs = FEES.estimate_round_trip(D("10"), D("9.5"), 100)
        common = pre_trade_values(candidate, round_trip_cost=costs)
        result = PreTradeResult(**common)
        self.assertEqual(result.fee_schedule_sha256, FEES.contract_sha256)
        with self.assertRaisesRegex(ValueError, "fee schedule contract"):
            PreTradeResult(**{**common, "fee_schedule_sha256": "d" * 64})

    def test_allowed_pre_trade_result_requires_evidence_but_rejected_may_omit_it(self) -> None:
        candidate = make_candidate()
        common = pre_trade_values(candidate)
        PreTradeResult(**common)
        for field, value in (
            ("broker_snapshot_id", "not-applicable"),
            ("broker_snapshot_sha256", "not-applicable"),
            ("quote_snapshot_id", "not-applicable"),
            ("quote_snapshot_sha256", "not-applicable"),
            ("instrument_rules_sha256", "not-applicable"),
            ("instrument_rules_sha256", "x"),
            ("submission_attempt_id", "not-applicable"),
            ("round_trip_cost", None),
            ("round_trip_cost", FEES.estimate_round_trip(D("10"), D("9.5"), 200)),
            ("approved_qty", 0),
            ("target_position_qty", 0),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    PreTradeResult(**{**common, field: value})

        rejected = PreTradeResult(**rejected_pre_trade_values(candidate))
        self.assertFalse(rejected.allowed)

    def test_pre_trade_result_freezes_side_specific_risk_and_fee_evidence(self) -> None:
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                candidate = make_candidate(side=side)
                execution_price = D("10") if side == "buy" else candidate.sell_limit_price
                execution_fee = FEES.estimate(side, execution_price, 100)
                values = pre_trade_values(
                    candidate,
                    pre_trade_result_id=f"risk-{side}",
                    execution_fee=execution_fee,
                )

                result = PreTradeResult(**values)

                self.assertEqual(result.execution_fee.side, side)
                self.assertEqual(result.execution_fee.qty, 100)
                self.assertEqual(
                    PreTradeResult.from_dict(result.to_dict()).result_sha256,
                    result.result_sha256,
                )
                required = ["execution_fee"]
                if side == "buy":
                    required += [
                        "round_trip_cost", "target_round_trip_cost",
                        "planned_stop_loss_yuan", "gap_price", "gap_round_trip_cost",
                        "gap_loss_yuan", "fee_erosion_ratio", "cost_to_expected_edge_ratio",
                    ]
                for field in required:
                    with self.subTest(side=side, missing=field):
                        with self.assertRaisesRegex(ValueError, field):
                            PreTradeResult(**{**values, field: None})
                with self.assertRaisesRegex(ValueError, "execution_fee"):
                    PreTradeResult(
                        **{
                            **values,
                            "execution_fee": FEES.estimate(
                                "sell" if side == "buy" else "buy", D("10"), 100
                            ),
                        }
                    )
                if side == "buy":
                    with self.assertRaisesRegex(ValueError, "per_trade_risk_yuan"):
                        PreTradeResult(**{**values, "per_trade_risk_yuan": D("80")})

    def test_buy_result_rejects_contradictory_fee_prices_and_non_positive_stop_risk(self) -> None:
        with self.assertRaisesRegex(ValueError, "stop_price"):
            make_candidate(stop_price="10")

        candidate = make_candidate()
        values = pre_trade_values(candidate)
        with self.assertRaisesRegex(ValueError, "execution_fee|round_trip_cost"):
            PreTradeResult(
                **{
                    **values,
                    "round_trip_cost": FEES.estimate_round_trip(D("20"), D("9.5"), 100),
                }
            )
        with self.assertRaisesRegex(ValueError, "planned_stop_loss_yuan"):
            PreTradeResult(
                **{
                    **values,
                    "planned_stop_loss_yuan": D("0"),
                    "gap_loss_yuan": D("0"),
                    "per_trade_risk_yuan": D("0"),
                }
            )

    def test_cap_only_buy_prices_fee_at_the_approved_cap(self) -> None:
        candidate = make_candidate()
        values = pre_trade_values(
            candidate,
            approved_limit_price=None,
            approved_price_cap=D("100"),
        )

        with self.assertRaisesRegex(ValueError, "approved_price_cap"):
            PreTradeResult(**values)

    def test_allowed_buy_requires_reproducible_positive_gap_loss(self) -> None:
        candidate = make_candidate(buy_gap_price=D("9.8"))
        shallower_gap_cost = FEES.estimate_round_trip(D("10"), D("9.8"), 100)
        PreTradeResult(
            **pre_trade_values(
                candidate,
                gap_price=D("9.8"),
                gap_round_trip_cost=shallower_gap_cost,
                planned_stop_loss_yuan=D("62.45"),
                gap_loss_yuan=D("32.49"),
                fee_erosion_ratio=D("0.01267"),
                cost_to_expected_edge_ratio=D("0.1267"),
                per_trade_risk_yuan=D("62.45"),
            )
        )
        for gap_loss in (D("0"), D("79.99")):
            with self.subTest(gap_loss=gap_loss):
                with self.assertRaisesRegex(ValueError, "gap_loss_yuan"):
                    PreTradeResult(
                        **pre_trade_values(
                            candidate,
                            gap_loss_yuan=gap_loss,
                            per_trade_risk_yuan=D("80"),
                        )
                    )

    def test_scenario_loss_yuan_recomputes_frozen_price_quantity_and_fees(self) -> None:
        planned_cost = FEES.estimate_round_trip(D("10"), D("9.5"), 100)

        self.assertEqual(
            contracts.scenario_loss_yuan(D("10"), D("9.5"), 100, planned_cost),
            D("62.45"),
        )
        with self.assertRaisesRegex(ValueError, "quantity|price"):
            contracts.scenario_loss_yuan(D("10"), D("9.5"), 200, planned_cost)

    def test_allowed_buy_freezes_target_cost_and_rejects_wrong_target_evidence(self) -> None:
        candidate = make_candidate(target_price="10.7")
        target_cost = FEES.estimate_round_trip(D("10"), candidate.target_price, 100)
        values = pre_trade_values(
            candidate,
            target_round_trip_cost=target_cost,
            fee_erosion_ratio=D("0.01263"),
            cost_to_expected_edge_ratio=D("0.18042857"),
        )

        result = PreTradeResult(**values)

        payload = result.to_dict()
        self.assertEqual(
            payload["target_round_trip_cost"]["content_sha256"],
            target_cost.content_sha256,
        )
        self.assertEqual(
            PreTradeResult.from_dict(payload).result_sha256,
            result.result_sha256,
        )
        changed_payload = dict(payload)
        changed_payload["target_round_trip_cost"] = FEES.estimate_round_trip(
            D("10"), D("10.6"), 100
        ).to_dict()
        self.assertNotEqual(
            result.result_sha256,
            canonical_sha256(
                {key: value for key, value in changed_payload.items() if key != "result_sha256"}
            ),
        )

        missing = dict(payload)
        missing.pop("target_round_trip_cost")
        missing["result_sha256"] = canonical_sha256(
            {key: value for key, value in missing.items() if key != "result_sha256"}
        )
        with self.assertRaisesRegex(ValueError, "fields|target_round_trip_cost"):
            PreTradeResult.from_dict(missing)

        wrong_contract = FEES.derive_variant(
            "wrong-target-fees", sell_minimum_commission_yuan=D("6")
        ).estimate_round_trip(D("10"), candidate.target_price, 100)
        for cost in (
            FEES.estimate_round_trip(D("10"), D("10.6"), 100),
            wrong_contract,
        ):
            with self.subTest(cost=cost):
                with self.assertRaisesRegex(ValueError, "target_round_trip_cost"):
                    PreTradeResult(**{**values, "target_round_trip_cost": cost})
                broken = {**payload, "target_round_trip_cost": cost.to_dict()}
                broken["result_sha256"] = canonical_sha256(
                    {key: value for key, value in broken.items() if key != "result_sha256"}
                )
                with self.assertRaisesRegex(ValueError, "target_round_trip_cost"):
                    PreTradeResult.from_dict(broken)

    def test_allowed_buy_rejects_understated_reproducible_risk_and_ratios(self) -> None:
        candidate = make_candidate()
        common = pre_trade_values(candidate)
        cases = (
            (
                "planned_stop_loss_yuan",
                {"planned_stop_loss_yuan": D("0.01")},
            ),
            (
                "gap_loss_yuan",
                {"gap_loss_yuan": D("80"), "per_trade_risk_yuan": D("80")},
            ),
            ("fee_erosion_ratio", {"fee_erosion_ratio": D("0")}),
            (
                "cost_to_expected_edge_ratio",
                {"cost_to_expected_edge_ratio": D("0")},
            ),
        )

        for message, changes in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    PreTradeResult(**{**common, **changes})

    def test_pre_trade_gap_evidence_is_signed_and_from_dict_fails_closed(self) -> None:
        candidate = make_candidate()
        gap_cost = FEES.estimate_round_trip(D("10"), D("9"), 100)
        result = PreTradeResult(
            **pre_trade_values(
                candidate,
                gap_price=D("9"),
                gap_round_trip_cost=gap_cost,
                planned_stop_loss_yuan=D("62.45"),
                gap_loss_yuan=D("112.37"),
                fee_erosion_ratio=D("0.01267"),
                cost_to_expected_edge_ratio=D("0.1267"),
                per_trade_risk_yuan=D("112.37"),
            )
        )

        payload = result.to_dict()
        self.assertEqual(payload["gap_price"], "9")
        self.assertEqual(payload["gap_round_trip_cost"]["content_sha256"], gap_cost.content_sha256)
        self.assertEqual(
            PreTradeResult.from_dict(payload).result_sha256,
            result.result_sha256,
        )
        for field in ("gap_price", "gap_round_trip_cost"):
            with self.subTest(missing=field):
                broken = dict(payload)
                broken.pop(field)
                broken["result_sha256"] = canonical_sha256(
                    {key: value for key, value in broken.items() if key != "result_sha256"}
                )
                with self.assertRaisesRegex(ValueError, "fields|gap"):
                    PreTradeResult.from_dict(broken)

        for field, value, related in (
            ("planned_stop_loss_yuan", D("0.01"), {}),
            (
                "gap_loss_yuan",
                D("62.45"),
                {"per_trade_risk_yuan": D("62.45")},
            ),
            ("fee_erosion_ratio", D("0"), {}),
            ("cost_to_expected_edge_ratio", D("0"), {}),
        ):
            with self.subTest(resigned_understatement=field):
                understated = {**payload, field: value, **related}
                understated["result_sha256"] = canonical_sha256(
                    {key: value for key, value in understated.items() if key != "result_sha256"}
                )
                with self.assertRaisesRegex(ValueError, field):
                    PreTradeResult.from_dict(understated)

    def test_pre_trade_result_enforces_candidate_time_window(self) -> None:
        candidate = make_candidate()
        common = pre_trade_values(candidate, pre_trade_result_id="risk-time")
        common.pop("checked_at")
        common.pop("valid_until")

        for checked_at, valid_until in (
            (candidate.signal_time, candidate.signal_time),
            (candidate.signal_time, candidate.frozen_valid_until),
            (candidate.frozen_valid_until, candidate.frozen_valid_until),
        ):
            with self.subTest(boundary=(checked_at, valid_until)):
                PreTradeResult(**common, checked_at=checked_at, valid_until=valid_until)

        for checked_at, valid_until in (
            ("2026-07-28T09:54:59+08:00", "2026-07-28T10:00:00+08:00"),
            ("2026-07-28T10:05:01+08:00", "2026-07-28T10:05:01+08:00"),
            ("2026-07-28T10:00:00+08:00", "2026-07-28T10:05:01+08:00"),
        ):
            with self.subTest(invalid_allowed=(checked_at, valid_until)):
                with self.assertRaisesRegex(ValueError, "candidate|signal_time|frozen_valid_until"):
                    PreTradeResult(**common, checked_at=checked_at, valid_until=valid_until)

        rejected = rejected_pre_trade_values(
            candidate,
            hard_blocks=("STALE_SIGNAL",),
            checked_at="2026-07-28T10:06:00+08:00",
            pre_trade_result_id="risk-time",
        )
        rejected.pop("checked_at")
        rejected.pop("valid_until")
        PreTradeResult(
            **rejected,
            checked_at="2026-07-28T10:06:00+08:00",
            valid_until="2026-07-28T10:06:00+08:00",
        )
        for checked_at, valid_until in (
            ("2026-07-28T10:06:00+08:00", "2026-07-28T10:06:01+08:00"),
            ("2026-07-28T09:54:59+08:00", "2026-07-28T09:54:59+08:00"),
        ):
            with self.subTest(invalid_rejected=(checked_at, valid_until)):
                with self.assertRaisesRegex(ValueError, "checked_at|signal_time|valid_until"):
                    PreTradeResult(**rejected, checked_at=checked_at, valid_until=valid_until)

    def test_execution_intent_rejects_unsafe_price_stop_and_quantity_relationships(self) -> None:
        buy = intent_values(make_candidate())
        sell = intent_values(make_candidate(side="sell"))
        cases = (
            ("price protection", {**buy, "limit_price": None, "price_cap": None}),
            ("stop_price", {**buy, "stop_price": None}),
            ("buy target", {**buy, "expected_current_qty": 100, "target_position_qty": 150}),
            ("oversell", {**sell, "order_qty": 200}),
            ("sell target", {**sell, "order_qty": 50, "target_position_qty": 80}),
            ("price protection", {**sell, "limit_price": None}),
            ("buy limit", {**buy, "limit_price": D("10.20"), "price_cap": D("10.10")}),
            ("sell limit", {**sell, "limit_price": D("9.80"), "price_cap": D("9.90")}),
        )
        for message, values in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    ExecutionIntent(**values)

    def test_signed_from_dict_requires_present_well_formed_hashes(self) -> None:
        candidate = make_candidate()
        result = PreTradeResult(
            **pre_trade_values(candidate, pre_trade_result_id="risk-signed")
        )
        quote = QuoteSnapshot.from_values(
            code="600000", quote_time="2026-07-28T10:00:00+08:00",
            last_price="10", bid_price=None, ask_price=None,
            limit_up_price=None, limit_down_price=None,
        )
        broker = BrokerSnapshot.from_values(
            account_scope_id="scope-uuid", trade_date="2026-07-28",
            broker_time="2026-07-28T10:00:00+08:00", total_equity=50_000,
            cash=20_000, available_cash=20_000, frozen_cash=0, positions=[],
            open_orders=[], fills=[], adapter_version="sim-1", node_version="node-1",
            session_id="session-1", capabilities_version="cap-1",
        )
        rules = InstrumentRules.a_share("600000")
        intent = ExecutionIntent(**intent_values(candidate))
        missing = object()
        records = (
            (type(FEES.estimate("buy", D("10"), 100)).from_dict,
             FEES.estimate("buy", D("10"), 100).to_dict(), "content_sha256"),
            (RoundTripCost.from_dict,
             FEES.estimate_round_trip(D("10"), D("9.5"), 100).to_dict(),
             "content_sha256"),
            (FeeSchedule.from_dict, FEES.to_dict(), "contract_sha256"),
            (InstrumentRules.from_dict, rules.to_dict(), "rules_sha256"),
            (QuoteSnapshot.from_dict, quote.to_dict(), "quote_sha256"),
            (BrokerSnapshot.from_dict, broker.to_dict(), "snapshot_sha256"),
            (StrategyOrderCandidate.from_dict, candidate.to_dict(), "payload_sha256"),
            (PreTradeResult.from_dict, result.to_dict(), "result_sha256"),
            (ExecutionIntent.from_dict, intent.to_dict(), "intent_sha256"),
        )
        for loader, payload, hash_field in records:
            for bad_hash in (missing, None, "", "a" * 63, "z" * 64, "d" * 64):
                with self.subTest(loader=loader.__qualname__, hash=bad_hash):
                    broken = dict(payload)
                    if bad_hash is missing:
                        broken.pop(hash_field)
                    else:
                        broken[hash_field] = bad_hash
                    with self.assertRaises(ValueError):
                        loader(broken)

        tampered_fee = FEES.estimate("buy", D("10"), 100).to_dict()
        tampered_fee.pop("content_sha256")
        tampered_fee["commission_yuan"] = "0"
        with self.assertRaises(ValueError):
            type(FEES.estimate("buy", D("10"), 100)).from_dict(tampered_fee)
        tampered_candidate = candidate.to_dict()
        tampered_candidate.pop("payload_sha256")
        tampered_candidate["target_price"] = "99"
        with self.assertRaises(ValueError):
            StrategyOrderCandidate.from_dict(tampered_candidate)

    def test_pre_trade_result_binds_snapshot_content_hashes(self) -> None:
        candidate = make_candidate()
        values = pre_trade_values(candidate, pre_trade_result_id="risk-snapshots")

        result = PreTradeResult(**values)

        self.assertEqual(result.broker_snapshot_sha256, "a" * 64)
        self.assertEqual(result.quote_snapshot_sha256, "b" * 64)
        self.assertEqual(PreTradeResult.from_dict(result.to_dict()).result_sha256, result.result_sha256)
        self.assertNotEqual(
            result.result_sha256,
            PreTradeResult(**{**values, "broker_snapshot_sha256": "d" * 64}).result_sha256,
        )
        for field, value in (
            ("broker_snapshot_sha256", "not-applicable"),
            ("quote_snapshot_sha256", "not-applicable"),
            ("broker_snapshot_sha256", "x"),
            ("quote_snapshot_sha256", "x"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, field):
                    PreTradeResult(**{**values, field: value})

        rejected = PreTradeResult(
            **rejected_pre_trade_values(
                candidate, pre_trade_result_id="risk-snapshots"
            )
        )
        self.assertEqual(rejected.broker_snapshot_sha256, "not-applicable")

    def test_execution_intent_binds_allowed_result_and_canonical_order_id(self) -> None:
        candidate = make_candidate()
        result = PreTradeResult(
            **pre_trade_values(candidate, pre_trade_result_id="risk-chain")
        )
        exact_order = {
            "code": candidate.code, "side": candidate.side, "order_qty": 100,
            "expected_current_qty": 0, "target_position_qty": 100,
            "limit_price": D("10"), "price_cap": D("10.10"),
            "stop_price": D("9.5"), "expires_at": "2026-07-28T10:01:00+08:00",
        }
        order_id = client_order_id(
            candidate.account_scope_id, "joinquant", candidate.logical_signal_id,
            result.pre_trade_result_id, exact_order, candidate.candidate_id,
        )
        values = {
            **intent_values(candidate, result), "client_order_id": order_id,
            "pre_trade_result_id": result.pre_trade_result_id,
            "pre_trade_result": result,
            "pre_trade_result_sha256": result.result_sha256,
            "fee_schedule_sha256": FEES.contract_sha256,
        }

        intent = ExecutionIntent(**values)

        self.assertEqual(intent.pre_trade_result.result_sha256, result.result_sha256)
        self.assertEqual(ExecutionIntent.from_dict(intent.to_dict()).intent_sha256, intent.intent_sha256)
        for field, value in (
            ("client_order_id", "arbitrary-order-id"),
            ("pre_trade_result_id", "other-result"),
            ("pre_trade_result_sha256", "d" * 64),
            ("fee_schedule_version", "other-fees"),
            ("fee_schedule_sha256", "d" * 64),
            ("broker_snapshot_id", "other-broker"),
            ("broker_snapshot_sha256", "d" * 64),
            ("quote_snapshot_id", "other-quote"),
            ("quote_snapshot_sha256", "d" * 64),
            ("instrument_rules_sha256", "d" * 64),
            ("account_scope_id", "other-scope"),
            ("logical_signal_id", "other-logical"),
            ("source_signal_id", "other-source"),
            ("strategy_id", "other-strategy"),
            ("strategy_version", "other-strategy-version"),
            ("parameter_version", "other-parameters"),
            ("model_version", "other-model"),
            ("code", "000001"),
            ("signal_time", "2026-07-28T09:56:00+08:00"),
            ("stop_price", D("9.4")),
            ("order_qty", 200),
            ("target_position_qty", 200),
            ("expires_at", "2026-07-28T10:02:00+08:00"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    ExecutionIntent(**{**values, field: value})

        rejected_result = PreTradeResult(
            **rejected_pre_trade_values(
                candidate,
                pre_trade_result_id="risk-rejected",
                hard_blocks=("NO_QUOTE",),
            )
        )
        with self.assertRaisesRegex(ValueError, "allowed pre_trade_result"):
            ExecutionIntent(
                **{
                    **values,
                    "pre_trade_result_id": rejected_result.pre_trade_result_id,
                    "pre_trade_result": rejected_result,
                    "pre_trade_result_sha256": rejected_result.result_sha256,
                }
            )

        changed_order = {**exact_order, "limit_price": D("9.99")}
        self.assertNotEqual(
            order_id,
            client_order_id(
                candidate.account_scope_id, "joinquant", candidate.logical_signal_id,
                result.pre_trade_result_id, changed_order, candidate.candidate_id,
            ),
        )
        equivalent_order = {
            **exact_order,
            "limit_price": "10.00",
            "price_cap": "10.100",
            "expires_at": "2026-07-28T02:01:00Z",
        }
        self.assertEqual(
            order_id,
            client_order_id(
                candidate.account_scope_id, "joinquant", candidate.logical_signal_id,
                result.pre_trade_result_id, equivalent_order, candidate.candidate_id,
            ),
        )
        with self.assertRaisesRegex(ValueError, "exact_order"):
            incomplete_order = dict(exact_order)
            incomplete_order.pop("expires_at")
            client_order_id(
                candidate.account_scope_id, "joinquant", candidate.logical_signal_id,
                result.pre_trade_result_id, incomplete_order, candidate.candidate_id,
            )
        self.assertNotEqual(
            order_id,
            client_order_id(
                candidate.account_scope_id, "qmt", candidate.logical_signal_id,
                result.pre_trade_result_id, exact_order, candidate.candidate_id,
            ),
        )
        self.assertNotEqual(
            order_id,
            client_order_id(
                candidate.account_scope_id, "joinquant", candidate.logical_signal_id,
                result.pre_trade_result_id, exact_order, "attempt-2",
            ),
        )

    def test_execution_intent_cannot_precede_approval_or_change_approved_price(self) -> None:
        candidate = make_candidate()
        result = PreTradeResult(**pre_trade_values(candidate))
        values = intent_values(candidate, result)

        for field_changes, message in (
            ({"expires_at": "2026-07-28T09:55:30+08:00"}, "checked_at"),
            ({"expires_at": "2026-07-28T10:00:00+08:00"}, "valid_until"),
            ({"limit_price": D("11"), "price_cap": D("11.10")}, "limit_price|execution_fee"),
            ({"submission_attempt_id": "attempt-2"}, "submission_attempt_id"),
        ):
            changed = {**values, **field_changes}
            exact_order = {
                name: changed[name]
                for name in (
                    "code", "side", "order_qty", "expected_current_qty",
                    "target_position_qty", "limit_price", "price_cap", "stop_price",
                    "expires_at",
                )
            }
            changed["client_order_id"] = client_order_id(
                changed["account_scope_id"], changed["adapter"],
                changed["logical_signal_id"], changed["pre_trade_result_id"],
                exact_order, changed["submission_attempt_id"],
            )
            with self.subTest(changes=field_changes):
                with self.assertRaisesRegex(ValueError, message):
                    ExecutionIntent(**changed)

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
