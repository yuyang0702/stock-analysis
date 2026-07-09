import unittest

import pandas as pd

import a_share_strategy


class AlertMarkdownTest(unittest.TestCase):
    def test_buy_alert_shows_current_price_and_suggested_entry_separately(self) -> None:
        row = pd.Series(
            {
                "code": "600000",
                "name": "PF Bank",
                "price": 10.12,
                "entry_price": 10.50,
                "stop_loss": 9.80,
                "take_profit": 11.20,
                "position_pct": 10,
                "risk_reward": 2.3,
                "risk_confidence": 0.72,
                "risk_reason": "测试",
                "theme_label": "银行",
                "theme_heat_level": "中",
            }
        )

        md = a_share_strategy.build_alert_markdown(row, "买点", {"state": "强势进攻"}, "无", "intraday")

        self.assertIn("当前价：10.12", md)
        self.assertIn("建议入场：10.50", md)
        self.assertIn("距入场：+3.75%，未到确认位", md)
        self.assertNotIn("- 入场：10.50", md)

    def test_buy_alert_shows_entry_reached_when_current_price_is_above_entry(self) -> None:
        row = pd.Series(
            {
                "code": "600000",
                "name": "PF Bank",
                "price": 10.62,
                "entry_price": 10.50,
                "stop_loss": 9.80,
                "take_profit": 11.20,
                "position_pct": 10,
                "risk_reward": 2.3,
                "risk_confidence": 0.72,
                "risk_reason": "测试",
                "theme_label": "银行",
                "theme_heat_level": "中",
            }
        )

        md = a_share_strategy.build_alert_markdown(row, "买点", {"state": "强势进攻"}, "无", "intraday")

        self.assertIn("入场状态：已到确认位", md)

    def test_alert_marks_invalid_take_profit_when_not_above_entry(self) -> None:
        row = pd.Series(
            {
                "code": "600000",
                "name": "PF Bank",
                "price": 10.00,
                "entry_price": 10.00,
                "stop_loss": 9.50,
                "take_profit": 10.00,
                "position_pct": 10,
                "risk_reward": 0,
                "risk_confidence": 0.72,
                "risk_reason": "上方压力太近",
                "theme_label": "银行",
                "theme_heat_level": "中",
            }
        )

        md = a_share_strategy.build_alert_markdown(row, "买点", {"state": "强势进攻"}, "无", "intraday")

        self.assertIn("止盈：无有效空间", md)
        self.assertIn("上方空间不足", md)

    def test_summary_marks_invalid_take_profit_when_not_above_entry(self) -> None:
        rows = pd.DataFrame(
            [
                {
                    "code": "600000",
                    "name": "PF Bank",
                    "mode": "intraday",
                    "entry_price": 10.0,
                    "stop_loss": 9.5,
                    "take_profit": 10.0,
                    "position_pct": 10,
                    "has_holding": False,
                    "risk_reason": "上方压力太近",
                }
            ]
        )

        md = a_share_strategy.build_summary_markdown(rows, {"state": "强势进攻"}, "无", a_share_strategy.Config())

        self.assertIn("止盈 无有效空间", md)
        self.assertIn("上方空间不足", md)
        self.assertNotIn("止盈 10.00", md)

    def test_intraday_buy_rows_exclude_limit_up_candidates(self) -> None:
        rows = pd.DataFrame(
            [
                {
                    "code": "600000",
                    "buy_state": "已到买点",
                    "limit_quality": "强势拉升",
                    "final_score": 80,
                    "pct_chg": 9.95,
                },
                {
                    "code": "000001",
                    "buy_state": "已到买点",
                    "limit_quality": "强势拉升",
                    "final_score": 80,
                    "pct_chg": 5.2,
                },
            ]
        )

        selected = a_share_strategy.select_intraday_buy_rows(rows, a_share_strategy.Config())

        self.assertEqual(selected["code"].tolist(), ["000001"])

    def test_intraday_buy_rows_exclude_invalid_take_profit(self) -> None:
        rows = pd.DataFrame(
            [
                {
                    "code": "600000",
                    "buy_state": "已到买点",
                    "limit_quality": "强势拉升",
                    "final_score": 80,
                    "pct_chg": 5.2,
                    "entry_price": 10.0,
                    "take_profit": 10.0,
                },
                {
                    "code": "000001",
                    "buy_state": "已到买点",
                    "limit_quality": "强势拉升",
                    "final_score": 80,
                    "pct_chg": 5.2,
                    "entry_price": 10.0,
                    "take_profit": 11.0,
                },
            ]
        )

        selected = a_share_strategy.select_intraday_buy_rows(rows, a_share_strategy.Config())

        self.assertEqual(selected["code"].tolist(), ["000001"])


if __name__ == "__main__":
    unittest.main()
