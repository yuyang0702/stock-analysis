import csv
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from historical_data import (
    STRICT_FEATURES,
    HistoricalDataConflict,
    HistoricalDataValidationError,
    HistoricalStorageLimitError,
    HistoricalStore,
    strict_table_hash,
    validate_dataset,
)
from ml_contracts import CandidateSample, TimedFeature, canonical_hash


JOINQUANT_FIELDS = [
    "trade_date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "amount",
    "adjust_factor",
]


class HistoricalDataTest(unittest.TestCase):
    def _strict_candidate(
        self,
        *,
        decision_at: str = "2025-01-02T10:00:00+08:00",
        code: str = "600000",
        available_at: str = "2025-01-02T09:59:59+08:00",
        selected: bool = True,
    ) -> CandidateSample:
        return CandidateSample.from_values(
            source="strict_history",
            dataset_id="strict-1",
            decision_at=decision_at,
            code=code,
            strategy_version="strategy-v1",
            parameter_version="params-v1",
            feature_schema_version="features-v1",
            features={
                "price": TimedFeature(10.5, available_at),
                "market_regime": TimedFeature("NORMAL", available_at),
            },
            selected=selected,
            rejection_stage="selected" if selected else "score",
            rejection_code="" if selected else "BELOW_SCORE",
            final_action="selected" if selected else "score_rejected",
            universe_hash="universe-sha",
            market_data_version="market-v1",
            code_hash="code-sha",
            generator_hash="generator-sha",
        )

    def _strict_manifest(
        self,
        samples: list[CandidateSample],
        *,
        expected_codes: list[str] | None = None,
        candidate_hash: str | None = None,
        price_hash: str = "",
    ) -> dict:
        decision_at = samples[0].decision_at
        hashes = [canonical_hash(sample) for sample in sorted(samples, key=lambda row: (row.decision_at, row.code))]
        return {
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
                decision_at: {
                    "codes": expected_codes or [sample.code for sample in samples],
                    "universe_hash": "universe-sha",
                }
            },
            "table_hashes": {
                "decision_candidates": candidate_hash or canonical_hash(hashes),
                "candidate_prices": price_hash,
            },
        }

    @staticmethod
    def _candidate_payload(sample: CandidateSample) -> dict:
        return {
            "sample_id": sample.sample_id,
            "source": sample.source,
            "dataset_id": sample.dataset_id,
            "trade_date": sample.trade_date,
            "decision_at": sample.decision_at,
            "code": sample.code,
            "strategy_version": sample.strategy_version,
            "parameter_version": sample.parameter_version,
            "feature_schema_version": sample.feature_schema_version,
            "features": {
            name: {"value": feature.value, "available_at": feature.available_at}
            for name, feature in sample.features.items()
            },
            "selected": sample.selected,
            "rejection_stage": sample.rejection_stage,
            "rejection_code": sample.rejection_code,
            "final_action": sample.final_action,
            "universe_hash": sample.universe_hash,
            "market_data_version": sample.market_data_version,
            "code_hash": sample.code_hash,
            "generator_hash": sample.generator_hash,
        }

    @staticmethod
    def _prices() -> list[dict]:
        return [
            {
                "dataset_id": "strict-1",
                "code": "600000",
                "bar_at": "2025-01-02T10:05:00+08:00",
                "available_at": "2025-01-02T10:05:01+08:00",
                "open": 10.5,
                "high": 10.7,
                "low": 10.4,
                "close": 10.6,
                "volume": 10000,
                "amount": 106000,
                "paused": 0,
                "limit_up": 11.5,
                "limit_down": 9.5,
                "adjustment_version": "raw-v1",
            },
            {
                "dataset_id": "strict-1",
                "code": "600000",
                "bar_at": "2025-01-02T10:10:00+08:00",
                "available_at": "2025-01-02T10:10:01+08:00",
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "volume": None,
                "amount": None,
                "paused": 1,
                "limit_up": 11.5,
                "limit_down": 9.5,
                "adjustment_version": "raw-v1",
            },
        ]

    def _write_csv(self, path: Path, fields: list[str], rows: list[dict]) -> Path:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _bars(self) -> list[dict]:
        return [
            {
                "trade_date": "2025-01-02",
                "code": "600000.XSHG",
                "open": "10",
                "high": "10.8",
                "low": "9.8",
                "close": "10.5",
                "prev_close": "9.9",
                "volume": "100000",
                "amount": "1030000",
                "adjust_factor": "1",
            },
            {
                "trade_date": "2025-01-03",
                "code": "600000.XSHG",
                "open": "10.6",
                "high": "11",
                "low": "10.2",
                "close": "10.8",
                "prev_close": "10.5",
                "volume": "120000",
                "amount": "1280000",
                "adjust_factor": "1",
            },
        ]

    def _import_market_scaffold(self, root: Path, store: HistoricalStore) -> None:
        bars = self._write_csv(root / "bars.csv", JOINQUANT_FIELDS, self._bars())
        status_fields = [
            "trade_date",
            "code",
            "listed",
            "st",
            "suspended",
            "limit_up",
            "limit_down",
        ]
        status_rows = [
            {
                "trade_date": row["trade_date"],
                "code": row["code"],
                "listed": "1",
                "st": "0",
                "suspended": "0",
                "limit_up": "11.55",
                "limit_down": "9.45",
            }
            for row in self._bars()
        ]
        status = self._write_csv(root / "status.csv", status_fields, status_rows)
        universe = self._write_csv(
            root / "universe.csv",
            ["trade_date", "code"],
            [{"trade_date": row["trade_date"], "code": row["code"]} for row in self._bars()],
        )
        store.import_csv("d1", "bars", bars, "joinquant", "raw")
        store.import_csv("d1", "status", status, "joinquant", "raw")
        store.import_csv("d1", "universe", universe, "joinquant", "raw")

    def test_initializes_schema_and_import_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bars = self._write_csv(root / "bars.csv", JOINQUANT_FIELDS, self._bars())
            store = HistoricalStore(root / "history.db")

            store.initialize()

            self.assertEqual(store.schema_version(), 2)
            self.assertEqual(store.import_csv("d1", "bars", bars, "joinquant", "raw"), 2)
            self.assertEqual(store.import_csv("d1", "bars", bars, "joinquant", "raw"), 0)
            self.assertEqual(store.dataset_counts("d1")["daily_bars"], 2)

            with store.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertTrue(
                {
                    "dataset_manifests",
                    "daily_bars",
                    "daily_status",
                    "daily_universe",
                    "point_in_time_features",
                    "backtest_runs",
                    "backtest_equity",
                    "backtest_trades",
                    "decision_candidates",
                    "candidate_prices",
                }.issubset(tables)
            )

            with store.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_schema_one_database_migrates_additively_to_schema_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.db"
            import sqlite3

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (1, '2025-01-01T00:00:00+00:00')"
                )
                connection.commit()
            finally:
                connection.close()

            store = HistoricalStore(path)
            store.initialize()

            self.assertEqual(store.schema_version(), 2)
            with store.connect() as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"decision_candidates", "candidate_prices"}.issubset(tables))

    def test_strict_candidate_round_trip_is_complete_idempotent_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            samples = [
                self._strict_candidate(code="000001"),
                self._strict_candidate(code="600000", selected=False),
            ]
            manifest = self._strict_manifest(samples)

            before = store.dataset_hash("strict-1")
            self.assertEqual(store.import_candidate_cohorts(samples, manifest=manifest), 2)
            after = store.dataset_hash("strict-1")
            self.assertNotEqual(before, after)
            self.assertEqual(store.import_candidate_cohorts(list(reversed(samples)), manifest=manifest), 0)
            self.assertEqual(store.dataset_hash("strict-1"), after)
            self.assertEqual(
                store.decision_times(
                    "strict-1",
                    "2025-01-02T09:55:00+08:00",
                    "2025-01-02T10:05:00+08:00",
                ),
                ["2025-01-02T10:00:00+08:00"],
            )
            self.assertEqual(store.candidate_cohort("strict-1", samples[0].decision_at), samples)
            self.assertEqual(store.dataset_counts("strict-1")["decision_candidates"], 2)

    def test_strict_candidate_read_rejects_tampered_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            sample = self._strict_candidate()
            store.import_candidate_cohorts(
                [sample], manifest=self._strict_manifest([sample])
            )
            with store.connect() as connection:
                connection.execute(
                    "UPDATE decision_candidates SET content_sha256=? "
                    "WHERE dataset_id=? AND sample_id=?",
                    ("0" * 64, sample.dataset_id, sample.sample_id),
                )

            with self.assertRaisesRegex(
                HistoricalDataValidationError, "STRICT_COHORT_HASH_MISMATCH"
            ):
                store.candidate_cohort(sample.dataset_id, sample.decision_at)

    def test_strict_candidate_rejects_future_feature_and_current_cache_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            sample = self._strict_candidate()
            future = self._candidate_payload(sample)
            future["features"]["price"]["available_at"] = "2025-01-02T10:00:01+08:00"
            with self.assertRaisesRegex(HistoricalDataValidationError, "FEATURE_FROM_FUTURE"):
                store.import_candidate_cohorts([future], manifest=self._strict_manifest([sample]))

            cache_row = self._candidate_payload(sample)
            cache_row["source"] = "current_cache"
            with self.assertRaisesRegex(HistoricalDataValidationError, "STRICT_HISTORY_SOURCE_REQUIRED"):
                store.import_candidate_cohorts([cache_row], manifest=self._strict_manifest([sample]))

    def test_strict_candidate_conflict_rolls_back_and_missing_cohort_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            first = self._strict_candidate(code="000001")
            second = self._strict_candidate(code="600000")

            with self.assertRaisesRegex(HistoricalDataValidationError, "INCOMPLETE_COHORT"):
                store.import_candidate_cohorts(
                    [first], manifest=self._strict_manifest([first], expected_codes=["000001", "600000"])
                )

            store.import_candidate_cohorts([first, second], manifest=self._strict_manifest([first, second]))
            changed = replace(first, selected=False, rejection_stage="score", rejection_code="LOW", final_action="score_rejected")
            with self.assertRaises(HistoricalDataConflict):
                store.import_candidate_cohorts(
                    [changed, second], manifest=self._strict_manifest([changed, second])
                )
            self.assertEqual(store.candidate_cohort("strict-1", first.decision_at), [first, second])

    def test_strict_manifest_rejects_table_hash_and_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            sample = self._strict_candidate()
            with self.assertRaisesRegex(HistoricalDataValidationError, "TABLE_HASH_MISMATCH"):
                store.import_candidate_cohorts(
                    [sample], manifest=self._strict_manifest([sample], candidate_hash="bad")
                )
            manifest = self._strict_manifest([sample])
            manifest["parameter_version"] = "wrong"
            with self.assertRaisesRegex(HistoricalDataValidationError, "MANIFEST_VERSION_MISMATCH"):
                store.import_candidate_cohorts([sample], manifest=manifest)

    def test_candidate_prices_preserve_pauses_exact_availability_and_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            prices = self._prices()
            price_hash = strict_table_hash("candidate_prices", prices)
            manifest = self._strict_manifest([self._strict_candidate()], price_hash=price_hash)

            self.assertEqual(store.import_candidate_prices(prices, manifest=manifest), 2)
            self.assertEqual(store.import_candidate_prices(list(reversed(prices)), manifest=manifest), 0)
            self.assertEqual(
                store.candidate_price_path(
                    "strict-1",
                    "600000",
                    "2025-01-02T10:00:00+08:00",
                    "2025-01-02T10:10:01+08:00",
                ),
                prices,
            )
            changed = [{**prices[0], "close": 10.65}, prices[1]]
            changed_manifest = self._strict_manifest(
                [self._strict_candidate()],
                price_hash=strict_table_hash("candidate_prices", changed),
            )
            with self.assertRaises(HistoricalDataConflict):
                store.import_candidate_prices(changed, manifest=changed_manifest)
            self.assertEqual(store.candidate_price_path(
                "strict-1", "600000", "2025-01-02T10:00:00+08:00", "2025-01-02T10:10:01+08:00"
            ), prices)

    def test_candidate_price_read_rejects_tampered_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            prices = self._prices()
            manifest = self._strict_manifest(
                [self._strict_candidate()],
                price_hash=strict_table_hash("candidate_prices", prices),
            )
            store.import_candidate_prices(prices, manifest=manifest)
            with store.connect() as connection:
                connection.execute(
                    "UPDATE candidate_prices SET content_sha256=? "
                    "WHERE dataset_id=? AND code=? AND bar_at=?",
                    ("0" * 64, "strict-1", "600000", prices[0]["bar_at"]),
                )

            with self.assertRaisesRegex(
                HistoricalDataValidationError, "STRICT_PRICE_HASH_MISMATCH"
            ):
                store.candidate_price_path(
                    "strict-1",
                    "600000",
                    "2025-01-02T10:00:00+08:00",
                    "2025-01-02T10:10:01+08:00",
                )

    def test_candidate_prices_reject_late_visibility_invalid_ohlc_and_naive_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "history.db")
            store.initialize()
            base = self._prices()[0]
            cases = (
                ({**base, "available_at": "2025-01-02T10:04:59+08:00"}, "PRICE_AVAILABLE_BEFORE_BAR"),
                ({**base, "high": 10.0}, "INVALID_PRICE_OHLC"),
                ({**base, "bar_at": "2025-01-02T10:05:00"}, "TIMEZONE_AWARE_TIMESTAMP_REQUIRED"),
            )
            for row, code in cases:
                with self.subTest(code=code):
                    manifest = self._strict_manifest(
                        [self._strict_candidate()], price_hash=canonical_hash([row])
                    )
                    with self.assertRaisesRegex(HistoricalDataValidationError, code):
                        store.import_candidate_prices([row], manifest=manifest)

    def test_strict_import_preflight_counts_database_and_wal_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.db"
            bootstrap = HistoricalStore(path)
            bootstrap.initialize()
            current = path.stat().st_size
            store = HistoricalStore(path, max_db_bytes=current + 4096)
            sample = self._strict_candidate()

            with self.assertRaises(HistoricalStorageLimitError):
                store.import_candidate_cohorts([sample], manifest=self._strict_manifest([sample]))

            self.assertEqual(store.dataset_counts("strict-1")["decision_candidates"], 0)

    def test_conflicting_replay_rolls_back_the_entire_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = HistoricalStore(root / "history.db")
            store.initialize()
            original = self._write_csv(root / "original.csv", JOINQUANT_FIELDS, self._bars())
            store.import_csv("d1", "bars", original, "joinquant", "raw")

            changed = self._bars()
            changed.insert(
                0,
                {
                    **changed[0],
                    "trade_date": "2025-01-06",
                    "close": "10.7",
                },
            )
            changed[-1] = {**changed[-1], "close": "99"}
            replay = self._write_csv(root / "conflict.csv", JOINQUANT_FIELDS, changed)

            with self.assertRaises(HistoricalDataConflict):
                store.import_csv("d1", "bars", replay, "joinquant", "raw")

            self.assertEqual(store.dataset_counts("d1")["daily_bars"], 2)
            with store.connect() as connection:
                close = connection.execute(
                    "SELECT close FROM daily_bars "
                    "WHERE dataset_id = ? AND trade_date = ? AND code = ?",
                    ("d1", "2025-01-03", "600000"),
                ).fetchone()[0]
            self.assertEqual(close, 10.8)

    def test_joinquant_and_akshare_adapters_have_same_canonical_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jq_store = HistoricalStore(root / "jq.db")
            ak_store = HistoricalStore(root / "ak.db")
            jq_store.initialize()
            ak_store.initialize()

            jq = self._write_csv(root / "jq.csv", JOINQUANT_FIELDS, [self._bars()[0]])
            ak_fields = [
                "日期",
                "股票代码",
                "开盘",
                "最高",
                "最低",
                "收盘",
                "昨收",
                "成交量",
                "成交额",
                "复权因子",
            ]
            ak = self._write_csv(
                root / "ak.csv",
                ak_fields,
                [dict(zip(ak_fields, ["2025/01/02", "sh600000", "10", "10.8", "9.8", "10.5", "9.9", "100000", "1030000", "1"]))],
            )

            jq_store.import_csv("same", "bars", jq, "joinquant", "raw")
            ak_store.import_csv("same", "bars", ak, "akshare", "raw")

            self.assertEqual(jq_store.dataset_hash("same"), ak_store.dataset_hash("same"))
            with jq_store.connect() as jq_connection, ak_store.connect() as ak_connection:
                jq_row = tuple(
                    jq_connection.execute(
                        "SELECT trade_date, code, open, high, low, close, prev_close, "
                        "volume, amount, adjust_factor FROM daily_bars"
                    ).fetchone()
                )
                ak_row = tuple(
                    ak_connection.execute(
                        "SELECT trade_date, code, open, high, low, close, prev_close, "
                        "volume, amount, adjust_factor FROM daily_bars"
                    ).fetchone()
                )
            self.assertEqual(jq_row, ak_row)

    def test_history_store_does_not_touch_formal_trading_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            formal_db = root / "cache" / "trading" / "trading.db"
            formal_db.parent.mkdir(parents=True)
            sentinel = b"formal-trading-ledger-sentinel\x00\xff"
            formal_db.write_bytes(sentinel)

            store = HistoricalStore(root / "cache" / "backtest" / "history.db")
            store.initialize()
            bars = self._write_csv(root / "bars.csv", JOINQUANT_FIELDS, self._bars())
            store.import_csv("d1", "bars", bars, "joinquant", "raw")

            self.assertEqual(formal_db.read_bytes(), sentinel)

    def test_strict_rejects_missing_features_and_proxy_labels_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = HistoricalStore(root / "history.db")
            store.initialize()
            self._import_market_scaffold(root, store)

            strict = validate_dataset(
                store, "d1", "2025-01-01", "2025-12-31", "strict", STRICT_FEATURES
            )
            proxy = validate_dataset(
                store, "d1", "2025-01-01", "2025-12-31", "price_core", STRICT_FEATURES
            )

            self.assertFalse(strict.accepted)
            self.assertIn("MISSING_POINT_IN_TIME_FEATURES", [issue.code for issue in strict.issues])
            self.assertTrue(proxy.accepted)
            self.assertTrue(proxy.proxy_only)
            self.assertIn("news_score", proxy.excluded_features)
            self.assertEqual(proxy.input_hash, store.dataset_hash("d1"))

    def test_quality_gate_rejects_structural_and_future_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = HistoricalStore(root / "history.db")
            store.initialize()
            self._import_market_scaffold(root, store)
            with store.connect() as connection:
                connection.execute(
                    "UPDATE daily_bars SET high = 9, adjust_factor = 0 "
                    "WHERE dataset_id = 'd1' AND trade_date = '2025-01-02'"
                )
                connection.execute(
                    "DELETE FROM daily_status "
                    "WHERE dataset_id = 'd1' AND trade_date = '2025-01-03'"
                )
                connection.execute(
                    "DELETE FROM daily_universe "
                    "WHERE dataset_id = 'd1' AND trade_date = '2025-01-02'"
                )
                connection.execute(
                    "INSERT INTO point_in_time_features "
                    "(dataset_id, trade_date, code, feature_name, feature_value, event_at, available_at) "
                    "VALUES ('d1', '2025-01-03', '600000', 'score', '80', "
                    "'2025-01-03T15:00:00', '2025-01-04T09:00:00')"
                )

            report = validate_dataset(
                store, "d1", "2025-01-01", "2025-12-31", "price_core", STRICT_FEATURES
            )
            codes = {issue.code for issue in report.issues}

            self.assertFalse(report.accepted)
            self.assertTrue(
                {
                    "INVALID_OHLC",
                    "INVALID_ADJUSTMENT_FACTOR",
                    "MISSING_STATUS",
                    "BAR_OUTSIDE_DAILY_UNIVERSE",
                    "FUTURE_FEATURE_AVAILABILITY",
                }.issubset(codes)
            )
            self.assertTrue(all(len(issue.examples) <= 10 for issue in report.issues))

    def test_strict_rejects_mixed_source_or_adjust_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = HistoricalStore(root / "history.db")
            store.initialize()
            self._import_market_scaffold(root, store)
            with store.connect() as connection:
                connection.execute(
                    "INSERT INTO dataset_manifests "
                    "(dataset_id, kind, source, adjust, file_sha256, imported_at, row_count) "
                    "VALUES ('d1', 'bars', 'akshare', 'qfq', 'second', '2025-01-01T00:00:00Z', 0)"
                )

            report = validate_dataset(
                store, "d1", "2025-01-01", "2025-12-31", "strict", STRICT_FEATURES
            )

            self.assertFalse(report.accepted)
            self.assertIn("MIXED_SOURCE_OR_ADJUST", [issue.code for issue in report.issues])

    def test_dataset_hash_is_independent_of_import_row_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = HistoricalStore(root / "first.db")
            second = HistoricalStore(root / "second.db")
            first.initialize()
            second.initialize()
            ordered = self._write_csv(root / "ordered.csv", JOINQUANT_FIELDS, self._bars())
            reversed_rows = self._write_csv(
                root / "reversed.csv", JOINQUANT_FIELDS, list(reversed(self._bars()))
            )

            first.import_csv("d1", "bars", ordered, "joinquant", "raw")
            second.import_csv("d1", "bars", reversed_rows, "joinquant", "raw")

            self.assertEqual(first.dataset_hash("d1"), second.dataset_hash("d1"))

    def test_import_refuses_to_grow_database_past_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = HistoricalStore(root / "history.db", max_db_bytes=1)
            store.initialize()
            bars = self._write_csv(root / "bars.csv", JOINQUANT_FIELDS, self._bars())

            with self.assertRaises(HistoricalStorageLimitError):
                store.import_csv("d1", "bars", bars, "joinquant", "raw")

            self.assertEqual(store.dataset_counts("d1")["daily_bars"], 0)


if __name__ == "__main__":
    unittest.main()
