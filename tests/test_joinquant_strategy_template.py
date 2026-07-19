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
            "target_qty": 100, "entry_path": "gap_reentry",
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
        self.assertIn("order_target_value", text)
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

        self.assertIn('STRATEGY_TEMPLATE_VERSION = "2026-07-18.1-gap-reentry"', text)
        self.assertIn('JOINQUANT_TEMPLATE_VERSION = "2026-07-18.1-gap-reentry"', config_text)
        self.assertIn('"Authorization": "Bearer " + SYNC_TOKEN', text)
        self.assertIn('"strategy_template_version": STRATEGY_TEMPLATE_VERSION', text)

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
        self.assertIn("_ensure_runtime_state(context)\n    fetch_and_execute(context)", text)
        self.assertIn('if not isinstance(getattr(g, "order_signal_ids", None), dict):', text)

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
        self.assertIn('"trades": _platform_trade_events()', text)


if __name__ == "__main__":
    unittest.main()
