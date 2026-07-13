from pathlib import Path
import unittest


class JoinQuantStrategyTemplateTest(unittest.TestCase):
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

        self.assertIn('STRATEGY_TEMPLATE_VERSION = "2026-07-14.1-ledger-v6"', text)
        self.assertIn('"strategy_template_version": STRATEGY_TEMPLATE_VERSION', text)

    def test_template_retries_pending_order_event_callback(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")

        self.assertIn("fetch_and_execute(context)\n    post_account_snapshot(context)", text)
        self.assertIn("return execute_signals(context)", text)
        self.assertIn('signal.get("max_age_min") or MAX_SIGNAL_AGE_MIN', text)

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
