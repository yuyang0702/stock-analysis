import unittest

import pandas as pd

from shadow_score import apply_shadow_scores, build_shadow_score


class ShadowScoreTest(unittest.TestCase):
    def test_builds_shadow_score_without_replacing_final_score(self) -> None:
        row = pd.Series(
            {
                "final_score": 82,
                "news_score": 6,
                "theme_heat_level": "高",
                "theme_heat_score": 7,
                "market_state": "强势进攻",
                "trade_score": 78,
            }
        )

        result = build_shadow_score(row, global_risk_score=-1)

        self.assertEqual(result["shadow_base_score"], 82.0)
        self.assertGreater(result["enhanced_score"], 82.0)
        self.assertEqual(result["global_risk_score"], -1.0)
        self.assertIn("消息+", result["shadow_reason"])
        self.assertIn("题材+", result["shadow_reason"])

    def test_apply_shadow_scores_keeps_original_sort_key_available(self) -> None:
        frame = pd.DataFrame(
            [
                {"code": "600000", "final_score": 80, "news_score": -8, "theme_heat_level": "低", "market_state": "弱势震荡"},
                {"code": "000001", "final_score": 79, "news_score": 6, "theme_heat_level": "高", "market_state": "强势进攻"},
            ]
        )

        enriched = apply_shadow_scores(frame, global_risk_score=0)

        self.assertEqual(list(enriched["final_score"]), [80, 79])
        self.assertIn("enhanced_score", enriched.columns)
        self.assertIn("shadow_rank", enriched.columns)
        self.assertLess(enriched.loc[0, "enhanced_score"], enriched.loc[1, "enhanced_score"])
        self.assertEqual(enriched.loc[1, "shadow_rank"], 1)

    def test_sector_position_adjusts_shadow_score_only(self) -> None:
        strong = pd.Series(
            {
                "final_score": 80,
                "news_score": 0,
                "theme_heat_level": "待确认",
                "market_state": "震荡",
                "trade_score": 70,
                "sector_rank_pct": 0.9,
                "sector_hot_level": "强",
            }
        )
        weak = strong.copy()
        weak["sector_rank_pct"] = 0.2
        weak["sector_hot_level"] = "弱"

        strong_score = build_shadow_score(strong, global_risk_score=0)
        weak_score = build_shadow_score(weak, global_risk_score=0)

        self.assertEqual(strong_score["shadow_base_score"], 80.0)
        self.assertGreater(strong_score["enhanced_score"], weak_score["enhanced_score"])
        self.assertGreater(strong_score["sector_position_score"], 0)
        self.assertLess(weak_score["sector_position_score"], 0)
        self.assertIn("板块+", strong_score["shadow_reason"])


if __name__ == "__main__":
    unittest.main()
