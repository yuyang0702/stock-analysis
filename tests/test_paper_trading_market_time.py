from datetime import datetime
from unittest.mock import patch
import unittest

import pandas as pd

import a_share_strategy


class PaperTradingMarketTimeTest(unittest.TestCase):
    def test_a_share_trading_time_requires_weekday_and_session(self) -> None:
        self.assertTrue(a_share_strategy.is_a_share_trading_time(datetime(2026, 7, 7, 10, 0)))
        self.assertTrue(a_share_strategy.is_a_share_trading_time(datetime(2026, 7, 7, 13, 30)))
        self.assertFalse(a_share_strategy.is_a_share_trading_time(datetime(2026, 7, 7, 9, 29)))
        self.assertTrue(a_share_strategy.is_a_share_trading_time(datetime(2026, 7, 7, 9, 30)))
        self.assertFalse(a_share_strategy.is_a_share_trading_time(datetime(2026, 7, 7, 12, 0)))
        self.assertFalse(a_share_strategy.is_a_share_trading_time(datetime(2026, 7, 11, 10, 0)))

    def test_runtime_phase_does_not_treat_call_auction_as_intraday(self) -> None:
        self.assertEqual(a_share_strategy.resolve_runtime_phase(datetime(2026, 7, 7, 0, 12)), "closed")
        self.assertEqual(a_share_strategy.resolve_runtime_phase(datetime(2026, 7, 7, 9, 14, 59)), "closed")
        self.assertEqual(a_share_strategy.resolve_runtime_phase(datetime(2026, 7, 7, 9, 15)), "pre")
        self.assertEqual(a_share_strategy.resolve_runtime_phase(datetime(2026, 7, 7, 9, 29)), "pre")
        self.assertEqual(a_share_strategy.resolve_runtime_phase(datetime(2026, 7, 7, 9, 30)), "intraday")
        self.assertEqual(a_share_strategy.resolve_runtime_phase(datetime(2026, 7, 7, 12, 0)), "lunch")
        self.assertEqual(a_share_strategy.resolve_runtime_phase(datetime(2026, 7, 7, 13, 0)), "intraday")
        self.assertEqual(a_share_strategy.resolve_runtime_phase(datetime(2026, 7, 11, 10, 0)), "closed")

    def test_runtime_wake_never_crosses_market_phase_boundary(self) -> None:
        self.assertEqual(
            a_share_strategy.next_runtime_wake(
                datetime(2026, 7, 7, 9, 29, 50), "pre", 300, 30
            ),
            datetime(2026, 7, 7, 9, 30),
        )
        self.assertEqual(
            a_share_strategy.next_runtime_wake(
                datetime(2026, 7, 7, 11, 31), "lunch", 300, 30
            ),
            datetime(2026, 7, 7, 13, 0),
        )

    def test_local_paper_trading_skips_outside_trading_time(self) -> None:
        cfg = a_share_strategy.Config()
        rows = pd.DataFrame([{"code": "600000", "price": 10, "position_pct": 10, "final_score": 90}])

        with patch("a_share_strategy.apply_paper_trades") as apply_mock:
            md = a_share_strategy.run_paper_trading(
                cfg,
                rows,
                now=datetime(2026, 7, 7, 12, 0),
            )

        apply_mock.assert_not_called()
        self.assertIn("非A股交易时间", md)


if __name__ == "__main__":
    unittest.main()
