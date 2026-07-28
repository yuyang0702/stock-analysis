from datetime import datetime
from unittest.mock import patch
import unittest

import pandas as pd

import a_share_strategy
from paper_trading import new_account


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

        with (
            patch("a_share_strategy.load_account", return_value=new_account(10_000)),
            patch("a_share_strategy.save_account") as save_mock,
            patch("a_share_strategy.apply_paper_trades") as apply_mock,
        ):
            md = a_share_strategy.run_paper_trading(
                cfg,
                rows,
                now=datetime(2026, 7, 7, 12, 0),
            )

        apply_mock.assert_not_called()
        self.assertIn("非A股交易时间", md)


    def test_after_hours_report_stamps_current_fee_contract_before_return(self) -> None:
        cfg = a_share_strategy.Config(
            mode="after",
            paper_trade_commission_rate=0.0007,
            paper_trade_stamp_tax_rate=0.0008,
            paper_trade_slippage_pct=0.0002,
        )
        account = new_account(10_000)
        account.update(
            {
                "active_fee_schedule_version": "old-v1",
                "active_fee_schedule_sha256": "a" * 64,
                "active_fee_schedule": {
                    "version": "old-v1", "contract_sha256": "a" * 64,
                },
                "fee_schedule_version": "old-v1",
                "fee_schedule_sha256": "a" * 64,
            }
        )

        with (
            patch("a_share_strategy.load_account", return_value=account),
            patch("a_share_strategy.save_account") as save_mock,
            patch("a_share_strategy.apply_paper_trades") as apply_mock,
        ):
            markdown = a_share_strategy.run_paper_trading(
                cfg, pd.DataFrame(), now=datetime(2026, 7, 7, 16, 0)
            )

        apply_mock.assert_not_called()
        save_mock.assert_called_once()
        save_mock.assert_called_once()
        self.assertNotEqual(account["active_fee_schedule_sha256"], "a" * 64)
        self.assertEqual(
            account["active_fee_schedule"]["buy_commission_rate"], "0.0007"
        )
        self.assertIn(account["active_fee_schedule_version"], markdown)
        self.assertIn(account["active_fee_schedule_sha256"], markdown)


if __name__ == "__main__":
    unittest.main()
