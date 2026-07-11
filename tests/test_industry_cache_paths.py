import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from a_share_strategy import IndustryMapper


class IndustryCachePathsTest(unittest.TestCase):
    def test_default_industry_cache_is_runtime_data(self) -> None:
        self.assertEqual(config.INDUSTRY_CACHE, config.CACHE_DIR / "industry" / "stock_industry_db.json")

    def test_legacy_files_are_copied_without_overwriting_new_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "stock_industry_db.json").write_text(
                json.dumps({"600000": "银行"}), encoding="utf-8"
            )
            (base / "industry_pending.json").write_text(
                json.dumps({"000001": {"name": "平安银行"}}), encoding="utf-8"
            )
            target = base / "cache" / "industry" / "stock_industry_db.json"
            with patch.object(config, "BASE_DIR", base):
                mapper = IndustryMapper(cache_file=target)

            self.assertEqual(mapper.get("600000"), "银行")
            self.assertTrue(target.exists())
            self.assertTrue(target.with_name("industry_pending.json").exists())


if __name__ == "__main__":
    unittest.main()
