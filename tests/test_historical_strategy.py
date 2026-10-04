import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from historical_data import HistoricalDataValidationError, HistoricalStore, STRICT_FEATURES
from historical_strategy import (
    _price_core_market_regime,
    generate_candidates_at,
    generate_daily_candidates,
)
from ml_contracts import CandidateSample, TimedFeature, canonical_hash


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

    def _insert_features(
        self,
        store: HistoricalStore,
        day: str,
        values: dict[str, object],
        code: str = "600000",
    ) -> None:
        with store.connect() as connection:
            for name in STRICT_FEATURES:
                value = values.get(name, "unknown" if name in {"industry", "theme"} else 0)
                connection.execute(
                    "INSERT INTO point_in_time_features VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("d1", day, code, name, str(value), f"{day}T14:00:00", f"{day}T14:00:00"),
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
            self.assertAlmostEqual(candidates[0].score, 79.4)
            self.assertEqual(candidates[0].entry_price, 10)
            self.assertFalse(candidates[0].evidence["proxy_only"])

    def test_strict_uses_live_average_ties_and_includes_threshold_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            day = "2025-01-02"
            rows = (
                ("600000", 70, 3, 4),
                ("600001", 70, 3, 2),
                ("600002", 68, 1, 2),
            )
            for code, score, pct_chg, turnover in rows:
                self._insert_market_day(store, day, code, 10.0)
                self._insert_features(
                    store,
                    day,
                    {
                        "score": score,
                        "news_score": 0,
                        "pct_chg": pct_chg,
                        "turnover": turnover,
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
                    code,
                )

            candidates = generate_daily_candidates(
                store,
                "d1",
                day,
                mode="strict",
                parameter_version="v1",
                min_score=75.1667,
            )

            self.assertEqual(
                [(candidate.code, candidate.score) for candidate in candidates],
                [("600000", 76.1667), ("600001", 75.1667)],
            )

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
            cooled = generate_daily_candidates(
                store,
                "d1",
                final_day,
                mode="price_core",
                parameter_version="v1",
                min_score=0,
                cooldown_codes={"600000"},
            )
            second = generate_daily_candidates(
                store, "d1", final_day, mode="price_core", parameter_version="v1", min_score=0
            )

            self.assertEqual(first, second)
            self.assertEqual([candidate.code for candidate in first], ["600000"])
            self.assertEqual(cooled, [])
            self.assertTrue(first[0].evidence["proxy_only"])
            self.assertGreater(first[0].atr14, 0)

    def test_price_core_market_regime_reduces_risk_on_negative_tape(self) -> None:
        self.assertEqual(_price_core_market_regime(0.01, 0.02), "NORMAL")
        self.assertEqual(_price_core_market_regime(-0.01, 0.01), "CAUTION")
        self.assertEqual(_price_core_market_regime(-0.01, -0.031), "RISK_OFF")

    def test_relative_profile_records_market_width_and_execution_quality(self) -> None:
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
            candidates = generate_daily_candidates(
                store,
                "d1",
                final_day,
                mode="price_core",
                parameter_version="relative-v1",
                min_score=0,
                alpha_profile="relative_v1",
            )
            self.assertEqual([item.code for item in candidates], ["600000"])
            evidence = candidates[0].evidence
            self.assertEqual(evidence["alpha_profile"], "relative_v1")
            self.assertIn("market_breadth", evidence)
            self.assertIn("volatility_20", evidence)
            self.assertIn("liquidity_ratio", evidence)

    def test_relative_v2_requires_independent_benchmark_and_records_excess_strength(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            start = date(2024, 11, 20)
            prior = 8.0
            benchmark = {}
            for offset in range(35):
                day = (start + timedelta(days=offset)).isoformat()
                close = round(8.0 + offset * 0.1, 2)
                self._insert_market_day(store, day, "600000", close, prev_close=prior)
                prior = close
                benchmark[day] = 100.0 + offset * 0.01
            final_day = (start + timedelta(days=34)).isoformat()
            candidates = generate_daily_candidates(
                store,
                "d1",
                final_day,
                mode="price_core",
                parameter_version="relative-v2",
                min_score=0,
                alpha_profile="relative_v2",
                benchmark_closes=benchmark,
            )
            self.assertEqual([item.code for item in candidates], ["600000"])
            self.assertEqual(candidates[0].evidence["alpha_profile"], "relative_v2")
            self.assertGreater(candidates[0].evidence["excess_strength_20"], 0)

    def test_research_profiles_have_explicit_entry_horizons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            start = date(2024, 11, 20)
            prior = 8.0
            for offset in range(45):
                day = (start + timedelta(days=offset)).isoformat()
                close = round(10.0 + (offset % 6) * 0.03 + offset * 0.02, 2)
                self._insert_market_day(store, day, "600000", close, prev_close=prior)
                prior = close
            day = (start + timedelta(days=44)).isoformat()
            for profile, horizon in (("pullback_trend_v1", 5), ("short_reversal_v1", 3), ("breakout_v1", 1)):
                candidates = generate_daily_candidates(
                    store, "d1", day, mode="price_core", parameter_version=profile,
                    min_score=0, alpha_profile=profile, max_chase_atr=10,
                )
                for candidate in candidates:
                    self.assertEqual(candidate.evidence["horizon_days"], horizon)

    def test_strict_returns_no_candidates_when_features_are_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            day = "2025-01-02"
            self._insert_market_day(store, day, "600000", 10.0)

            candidates = generate_daily_candidates(
                store, "d1", day, mode="strict", parameter_version="v1", min_score=0
            )

            self.assertEqual(candidates, [])

    def test_exact_time_replay_reads_only_imported_candidate_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            sample = CandidateSample.from_values(
                source="strict_history",
                dataset_id="strict-1",
                decision_at="2025-01-02T10:00:00+08:00",
                code="600000",
                strategy_version="strategy-v1",
                parameter_version="params-v1",
                feature_schema_version="features-v1",
                features={
                    "price": TimedFeature(10.5, "2025-01-02T09:59:59+08:00"),
                    "market_regime": TimedFeature("NORMAL", "2025-01-02T09:59:59+08:00"),
                },
                selected=True,
                rejection_stage="selected",
                rejection_code="",
                final_action="selected",
                universe_hash="universe-sha",
                market_data_version="market-v1",
                code_hash="code-sha",
                generator_hash="generator-sha",
            )
            manifest = {
                "dataset_id": "strict-1",
                "source": "strict_history",
                "strategy_version": "strategy-v1",
                "parameter_version": "params-v1",
                "feature_schema_version": "features-v1",
                "market_data_version": "market-v1",
                "code_hash": "code-sha",
                "generator_hash": "generator-sha",
                "adjustment_version": "raw-v1",
                "cohorts": {sample.decision_at: {"codes": [sample.code], "universe_hash": sample.universe_hash}},
                "table_hashes": {"decision_candidates": canonical_hash([canonical_hash(sample)]), "candidate_prices": ""},
            }
            store.import_candidate_cohorts([sample], manifest=manifest)

            config = {
                "strategy_version": "strategy-v1",
                "parameter_version": "params-v1",
                "feature_schema_version": "features-v1",
                "market_data_version": "market-v1",
                "code_hash": "code-sha",
                "generator_hash": "generator-sha",
            }
            with patch.object(store, "daily_slice", side_effect=AssertionError("current cache")), patch(
                "historical_strategy.fetch_live_quotes", side_effect=AssertionError("network")
            ):
                rows = generate_candidates_at(store, "strict-1", sample.decision_at, config)

            self.assertEqual(rows, [sample])

    def test_exact_time_replay_fails_closed_on_strategy_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            sample = CandidateSample.from_values(
                source="strict_history",
                dataset_id="strict-1",
                decision_at="2025-01-02T10:00:00+08:00",
                code="600000",
                strategy_version="strategy-v1",
                parameter_version="params-v1",
                feature_schema_version="features-v1",
                features={
                    "price": TimedFeature(10.5, "2025-01-02T09:59:59+08:00"),
                    "market_regime": TimedFeature(
                        "NORMAL", "2025-01-02T09:59:59+08:00"
                    ),
                },
                selected=True,
                rejection_stage="selected",
                rejection_code="",
                final_action="selected",
                universe_hash="universe-sha",
                market_data_version="market-v1",
                code_hash="code-sha",
                generator_hash="generator-sha",
            )
            manifest = {
                "dataset_id": "strict-1",
                "source": "strict_history",
                "strategy_version": "strategy-v1",
                "parameter_version": "params-v1",
                "feature_schema_version": "features-v1",
                "market_data_version": "market-v1",
                "code_hash": "code-sha",
                "generator_hash": "generator-sha",
                "adjustment_version": "raw-v1",
                "cohorts": {
                    sample.decision_at: {
                        "codes": [sample.code],
                        "universe_hash": sample.universe_hash,
                    }
                },
                "table_hashes": {
                    "decision_candidates": canonical_hash([canonical_hash(sample)]),
                    "candidate_prices": "",
                },
            }
            store.import_candidate_cohorts([sample], manifest=manifest)

            with self.assertRaisesRegex(
                HistoricalDataValidationError,
                "STRICT_COHORT_VERSION_MISMATCH: strategy_version",
            ):
                generate_candidates_at(
                    store,
                    "strict-1",
                    sample.decision_at,
                    {
                        "strategy_version": "strategy-v2",
                        "parameter_version": "params-v1",
                        "feature_schema_version": "features-v1",
                        "market_data_version": "market-v1",
                        "code_hash": "code-sha",
                        "generator_hash": "generator-sha",
                    },
                )

    def test_exact_time_replay_requires_complete_version_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(Path(tmp))
            with self.assertRaisesRegex(
                HistoricalDataValidationError, "STRICT_STRATEGY_CONFIG_INCOMPLETE"
            ):
                generate_candidates_at(
                    store,
                    "strict-1",
                    "2025-01-02T10:00:00+08:00",
                    {"strategy_version": "strategy-v1"},
                )


if __name__ == "__main__":
    unittest.main()
