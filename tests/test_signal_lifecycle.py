import tempfile
import unittest
from datetime import date
from pathlib import Path

from a_share_strategy import DiskCache


class SignalLifecycleTest(unittest.TestCase):
    def test_short_signal_expires_after_several_days(self) -> None:
        from a_share_strategy import build_signal_lifecycle_bundle

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DiskCache(Path(tmpdir) / "cache.json")
            row = {
                "code": "600000",
                "name": "示例股",
                "mode": "short",
                "entry_price": 10.0,
                "take_profit": 11.0,
                "stop_loss": 9.5,
                "price": 9.8,
                "has_holding": False,
            }

            first = build_signal_lifecycle_bundle(row, cache, current_day=date(2026, 1, 1))
            self.assertEqual(first["signal_state"], "fresh")
            self.assertEqual(first["signal_action"], "continue")

            later = build_signal_lifecycle_bundle(row, cache, current_day=date(2026, 1, 5))
            self.assertEqual(later["signal_state"], "time_stop")
            self.assertEqual(later["signal_action"], "time_stop")
            self.assertGreaterEqual(later["signal_age_days"], 3)


if __name__ == "__main__":
    unittest.main()
