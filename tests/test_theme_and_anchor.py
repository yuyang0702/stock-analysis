import unittest
import tempfile
from pathlib import Path

import pandas as pd

import a_share_strategy as strat


class DummyCache:
    def __init__(self):
        self.db = {}

    def get(self, key, ttl_sec=None):
        return self.db.get(key)

    def set(self, key, value):
        self.db[key] = value


class ThemeAndAnchorTests(unittest.TestCase):
    def test_infer_theme_label_falls_back_to_confirmation(self):
        row = pd.Series(
            {
                "industry": "未识别",
                "news_tags": "",
                "news_hits": "",
                "entry_reason": "",
                "history_replay": "",
                "trade_playbook": "",
                "name": "测试票",
            }
        )
        self.assertEqual(strat.infer_theme_label(row), "题材待确认")

    def test_theme_heat_prefers_policy_and_money(self):
        row = pd.Series(
            {
                "amount_rank_pct": 0.92,
                "news_score": 9,
                "news_tags": "公司公告:政策,公司公告:业绩",
                "news_hits": "政策 | 业绩",
                "lhb_tag": "机构参与",
                "limit_quality": "封板较强",
                "pressure_label": "突破/新高",
            }
        )
        heat = strat.build_theme_heat_bundle(row, "题材催化偏强")
        self.assertEqual(heat["theme_heat_level"], "高")
        self.assertGreater(float(heat["theme_heat_score"]), 6)

    def test_sector_position_bundle_matches_theme_or_industry(self):
        sector_context = {
            "人工智能": {
                "sector_pct_chg": 3.2,
                "sector_rank_pct": 0.95,
                "sector_amount_rank_pct": 0.9,
                "sector_hot_level": "强",
            }
        }
        row = pd.Series({"industry": "软件开发", "theme_label": "人工智能"})

        bundle = strat.build_sector_position_bundle(row, sector_context)

        self.assertEqual(bundle["sector_hot_level"], "强")
        self.assertEqual(bundle["sector_pct_chg"], 3.2)
        self.assertEqual(bundle["sector_rank_pct"], 0.95)
        self.assertIn("人工智能", bundle["sector_position_reason"])

    def test_build_sector_context_ranks_strong_board_higher(self):
        frame = pd.DataFrame(
            [
                {"板块名称": "人工智能", "涨跌幅": 4.2, "成交额": 2000000000},
                {"板块名称": "煤炭", "涨跌幅": -1.0, "成交额": 500000000},
            ]
        )

        context = strat.build_sector_market_context([frame])

        self.assertGreater(context["人工智能"]["sector_rank_pct"], context["煤炭"]["sector_rank_pct"])
        self.assertEqual(context["人工智能"]["sector_hot_level"], "强")
        self.assertEqual(context["煤炭"]["sector_hot_level"], "弱")

    def test_fetch_sector_context_reuses_cache_when_source_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "sector_context.json"
            strat.save_sector_market_context(
                {"人工智能": {"sector_pct_chg": 3.2, "sector_rank_pct": 0.95, "sector_hot_level": "强"}},
                cache_path,
            )

            def fail_loader():
                raise RuntimeError("remote disconnected")

            context = strat.fetch_sector_market_context(loaders=[fail_loader], cache_path=cache_path)

            self.assertEqual(context["人工智能"]["sector_hot_level"], "强")
            self.assertEqual(context["_status"], "cache")
            self.assertIn("remote disconnected", context["_reason"])

    def test_sector_position_reports_neutral_when_context_failed(self):
        context = {"_status": "error", "_reason": "板块行情获取失败，按中性处理：remote disconnected"}
        row = pd.Series({"industry": "半导体", "theme_label": "半导体"})

        bundle = strat.build_sector_position_bundle(row, context)

        self.assertEqual(bundle["sector_hot_level"], "中性")
        self.assertEqual(bundle["sector_rank_pct"], -1.0)
        self.assertIn("板块行情获取失败", bundle["sector_position_reason"])

    def test_signal_anchor_locks_initial_entry(self):
        cache = DummyCache()
        first = pd.Series(
            {
                "code": "600000",
                "mode": "mid",
                "price": 10.0,
                "entry_price": 10.0,
                "stop_loss": 9.0,
                "take_profit": 13.0,
                "risk_reward": 3.0,
                "position_pct": 5.0,
                "risk_reason": "回踩型",
                "risk_confidence": 0.8,
                "buy_state": "已到买点",
                "buy_reason": "价格到位",
                "theme_label": "机器人",
                "theme_heat_level": "高",
                "theme_heat_reason": "资金活跃，政策催化",
                "signal_first_seen": "2026-07-06",
                "signal_state": "fresh",
                "signal_action": "continue",
                "has_holding": False,
            }
        )
        second = first.copy()
        second["price"] = 8.5
        second["entry_price"] = 8.2
        second["stop_loss"] = 7.6
        second["take_profit"] = 11.0

        anchored_first = strat.build_signal_anchor_bundle(first, cache)
        anchored_second = strat.build_signal_anchor_bundle(second, cache)

        self.assertEqual(float(anchored_first["entry_price"]), 10.0)
        self.assertEqual(float(anchored_second["entry_price"]), 10.0)
        self.assertTrue(bool(anchored_second["signal_anchor_locked"]))


if __name__ == "__main__":
    unittest.main()
