import csv
import tempfile
import unittest
from pathlib import Path

from historical_data import (
    STRICT_FEATURES,
    HistoricalDataConflict,
    HistoricalStorageLimitError,
    HistoricalStore,
    validate_dataset,
)


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

            self.assertEqual(store.schema_version(), 1)
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
                }.issubset(tables)
            )

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
