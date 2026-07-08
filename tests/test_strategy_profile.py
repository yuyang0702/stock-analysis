import unittest

from strategy_profile import build_strategy_profile


class StrategyProfileTest(unittest.TestCase):
    def test_short_and_mid_defaults_follow_design(self) -> None:
        short = build_strategy_profile("short")
        mid = build_strategy_profile("mid")

        self.assertEqual(short.mode, "short")
        self.assertEqual(mid.mode, "mid")
        self.assertAlmostEqual(short.stop_atr_mult, 1.8)
        self.assertAlmostEqual(short.take_profit_r, 2.0)
        self.assertAlmostEqual(short.max_loss_pct, 1.0)
        self.assertAlmostEqual(short.entry_confirm_pct, 0.3)
        self.assertAlmostEqual(mid.stop_atr_mult, 2.5)
        self.assertAlmostEqual(mid.take_profit_r, 3.0)
        self.assertAlmostEqual(mid.max_loss_pct, 1.5)
        self.assertAlmostEqual(mid.entry_confirm_pct, 1.0)


if __name__ == "__main__":
    unittest.main()
