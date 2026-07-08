import unittest

import pandas as pd

from paper_trading import apply_paper_trades, new_account, summarize_account


class PaperTradingTest(unittest.TestCase):
    def test_buys_100_share_lots_from_signal_position_pct(self) -> None:
        account = new_account(100_000)
        rows = pd.DataFrame(
            [
                {
                    "code": "600000",
                    "name": "示例股",
                    "price": 10.0,
                    "entry_price": 10.0,
                    "stop_loss": 9.5,
                    "take_profit": 11.0,
                    "position_pct": 10,
                    "final_score": 90,
                    "signal_action": "continue",
                }
            ]
        )

        events = apply_paper_trades(account, rows, trade_date="2026-07-07", commission_rate=0, stamp_tax_rate=0, slippage_pct=0)

        self.assertEqual(events[0]["action"], "buy")
        self.assertEqual(account["positions"]["600000"]["qty"], 1000)
        self.assertEqual(account["cash"], 90_000)

    def test_t_plus_one_blocks_same_day_sell(self) -> None:
        account = new_account(100_000)
        apply_paper_trades(
            account,
            pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "示例股",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 10,
                        "final_score": 90,
                    }
                ]
            ),
            trade_date="2026-07-07",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )

        events = apply_paper_trades(
            account,
            pd.DataFrame([{"code": "600000", "name": "示例股", "price": 9.4, "stop_loss": 9.5, "take_profit": 11.0}]),
            trade_date="2026-07-07",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )

        self.assertEqual(events, [])
        self.assertEqual(account["positions"]["600000"]["qty"], 1000)

    def test_sells_next_day_when_stop_loss_hits(self) -> None:
        account = new_account(100_000)
        apply_paper_trades(
            account,
            pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "示例股",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 10,
                        "final_score": 90,
                    }
                ]
            ),
            trade_date="2026-07-07",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )

        events = apply_paper_trades(
            account,
            pd.DataFrame([{"code": "600000", "name": "示例股", "price": 9.4, "stop_loss": 9.5, "take_profit": 11.0}]),
            trade_date="2026-07-08",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )

        self.assertEqual(events[0]["action"], "sell")
        self.assertEqual(events[0]["reason"], "stop_loss")
        self.assertNotIn("600000", account["positions"])
        self.assertEqual(account["cash"], 99_400)

    def test_summary_marks_equity_and_realized_pnl(self) -> None:
        account = new_account(100_000)
        apply_paper_trades(
            account,
            pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "示例股",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 10,
                        "final_score": 90,
                    }
                ]
            ),
            trade_date="2026-07-07",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )
        apply_paper_trades(
            account,
            pd.DataFrame([{"code": "600000", "name": "示例股", "price": 11.2, "stop_loss": 9.5, "take_profit": 11.0}]),
            trade_date="2026-07-08",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )

        summary = summarize_account(account)

        self.assertEqual(summary["equity"], 101_200)
        self.assertEqual(summary["realized_pnl"], 1200)
        self.assertEqual(summary["closed_trades"], 1)

    def test_realistic_costs_reduce_cash_and_pnl(self) -> None:
        account = new_account(100_000)

        apply_paper_trades(
            account,
            pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "示例股",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 10,
                        "final_score": 90,
                    }
                ]
            ),
            trade_date="2026-07-07",
            commission_rate=0.0003,
            stamp_tax_rate=0.001,
            slippage_pct=0.001,
        )
        events = apply_paper_trades(
            account,
            pd.DataFrame([{"code": "600000", "name": "示例股", "price": 11.2, "stop_loss": 9.5, "take_profit": 11.0}]),
            trade_date="2026-07-08",
            commission_rate=0.0003,
            stamp_tax_rate=0.001,
            slippage_pct=0.001,
        )

        self.assertLess(events[0]["pnl"], 1200)
        self.assertGreater(events[0]["fees"], 0)

    def test_limit_up_buy_and_limit_down_sell_are_blocked(self) -> None:
        account = new_account(100_000)

        buy_events = apply_paper_trades(
            account,
            pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "示例股",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 10,
                        "final_score": 90,
                        "limit_quality": "一字涨停",
                    }
                ]
            ),
            trade_date="2026-07-07",
        )
        self.assertEqual(buy_events, [])

        apply_paper_trades(
            account,
            pd.DataFrame(
                [
                    {
                        "code": "600001",
                        "name": "示例股2",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 10,
                        "final_score": 90,
                    }
                ]
            ),
            trade_date="2026-07-07",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )
        sell_events = apply_paper_trades(
            account,
            pd.DataFrame([{"code": "600001", "name": "示例股2", "price": 9.4, "pct_chg": -10.0, "stop_loss": 9.5}]),
            trade_date="2026-07-08",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )
        self.assertEqual(sell_events, [])
        self.assertIn("600001", account["positions"])

    def test_stop_loss_adds_code_cooldown(self) -> None:
        account = new_account(100_000)
        apply_paper_trades(
            account,
            pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "示例股",
                        "price": 10.0,
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.0,
                        "position_pct": 10,
                        "final_score": 90,
                    }
                ]
            ),
            trade_date="2026-07-07",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )
        apply_paper_trades(
            account,
            pd.DataFrame([{"code": "600000", "name": "示例股", "price": 9.4, "stop_loss": 9.5, "take_profit": 11.0}]),
            trade_date="2026-07-08",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
            cooldown_days=3,
        )

        events = apply_paper_trades(
            account,
            pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "示例股",
                        "price": 9.8,
                        "entry_price": 9.8,
                        "stop_loss": 9.2,
                        "take_profit": 10.8,
                        "position_pct": 10,
                        "final_score": 90,
                    }
                ]
            ),
            trade_date="2026-07-09",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
        )

        self.assertEqual(events, [])
        self.assertEqual(account["cooldown"]["600000"], "2026-07-11")

    def test_portfolio_caps_limit_new_positions(self) -> None:
        account = new_account(100_000)
        rows = pd.DataFrame(
            [
                {
                    "code": "600000",
                    "name": "示例股",
                    "price": 10.0,
                    "entry_price": 10.0,
                    "stop_loss": 9.5,
                    "take_profit": 11.0,
                    "position_pct": 50,
                    "final_score": 90,
                },
                {
                    "code": "600001",
                    "name": "示例股2",
                    "price": 10.0,
                    "entry_price": 10.0,
                    "stop_loss": 9.5,
                    "take_profit": 11.0,
                    "position_pct": 50,
                    "final_score": 90,
                },
            ]
        )

        events = apply_paper_trades(
            account,
            rows,
            trade_date="2026-07-07",
            commission_rate=0,
            stamp_tax_rate=0,
            slippage_pct=0,
            max_positions=1,
            max_position_pct=20,
            max_total_position_pct=30,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["qty"], 2000)
        self.assertEqual(summarize_account(account)["position_count"], 1)


if __name__ == "__main__":
    unittest.main()
