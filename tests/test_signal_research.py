import unittest

from signal_research import _bucket, _metric


class SignalResearchTest(unittest.TestCase):
    def test_metric_reports_cost_adjusted_return_summary(self) -> None:
        result = _metric([0.02, -0.01, 0.03])
        self.assertEqual(result["count"], 3)
        self.assertAlmostEqual(result["mean"], 0.01333333)
        self.assertAlmostEqual(result["win_rate"], 2 / 3)
        self.assertGreater(result["profit_factor"], 4.0)

    def test_score_bucket_is_stable(self) -> None:
        self.assertEqual(_bucket(79.99), "75-80")
        self.assertEqual(_bucket(80.0), "80-85")


if __name__ == "__main__":
    unittest.main()
