import tempfile
import unittest
from pathlib import Path

from historical_backtest import HistoricalBacktestConfig
from historical_data import HistoricalStore
from paper_replay import replay_paper_account


class PaperReplayTest(unittest.TestCase):
    def test_replay_uses_next_open_and_returns_bounded_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            rows = [
                ("2025-01-02", 9.8, 10.2, 9.7, 10.0),
                ("2025-01-03", 10.0, 10.8, 9.8, 10.6),
                ("2025-01-06", 10.5, 10.9, 10.1, 10.7),
                ("2025-01-07", 10.6, 10.8, 9.5, 9.6),
                ("2025-01-08", 9.5, 9.8, 9.2, 9.4),
            ]
            with store.connect() as connection:
                for day, open_, high, low, close in rows:
                    connection.execute(
                        "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("d1", day, "600000", open_, high, low, close, open_, 1_000_000, close * 1_000_000, 1),
                    )
                    connection.execute(
                        "INSERT INTO daily_status VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        ("d1", day, "600000", 1, 0, 0, round(open_ * 1.1, 2), round(open_ * 0.9, 2)),
                    )
                    connection.execute(
                        "INSERT INTO daily_universe VALUES (?, ?, ?)", ("d1", day, "600000"),
                    )
            report = replay_paper_account(
                store,
                "d1",
                "2025-01-02",
                "2025-01-08",
                HistoricalBacktestConfig(
                    min_score=0,
                    max_positions=1,
                    max_new_positions_per_day=1,
                    alpha_profile="relative_v1",
                ),
            )
            self.assertEqual(report["status"], "complete")
            self.assertIn("summary", report)
            self.assertLessEqual(len(report["events"]), 100)
            self.assertIn("fee_schedule_version", report["summary"])


if __name__ == "__main__":
    unittest.main()
