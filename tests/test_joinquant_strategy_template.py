from pathlib import Path
import unittest


class JoinQuantStrategyTemplateTest(unittest.TestCase):
    def test_template_defaults_to_joinquant_simulated_orders(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")

        self.assertIn("DRY_RUN = False", text)
        self.assertIn("def handle_data(context, data):", text)
        self.assertIn("fetch_and_execute(context)", text)
        self.assertNotIn('run_daily(execute_signals, time="09:35")', text)
        self.assertIn("order_target_percent", text)
        self.assertIn("order_target(jq_code, 0)", text)
        self.assertIn('return False, "not_holding"', text)
        self.assertIn('if reason == "duplicate":', text)
        self.assertIn("return event_count", text)
        self.assertIn("g.order_events", text)
        self.assertIn('"orders":', text)
        self.assertIn("record order", text)
        self.assertIn("post snapshot ok", text)

    def test_template_retries_pending_order_event_callback(self) -> None:
        text = Path("joinquant_strategy.py").read_text(encoding="utf-8")

        self.assertIn('if event_count or getattr(g, "order_events", []):', text)


if __name__ == "__main__":
    unittest.main()
