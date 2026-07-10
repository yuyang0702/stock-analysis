from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_runs (
    run_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    git_commit TEXT,
    strategy_version TEXT,
    parameters_version TEXT,
    data_status TEXT,
    result TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES strategy_runs(run_id),
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    jq_code TEXT NOT NULL,
    action TEXT NOT NULL,
    target_position REAL,
    signal_price REAL,
    stop_loss REAL,
    take_profit REAL,
    final_score REAL,
    strategy_mode TEXT,
    generated_at TEXT NOT NULL,
    expires_at TEXT,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(signal_id)
);
CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    risk_mode TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    hard_block_code TEXT,
    shadow_codes TEXT,
    cash REAL,
    total_assets REAL,
    position_value REAL,
    current_single_exposure REAL,
    projected_single_exposure REAL,
    current_portfolio_exposure REAL,
    projected_portfolio_exposure REAL,
    current_industry_exposure REAL,
    projected_industry_exposure REAL,
    daily_profit_loss REAL,
    account_drawdown REAL,
    turnover_rate REAL,
    snapshot_at TEXT,
    raw_json TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_strategy_runs_trade_date ON strategy_runs(trade_date);
CREATE INDEX IF NOT EXISTS idx_signals_action ON signals(action);
CREATE INDEX IF NOT EXISTS idx_signals_run_id ON signals(run_id);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_signal_id ON risk_decisions(signal_id);
"""


@dataclass(frozen=True)
class StoreHealth:
    ok: bool
    schema_version: int
    error: str = ""


@dataclass(frozen=True)
class StrategyRunRecord:
    run_id: str
    trade_date: str
    started_at: str
    strategy_version: str
    parameter_version: str


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    run_id: str
    trade_date: str
    code: str
    jq_code: str
    action: str
    position_pct: float
    generated_at: str
    expires_at: str
    raw_json: str


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class TradingStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_V1)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, datetime('now'))")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def health(self) -> StoreHealth:
        try:
            with self.connect() as conn:
                version = int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)
                conn.execute("SELECT 1").fetchone()
            return StoreHealth(ok=version == SCHEMA_VERSION, schema_version=version)
        except Exception as exc:
            return StoreHealth(ok=False, schema_version=0, error=str(exc))

    def record_strategy_run(self, conn: sqlite3.Connection, run: StrategyRunRecord) -> None:
        conn.execute(
            """
            INSERT INTO strategy_runs(
                run_id, trade_date, started_at, strategy_version, parameters_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(run_id) DO NOTHING
            """,
            (run.run_id, run.trade_date, run.started_at, run.strategy_version, run.parameter_version),
        )

    def record_signal(self, conn: sqlite3.Connection, signal: SignalRecord) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO signals(
                signal_id, run_id, trade_date, stock_code, jq_code, action,
                target_position, generated_at, expires_at, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                signal.signal_id, signal.run_id, signal.trade_date, signal.code,
                signal.jq_code, signal.action, signal.position_pct, signal.generated_at,
                signal.expires_at, signal.raw_json,
            ),
        )
        return cursor.rowcount == 1

    def set_system_state(
        self, conn: sqlite3.Connection, key: str, value: str, reason: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO system_state(key, value, updated_at, reason)
            VALUES (?, ?, datetime('now'), ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                reason = excluded.reason
            """,
            (key, value, reason),
        )

    def get_system_state(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row[0])
