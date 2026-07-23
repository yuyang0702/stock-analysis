import unittest

import pandas as pd

import a_share_strategy
import exit_policy
from risk_engine import build_risk_decision, classify_trade_mode
from strategy_profile import build_strategy_profile


class RiskEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {
            "code": "600000",
            "name": "示例股",
            "price": 10.0,
            "high": 10.2,
            "low": 9.8,
            "open": 9.95,
            "prev_close": 9.85,
            "amount": 120_000_000,
            "pct_chg": 5.8,
            "turnover": 8.2,
            "ma5": 9.72,
            "ma10": 9.45,
            "ma20": 9.08,
            "ma30": 8.92,
            "atr14": 0.5,
            "support_level": 9.15,
            "pressure_level": 10.0,
            "market_state": "强势进攻",
            "trend_state": "强势上行",
            "limit_quality": "封板较强",
            "news_score": 8,
            "holding_brief": "未持仓",
            "has_holding": False,
        }
        self.market_info = {"state": "强势进攻", "sh_pct": 1.2}

    def test_classify_breakout_stock_as_short(self) -> None:
        self.assertEqual(classify_trade_mode(self.row, self.market_info, ""), "short")

    def test_build_breakout_risk_decision(self) -> None:
        profile = build_strategy_profile("short")
        decision = build_risk_decision(self.row, self.market_info, profile=profile)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.mode, "short")
        self.assertGreater(decision.entry_price, 10.0)
        self.assertLess(decision.stop_loss, decision.entry_price)
        self.assertGreater(decision.take_profit, decision.entry_price)
        self.assertGreater(decision.risk_reward, 1.5)
        self.assertGreater(decision.position_pct, 0)
        self.assertIn("突破", decision.reason)

    def test_build_risk_decision_accepts_series_holding(self) -> None:
        holding = pd.Series({"current_price": 10.1, "cost_price": 9.5})

        decision = build_risk_decision(
            self.row,
            self.market_info,
            profile=build_strategy_profile("short"),
            holding=holding,
        )

        self.assertGreaterEqual(decision.entry_price, 10.1)

    def test_weak_market_blocks_trade(self) -> None:
        weak_row = dict(self.row)
        weak_row["market_state"] = "风险释放"
        weak_market = {"state": "风险释放", "sh_pct": -1.5}
        profile = build_strategy_profile("mid")
        decision = build_risk_decision(weak_row, weak_market, profile=profile)

        self.assertFalse(decision.allowed)
        self.assertLess(decision.confidence, 0.5)

    def test_mid_mode_rejects_price_far_above_support(self) -> None:
        chase_row = dict(self.row)
        chase_row.update(
            {
                "price": 12.0,
                "pct_chg": 6.5,
                "support_level": 10.0,
                "pressure_level": 13.5,
                "trend_state": "趋势修复",
                "limit_quality": "强势拉升",
                "news_score": 3,
            }
        )
        profile = build_strategy_profile("mid")
        decision = build_risk_decision(chase_row, self.market_info, profile=profile)

        self.assertFalse(decision.allowed)
        self.assertIn("追高", decision.reason)

    def test_mid_mode_uses_pressure_to_reject_bad_real_risk_reward(self) -> None:
        cramped_row = dict(self.row)
        cramped_row.update(
            {
                "price": 10.0,
                "support_level": 9.8,
                "pressure_level": 10.15,
                "atr14": 0.35,
                "pct_chg": 3.5,
                "trend_state": "趋势修复",
                "limit_quality": "强势拉升",
                "news_score": 2,
            }
        )
        profile = build_strategy_profile("mid")
        decision = build_risk_decision(cramped_row, self.market_info, profile=profile)

        self.assertFalse(decision.allowed)
        self.assertLess(decision.risk_reward, profile.min_risk_reward)

    def test_risk_bundle_exposes_current_execution_rejection(self) -> None:
        row = pd.Series({
            "code": "600000",
            "price": 10.0,
            "amount": 100_000_000,
            "support_level": 9.6,
            "atr14": 0.2,
            "trend_state": "明显破坏",
            "market_state": "NORMAL",
            "has_holding": False,
        })

        bundle = a_share_strategy.build_risk_bundle(
            row,
            {"state": "NORMAL"},
            "",
        )

        self.assertFalse(bundle["execution_allowed"])
        self.assertEqual(
            bundle["execution_plan_version"],
            exit_policy.EXECUTION_PLAN_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
