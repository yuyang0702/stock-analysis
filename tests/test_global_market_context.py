import json
import tempfile
import unittest
from pathlib import Path

from global_market_context import build_global_context, load_global_context


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


if __name__ == "__main__":
    unittest.main()
