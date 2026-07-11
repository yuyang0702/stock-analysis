import unittest

from pre_trade_check import PortfolioState, RiskLimits, evaluate_observation


class PreTradeCheckTest(unittest.TestCase):
    def test_valid_sell_without_position_pct_is_allowed(self) -> None:
        result = evaluate_observation(
            {"action": "sell", "code": "600000", "price": 10.5},
            PortfolioState.empty(),
            RiskLimits(),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.hard_blocks, ())

    def test_portfolio_sector_exposure_is_defensively_immutable(self) -> None:
        exposure = {"semiconductor": 30}
        portfolio = PortfolioState(sector_exposure_pct=exposure)
        exposure["semiconductor"] = 99
        self.assertEqual(portfolio.sector_exposure_pct["semiconductor"], 30)
        with self.assertRaises(TypeError):
            portfolio.sector_exposure_pct["semiconductor"] = 40

    def test_result_metrics_are_defensively_immutable(self) -> None:
        metrics = {"value": 1}
        from pre_trade_check import RiskCheckResult

        result = RiskCheckResult(True, (), (), metrics)
        metrics["value"] = 2
        self.assertEqual(result.metrics["value"], 1)
        with self.assertRaises(TypeError):
            result.metrics["value"] = 3

    def test_soft_limit_warnings_do_not_block_signal(self) -> None:
        result = evaluate_observation(
            {"action": "buy", "position_pct": 40, "sector": "半导体"},
            PortfolioState(
                total_position_pct=90, cash_reserve_pct=10,
                sector_exposure_pct={"半导体": 30}, new_positions_today=10,
                orders_today=50, daily_turnover_pct=190,
                daily_pnl_pct=-6, account_drawdown_pct=-16,
            ),
            RiskLimits(),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.hard_blocks, ())
        self.assertIn("SINGLE_POSITION_LIMIT", result.soft_warnings)
        self.assertIn("TOTAL_POSITION_LIMIT", result.soft_warnings)
        self.assertIn("DAILY_LOSS_WARNING", result.soft_warnings)

    def test_invalid_signal_is_a_hard_block(self) -> None:
        result = evaluate_observation(
            {"action": "buy", "position_pct": 10, "price": 0},
            PortfolioState.empty(),
            RiskLimits(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.hard_blocks, ("INVALID_ORDER_INPUT",))


if __name__ == "__main__":
    unittest.main()
