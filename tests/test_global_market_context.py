import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from global_market_context import build_global_context, fetch_global_context, load_global_context, save_global_context


class GlobalMarketContextTest(unittest.TestCase):
    def test_builds_risk_score_from_us_japan_korea(self) -> None:
        context = build_global_context(us_pct=-1.8, japan_pct=0.4, korea_pct=-0.9)

        self.assertLess(context["global_risk_score"], 0)
        self.assertIn("美股", context["global_reason"])
        self.assertIn("日本", context["global_reason"])
        self.assertIn("韩国", context["global_reason"])

    def test_loads_existing_context_or_defaults_to_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "global_context.json"
            path.write_text(
                json.dumps({"global_risk_score": -2, "global_reason": "外盘偏弱"}, ensure_ascii=False),
                encoding="utf-8",
            )

            loaded = load_global_context(path)
            missing = load_global_context(Path(tmp) / "missing.json")

            self.assertEqual(loaded["global_risk_score"], -2.0)
            self.assertEqual(missing["global_risk_score"], 0.0)
            self.assertIn("未提供", missing["global_reason"])

    def test_fetches_context_from_global_index_snapshot(self) -> None:
        frame = pd.DataFrame(
            [
                {"名称": "纳斯达克", "涨跌幅": -1.4},
                {"名称": "标普500", "涨跌幅": -0.7},
                {"名称": "道琼斯", "涨跌幅": -0.2},
                {"名称": "日经225", "涨跌幅": 0.8},
                {"名称": "韩国KOSPI", "涨跌幅": -1.1},
                {"名称": "韩国KOSDAQ", "涨跌幅": -0.6},
            ]
        )

        context = fetch_global_context(fetcher=lambda: frame)

        self.assertLess(context["global_risk_score"], 0)
        self.assertAlmostEqual(context["us_pct"], -0.77, places=2)
        self.assertAlmostEqual(context["japan_pct"], 0.8, places=2)
        self.assertAlmostEqual(context["korea_pct"], -0.85, places=2)
        self.assertIn("纳斯达克", context["global_reason"])

    def test_fetch_uses_fallback_source_when_primary_fails(self) -> None:
        frame = pd.DataFrame(
            [
                {"名称": "NASDAQ", "涨跌幅": 1.2},
                {"名称": "日经225", "涨跌幅": -0.4},
                {"名称": "KOSPI", "涨跌幅": 0.3},
            ]
        )

        def fail_primary() -> pd.DataFrame:
            raise RuntimeError("primary disconnected")

        context = fetch_global_context(fetchers=[("primary", fail_primary), ("fallback", lambda: frame)])

        self.assertEqual(context["fetch_status"], "ok")
        self.assertEqual(context["source"], "fallback")
        self.assertIn("primary", context["fallback_errors"][0])

    def test_fetch_reuses_fresh_cache_when_all_sources_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "global_context.json"
            path.write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "fetch_status": "ok",
                        "global_risk_score": -1.5,
                        "global_reason": "上次成功数据",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def fail_source() -> pd.DataFrame:
                raise RuntimeError("network disconnected")

            context = fetch_global_context(fetchers=[("primary", fail_source)], previous_path=path)

            self.assertEqual(context["fetch_status"], "reused_cache")
            self.assertEqual(context["global_risk_score"], -1.5)
            self.assertIn("复用最近成功海外数据", context["global_reason"])

    def test_fetch_does_not_reuse_failed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "global_context.json"
            path.write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "fetch_status": "error",
                        "global_risk_score": 0,
                        "global_reason": "上次失败中性数据",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def fail_source() -> pd.DataFrame:
                raise RuntimeError("network disconnected")

            context = fetch_global_context(fetchers=[("primary", fail_source)], previous_path=path)

            self.assertEqual(context["fetch_status"], "error")
            self.assertEqual(context["global_risk_score"], 0.0)
            self.assertNotIn("复用最近成功海外数据", context["global_reason"])

    def test_saves_global_context_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market" / "global_context.json"
            context = build_global_context(us_pct=1.2, japan_pct=0.3, korea_pct=0.1)

            saved = save_global_context(context, path)
            loaded = load_global_context(saved)

            self.assertEqual(saved, path)
            self.assertEqual(loaded["global_risk_score"], context["global_risk_score"])


if __name__ == "__main__":
    unittest.main()
