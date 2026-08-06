import unittest
from dataclasses import replace
from decimal import ROUND_DOWN, getcontext

from execution_contracts import (
    BrokerPosition,
    BrokerSnapshot,
    ExecutionIntent,
    InstrumentRules,
    QuoteSnapshot,
)
from pre_trade_check import (
    CapacityReservationEvidence,
    PortfolioState,
    PositionCapacityEvidence,
    ReservationView,
    RiskLimits,
    RiskPolicy,
    evaluate_observation,
    pre_trade_check,
)
from tests.test_execution_contracts import (
    D,
    FEES,
    intent_values,
    make_candidate,
)


CHECKED_AT = "2026-07-28T10:00:00+08:00"
SYSTEM_STATE = {
    "buy_enabled": "1",
    "sell_enabled": "1",
    "kill_switch": "0",
    "market_regime": "NORMAL",
}


def make_policy(**changes):
    values = {
        "checked_at": CHECKED_AT,
        "mode": "enforce",
        "adapter": "joinquant",
        "fee_schedule": FEES,
        "risk_cap_yuan": D("500"),
    }
    values.update(changes)
    return RiskPolicy(**values)


def make_rules(**changes):
    values = {
        "code": "600000",
        "exchange": "XSHG",
        "board": "main",
        "source": "test",
        "as_of": "2026-07-28T09:00:00+08:00",
        "valid_until": "2026-07-28T15:00:00+08:00",
        "limit_up_price": D("11"),
        "limit_down_price": D("9"),
    }
    values.update(changes)
    return InstrumentRules.a_share(**values)


def make_quote(**changes):
    values = {
        "code": "600000",
        "quote_time": "2026-07-28T09:59:30+08:00",
        "last_price": D("10"),
        "bid_price": D("9.99"),
        "ask_price": D("10"),
        "limit_up_price": D("11"),
        "limit_down_price": D("9"),
    }
    values.update(changes)
    return QuoteSnapshot.from_values(**values)


def make_broker(*, positions=(), open_orders=(), **changes):
    values = {
        "account_scope_id": "scope-uuid",
        "trade_date": "2026-07-28",
        "broker_time": "2026-07-28T09:59:40+08:00",
        "total_equity": D("100000"),
        "cash": D("100000"),
        "available_cash": D("100000"),
        "frozen_cash": D("0"),
        "positions": positions,
        "open_orders": open_orders,
        "fills": (),
        "adapter_version": "joinquant-v1",
        "node_version": "server-v1",
        "session_id": "session-1",
        "capabilities_version": "cap-v1",
        "daily_risk_evidence_status": "reported",
        "intraday_pnl": D("0"),
        "account_drawdown_pct": D("0"),
        "daily_turnover_fraction": D("0"),
        "consecutive_losses": 0,
    }
    values.update(changes)
    return BrokerSnapshot.from_values(**values)


def position(
    code="600001", *, total_qty=100, sellable_qty=100, today_buy_qty=0,
    last_price=D("10"), market_value=None,
):
    return BrokerPosition.from_values(
        code=code,
        total_qty=total_qty,
        sellable_qty=sellable_qty,
        today_buy_qty=today_buy_qty,
        average_cost=last_price,
        last_price=last_price,
        market_value=last_price * total_qty if market_value is None else market_value,
    )


def open_order(*, side="buy", code="600000"):
    return {
        "client_order_id": f"open-{side}-{code}",
        "broker_order_id": f"broker-{side}-{code}",
        "stock_code": code,
        "side": side,
        "target_qty": 100,
        "filled_qty": 0,
        "status": "submitted",
        "updated_at": "2026-07-28T09:59:00+08:00",
    }


def make_position_evidence(
    broker,
    *,
    industry="technology",
    theme="artificial-intelligence",
    stop_price=D("9.50"),
):
    return tuple(
        PositionCapacityEvidence(
            position_cycle_id=f"cycle-{item.code}",
            entry_intent=ExecutionIntent(**intent_values(make_candidate(
                code=item.code,
                industry=industry,
                theme=theme,
            ))),
            effective_stop_price=stop_price,
        )
        for item in broker.positions
        if item.total_qty > 0
    )


