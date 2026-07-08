import unittest

import pandas as pd

from a_share_strategy import Config, build_intraday_buy_state


class IntradayBuyStateTest(unittest.TestCase):
    def test_breakout_near_pressure_triggers_buy_point(self) -> None:
        row = pd.Series(
            {
                "limit_quality": "封板较强",
                "pressure_label": "贴近前高",
                "pressure_pct": 0.8,
                "pct_chg": 5.2,
                "amount_rank_pct": 0.7,
                "news_score": 2,
                "lhb_tag": "未检查",
            }
        )
        state, reason = build_intraday_buy_state(row, {"state": "强势进攻"}, Config())

        self.assertEqual(state, "已到买点")
        self.assertIn("突破", reason)


if __name__ == "__main__":
    unittest.main()
