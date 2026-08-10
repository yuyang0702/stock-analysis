from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import joinquant_strategy


class JoinQuantStrategyTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        joinquant_strategy.g = SimpleNamespace(
            gap_reentry_orders={},
            executed_signal_ids=set(),
            signals=[],
            order_events=[],
            order_signal_ids={},
        )
        joinquant_strategy.log = SimpleNamespace(info=Mock(), warn=Mock())

    def test_gap_reentry_partial_fill_cancels_remaining_order(self) -> None:
        order = SimpleNamespace(
            order_id="gap-order-1", security="002432.XSHE",
            amount=200, filled=100,
        )
        quote = SimpleNamespace(last_price=10.50, high_limit=11.00)
        joinquant_strategy.g.gap_reentry_orders["gap-order-1"] = {
            "id": "gap-signal-1", "jq_code": "002432.XSHE",
            "reentry_cap_price": 10.80,
        }

        with patch.object(joinquant_strategy, "get_open_orders", return_value={"gap-order-1": order}, create=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"002432.XSHE": quote}, create=True), \
             patch.object(joinquant_strategy, "cancel_order", create=True) as cancel, \
             patch.object(joinquant_strategy, "_record_order") as record:
            joinquant_strategy._cancel_invalid_gap_reentry_orders()

        cancel.assert_called_once_with(order)
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2], "gap_reentry_partial_fill_complete")
        self.assertNotIn("gap-order-1", joinquant_strategy.g.gap_reentry_orders)

    def test_gap_reentry_exact_lot_uses_quantity_order(self) -> None:
        signal = {
            "id": "gap-signal-1", "action": "buy", "code": "002432",
            "jq_code": "002432.XSHE", "position_pct": 7.6,
            "target_qty": 100, "entry_path": "gap_reentry",
        }
        joinquant_strategy.g.signals = [signal]
        context = SimpleNamespace(
            portfolio=SimpleNamespace(total_value=100_000, positions={}),
        )
        order = SimpleNamespace(order_id="gap-order-1", status="held")

        with patch.object(joinquant_strategy, "_cancel_invalid_gap_reentry_orders"), \
             patch.object(joinquant_strategy, "_can_execute", return_value=(True, "")), \
             patch.object(joinquant_strategy, "order_target", return_value=order, create=True) as by_qty, \
             patch.object(joinquant_strategy, "order_target_value", create=True) as by_value:
            joinquant_strategy.execute_signals(context)

        by_qty.assert_called_once_with("002432.XSHE", 100)
        by_value.assert_not_called()

    def test_gap_reentry_cash_guard_uses_current_price_and_exact_quantity(self) -> None:
        signal = {
            "id": "gap-signal-1", "action": "buy", "code": "002432",
            "jq_code": "002432.XSHE", "position_pct": 0.5,
            "target_qty": 100, "target_position": 100, "order_qty": 100,
            "expected_current_qty": 0, "entry_path": "gap_reentry",
            "account_scope_id": "scope-1",
            "client_order_id": "gap-client-1", "execution_intent_sha256": "a" * 64,
            "pre_trade_result_id": "risk-1", "pre_trade_result_sha256": "b" * 64,
            "broker_snapshot_id": "broker-1", "broker_snapshot_sha256": "c" * 64,
            "quote_snapshot_id": "quote-1", "quote_snapshot_sha256": "d" * 64,
            "instrument_rules_sha256": "e" * 64,
            "expires_at": "2099-07-18T10:02:00+08:00",
            "price_cap": 11.0, "required_cash_yuan": 1001,
            "reentry_cap_price": 11.0,
            "entry_price": 10.0, "price": 10.0, "final_score": 90,
            "created_at": "2026-07-18 10:00:00",
        }
        context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000, available_cash=1_000, cash=1_000, positions={},
        ))
        quote = SimpleNamespace(
            last_price=10.0, paused=False, low_limit=9.0, high_limit=11.0,
        )

        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"002432.XSHE": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True):
            allowed, reason = joinquant_strategy._can_execute(context, signal)

        self.assertFalse(allowed)
        self.assertEqual(reason, "insufficient_cash")

    def test_gap_reentry_rechecks_absolute_cap_before_order(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
        self.assertIn('signal.get("entry_path") == "gap_reentry"', text)
        self.assertIn('return False, "gap_reentry_price_moved"', text)
        self.assertIn("def _cancel_invalid_gap_reentry_orders", text)
        self.assertIn("cancel_order(order)", text)

    def test_template_defaults_to_joinquant_simulated_orders(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")

        self.assertIn("DRY_RUN = False", text)
        self.assertIn("def handle_data(context, data):", text)
        self.assertIn("fetch_and_execute(context)", text)
        self.assertNotIn('run_daily(execute_signals, time="09:35")', text)
        self.assertNotIn("order_target_percent", text)
        self.assertNotIn("order_target_value(", text)
        self.assertIn("context.portfolio.total_value", text)
        self.assertIn("order_target(jq_code, target_qty)", text)
        self.assertIn('return False, "not_holding"', text)
        self.assertIn('if reason == "duplicate":', text)
        self.assertIn("return event_count", text)
        self.assertIn("g.order_events", text)
        self.assertIn('"orders":', text)
        self.assertIn("record order", text)
        self.assertIn("post snapshot ok", text)
        self.assertIn("default=str", text)
        self.assertIn("_order_status_text", text)

    def test_template_posts_version_with_snapshot(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
        config_text = Path("config.py").read_text(encoding="utf-8")

        self.assertIn('STRATEGY_TEMPLATE_VERSION = "2026-08-11.1-runtime-isolation"', text)
        self.assertIn('JOINQUANT_TEMPLATE_VERSION = "2026-08-11.1-runtime-isolation"', config_text)
        self.assertIn('"Authorization": "Bearer " + SYNC_TOKEN', text)
        self.assertIn('"strategy_template_version": STRATEGY_TEMPLATE_VERSION', text)
        self.assertIn('"X-JoinQuant-Run-Type": LIVE_RUN_TYPE', text)
        self.assertIn('"runtime_mode": LIVE_RUN_TYPE', text)

    def test_template_rechecks_five_positions_and_eighty_percent_total(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")

        self.assertIn("MAX_POSITIONS = 5", text)
        self.assertIn("MAX_TOTAL_POSITION_PCT = 80.0", text)
        self.assertIn('return False, "max_positions"', text)
        self.assertIn('return False, "total_position_limit"', text)

    def test_template_retries_pending_order_event_callback(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")

        self.assertIn("fetch_and_execute(context)\n    post_account_snapshot(context)", text)
        self.assertIn("return execute_signals(context)", text)
        self.assertIn('signal.get("max_age_min") or MAX_SIGNAL_AGE_MIN', text)
        self.assertIn('signal.get("validated_at")', text)
        self.assertIn('signal.get("created_at")', text)

    def test_template_self_heals_runtime_globals_after_online_update(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
        self.assertIn("def _ensure_runtime_state(context):", text)
        self.assertIn("_ensure_runtime_state(context)\n    if not _is_live_runtime(context):", text)
        self.assertIn('if not isinstance(getattr(g, "order_signal_ids", None), dict):', text)

    def test_backtest_runtime_never_registers_callbacks_or_touches_network(self) -> None:
        context = SimpleNamespace(
            run_params=SimpleNamespace(type="full_backtest"),
            portfolio=SimpleNamespace(total_value=100_000, positions={}),
        )
        with patch.object(joinquant_strategy, "run_daily", create=True) as run_daily, \
             patch.object(joinquant_strategy, "startup_self_test") as startup, \
             patch.object(joinquant_strategy, "fetch_signals") as fetch, \
             patch.object(joinquant_strategy, "post_account_snapshot") as post:
            joinquant_strategy.initialize(context)
            joinquant_strategy.handle_data(context, None)

        run_daily.assert_not_called()
        startup.assert_not_called()
        fetch.assert_not_called()
        post.assert_not_called()
        self.assertEqual(joinquant_strategy.g.runtime_mode, "full_backtest")

    def test_unknown_runtime_fails_closed(self) -> None:
        context = SimpleNamespace(
            portfolio=SimpleNamespace(total_value=100_000, positions={}),
        )
        with patch.object(joinquant_strategy, "fetch_signals") as fetch:
            self.assertEqual(joinquant_strategy.fetch_and_execute(context), 0)
        fetch.assert_not_called()

    def test_runtime_headers_bind_sim_trade_template_and_protocol(self) -> None:
        headers = joinquant_strategy._runtime_headers()
        self.assertEqual(headers["X-JoinQuant-Run-Type"], "sim_trade")
        self.assertEqual(
            headers["X-JoinQuant-Template-Version"],
            joinquant_strategy.STRATEGY_TEMPLATE_VERSION,
        )
        self.assertEqual(headers["X-JoinQuant-Protocol-Version"], "1")

    def test_template_posts_startup_self_test_without_orders(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")

        self.assertIn("STARTUP_SELF_TEST = True", text)
        self.assertIn("startup_self_test(context)", text)
        self.assertIn("def startup_self_test(context):", text)
        self.assertIn("startup self test ok", text)
        self.assertNotIn("execute_signals(context)\\n        post_account_snapshot(context)", text)

    def test_template_executes_partial_sell_target_and_blocks_open_order_duplicate(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")

        self.assertIn('target_qty = signal.get("target_qty")', text)
        self.assertIn('return False, "pending_order"', text)
        self.assertIn('if signal.get("action") == "buy" and signal.get("id"):', text)

    def test_snapshot_reports_sellable_and_frozen_position_amounts(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
        self.assertIn('"closeable_amount": _position_attr(pos, "closeable_amount", 0)', text)
        self.assertIn('"locked_amount": _position_attr(pos, "locked_amount", 0)', text)
        self.assertIn('"today_amount": _position_attr(pos, "today_amount", 0)', text)
        self.assertIn("def _attainable_sell_target", text)
        self.assertIn('reason == "t_plus_one"', text)

    def test_snapshot_reports_account_risk_metrics(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
        self.assertIn('"daily_turnover_pct": metrics["daily_turnover_pct"]', text)
        self.assertIn('"daily_pnl_pct": metrics["daily_pnl_pct"]', text)
        self.assertIn('"account_drawdown_pct": metrics["account_drawdown_pct"]', text)
        self.assertIn('"consecutive_losses": metrics["consecutive_losses"]', text)
        self.assertIn('"pending_buy_position_pct": metrics["pending_buy_position_pct"]', text)
        self.assertIn('"pending_buy_risk_pct": metrics["pending_buy_risk_pct"]', text)

    def test_template_rechecks_current_quote_and_cash_before_buy(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
        self.assertIn("get_current_data()", text)
        self.assertIn('return False, "insufficient_cash"', text)
        self.assertIn('return False, "price_moved"', text)
        self.assertIn('return False, "suspended"', text)
        self.assertIn('return False, "limit_down"', text)

    def test_exact_buy_requires_signed_unexpired_quantity_contract(self) -> None:
        context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000,
            available_cash=100_000,
            cash=100_000,
            positions={},
        ))
        quote = SimpleNamespace(
            last_price=10.0, paused=False, low_limit=9.0, high_limit=11.0,
        )
        signal = {
            "id": "exact-1", "action": "buy", "code": "600000",
            "jq_code": "600000.XSHG", "position_pct": 10,
            "target_qty": 1000, "target_position": 1000,
            "order_qty": 1000, "expected_current_qty": 0,
            "account_scope_id": "scope-1",
            "client_order_id": "client-1", "execution_intent_sha256": "a" * 64,
            "pre_trade_result_id": "risk-1", "pre_trade_result_sha256": "b" * 64,
            "broker_snapshot_id": "broker-1", "broker_snapshot_sha256": "c" * 64,
            "quote_snapshot_id": "quote-1", "quote_snapshot_sha256": "d" * 64,
            "instrument_rules_sha256": "e" * 64,
            "expires_at": "2099-07-28T10:02:00+08:00",
            "price_cap": 10.1, "required_cash_yuan": 10100,
            "entry_price": 10.0, "price": 10.0, "final_score": 95,
            "created_at": "2026-07-28 10:00:00",
        }
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"600000.XSHG": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True):
            allowed, reason = joinquant_strategy._can_execute(context, signal)
            missing, missing_reason = joinquant_strategy._can_execute(
                context, {key: value for key, value in signal.items() if key != "client_order_id"},
            )
            expired, expired_reason = joinquant_strategy._can_execute(
                context, {**signal, "expires_at": "2000-01-01T00:00:00+08:00"},
            )

        self.assertTrue(allowed, reason)
        self.assertFalse(missing)
        self.assertEqual(missing_reason, "missing_execution_intent")
        self.assertFalse(expired)
        self.assertEqual(expired_reason, "intent_expired")

    def test_exact_buy_rechecks_position_price_and_cash_without_resizing(self) -> None:
        signal = {
            "id": "exact-1", "action": "buy", "code": "600000",
            "jq_code": "600000.XSHG", "position_pct": 10,
            "target_qty": 1000, "target_position": 1000,
            "order_qty": 1000, "expected_current_qty": 0,
            "account_scope_id": "scope-1",
            "client_order_id": "client-1", "execution_intent_sha256": "a" * 64,
            "pre_trade_result_id": "risk-1", "pre_trade_result_sha256": "b" * 64,
            "broker_snapshot_id": "broker-1", "broker_snapshot_sha256": "c" * 64,
            "quote_snapshot_id": "quote-1", "quote_snapshot_sha256": "d" * 64,
            "instrument_rules_sha256": "e" * 64,
            "expires_at": "2099-07-28T10:02:00+08:00",
            "price_cap": 10.1, "required_cash_yuan": 10100,
            "entry_price": 10.0, "price": 10.0, "final_score": 95,
        }
        held = SimpleNamespace(total_amount=100, value=1000)
        holding_context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000, available_cash=100_000, cash=100_000,
            positions={"600000.XSHG": held},
        ))
        empty_context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000, available_cash=100_000, cash=100_000,
            positions={},
        ))
        normal_quote = SimpleNamespace(
            last_price=10.0, paused=False, low_limit=9.0, high_limit=11.0,
        )
        moved_quote = SimpleNamespace(
            last_price=10.2, paused=False, low_limit=9.0, high_limit=11.0,
        )
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"600000.XSHG": normal_quote}, create=True):
            held_allowed, held_reason = joinquant_strategy._can_execute(
                holding_context, signal,
            )
            cash_allowed, cash_reason = joinquant_strategy._can_execute(
                empty_context, {**signal, "required_cash_yuan": 100001},
            )
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"600000.XSHG": moved_quote}, create=True):
            moved_allowed, moved_reason = joinquant_strategy._can_execute(
                empty_context, signal,
            )

        self.assertFalse(held_allowed)
        self.assertEqual(held_reason, "already_holding")
        self.assertFalse(cash_allowed)
        self.assertEqual(cash_reason, "insufficient_cash")
        self.assertFalse(moved_allowed)
        self.assertEqual(moved_reason, "price_moved")

        add_signal = {
            **signal,
            "target_qty": 1100,
            "target_position": 1100,
            "order_qty": 1000,
            "expected_current_qty": 100,
        }
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"600000.XSHG": normal_quote}, create=True):
            add_allowed, add_reason = joinquant_strategy._can_execute(
                holding_context, add_signal,
            )
        self.assertFalse(add_allowed)
        self.assertEqual(add_reason, "already_holding")

    def test_exact_buy_fails_closed_when_open_orders_or_numbers_are_unavailable(self) -> None:
        signal = {
            "id": "exact-1", "action": "buy", "code": "600000",
            "jq_code": "600000.XSHG", "position_pct": 10,
            "target_qty": 1000, "target_position": 1000,
            "order_qty": 1000, "expected_current_qty": 0,
            "account_scope_id": "scope-1", "client_order_id": "client-1",
            "execution_intent_sha256": "a" * 64,
            "pre_trade_result_id": "risk-1", "pre_trade_result_sha256": "b" * 64,
            "broker_snapshot_id": "broker-1", "broker_snapshot_sha256": "c" * 64,
            "quote_snapshot_id": "quote-1", "quote_snapshot_sha256": "d" * 64,
            "instrument_rules_sha256": "e" * 64,
            "expires_at": "2099-07-28T10:02:00+08:00",
            "price_cap": 10.1, "required_cash_yuan": 10100,
            "entry_price": 10.0, "price": 10.0, "final_score": 95,
        }
        context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000, available_cash=100_000, cash=100_000,
            positions={},
        ))
        quote = SimpleNamespace(
            last_price=10.0, paused=False, low_limit=9.0, high_limit=11.0,
        )
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"600000.XSHG": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", side_effect=RuntimeError("offline"), create=True):
            open_allowed, open_reason = joinquant_strategy._can_execute(context, signal)
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"600000.XSHG": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={"unknown": SimpleNamespace()}, create=True):
            malformed_allowed, malformed_reason = joinquant_strategy._can_execute(
                context, signal,
            )
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={
                 "600000.XSHG": SimpleNamespace(
                     last_price=float("nan"), paused=False, low_limit=9.0, high_limit=11.0,
                 ),
             }, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True):
            price_allowed, price_reason = joinquant_strategy._can_execute(context, signal)
        invalid_results = []
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"600000.XSHG": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True):
            for changes in (
                {"final_score": "not-a-number"},
                {"final_score": float("nan")},
                {"position_pct": float("nan")},
            ):
                invalid_results.append(joinquant_strategy._can_execute(
                    context, {**signal, **changes},
                ))

        self.assertFalse(open_allowed)
        self.assertEqual(open_reason, "open_orders_unavailable")
        self.assertFalse(malformed_allowed)
        self.assertEqual(malformed_reason, "open_orders_unavailable")
        self.assertFalse(price_allowed)
        self.assertEqual(price_reason, "price_invalid")
        self.assertEqual(
            invalid_results,
            [(False, "invalid_signal_numbers")] * 3,
        )

    def test_exact_buy_projects_total_exposure_at_the_frozen_price_cap(self) -> None:
        signal = {
            "id": "exact-cap", "action": "buy", "code": "600000",
            "jq_code": "600000.XSHG", "position_pct": 10,
            "target_qty": 1000, "target_position": 1000,
            "order_qty": 1000, "expected_current_qty": 0,
            "account_scope_id": "scope-1", "client_order_id": "client-cap",
            "execution_intent_sha256": "a" * 64,
            "pre_trade_result_id": "risk-1", "pre_trade_result_sha256": "b" * 64,
            "broker_snapshot_id": "broker-1", "broker_snapshot_sha256": "c" * 64,
            "quote_snapshot_id": "quote-1", "quote_snapshot_sha256": "d" * 64,
            "instrument_rules_sha256": "e" * 64,
            "expires_at": "2099-07-28T10:02:00+08:00",
            "price_cap": 10.1, "required_cash_yuan": 10100,
            "entry_price": 10.0, "price": 9.9, "final_score": 95,
        }
        context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000, available_cash=30_000, cash=30_000,
            positions={
                "000001.XSHE": SimpleNamespace(value=70_000),
            },
        ))
        quote = SimpleNamespace(
            last_price=9.9, paused=False, low_limit=9.0, high_limit=11.0,
        )

        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"600000.XSHG": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True):
            allowed, reason = joinquant_strategy._can_execute(context, signal)

        self.assertFalse(allowed)
        self.assertEqual(reason, "total_position_limit")

    def test_invalid_buy_age_does_not_block_a_later_protective_sell(self) -> None:
        now = joinquant_strategy.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        joinquant_strategy.g.signals = [
            {
                "id": "bad-buy", "action": "buy", "created_at": now,
                "max_age_min": "not-a-number",
            },
            {
                "id": "protective-sell", "action": "sell", "code": "000001",
                "jq_code": "000001.XSHE", "created_at": now,
                "max_age_min": 20, "target_qty": 0,
            },
        ]
        position = SimpleNamespace(
            value=1000, total_amount=100, closeable_amount=100,
        )
        context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000, available_cash=99_000, cash=99_000,
            positions={"000001.XSHE": position},
        ))
        quote = SimpleNamespace(
            last_price=10.0, paused=False, low_limit=9.0, high_limit=11.0,
        )
        order = SimpleNamespace(order_id="sell-1", status="held")

        with patch.object(joinquant_strategy, "_cancel_invalid_gap_reentry_orders"), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"000001.XSHE": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True), \
             patch.object(joinquant_strategy, "order_target", return_value=order, create=True) as submit, \
             patch.object(joinquant_strategy, "_record_order"):
            joinquant_strategy.execute_signals(context)

        submit.assert_called_once_with("000001.XSHE", 0)

    def test_gap_cleanup_failure_does_not_block_a_protective_sell(self) -> None:
        now = joinquant_strategy.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        joinquant_strategy.g.gap_reentry_orders["gap-1"] = {
            "id": "gap-buy", "action": "buy", "code": "600000",
            "jq_code": "600000.XSHG", "reentry_cap_price": 10.1,
        }
        joinquant_strategy.g.signals = [{
            "id": "protective-sell", "action": "sell", "code": "000001",
            "jq_code": "000001.XSHE", "created_at": now,
            "max_age_min": 20, "target_qty": 0,
        }]
        gap_order = SimpleNamespace(
            order_id="gap-1", security="600000.XSHG", amount=100, filled=0,
        )
        sell_position = SimpleNamespace(
            value=1000, total_amount=100, closeable_amount=100,
        )
        context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000, available_cash=99_000, cash=99_000,
            positions={"000001.XSHE": sell_position},
        ))
        sell_quote = SimpleNamespace(
            last_price=10.0, paused=False, low_limit=9.0, high_limit=11.0,
        )
        submitted = SimpleNamespace(order_id="sell-1", status="held")

        with patch.object(
            joinquant_strategy, "get_open_orders",
            side_effect=[{"gap-1": gap_order}, {"gap-1": gap_order}], create=True,
        ), patch.object(
            joinquant_strategy, "get_current_data",
            side_effect=[{}, {"000001.XSHE": sell_quote}], create=True,
        ), patch.object(
            joinquant_strategy, "order_target", return_value=submitted, create=True,
        ) as submit, patch.object(joinquant_strategy, "_record_order"):
            joinquant_strategy.execute_signals(context)

        submit.assert_called_once_with("000001.XSHE", 0)
        joinquant_strategy.log.warn.assert_called_once()

    def test_sell_requires_authoritative_open_order_state(self) -> None:
        signal = {
            "id": "protective-sell", "action": "sell", "code": "000001",
            "jq_code": "000001.XSHE", "target_qty": 0,
        }
        position = SimpleNamespace(
            value=1000, total_amount=100, closeable_amount=100,
        )
        context = SimpleNamespace(portfolio=SimpleNamespace(
            total_value=100_000, available_cash=99_000, cash=99_000,
            positions={"000001.XSHE": position},
        ))
        quote = SimpleNamespace(
            last_price=10.0, paused=False, low_limit=9.0, high_limit=11.0,
        )

        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"000001.XSHE": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", side_effect=RuntimeError("offline"), create=True):
            unknown = joinquant_strategy._can_execute(context, signal)
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"000001.XSHE": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={"unknown": SimpleNamespace()}, create=True):
            malformed = joinquant_strategy._can_execute(context, signal)
        with patch.object(joinquant_strategy, "_signal_is_fresh", return_value=True), \
             patch.object(joinquant_strategy, "get_current_data", return_value={"000001.XSHE": quote}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True):
            confirmed_empty = joinquant_strategy._can_execute(context, signal)

        self.assertEqual(unknown, (False, "open_orders_unavailable"))
        self.assertEqual(malformed, (False, "open_orders_unavailable"))
        self.assertEqual(confirmed_empty, (True, ""))

    def test_order_and_trade_callbacks_keep_client_order_id(self) -> None:
        signal = {
            "id": "exact-1", "client_order_id": "client-1",
            "action": "buy", "code": "600000", "jq_code": "600000.XSHG",
            "target_qty": 100,
        }
        order = SimpleNamespace(
            order_id="broker-1", security="600000.XSHG", is_buy=True,
            amount=100, filled=0, status="held", price=10.0,
        )
        trade = SimpleNamespace(
            trade_id="trade-1", order_id="broker-1", security="600000.XSHG",
            is_buy=True, amount=100, price=10.0, commission=5.0,
        )
        joinquant_strategy._record_order(signal, "submitted", order=order)

        with patch.object(joinquant_strategy, "get_orders", return_value={"broker-1": order}, create=True), \
             patch.object(joinquant_strategy, "get_trades", return_value={"trade-1": trade}, create=True):
            order_event = joinquant_strategy._platform_order_events()[0]
            trade_event = joinquant_strategy._platform_trade_events()[0]

        self.assertEqual(joinquant_strategy.g.order_events[-1]["client_order_id"], "client-1")
        self.assertEqual(order_event["client_order_id"], "client-1")
        self.assertEqual(trade_event["client_order_id"], "client-1")

    def test_snapshot_reconciles_platform_order_states(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
        self.assertIn("def _platform_order_events", text)
        self.assertIn("get_orders()", text)
        self.assertIn('"status": status', text)
        self.assertIn("g.order_signal_ids", text)

    def test_snapshot_reports_platform_trade_events(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
        self.assertIn("def _platform_trade_events", text)
        self.assertIn("get_trades()", text)
        self.assertIn('"trade_id":', text)
        self.assertIn('"commission":', text)
        self.assertIn('"trades": trade_events', text)

    def test_full_snapshot_is_not_posted_when_execution_collections_are_unknown(self) -> None:
        context = SimpleNamespace(portfolio=SimpleNamespace(
            cash=100_000, available_cash=100_000, total_value=100_000,
            positions={},
        ), run_params=SimpleNamespace(type="sim_trade"))
        joinquant_strategy.g.order_events = [{"id": "pending-local-event"}]
        with patch.object(joinquant_strategy, "get_trades", return_value={}, create=True), \
             patch.object(joinquant_strategy, "get_open_orders", return_value={}, create=True), \
             patch.object(joinquant_strategy, "get_orders", side_effect=RuntimeError("orders unavailable"), create=True), \
             patch.object(joinquant_strategy, "_post_json") as post:
            joinquant_strategy.post_account_snapshot(context)

        post.assert_not_called()
        self.assertEqual(
            joinquant_strategy.g.order_events,
            [{"id": "pending-local-event"}],
        )


if __name__ == "__main__":
    unittest.main()
