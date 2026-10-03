import unittest
from pathlib import Path
from unittest.mock import patch

from backtest_sensitivity import run_cost_sensitivity
from historical_backtest import EquityPoint, HistoricalBacktestConfig, HistoricalBacktestResult
from historical_data import HistoricalStore


class BacktestSensitivityTest(unittest.TestCase):
    def test_runs_bounded_fee_and_slippage_grid(self) -> None:
        result = HistoricalBacktestResult(
            equity=[EquityPoint("2025-01-01", 100_000, 100_000), EquityPoint("2025-01-02", 101_000, 101_000)]
        )
        with patch("backtest_sensitivity.run_historical_backtest", return_value=result):
            report = run_cost_sensitivity(
                HistoricalStore(Path("unused.db")),
                "dataset",
                "2025-01-01",
                "2025-01-02",
                HistoricalBacktestConfig(),
                commission_multipliers=(1.0, 1.2),
                slippage_multipliers=(0.5, 1.0),
            )
        self.assertEqual(len(report["scenarios"]), 4)
        self.assertAlmostEqual(report["scenarios"][0]["metrics"]["net_return"], 0.01)


if __name__ == "__main__":
    unittest.main()
