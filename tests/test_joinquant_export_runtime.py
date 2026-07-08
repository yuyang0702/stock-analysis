import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import a_share_strategy


class JoinQuantExportRuntimeTest(unittest.TestCase):
    def test_runtime_blocks_buy_export_outside_a_share_trading_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "signals.json"
            rows = pd.DataFrame(
                [
                    {
                        "code": "600000",
                        "name": "PF Bank",
                        "price": 10.5,
                        "entry_price": 10.5,
                        "position_pct": 12,
                        "final_score": 90,
                        "signal_action": "continue",
                        "pct_chg": 2.1,
                    }
                ]
            )

            with patch("joinquant_exporter.app_config.JOINQUANT_SIGNAL_FILE", output_path), patch(
                "a_share_strategy.is_a_share_trading_time", return_value=False
            ):
                result = a_share_strategy.run_joinquant_export(a_share_strategy.Config(), rows)

            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["signals"], [])


if __name__ == "__main__":
    unittest.main()
