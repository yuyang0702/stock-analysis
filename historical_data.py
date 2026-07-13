"""Independent point-in-time history storage for deterministic backtests."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


HISTORY_SCHEMA_VERSION = 1

STRICT_FEATURES = frozenset(
    {
        "score",
        "news_score",
        "pct_chg",
        "turnover",
        "position_pct",
        "entry_price",
        "stop_loss",
        "take_profit",
        "atr14",
        "support_level",
        "strategy_mode",
        "market_regime",
        "industry",
        "theme",
    }
)

REQUIRED_COLUMNS = {
    "bars": {
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
    },
    "status": {
        "trade_date",
        "code",
        "listed",
        "st",
        "suspended",
        "limit_up",
        "limit_down",
    },
    "universe": {"trade_date", "code"},
    "features": {
        "trade_date",
        "code",
        "feature_name",
        "feature_value",
        "event_at",
        "available_at",
    },
}

SOURCE_COLUMNS = {
    "joinquant": {
        "trade_date": "trade_date",
        "code": "code",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "prev_close": "prev_close",
        "volume": "volume",
        "amount": "amount",
        "adjust_factor": "adjust_factor",
        "listed": "listed",
        "st": "st",
        "suspended": "suspended",
        "limit_up": "limit_up",
        "limit_down": "limit_down",
        "feature_name": "feature_name",
        "feature_value": "feature_value",
        "event_at": "event_at",
        "available_at": "available_at",
    },
    "akshare": {
        "trade_date": "日期",
        "code": "股票代码",
        "open": "开盘",
        "high": "最高",
        "low": "最低",
        "close": "收盘",
        "prev_close": "昨收",
        "volume": "成交量",
        "amount": "成交额",
        "adjust_factor": "复权因子",
        "listed": "已上市",
        "st": "ST",
        "suspended": "停牌",
        "limit_up": "涨停价",
        "limit_down": "跌停价",
        "feature_name": "特征名",
        "feature_value": "特征值",
        "event_at": "事件时间",
        "available_at": "可用时间",
    },
}

TABLE_SPECS = {
    "bars": (
        "daily_bars",
        ("trade_date", "code"),
        (
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
        ),
    ),
    "status": (
        "daily_status",
        ("trade_date", "code"),
        ("trade_date", "code", "listed", "st", "suspended", "limit_up", "limit_down"),
    ),
    "universe": ("daily_universe", ("trade_date", "code"), ("trade_date", "code")),
    "features": (
        "point_in_time_features",
        ("trade_date", "code", "feature_name", "available_at"),
        ("trade_date", "code", "feature_name", "feature_value", "event_at", "available_at"),
    ),
}


class HistoricalDataError(RuntimeError):
    """Base error for history data operations."""


class HistoricalDataConflict(HistoricalDataError):
    """An existing logical row differs from a replayed row."""


class HistoricalDataValidationError(HistoricalDataError):
    """A source file cannot be converted to the canonical schema."""


class HistoricalStorageLimitError(HistoricalDataError):
    """The independent history database reached its configured growth ceiling."""


@dataclass(frozen=True)
class QualityIssue:
    code: str
    count: int
    examples: tuple[str, ...]


@dataclass(frozen=True)
class QualityReport:
    dataset_id: str
    mode: str
    accepted: bool
    proxy_only: bool
    coverage: dict[str, float]
    excluded_features: tuple[str, ...]
    issues: tuple[QualityIssue, ...]
    input_hash: str


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class HistoricalStore:
    def __init__(self, db_path: Path | str, max_db_bytes: int = 3 * 1024**3):
        self.db_path = Path(db_path)
        self.max_db_bytes = int(max_db_bytes)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dataset_manifests (
                    dataset_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    adjust TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    PRIMARY KEY (dataset_id, kind, source, adjust, file_sha256)
                );
                CREATE TABLE IF NOT EXISTS daily_bars (
                    dataset_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    prev_close REAL NOT NULL,
                    volume REAL NOT NULL,
                    amount REAL NOT NULL,
                    adjust_factor REAL NOT NULL,
                    PRIMARY KEY (dataset_id, trade_date, code)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_bars_code_date
                    ON daily_bars(dataset_id, code, trade_date);
                CREATE TABLE IF NOT EXISTS daily_status (
                    dataset_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    listed INTEGER NOT NULL,
                    st INTEGER NOT NULL,
                    suspended INTEGER NOT NULL,
                    limit_up REAL NOT NULL,
                    limit_down REAL NOT NULL,
                    PRIMARY KEY (dataset_id, trade_date, code)
                );
                CREATE TABLE IF NOT EXISTS daily_universe (
                    dataset_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, trade_date, code)
                );
                CREATE TABLE IF NOT EXISTS point_in_time_features (
                    dataset_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    feature_name TEXT NOT NULL,
                    feature_value TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, trade_date, code, feature_name, available_at)
                );
                CREATE INDEX IF NOT EXISTS idx_features_date_available
                    ON point_in_time_features(dataset_id, trade_date, available_at);
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    dataset_hash TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS backtest_equity (
                    run_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    PRIMARY KEY (run_id, trade_date),
                    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS backtest_trades (
                    trade_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    decision_date TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    fee REAL NOT NULL,
                    reason TEXT NOT NULL,
                    pnl REAL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (HISTORY_SCHEMA_VERSION, _utc_now()),
            )

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def import_csv(
        self,
        dataset_id: str,
        kind: str,
        path: Path | str,
        source: str,
        adjust: str,
    ) -> int:
        if not dataset_id.strip():
            raise HistoricalDataValidationError("dataset_id is required")
        if kind not in TABLE_SPECS:
            raise HistoricalDataValidationError(f"unknown dataset kind: {kind}")
        if source not in SOURCE_COLUMNS:
            raise HistoricalDataValidationError(f"unknown source: {source}")
        source_path = Path(path)
        if self.db_path.exists() and self.db_path.stat().st_size >= self.max_db_bytes:
            raise HistoricalStorageLimitError(
                f"history database reached {self.max_db_bytes} byte import limit"
            )
        file_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        rows = self._read_rows(source_path, kind, source)
        table, primary_keys, columns = TABLE_SPECS[kind]
        inserted = 0
        with self.transaction() as connection:
            for row in rows:
                where = " AND ".join(f"{key} = ?" for key in primary_keys)
                existing = connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table} "
                    f"WHERE dataset_id = ? AND {where}",
                    (dataset_id, *(row[key] for key in primary_keys)),
                ).fetchone()
                canonical = tuple(row[column] for column in columns)
                if existing is not None:
                    if tuple(existing) != canonical:
                        identity = ", ".join(str(row[key]) for key in primary_keys)
                        raise HistoricalDataConflict(f"conflicting {kind} row: {identity}")
                    continue
                names = ("dataset_id", *columns)
                placeholders = ", ".join("?" for _ in names)
                connection.execute(
                    f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})",
                    (dataset_id, *canonical),
                )
                inserted += 1
            connection.execute(
                "INSERT OR IGNORE INTO dataset_manifests "
                "(dataset_id, kind, source, adjust, file_sha256, imported_at, row_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (dataset_id, kind, source, adjust, file_sha256, _utc_now(), len(rows)),
            )
        return inserted

    def dataset_counts(self, dataset_id: str) -> dict[str, int]:
        tables = ("daily_bars", "daily_status", "daily_universe", "point_in_time_features")
        with self.connect() as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE dataset_id = ?", (dataset_id,)
                    ).fetchone()[0]
                )
                for table in tables
            }

    def dataset_hash(self, dataset_id: str) -> str:
        digest = hashlib.sha256()
        tables = {
            "daily_bars": (
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
            ),
            "daily_status": (
                "trade_date",
                "code",
                "listed",
                "st",
                "suspended",
                "limit_up",
                "limit_down",
            ),
            "daily_universe": ("trade_date", "code"),
            "point_in_time_features": (
                "trade_date",
                "code",
                "feature_name",
                "feature_value",
                "event_at",
                "available_at",
            ),
        }
        with self.connect() as connection:
            for table, columns in tables.items():
                order_by = ", ".join(columns)
                rows = connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table} "
                    f"WHERE dataset_id = ? ORDER BY {order_by}",
                    (dataset_id,),
                )
                for row in rows:
                    payload = json.dumps(
                        [table, *tuple(row)], ensure_ascii=False, separators=(",", ":")
                    )
                    digest.update(payload.encode("utf-8"))
                    digest.update(b"\n")
        return digest.hexdigest()

    def daily_slice(self, dataset_id: str, trade_date: str) -> list[dict]:
        day = _date(trade_date)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT b.trade_date, b.code, b.open, b.high, b.low, b.close, b.prev_close, "
                "b.volume, b.amount, b.adjust_factor, s.listed, s.st, s.suspended, "
                "s.limit_up, s.limit_down FROM daily_bars b "
                "JOIN daily_universe u ON u.dataset_id = b.dataset_id "
                "AND u.trade_date = b.trade_date AND u.code = b.code "
                "JOIN daily_status s ON s.dataset_id = b.dataset_id "
                "AND s.trade_date = b.trade_date AND s.code = b.code "
                "WHERE b.dataset_id = ? AND b.trade_date = ? ORDER BY b.code",
                (dataset_id, day),
            ).fetchall()
        return [dict(row) for row in rows]

    def history_until(
        self, dataset_id: str, code: str, trade_date: str, limit: int
    ) -> list[dict]:
        day = _date(trade_date)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT trade_date, code, open, high, low, close, prev_close, volume, amount, "
                "adjust_factor FROM daily_bars WHERE dataset_id = ? AND code = ? "
                "AND trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
                (dataset_id, _code(code), day, int(limit)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def features_for_date(self, dataset_id: str, trade_date: str) -> dict[str, dict[str, str]]:
        day = _date(trade_date)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT code, feature_name, feature_value, available_at "
                "FROM point_in_time_features WHERE dataset_id = ? AND trade_date = ? "
                "AND substr(available_at, 1, 10) <= ? "
                "ORDER BY code, feature_name, available_at",
                (dataset_id, day, day),
            ).fetchall()
        values: dict[str, dict[str, str]] = {}
        for row in rows:
            values.setdefault(str(row[0]), {})[str(row[1])] = str(row[2])
        return values

    def trade_dates(self, dataset_id: str, start: str, end: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM daily_bars WHERE dataset_id = ? "
                "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                (dataset_id, _date(start), _date(end)),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def prune_runs(self, keep_complete: int = 20) -> int:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT run_id FROM backtest_runs WHERE status = 'complete' AND pinned = 0 "
                "ORDER BY COALESCE(completed_at, created_at) DESC, run_id DESC"
            ).fetchall()
            doomed = [str(row[0]) for row in rows[max(0, keep_complete):]]
            connection.executemany("DELETE FROM backtest_runs WHERE run_id = ?", [(run_id,) for run_id in doomed])
        return len(doomed)

    def _read_rows(self, path: Path, kind: str, source: str) -> list[dict]:
        mapping = SOURCE_COLUMNS[source]
        required = REQUIRED_COLUMNS[kind]
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = set(reader.fieldnames or ())
            missing = sorted(mapping[field] for field in required if mapping.get(field) not in headers)
            if missing:
                raise HistoricalDataValidationError(
                    f"missing required columns for {source}/{kind}: {', '.join(missing)}"
                )
            return [self._canonical_row(kind, raw, mapping) for raw in reader]

    def _canonical_row(self, kind: str, raw: dict, mapping: dict[str, str]) -> dict:
        value = lambda field: (raw.get(mapping[field]) or "").strip()
        row: dict[str, object] = {
            "trade_date": _date(value("trade_date")),
            "code": _code(value("code")),
        }
        if kind == "bars":
            for field in (
                "open",
                "high",
                "low",
                "close",
                "prev_close",
                "volume",
                "amount",
                "adjust_factor",
            ):
                row[field] = _number(value(field), field)
        elif kind == "status":
            for field in ("listed", "st", "suspended"):
                row[field] = _boolean(value(field), field)
            row["limit_up"] = _number(value("limit_up"), "limit_up")
            row["limit_down"] = _number(value("limit_down"), "limit_down")
        elif kind == "features":
            row.update(
                feature_name=value("feature_name"),
                feature_value=value("feature_value"),
                event_at=_timestamp(value("event_at")),
                available_at=_timestamp(value("available_at")),
            )
            if not row["feature_name"]:
                raise HistoricalDataValidationError("feature_name is required")
        return row


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date(value: str) -> str:
    cleaned = value.replace("/", "-")
    if re.fullmatch(r"\d{8}", cleaned):
        cleaned = f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:]}"
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise HistoricalDataValidationError(f"invalid trade_date: {value}") from exc


def _code(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", value)
    if not match:
        raise HistoricalDataValidationError(f"invalid stock code: {value}")
    return match.group(1)


def _number(value: str, field: str) -> float:
    try:
        number = float(value.replace(",", ""))
    except ValueError as exc:
        raise HistoricalDataValidationError(f"invalid {field}: {value}") from exc
    if not math.isfinite(number):
        raise HistoricalDataValidationError(f"invalid {field}: {value}")
    return number


def _boolean(value: str, field: str) -> int:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "是"}:
        return 1
    if normalized in {"0", "false", "no", "n", "否"}:
        return 0
    raise HistoricalDataValidationError(f"invalid {field}: {value}")


def _timestamp(value: str) -> str:
    cleaned = value.strip().replace(" ", "T")
    if not cleaned:
        raise HistoricalDataValidationError("timestamp is required")
    try:
        datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalDataValidationError(f"invalid timestamp: {value}") from exc
    return cleaned


def validate_dataset(
    store: HistoricalStore,
    dataset_id: str,
    start: str,
    end: str,
    mode: str,
    required_features: set[str] | frozenset[str],
) -> QualityReport:
    """Validate imported rows without consulting their original source files."""
    if mode not in {"strict", "price_core"}:
        raise HistoricalDataValidationError(f"unknown validation mode: {mode}")
    start_date = _date(start)
    end_date = _date(end)
    if start_date > end_date:
        raise HistoricalDataValidationError("start must not be after end")

    issues: list[QualityIssue] = []
    with store.connect() as connection:
        bars = connection.execute(
            "SELECT trade_date, code, open, high, low, close, prev_close, volume, amount, "
            "adjust_factor FROM daily_bars WHERE dataset_id = ? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date, code",
            (dataset_id, start_date, end_date),
        ).fetchall()
        statuses = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT trade_date, code FROM daily_status "
                "WHERE dataset_id = ? AND trade_date BETWEEN ? AND ?",
                (dataset_id, start_date, end_date),
            )
        }
        universe = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT trade_date, code FROM daily_universe "
                "WHERE dataset_id = ? AND trade_date BETWEEN ? AND ?",
                (dataset_id, start_date, end_date),
            )
        }
        feature_rows = connection.execute(
            "SELECT trade_date, code, feature_name, available_at "
            "FROM point_in_time_features "
            "WHERE dataset_id = ? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date, code, feature_name, available_at",
            (dataset_id, start_date, end_date),
        ).fetchall()
        declarations = connection.execute(
            "SELECT DISTINCT source, adjust FROM dataset_manifests WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchall()

    bar_keys = {(row[0], row[1]) for row in bars}
    invalid_ohlc = []
    invalid_factor = []
    for row in bars:
        trade_date, code = row[0], row[1]
        open_, high, low, close, prev_close, volume, amount, factor = map(float, row[2:])
        if not (
            open_ > 0
            and close > 0
            and prev_close > 0
            and low > 0
            and low <= min(open_, close)
            and high >= max(open_, close)
            and high >= low
            and volume >= 0
            and amount >= 0
        ):
            invalid_ohlc.append(f"{trade_date}:{code}")
        if factor <= 0:
            invalid_factor.append(f"{trade_date}:{code}")

    _append_issue(issues, "INVALID_OHLC", invalid_ohlc)
    _append_issue(issues, "INVALID_ADJUSTMENT_FACTOR", invalid_factor)
    _append_issue(
        issues,
        "MISSING_STATUS",
        [f"{date}:{code}" for date, code in sorted(bar_keys - statuses)],
    )
    _append_issue(
        issues,
        "BAR_OUTSIDE_DAILY_UNIVERSE",
        [f"{date}:{code}" for date, code in sorted(bar_keys - universe)],
    )
    _append_issue(
        issues,
        "MISSING_BAR_FOR_UNIVERSE",
        [f"{date}:{code}" for date, code in sorted(universe - bar_keys)],
    )
    future_features = [
        f"{row[0]}:{row[1]}:{row[2]}:{row[3]}"
        for row in feature_rows
        if str(row[3])[:10] > str(row[0])
    ]
    _append_issue(issues, "FUTURE_FEATURE_AVAILABILITY", future_features)
    if len(declarations) > 1:
        _append_issue(
            issues,
            "MIXED_SOURCE_OR_ADJUST",
            [f"{row[0]}:{row[1]}" for row in declarations],
        )

    available = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in feature_rows
        if str(row[3])[:10] <= str(row[0])
    }
    required = set(required_features)
    missing_features = [
        f"{date}:{code}:{feature}"
        for date, code in sorted(bar_keys)
        for feature in sorted(required)
        if (date, code, feature) not in available
    ]
    if mode == "strict":
        _append_issue(issues, "MISSING_POINT_IN_TIME_FEATURES", missing_features)

    total_keys = len(bar_keys)
    feature_coverage = {
        feature: (
            sum((date, code, feature) in available for date, code in bar_keys) / total_keys
            if total_keys
            else 0.0
        )
        for feature in required
    }
    coverage = {
        "bars": 1.0 if total_keys else 0.0,
        "status": len(bar_keys & statuses) / total_keys if total_keys else 0.0,
        "universe": len(bar_keys & universe) / total_keys if total_keys else 0.0,
        **{f"feature:{key}": value for key, value in sorted(feature_coverage.items())},
    }
    if not bars:
        _append_issue(issues, "NO_BARS_IN_WINDOW", [f"{start_date}:{end_date}"])

    excluded = tuple(sorted(feature for feature, ratio in feature_coverage.items() if ratio < 1.0))
    return QualityReport(
        dataset_id=dataset_id,
        mode=mode,
        accepted=not issues,
        proxy_only=mode == "price_core",
        coverage=coverage,
        excluded_features=excluded if mode == "price_core" else (),
        issues=tuple(issues),
        input_hash=store.dataset_hash(dataset_id),
    )


def _append_issue(issues: list[QualityIssue], code: str, examples: list[str]) -> None:
    if examples:
        issues.append(QualityIssue(code=code, count=len(examples), examples=tuple(examples[:10])))
