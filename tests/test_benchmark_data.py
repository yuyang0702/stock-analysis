import tempfile
import unittest
from pathlib import Path

import pandas as pd

from benchmark_data import load_benchmark_csv, normalize_benchmark_frame, write_benchmark_csv


class BenchmarkDataTest(unittest.TestCase):
    def test_normalize_and_load_independent_benchmark(self) -> None:
        frame = pd.DataFrame([
            {"date": "2025-01-02", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10},
            {"date": "2025-01-03", "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": 11},
        ])
        rows = normalize_benchmark_frame(frame, "000300.XSHG")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmark.csv"
            write_benchmark_csv(path, rows)
            values = load_benchmark_csv(path)
        self.assertEqual(values["000300.XSHG"]["2025-01-03"], 101.5)


if __name__ == "__main__":
    unittest.main()