def make_reservation_view(
    broker=None,
    *,
    positions=None,
    active_reservations=(),
    **changes,
):
    broker = broker or make_broker()
    values = {
        "account_scope_id": broker.account_scope_id,
        "broker_snapshot_id": broker.snapshot_id,
        "broker_snapshot_sha256": broker.snapshot_sha256,
        "positions": (
            make_position_evidence(broker)
            if positions is None
            else tuple(positions)
        ),
        "active_reservations": tuple(active_reservations),
        "capacity_evidence_complete": True,
        "daily_evidence_complete": True,
        "daily_trade_date": broker.trade_date,
        "daily_source": "canonical-ledger-test",
    }
    values.update(changes)
    return ReservationView(**values)


def make_active_reservation(
    *, code="600000", status="submitted", remaining_qty=100
):
    intent = ExecutionIntent(**intent_values(make_candidate(code=code)))
    return CapacityReservationEvidence(
        intent=intent,
        status=status,
        remaining_qty=remaining_qty,
    )


class PreTradeCheckTest(unittest.TestCase):
    def test_valid_buy_is_deterministic_and_binds_exact_evidence(self) -> None:
        candidate = make_candidate()
        broker = make_broker()
        quote = make_quote()
        rules = make_rules()
        reservations = make_reservation_view(broker)
        before = (
            candidate.to_dict(), broker.to_dict(), quote.to_dict(), rules.to_dict(),
            reservations.to_dict(),
        )

        first = pre_trade_check(
            candidate, broker, quote, rules, SYSTEM_STATE,
            make_policy(), reservations,
        )
        second = pre_trade_check(
            candidate, broker, quote, rules, SYSTEM_STATE,
            make_policy(), reservations,
        )

        self.assertTrue(first.allowed)
        self.assertEqual(first.approved_qty, 400)
        self.assertEqual(first.target_position_qty, 400)
        self.assertEqual(first.broker_snapshot_sha256, broker.snapshot_sha256)
        self.assertEqual(first.quote_snapshot_sha256, quote.quote_sha256)
        self.assertEqual(first.instrument_rules_sha256, rules.rules_sha256)
        self.assertEqual(first.fee_schedule_sha256, FEES.contract_sha256)
        self.assertEqual(first.risk_policy_sha256, make_policy().policy_sha256)
        self.assertEqual(
            first.reservation_view_sha256, reservations.view_sha256,
        )
        self.assertGreater(first.per_trade_risk_yuan, D("0"))
        self.assertEqual(first.result_sha256, second.result_sha256)
        self.assertEqual(
            before,
            (
                candidate.to_dict(), broker.to_dict(), quote.to_dict(),
                rules.to_dict(), reservations.to_dict(),
            ),
        )

    def test_hard_safety_matrix_is_mode_invariant(self) -> None:
        candidate = make_candidate()
        broker = make_broker()
        quote = make_quote()
        rules = make_rules()
        cases = (
            ("broker", None, quote, rules, make_policy(), "BROKER_SNAPSHOT_REQUIRED"),
            ("quote", broker, None, rules, make_policy(), "QUOTE_REQUIRED"),
            (
                "rules", broker, quote, None, make_policy(),
                "INSTRUMENT_RULES_REQUIRED",
            ),
            (
                "fees", broker, quote, rules,
                make_policy(fee_schedule=None), "FEE_SCHEDULE_REQUIRED",
            ),
            (
                "stale broker",
                make_broker(broker_time="2026-07-28T09:50:00+08:00"),
                quote, rules, make_policy(), "ACCOUNT_SNAPSHOT_STALE",
            ),
            (
                "stale quote", broker,
                make_quote(quote_time="2026-07-28T09:50:00+08:00"),
                rules, make_policy(), "QUOTE_STALE",
            ),
            (
                "stale rules", broker, quote,
                make_rules(valid_until="2026-07-28T09:59:00+08:00"),
                make_policy(), "INSTRUMENT_RULES_STALE",
            ),
            (
                "buy disabled", broker, quote, rules, make_policy(),
                "BUY_DISABLED",
            ),
            (
                "duplicate", make_broker(open_orders=(open_order(),)),
                quote, rules, make_policy(), "DUPLICATE_ORDER",
            ),
        )
        for label, current_broker, current_quote, current_rules, policy, reason in cases:
            state = (
                {**SYSTEM_STATE, "buy_enabled": "0"}
                if label == "buy disabled" else SYSTEM_STATE
            )
            outcomes = []
            for mode in ("observe", "enforce"):
                reservations = make_reservation_view(
                    current_broker or broker
                )
                result = pre_trade_check(
                    candidate, current_broker, current_quote, current_rules,
                    state, replace(policy, mode=mode), reservations,
                )
                self.assertFalse(result.allowed, label)
                self.assertIn(reason, result.hard_blocks, label)
                outcomes.append(result.hard_blocks)
            self.assertEqual(outcomes[0], outcomes[1], label)

    def test_missing_or_invalid_control_state_fails_closed(self) -> None:
        candidate = make_candidate()
        broker = make_broker()
        common = (
            candidate, broker, make_quote(), make_rules(),
        )
        for state in (
            {},
            {**SYSTEM_STATE, "kill_switch": "unknown"},
            {key: value for key, value in SYSTEM_STATE.items() if key != "buy_enabled"},
        ):
            with self.subTest(state=state):
                result = pre_trade_check(
                    *common, state, make_policy(),
                    make_reservation_view(broker),
                )
                self.assertFalse(result.allowed)
                self.assertIn("SYSTEM_STATE_INCOMPLETE", result.hard_blocks)

    def test_future_evidence_is_rejected_and_validity_uses_shortest_ttl(self) -> None:
        future_broker = make_broker(
            broker_time="2026-07-28T10:01:00+08:00",
            generated_at="2026-07-28T10:01:00+08:00",
        )
        future_quote = make_quote(quote_time="2026-07-28T10:01:00+08:00")
        future_signal = make_candidate(
            signal_time="2026-07-28T10:01:00+08:00",
            frozen_valid_until="2026-07-28T10:05:00+08:00",
        )
        broker_result = pre_trade_check(
            make_candidate(), future_broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(), make_reservation_view(future_broker),
        )
        quote_result = pre_trade_check(
            make_candidate(), make_broker(), future_quote, make_rules(),
            SYSTEM_STATE, make_policy(), make_reservation_view(),
        )
        signal_result = pre_trade_check(
            future_signal, make_broker(), make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(), make_reservation_view(),
        )
        allowed = pre_trade_check(
            make_candidate(), make_broker(), make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(decision_ttl_sec=10),
            make_reservation_view(),
        )

        self.assertIn(
            "ACCOUNT_SNAPSHOT_FROM_FUTURE", broker_result.hard_blocks,
        )
        self.assertIn("QUOTE_FROM_FUTURE", quote_result.hard_blocks)
        self.assertIn("SIGNAL_FROM_FUTURE", signal_result.hard_blocks)
        self.assertEqual(
            allowed.valid_until, "2026-07-28T02:00:10+00:00",
        )

    def test_exact_freshness_boundaries_and_trade_date_fail_closed(self) -> None:
        candidate = make_candidate()
        cases = (
            (
                "signal",
                make_candidate(
                    signal_time="2026-07-28T09:40:00+08:00",
                    frozen_valid_until="2026-07-28T10:05:00+08:00",
                ),
                make_broker(), make_quote(), make_rules(),
                "SIGNAL_STALE",
            ),
            (
                "broker", candidate,
                make_broker(
                    broker_time="2026-07-28T09:55:00+08:00",
                    generated_at="2026-07-28T09:55:00+08:00",
                ),
                make_quote(), make_rules(), "ACCOUNT_SNAPSHOT_STALE",
            ),
            (
                "quote", candidate, make_broker(),
                make_quote(quote_time="2026-07-28T09:58:00+08:00"),
                make_rules(), "QUOTE_STALE",
            ),
            (
                "rules", candidate, make_broker(), make_quote(),
                make_rules(valid_until=CHECKED_AT), "INSTRUMENT_RULES_STALE",
            ),
            (
                "trade date", candidate,
                make_broker(trade_date="2026-07-25"),
                make_quote(), make_rules(), "ACCOUNT_TRADE_DATE_MISMATCH",
            ),
        )
        for label, current_candidate, broker, quote, rules, reason in cases:
            with self.subTest(label=label):
                result = pre_trade_check(
                    current_candidate, broker, quote, rules, SYSTEM_STATE,
                    make_policy(), make_reservation_view(broker),
                )
                self.assertFalse(result.allowed)
                self.assertIn(reason, result.hard_blocks)

    def test_pre_trade_check_does_not_mutate_decimal_context(self) -> None:
        context = getcontext()
        original = context.prec, context.rounding
        try:
            context.prec = 7
            context.rounding = ROUND_DOWN
            result = pre_trade_check(
                make_candidate(), make_broker(), make_quote(), make_rules(),
                SYSTEM_STATE, make_policy(), make_reservation_view(),
            )
            self.assertTrue(result.allowed)
            self.assertEqual((context.prec, context.rounding), (7, ROUND_DOWN))
        finally:
            context.prec, context.rounding = original

    def test_unknown_daily_risk_evidence_blocks_new_buys(self) -> None:
        broker = make_broker(daily_risk_evidence_status="unknown")
        result = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(), make_reservation_view(broker),
        )
        self.assertFalse(result.allowed)
        self.assertIn("DAILY_RISK_STATE_INCOMPLETE", result.hard_blocks)

        reported = make_broker()
        wrong_date = pre_trade_check(
            make_candidate(), reported, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(), make_reservation_view(
                reported, daily_trade_date="2026-07-25",
            ),
        )
        self.assertFalse(wrong_date.allowed)
        self.assertIn("DAILY_RISK_STATE_INCOMPLETE", wrong_date.hard_blocks)

    def test_only_economic_edge_is_softened_in_observe(self) -> None:
        candidate = make_candidate(target_price="10.11")
        broker = make_broker()
        common = (
            candidate, broker, make_quote(), make_rules(), SYSTEM_STATE,
        )
        enforce = pre_trade_check(
            *common, make_policy(mode="enforce", max_cost_edge_ratio=D("0.01")),
            make_reservation_view(broker),
        )
        observe = pre_trade_check(
            *common, make_policy(mode="observe", max_cost_edge_ratio=D("0.01")),
            make_reservation_view(broker),
        )

        self.assertFalse(enforce.allowed)
        self.assertIn("ECONOMIC_EDGE_INSUFFICIENT", enforce.hard_blocks)
        self.assertTrue(observe.allowed)
        self.assertGreater(observe.approved_qty, 0)
        self.assertIn("ECONOMIC_EDGE_INSUFFICIENT", observe.warnings)

    def test_buy_market_price_matrix(self) -> None:
        candidate = make_candidate()
        broker = make_broker()
        reservations = make_reservation_view(broker)
        cases = (
            (
                make_quote(suspended=True), make_rules(),
                "INSTRUMENT_SUSPENDED",
            ),
            (
                make_quote(last_price=D("11"), ask_price=D("11")),
                make_rules(), "BUY_LIMIT_UP",
            ),
            (
                make_quote(last_price=D("10.20"), ask_price=D("10.20")),
                make_rules(), "BUY_PRICE_CAP_EXCEEDED",
            ),
            (
                make_quote(), make_rules(special_status="ST"),
                "INSTRUMENT_SPECIAL_STATUS",
            ),
        )
        for quote, rules, reason in cases:
            for mode in ("observe", "enforce"):
                with self.subTest(reason=reason, mode=mode):
                    result = pre_trade_check(
                        candidate, broker, quote, rules, SYSTEM_STATE,
                        make_policy(mode=mode), reservations,
                    )
                    self.assertFalse(result.allowed)
                    self.assertIn(reason, result.hard_blocks)

    def test_invalid_buy_order_rules_return_rejection_instead_of_raising(self) -> None:
        cases = (
            (
                make_candidate(buy_price_cap=D("10.005")),
                make_rules(),
                "PRICE_TICK_INVALID",
            ),
            (
                make_candidate(),
                make_rules(suspended=True),
                "INSTRUMENT_SUSPENDED",
            ),
        )
        for candidate, rules, reason in cases:
            with self.subTest(reason=reason):
                broker = make_broker()
                result = pre_trade_check(
                    candidate,
                    broker,
                    make_quote(),
                    rules,
                    SYSTEM_STATE,
                    make_policy(),
                    make_reservation_view(broker),
                )

                self.assertFalse(result.allowed)
                self.assertIn(reason, result.hard_blocks)
                self.assertEqual(result.approved_qty, 0)

    def test_qmt_requires_enforce_mode_and_positive_yuan_risk_cap(self) -> None:
        broker = make_broker(adapter="qmt", adapter_version="qmt-v1")
        common = (
            make_candidate(), broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_reservation_view(broker),
        )
        observe = pre_trade_check(
            *common[:5],
            make_policy(adapter="qmt", mode="observe"),
            common[5],
        )
        no_cap = pre_trade_check(
            *common[:5],
            make_policy(adapter="qmt", mode="enforce", risk_cap_yuan=None),
            common[5],
        )
        self.assertIn("QMT_ENFORCE_REQUIRED", observe.hard_blocks)
        self.assertIn("RISK_CAP_YUAN_REQUIRED", no_cap.hard_blocks)

    def test_qmt_buy_requires_a_live_approved_fee_schedule(self) -> None:
        broker = make_broker(adapter="qmt", adapter_version="qmt-v1")
        simulated = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(adapter="qmt"),
            make_reservation_view(broker),
        )
        self.assertFalse(simulated.allowed)
        self.assertIn("FEE_SCHEDULE_NOT_LIVE", simulated.hard_blocks)

        live_fees = replace(
            FEES, version="broker-live-v1", execution_scope="live",
        )
        live_candidate = make_candidate(
            fee_schedule_version=live_fees.version,
        )
        approved = pre_trade_check(
            live_candidate, broker, make_quote(), make_rules(), SYSTEM_STATE,
            make_policy(adapter="qmt", fee_schedule=live_fees),
            make_reservation_view(broker),
        )
        self.assertTrue(approved.allowed)

    def test_qmt_buy_requires_complete_limits_and_consistent_market_evidence(self) -> None:
        live_fees = replace(
            FEES, version="broker-live-v1", execution_scope="live",
        )
        candidate = make_candidate(fee_schedule_version=live_fees.version)
        broker = make_broker(adapter="qmt", adapter_version="qmt-v1")
        missing_limits = pre_trade_check(
            candidate, broker, make_quote(),
            make_rules(limit_up_price=None, limit_down_price=None),
            SYSTEM_STATE,
            make_policy(adapter="qmt", fee_schedule=live_fees),
            make_reservation_view(broker),
        )
        conflicting = pre_trade_check(
            candidate, broker, make_quote(limit_up_price=D("10.90")),
            make_rules(), SYSTEM_STATE,
            make_policy(adapter="qmt", fee_schedule=live_fees),
            make_reservation_view(broker),
        )
        self.assertIn("INSTRUMENT_RULES_INCOMPLETE", missing_limits.hard_blocks)
        self.assertIn("MARKET_RULE_EVIDENCE_CONFLICT", conflicting.hard_blocks)

    def test_caution_policy_must_match_state_and_halves_risk_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "halve"):
            make_policy(market_regime="CAUTION")

        caution_policy = make_policy(
            market_regime="CAUTION",
            per_trade_risk_fraction=D("0.005"),
            max_open_risk_fraction=D("0.02"),
            risk_cap_yuan=D("1000"),
        )
        mismatch = pre_trade_check(
            make_candidate(), make_broker(), make_quote(), make_rules(),
            SYSTEM_STATE, caution_policy, make_reservation_view(),
        )
        caution_state = {**SYSTEM_STATE, "market_regime": "CAUTION"}
        matched = pre_trade_check(
            make_candidate(), make_broker(), make_quote(), make_rules(),
            caution_state, caution_policy, make_reservation_view(),
        )
        normal = pre_trade_check(
            make_candidate(), make_broker(), make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(risk_cap_yuan=D("1000")),
            make_reservation_view(),
        )

        self.assertIn("POLICY_REGIME_MISMATCH", mismatch.hard_blocks)
        self.assertTrue(matched.allowed)
        self.assertLess(matched.approved_qty, normal.approved_qty)

    def test_broker_open_buy_must_be_tracked_without_double_counting_cash(self) -> None:
        active = make_active_reservation(code="600002")
        pending = {
            **open_order(code="600002"),
            "client_order_id": active.client_order_id,
        }
        broker = make_broker(
            open_orders=(pending,),
            cash=D("12100"),
            available_cash=D("11100"),
            frozen_cash=D("1000"),
        )
        tracked = make_reservation_view(
            broker,
            active_reservations=(active,),
        )
        untracked = replace(
            tracked,
            active_reservations=(),
        )

        result = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(risk_cap_yuan=D("1000")), tracked,
        )
        rejected = pre_trade_check(
            make_candidate(), broker, make_quote(), make_rules(),
            SYSTEM_STATE, make_policy(risk_cap_yuan=D("1000")), untracked,
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.approved_qty, 600)
        self.assertIn(
            "RESERVATION_EVIDENCE_INCOMPLETE", rejected.hard_blocks,
        )

    def test_valid_sell_without_position_pct_is_allowed(self) -> None:
        result = evaluate_observation(
            {"action": "sell", "code": "600000", "price": 10.5},
            PortfolioState.empty(),
            RiskLimits(),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.hard_blocks, ())

    def test_portfolio_sector_exposure_is_defensively_immutable(self) -> None:
        exposure = {"semiconductor": 30}
        portfolio = PortfolioState(sector_exposure_pct=exposure)
        exposure["semiconductor"] = 99
        self.assertEqual(portfolio.sector_exposure_pct["semiconductor"], 30)
        with self.assertRaises(TypeError):
            portfolio.sector_exposure_pct["semiconductor"] = 40

    def test_result_metrics_are_defensively_immutable(self) -> None:
        metrics = {"value": 1}
        from pre_trade_check import RiskCheckResult

        result = RiskCheckResult(True, (), (), metrics)
        metrics["value"] = 2
        self.assertEqual(result.metrics["value"], 1)
        with self.assertRaises(TypeError):
            result.metrics["value"] = 3

    def test_soft_limit_warnings_do_not_block_signal(self) -> None:
        result = evaluate_observation(
            {"action": "buy", "position_pct": 40, "sector": "半导体"},
            PortfolioState(
                total_position_pct=90, cash_reserve_pct=10,
                sector_exposure_pct={"半导体": 30}, new_positions_today=10,
                orders_today=50, daily_turnover_pct=190,
                daily_pnl_pct=-6, account_drawdown_pct=-16,
            ),
            RiskLimits(),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.hard_blocks, ())
        self.assertIn("SINGLE_POSITION_LIMIT", result.soft_warnings)
        self.assertIn("TOTAL_POSITION_LIMIT", result.soft_warnings)
        self.assertIn("DAILY_LOSS_WARNING", result.soft_warnings)

    def test_invalid_signal_is_a_hard_block(self) -> None:
        result = evaluate_observation(
            {"action": "buy", "position_pct": 10, "price": 0},
            PortfolioState.empty(),
            RiskLimits(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.hard_blocks, ("INVALID_ORDER_INPUT",))


if __name__ == "__main__":
    unittest.main()
