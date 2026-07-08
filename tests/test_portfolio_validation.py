import tempfile
import unittest
from pathlib import Path

from holdings_web import PositionStore


class PortfolioValidationTest(unittest.TestCase):
    def make_store(self, tmpdir: str) -> PositionStore:
        base = Path(tmpdir)
        return PositionStore(base / "positions.json", base / "events.jsonl")

    def test_rejects_negative_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            with self.assertRaisesRegex(ValueError, "持仓数量"):
                store.upsert(
                    {
                        "code": "600000",
                        "qty": "-100",
                        "cost_price": "10",
                        "current_price": "10",
                    },
                    source="manual",
                )

    def test_rejects_empty_stock_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            with self.assertRaisesRegex(ValueError, "股票代码"):
                store.upsert(
                    {
                        "code": "",
                        "qty": "100",
                        "cost_price": "10",
                        "current_price": "10",
                    },
                    source="manual",
                )

    def test_rejects_inverted_stop_and_take_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.make_store(tmpdir)
            with self.assertRaisesRegex(ValueError, "止损价"):
                store.upsert(
                    {
                        "code": "600000",
                        "qty": "100",
                        "cost_price": "10",
                        "current_price": "10",
                        "stop_price": "10.5",
                        "take_price": "11",
                    },
                    source="manual",
                )


if __name__ == "__main__":
    unittest.main()
