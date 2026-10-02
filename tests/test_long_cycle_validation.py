import unittest

from long_cycle_validation import (
    _equal_weight_benchmark,
    _production_gates,
    series_metrics,
)


class LongCycleValidationTest(unittest.TestCase):
    def test_series_metrics_reports_risk_and_drawdown_duration(self) -> None:
        metrics = series_metrics([100, 110, 99, 105])
        self.assertEqual(metrics["observations"], 4)
        self.assertAlmostEqual(metrics["net_return"], 0.05)
        self.assertGreater(metrics["max_drawdown"], 0.09)
        self.assertGreaterEqual(metrics["max_drawdown_days"], 1)
        self.assertIn("sharpe", metrics)
        self.assertIn("sortino", metrics)

    def test_equal_weight_benchmark_is_explicitly_proxy(self) -> None:
        rows = [
            {"trade_date": "2025-01-02", "code": "000001", "close": 10, "amount": 100000, "suspended": 0, "st": 0, "limit_up": 11, "limit_down": 9},
            {"trade_date": "2025-01-02", "code": "600000", "close": 20, "amount": 100000, "suspended": 0, "st": 0, "limit_up": 22, "limit_down": 18},
            {"trade_date": "2025-01-03", "code": "000001", "close": 11, "amount": 100000, "suspended": 0, "st": 0, "limit_up": 12, "limit_down": 10},
            {"trade_date": "2025-01-03", "code": "600000", "close": 19, "amount": 100000, "suspended": 0, "st": 0, "limit_up": 21, "limit_down": 17},
        ]
        benchmark = _equal_weight_benchmark(rows, 100000)
        self.assertTrue(benchmark["proxy_only"])
        self.assertEqual(benchmark["code_count"], 2)
        self.assertAlmostEqual(benchmark["metrics"]["net_return"], 0.025)

    def test_proxy_or_unobserved_gate_cannot_be_production_ready(self) -> None:
        result = _production_gates(
            quality={"accepted": True, "proxy_only": True},
            result_metrics={"net_return": 0.4},
            benchmark_metrics={"net_return": -0.1},
            stress=[
                {"label": "double_fees", "metrics": {"net_return": 0.1}},
                {"label": "double_slippage", "metrics": {"net_return": 0.1}},
            ],
            walk_forward={"holdout": {"metrics": {"net_return": 0.1}}},
        )
        self.assertFalse(result["production_ready"])
        self.assertFalse(result["gates"]["strict_point_in_time_data"])
        self.assertFalse(result["gates"]["broker_execution_observed"])


if __name__ == "__main__":
    unittest.main()
