import unittest

import a_share_strategy
import paper_trading


class JoinQuantNotificationTest(unittest.TestCase):
    def test_joinquant_simulated_order_markdown_is_distinct_from_local_paper_account(self) -> None:
        payload = {
            "run_id": "run-1",
            "trade_date": "2026-07-07",
            "dry_run": False,
            "signals": [
                {
                    "code": "600000",
                    "jq_code": "600000.XSHG",
                    "name": "PF Bank",
                    "action": "buy",
                    "position_pct": 12.5,
                    "price": 10.1,
                    "final_score": 88,
                    "enhanced_score": 91.5,
                    "reason": "signal",
                },
                {
                    "code": "000001",
                    "jq_code": "000001.XSHE",
                    "name": "PA Bank",
                    "action": "sell",
                    "price": 12.3,
                    "reason": "stop_loss",
                },
            ],
        }

        md = a_share_strategy.build_joinquant_dry_run_markdown(payload)

        self.assertIn("JoinQuant 模拟盘", md)
        self.assertIn("JoinQuant 模拟盘执行", md)
        self.assertIn("不是本地模拟盘", md)
        self.assertIn("计划买入", md)
        self.assertIn("计划卖出", md)
        self.assertIn("600000.XSHG", md)
        self.assertIn("分数 88", md)
        self.assertIn("影子 91.5", md)

    def test_local_paper_markdown_has_distinct_marker(self) -> None:
        account = paper_trading.new_account(100_000)

        md = paper_trading.build_paper_trade_markdown(account, [])

        self.assertIn("本地模拟盘", md)
        self.assertIn("不是 JoinQuant", md)

    def test_empty_joinquant_plan_shows_reject_diagnostics(self) -> None:
        payload = {
            "run_id": "run-empty",
            "trade_date": "2026-07-07",
            "dry_run": False,
            "signals": [],
            "diagnostics": {
                "candidate_count": 3,
                "allow_buy": False,
                "min_score": 75.0,
                "reject_reasons": {
                    "buy_disabled": 2,
                    "sell_without_holding": 1,
                },
            },
        }

        md = a_share_strategy.build_joinquant_dry_run_markdown(payload)

        self.assertIn("候选 3 只", md)
        self.assertIn("非交易时间禁止买入 2", md)
        self.assertIn("未持仓不卖出 1", md)


if __name__ == "__main__":
    unittest.main()
