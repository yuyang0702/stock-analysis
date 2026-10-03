import unittest
from execution_contracts import FeeSchedule
from historical_strategy import Candidate
from local_entry_policy import LocalEntryPolicy, aligned_correlation, check_local_entry


class LocalEntryPolicyTest(unittest.TestCase):
    def setUp(self):
        self.fees = FeeSchedule.simulation(version="local-policy-test-v1")
        self.policy = LocalEntryPolicy(
            require_correlation_history=False,
            max_round_trip_cost_rate=0.03,
        )

    @staticmethod
    def candidate(*, target=12.0):
        return Candidate(
            "600000", 90.0, 10.0, 10.0, 9.0, target, 0.3,
            "short", "NORMAL", "bank", "value", {},
        )

    def test_aligned_correlation_uses_common_dates(self):
        left = {str(index): float(index) for index in range(20)}
        right = {str(index): float(index) * 2 for index in range(5, 25)}
        self.assertAlmostEqual(aligned_correlation(left, right, 10), 1.0)

    def test_rejects_price_gap_before_economic_checks(self):
        reason = check_local_entry(
            candidate=self.candidate(), price=10.30, quantity=100,
            equity=100_000, holdings=(), returns={}, fees=self.fees,
            policy=self.policy,
        )
        self.assertEqual(reason, "BUY_PRICE_GAP_LIMIT")

    def test_rejects_target_that_does_not_cover_round_trip_cost(self):
        reason = check_local_entry(
            candidate=self.candidate(target=10.20), price=10.0, quantity=100,
            equity=100_000, holdings=(), returns={}, fees=self.fees,
            policy=self.policy,
        )
        self.assertEqual(reason, "BUY_TARGET_COST_COVERAGE")

    def test_accepts_cost_covered_candidate(self):
        reason = check_local_entry(
            candidate=self.candidate(), price=10.0, quantity=100,
            equity=100_000, holdings=(), returns={}, fees=self.fees,
            policy=self.policy,
        )
        self.assertEqual(reason, "")

    def test_rejects_calibrated_expected_return_below_net_threshold(self):
        candidate = Candidate(
            "600000", 90.0, 10.0, 10.0, 9.0, 12.0, 0.3,
            "short", "NORMAL", "bank", "value", {"expected_gross_return_bps": 30},
        )
        reason = check_local_entry(
            candidate=candidate, price=10.0, quantity=100,
            equity=100_000, holdings=(), returns={}, fees=self.fees,
            policy=self.policy,
        )
        self.assertEqual(reason, "BUY_EXPECTED_NET_RETURN_BELOW_THRESHOLD")


if __name__ == "__main__":
    unittest.main()
