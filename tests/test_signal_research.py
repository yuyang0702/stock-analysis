import unittest

from signal_research import _bucket, _metric, _spearman
from signal_calibration import calibrate_score_buckets


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

    def test_spearman_ic_detects_monotonic_factor(self) -> None:
        self.assertAlmostEqual(_spearman([(1, 0.1), (2, 0.2), (3, 0.3)]), 1.0)

    def test_score_calibration_shrinks_small_buckets_and_marks_training_only(self) -> None:
        report = {
            "horizon_metrics": {"5": {"mean": 0.01}},
            "by_horizon": {
                "5": {
                    "score_bucket": {
                        "70-75": {"count": 100, "mean": 0.02},
                        "95-100": {"count": 2, "mean": 0.20},
                    }
                }
            },
        }
        result = calibrate_score_buckets(report, horizon=5, min_samples=20)
        self.assertEqual(result["decision_use"], "training_evidence_only")
        self.assertTrue(result["buckets"]["70-75"]["eligible"])
        self.assertFalse(result["buckets"]["95-100"]["eligible"])
        self.assertLess(result["buckets"]["95-100"]["calibrated_net_mean_bps"], 2000)


if __name__ == "__main__":
    unittest.main()
