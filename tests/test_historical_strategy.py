import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from historical_data import HistoricalStore, STRICT_FEATURES
from historical_strategy import generate_daily_candidates


class HistoricalStrategyTest(unittest.TestCase):
    def _store(self, root: Path) -> HistoricalStore:
        store = HistoricalStore(root / "history.db")
        store.initialize()
        return store

    def _insert_market_day(
        self,
        store: HistoricalStore,
        trade_date: str,
        code: str,
        close: float,
        *,
        prev_close: float | None = None,
        st: int = 0,
        suspended: int = 0,
    ) -> None:
        prev = prev_close if prev_close is not None else close - 0.1
        with store.connect() as connection:
            connection.execute(
                "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("d1", trade_date, code, close - 0.1, close + 0.2, close - 0.2, close, prev, 100000, close * 100000, 1),
            )
            connection.execute(
                "INSERT INTO daily_status VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("d1", trade_date, code, 1, st, suspended, round(prev * 1.1, 2), round(prev * 0.9, 2)),
            )
            connection.execute(
                "INSERT INTO daily_universe VALUES (?, ?, ?)", ("d1", trade_date, code)
            )

    def _insert_features(self, store: HistoricalStore, day: str, values: dict[str, object]) -> None:
        with store.connect() as connection:
            for name in STRICT_FEATURES:
                value = values.get(name, "unknown" if name in {"industry", "theme"} else 0)
                connection.execute(
                    "INSERT INTO point_in_time_features VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("d1", day, "600000", name, str(value), f"{day}T14:00:00", f"{day}T14:00:00"),
                )

    def test_strict_reproduces_score_aggregation_and_ignores_future_feature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            day = "2025-01-02"
            self._insert_market_day(store, day, "600000", 10.0)
            self._insert_features(
                store,
                day,
                {
                    "score": 70,
                    "news_score": 2,
                    "pct_chg": 3,
                    "turnover": 4,
                    "position_pct": 10,
                    "entry_price": 10,
                    "stop_loss": 9.3,
                    "take_profit": 11.4,
                    "atr14": 0.3,
                    "support_level": 9.5,
                    "strategy_mode": "short",
                    "market_regime": "NORMAL",
                    "industry": "bank",
                    "theme": "value",
                },
            )
            with store.connect() as connection:
                connection.execute(
                    "INSERT INTO point_in_time_features VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("d1", day, "600000", "score", "999", f"{day}T15:00:00", "2025-01-03T09:00:00"),
                )

            candidates = generate_daily_candidates(
                store, "d1", day, mode="strict", parameter_version="v1", min_score=0
            )

            self.assertEqual(len(candidates), 1)
            self.assertAlmostEqual(candidates[0].score, 70 + 2 * 1.2 + 5 + 2)
            self.assertEqual(candidates[0].entry_price, 10)
            self.assertFalse(candidates[0].evidence["proxy_only"])

    def test_price_core_is_deterministic_and_excludes_ineligible_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            start = date(2024, 11, 20)
            prior = 8.0
            for offset in range(35):
                day = (start + timedelta(days=offset)).isoformat()
                close = round(8.0 + offset * 0.1, 2)
                self._insert_market_day(store, day, "600000", close, prev_close=prior)
                prior = close
            final_day = (start + timedelta(days=34)).isoformat()
            self._insert_market_day(store, final_day, "600001", 10, st=1)
            self._insert_market_day(store, final_day, "600002", 10, suspended=1)
            self._insert_market_day(store, final_day, "600003", 10)

            first = generate_daily_candidates(
                store, "d1", final_day, mode="price_core", parameter_version="v1", min_score=0
            )
            second = generate_daily_candidates(
                store, "d1", final_day, mode="price_core", parameter_version="v1", min_score=0
            )

            self.assertEqual(first, second)
            self.assertEqual([candidate.code for candidate in first], ["600000"])
            self.assertTrue(first[0].evidence["proxy_only"])
            self.assertGreater(first[0].atr14, 0)


if __name__ == "__main__":
    unittest.main()
