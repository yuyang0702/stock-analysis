import tempfile
import unittest
from pathlib import Path

from historical_data import HistoricalStore
from research_dataset import ResearchDataset


class ResearchDatasetTest(unittest.TestCase):
    def test_snapshot_is_bounded_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            with store.connect() as connection:
                for index, day in enumerate(("2025-01-02", "2025-01-03", "2025-01-06")):
                    close = 10.0 + index
                    connection.execute(
                        "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("d1", day, "600000", close, close + 0.2, close - 0.2,
                         close, close - 0.1, 100000, close * 100000, 1),
                    )
                    connection.execute(
                        "INSERT INTO daily_status VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        ("d1", day, "600000", 1, 0, 0, close * 1.1, close * 0.9),
                    )
                    connection.execute(
                        "INSERT INTO daily_universe VALUES (?, ?, ?)",
                        ("d1", day, "600000"),
                    )

            snapshot = ResearchDataset(store, "d1", "2025-01-03", "2025-01-06")
            self.assertEqual(snapshot.trade_dates("d1", "2025-01-03", "2025-01-06"),
                             ["2025-01-03", "2025-01-06"])
            self.assertEqual(len(snapshot.history_until("d1", "600000", "2025-01-06", 2)), 2)
            self.assertEqual(len(snapshot.daily_slice("d1", "2025-01-06")), 1)
            with self.assertRaisesRegex(ValueError, "RESEARCH_DATASET_MISMATCH"):
                snapshot.daily_slice("other", "2025-01-06")

            with store.connect() as connection:
                connection.execute(
                    "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("d1", "2025-01-07", "600001", 10, 10, 10, 10, 10, 1, 10, 1),
                )
            self.assertEqual(snapshot.daily_slice("d1", "2025-01-07"), [])


if __name__ == "__main__":
    unittest.main()
