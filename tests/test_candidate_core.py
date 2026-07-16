import unittest

import pandas as pd

from candidate_core import CandidatePoolConfig, build_candidate_pool, score_candidate_frame
from a_share_strategy import Config, build_pool


class CandidateCoreTest(unittest.TestCase):
    def test_live_score_keeps_average_rank_semantics_for_ties(self) -> None:
        rows = pd.DataFrame(
            [
                {"score": 70, "news_score": 0, "pct_chg": 3, "turnover": 4},
                {"score": 70, "news_score": 0, "pct_chg": 3, "turnover": 2},
                {"score": 68, "news_score": 0, "pct_chg": 1, "turnover": 2},
            ]
        )

        scored = score_candidate_frame(rows)

        self.assertEqual(
            [round(value, 4) for value in scored["final_score"]],
            [76.1667, 75.1667, 70.6667],
        )

    def test_shared_pool_keeps_top_30_and_reproduces_live_score(self) -> None:
        rows = pd.DataFrame(
            [
                {
                    "code": f"{i:06d}",
                    "name": "普通股",
                    "price": 10,
                    "amount": 1e8 + i,
                    "pct_chg": 4 + i / 100,
                    "turnover": 2 + i / 100,
                    "score": 70 + i / 10,
                    "news_score": i % 3,
                }
                for i in range(40)
            ]
        )

        pool = build_candidate_pool(
            rows, CandidatePoolConfig("intraday", 2, 5e7, 30)
        )
        scored = score_candidate_frame(pool)

        self.assertEqual(len(scored), 30)
        expected = scored["score"] + scored["news_score"] * 1.2
        expected += scored["pct_chg"].rank(pct=True) * 5
        expected += scored["turnover"].rank(pct=True) * 2
        pd.testing.assert_series_equal(
            scored["final_score"], expected, check_names=False
        )

    def test_pool_preserves_mode_filters_and_does_not_mutate_input(self) -> None:
        rows = pd.DataFrame(
            [
                {"code": "000001", "name": "普通股", "price": 10, "amount": 1e8, "pct_chg": 5, "gap": 2},
                {"code": "000002", "name": "ST股票", "price": 10, "amount": 1e8, "pct_chg": 6, "gap": 3},
                {"code": "000003", "name": "普通股", "price": 1, "amount": 1e8, "pct_chg": 7, "gap": 4},
                {"code": "000004", "name": "普通股", "price": 10, "amount": 1e6, "pct_chg": 8, "gap": 5},
            ]
        )
        original = rows.copy(deep=True)

        pool = build_candidate_pool(
            rows, CandidatePoolConfig("intraday", 2, 5e7, 30)
        )

        self.assertEqual(list(pool["code"]), ["000001"])
        pd.testing.assert_frame_equal(rows, original)

    def test_live_wrapper_preserves_zero_top_and_explicit_limit_behavior(self) -> None:
        rows = pd.DataFrame(
            [
                {"code": "000001", "name": "普通股", "price": 10, "amount": 1e8, "pct_chg": 5},
            ]
        )
        config = Config(mode="intraday", top=0, min_price=2, min_amount=5e7)

        self.assertTrue(build_pool(rows, config).empty)
        self.assertEqual(len(build_pool(rows, config, limit=0)), 1)


if __name__ == "__main__":
    unittest.main()
