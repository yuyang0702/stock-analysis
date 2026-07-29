from __future__ import annotations

import sqlite3
import hashlib
import json
import re
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from execution_contracts import (
    BrokerSnapshot,
    ExecutionIntent,
    PreTradeResult,
    StrategyOrderCandidate,
    canonical_json as contract_canonical_json,
    canonical_sha256,
)

SCHEMA_VERSION = 11


class SignalConflictError(RuntimeError):
    """Raised when an immutable signal ID is reused for different content."""


class FillConflictError(RuntimeError):
    """Raised when an immutable fill ID is reused for different content."""


def canonical_json(value: str | dict) -> str:
    parsed = json.loads(value) if isinstance(value, str) else value
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

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

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS position_cycles (
    position_cycle_id TEXT PRIMARY KEY,
    stock_code TEXT NOT NULL,
    entry_signal_id TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    initial_qty INTEGER NOT NULL,
    current_qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    initial_stop_price REAL NOT NULL,
    initial_r REAL NOT NULL,
    atr14 REAL NOT NULL,
    market_state TEXT NOT NULL,
    highest_price REAL NOT NULL,
    take_profit_stage INTEGER NOT NULL DEFAULT 0,
    last_snapshot_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_cycles_active_code
ON position_cycles(stock_code) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_position_cycles_status ON position_cycles(status);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS order_events (
    event_key TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    order_id TEXT,
    stock_code TEXT NOT NULL,
    action TEXT NOT NULL,
    target_qty INTEGER,
    requested_qty INTEGER NOT NULL DEFAULT 0,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    event_at TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_events_signal ON order_events(signal_id);
CREATE INDEX IF NOT EXISTS idx_order_events_status ON order_events(status);
"""

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS exit_intents (
    signal_id TEXT PRIMARY KEY,
    stock_code TEXT NOT NULL,
    target_qty INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    remaining_qty INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exit_intents_active_code
ON exit_intents(stock_code) WHERE status = 'active';
"""

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS trade_cooldowns (
    stock_code TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    until_date TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    signal_id TEXT REFERENCES signals(signal_id),
    order_id TEXT UNIQUE,
    stock_code TEXT NOT NULL,
    action TEXT NOT NULL,
    target_qty INTEGER,
    requested_qty INTEGER NOT NULL DEFAULT 0,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    average_fill_price REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    submit_count INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    first_submitted_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    client_order_id TEXT REFERENCES orders(client_order_id) ON DELETE SET NULL,
    order_id TEXT,
    signal_id TEXT REFERENCES signals(signal_id),
    stock_code TEXT NOT NULL,
    action TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    stamp_tax REAL NOT NULL DEFAULT 0,
    other_fee REAL NOT NULL DEFAULT 0,
    filled_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    cash REAL NOT NULL DEFAULT 0,
    available_cash REAL NOT NULL DEFAULT 0,
    total_value REAL NOT NULL DEFAULT 0,
    position_market_value REAL NOT NULL DEFAULT 0,
    daily_turnover_pct REAL NOT NULL DEFAULT 0,
    daily_pnl_pct REAL NOT NULL DEFAULT 0,
    account_drawdown_pct REAL NOT NULL DEFAULT 0,
    template_version TEXT NOT NULL DEFAULT '',
    state_hash TEXT NOT NULL,
    retained_details INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_id TEXT NOT NULL REFERENCES account_snapshots(snapshot_id) ON DELETE CASCADE,
    stock_code TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 0,
    closeable_qty INTEGER NOT NULL DEFAULT 0,
    locked_qty INTEGER NOT NULL DEFAULT 0,
    today_qty INTEGER NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    price REAL NOT NULL DEFAULT 0,
    market_value REAL NOT NULL DEFAULT 0,
    pnl REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(snapshot_id, stock_code)
);
CREATE TABLE IF NOT EXISTS daily_equity (
    trade_date TEXT PRIMARY KEY,
    opening_equity REAL NOT NULL DEFAULT 0,
    closing_equity REAL NOT NULL DEFAULT 0,
    cash REAL NOT NULL DEFAULT 0,
    position_market_value REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    net_deposit REAL NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0,
    first_snapshot_at TEXT NOT NULL,
    last_snapshot_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    reconciliation_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    snapshot_id TEXT REFERENCES account_snapshots(snapshot_id) ON DELETE SET NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    result TEXT NOT NULL,
    severity TEXT NOT NULL,
    difference_count INTEGER NOT NULL DEFAULT 0,
    control_action TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reconciliation_id TEXT NOT NULL REFERENCES reconciliation_runs(reconciliation_id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    object_id TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL,
    local_value TEXT NOT NULL DEFAULT '',
    platform_value TEXT NOT NULL DEFAULT '',
    tolerance REAL NOT NULL DEFAULT 0,
    severity TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_events (
    event_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    operator TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    reason TEXT NOT NULL,
    reconciliation_id TEXT REFERENCES reconciliation_runs(reconciliation_id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_signal ON orders(signal_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_signal ON fills(signal_id);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(filled_at);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_trade_date ON account_snapshots(trade_date, generated_at);
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_time ON reconciliation_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_result ON reconciliation_runs(result, severity);
CREATE INDEX IF NOT EXISTS idx_reconciliation_items_reason ON reconciliation_items(reason_code, severity);
"""

SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS execution_issue_state (
    issue_key TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    state TEXT NOT NULL,
    severity TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    stage_started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_transition_at TEXT NOT NULL,
    last_notified_at TEXT,
    recovered_at TEXT,
    signal_id TEXT,
    order_id TEXT,
    reconciliation_id TEXT,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_issue_state_active
ON execution_issue_state(recovered_at, severity, last_seen_at);
"""

SCHEMA_V8 = """
-- Columns are added idempotently in initialize() for SQLite compatibility.
"""

SCHEMA_V9 = """
CREATE TABLE IF NOT EXISTS gap_reentry_opportunities (
    opportunity_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    parent_signal_id TEXT NOT NULL,
    new_signal_id TEXT,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    original_entry_price REAL NOT NULL,
    original_stop_price REAL NOT NULL,
    original_risk_r REAL NOT NULL,
    reentry_cap_price REAL NOT NULL,
    first_open_at TEXT,
    first_open_price REAL,
    first_batch_id TEXT,
    confirmation_count INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    planned_entry_price REAL,
    planned_stop_price REAL,
    planned_take_profit REAL,
    planned_qty INTEGER,
    order_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(trade_date, stock_code, opportunity_id)
);
CREATE INDEX IF NOT EXISTS idx_gap_reentry_trade_state
ON gap_reentry_opportunities(trade_date, state);
CREATE INDEX IF NOT EXISTS idx_gap_reentry_code_date
ON gap_reentry_opportunities(stock_code, trade_date);
"""

SCHEMA_V10 = """
-- Evidence-status columns are added idempotently in initialize().
"""

SCHEMA_V11_TABLES = {
    "account_scopes": {
        "account_scope_id", "adapter", "scope_alias", "created_at",
    },
    "broker_snapshot_current": {
        "account_scope_id", "snapshot_id", "trade_date", "broker_time",
        "generated_at", "snapshot_sha256", "payload_json",
    },
    "broker_position_current": {
        "account_scope_id", "stock_code", "total_qty", "sellable_qty",
        "frozen_qty", "today_buy_qty", "average_cost", "last_price",
        "market_value",
    },
    "broker_order_current": {
        "account_scope_id", "client_order_id", "broker_order_id", "stock_code",
        "side", "target_qty", "filled_qty", "status", "updated_at",
        "content_sha256",
    },
    "strategy_order_candidates": {
        "account_scope_id", "candidate_id", "logical_signal_id",
        "payload_sha256", "payload_json", "created_at",
    },
    "pre_trade_results": {
        "account_scope_id", "pre_trade_result_id", "candidate_id", "allowed",
        "result_sha256", "payload_json", "checked_at", "valid_until",
    },
    "execution_intents": {
        "account_scope_id", "client_order_id", "pre_trade_result_id",
        "intent_sha256", "submission_attempt_id", "payload_json", "status",
        "expires_at",
    },
    "capacity_reservations": {
        "account_scope_id", "reservation_id", "client_order_id", "stock_code",
        "side", "target_qty", "cash_yuan", "position_value_yuan",
        "open_risk_yuan", "remaining_target_qty", "remaining_cash_yuan",
        "remaining_position_value_yuan", "remaining_open_risk_yuan",
        "industry", "theme", "uncategorized", "status", "created_at",
        "released_at", "release_reason",
    },
}

SCHEMA_V11_NAMED_INDEXES = {
    "idx_candidates_scope_signal": (
        "strategy_order_candidates", ("account_scope_id", "logical_signal_id"),
    ),
    "idx_execution_intents_scope_status": (
        "execution_intents", ("account_scope_id", "status"),
    ),
    "idx_reservations_scope_status": (
        "capacity_reservations", ("account_scope_id", "status"),
    ),
    "idx_broker_orders_scope_status": (
        "broker_order_current", ("account_scope_id", "status"),
    ),
}
SCHEMA_V11_INDEXES = frozenset(SCHEMA_V11_NAMED_INDEXES)

SCHEMA_V11_PRIMARY_KEYS = {
    "account_scopes": ("account_scope_id",),
    "broker_snapshot_current": ("account_scope_id",),
    "broker_position_current": ("account_scope_id", "stock_code"),
    "broker_order_current": ("account_scope_id", "client_order_id"),
    "strategy_order_candidates": ("account_scope_id", "candidate_id"),
    "pre_trade_results": ("account_scope_id", "pre_trade_result_id"),
    "execution_intents": ("account_scope_id", "client_order_id"),
    "capacity_reservations": ("account_scope_id", "reservation_id"),
}

SCHEMA_V11_UNIQUE_KEYS = {
    "account_scopes": {("adapter", "scope_alias")},
    "execution_intents": {("account_scope_id", "pre_trade_result_id")},
    "capacity_reservations": {("account_scope_id", "client_order_id")},
}

SCHEMA_V11_STATEMENTS = (
    """CREATE TABLE account_scopes(
       account_scope_id TEXT PRIMARY KEY,
       adapter TEXT NOT NULL CHECK(adapter IN ('joinquant','qmt')),
       scope_alias TEXT NOT NULL,
       created_at TEXT NOT NULL,
       UNIQUE(adapter, scope_alias)
       )""",
    """CREATE TABLE broker_snapshot_current(
       account_scope_id TEXT PRIMARY KEY
           REFERENCES account_scopes(account_scope_id),
       snapshot_id TEXT NOT NULL,
       trade_date TEXT NOT NULL,
       broker_time TEXT NOT NULL,
       generated_at TEXT NOT NULL,
       snapshot_sha256 TEXT NOT NULL,
       payload_json TEXT NOT NULL
       )""",
    """CREATE TABLE broker_position_current(
       account_scope_id TEXT NOT NULL,
       stock_code TEXT NOT NULL,
       total_qty INTEGER NOT NULL,
       sellable_qty INTEGER NOT NULL,
       frozen_qty INTEGER NOT NULL,
       today_buy_qty INTEGER NOT NULL,
       average_cost TEXT NOT NULL,
       last_price TEXT NOT NULL,
       market_value TEXT NOT NULL,
       PRIMARY KEY(account_scope_id, stock_code),
       FOREIGN KEY(account_scope_id)
           REFERENCES broker_snapshot_current(account_scope_id) ON DELETE CASCADE
       )""",
    """CREATE TABLE broker_order_current(
       account_scope_id TEXT NOT NULL,
       client_order_id TEXT NOT NULL,
       broker_order_id TEXT,
       stock_code TEXT NOT NULL,
       side TEXT NOT NULL,
       target_qty INTEGER NOT NULL,
       filled_qty INTEGER NOT NULL,
       status TEXT NOT NULL,
       updated_at TEXT NOT NULL,
       content_sha256 TEXT NOT NULL,
       PRIMARY KEY(account_scope_id, client_order_id),
       FOREIGN KEY(account_scope_id)
           REFERENCES broker_snapshot_current(account_scope_id) ON DELETE CASCADE
       )""",
    """CREATE TABLE strategy_order_candidates(
       account_scope_id TEXT NOT NULL,
       candidate_id TEXT NOT NULL,
       logical_signal_id TEXT NOT NULL,
       payload_sha256 TEXT NOT NULL,
       payload_json TEXT NOT NULL,
       created_at TEXT NOT NULL,
       PRIMARY KEY(account_scope_id, candidate_id),
       FOREIGN KEY(account_scope_id) REFERENCES account_scopes(account_scope_id)
       )""",
    """CREATE TABLE pre_trade_results(
       account_scope_id TEXT NOT NULL,
       pre_trade_result_id TEXT NOT NULL,
       candidate_id TEXT NOT NULL,
       allowed INTEGER NOT NULL,
       result_sha256 TEXT NOT NULL,
       payload_json TEXT NOT NULL,
       checked_at TEXT NOT NULL,
       valid_until TEXT NOT NULL,
       PRIMARY KEY(account_scope_id, pre_trade_result_id),
       FOREIGN KEY(account_scope_id, candidate_id)
           REFERENCES strategy_order_candidates(account_scope_id, candidate_id)
       )""",
    """CREATE TABLE execution_intents(
       account_scope_id TEXT NOT NULL,
       client_order_id TEXT NOT NULL,
       pre_trade_result_id TEXT NOT NULL,
       intent_sha256 TEXT NOT NULL,
       submission_attempt_id TEXT NOT NULL,
       payload_json TEXT NOT NULL,
       status TEXT NOT NULL,
       expires_at TEXT NOT NULL,
       PRIMARY KEY(account_scope_id, client_order_id),
       UNIQUE(account_scope_id, pre_trade_result_id),
       FOREIGN KEY(account_scope_id, pre_trade_result_id)
           REFERENCES pre_trade_results(account_scope_id, pre_trade_result_id)
       )""",
    """CREATE TABLE capacity_reservations(
       account_scope_id TEXT NOT NULL,
       reservation_id TEXT NOT NULL,
       client_order_id TEXT NOT NULL,
       stock_code TEXT NOT NULL,
       side TEXT NOT NULL,
       target_qty INTEGER NOT NULL,
       cash_yuan TEXT NOT NULL,
       position_value_yuan TEXT NOT NULL,
       open_risk_yuan TEXT NOT NULL,
       remaining_target_qty INTEGER NOT NULL,
       remaining_cash_yuan TEXT NOT NULL,
       remaining_position_value_yuan TEXT NOT NULL,
       remaining_open_risk_yuan TEXT NOT NULL,
       industry TEXT NOT NULL,
       theme TEXT NOT NULL,
       uncategorized INTEGER NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('active','released')),
       created_at TEXT NOT NULL,
       released_at TEXT,
       release_reason TEXT,
       PRIMARY KEY(account_scope_id, reservation_id),
       UNIQUE(account_scope_id, client_order_id),
       FOREIGN KEY(account_scope_id, client_order_id)
           REFERENCES execution_intents(account_scope_id, client_order_id)
       )""",
    """CREATE INDEX idx_candidates_scope_signal
       ON strategy_order_candidates(account_scope_id, logical_signal_id)""",
    """CREATE INDEX idx_execution_intents_scope_status
       ON execution_intents(account_scope_id, status)""",
    """CREATE INDEX idx_reservations_scope_status
       ON capacity_reservations(account_scope_id, status)""",
    """CREATE INDEX idx_broker_orders_scope_status
       ON broker_order_current(account_scope_id, status)""",
)


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
    data_status: str = "pending"
    result: str = "running"


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
    validated_at: str = ""
    published_at: str = ""
    signal_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    final_score: float | None = None
    strategy_mode: str = ""


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
            has_migrations = conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='schema_migrations'"""
            ).fetchone()
            current_version = 0
            if has_migrations:
                current_version = int(
                    conn.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0] or 0
                )
                if current_version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"database schema {current_version} is newer than supported {SCHEMA_VERSION}"
                    )
                if current_version == SCHEMA_VERSION:
                    self._validate_schema_v11(conn)
                    return
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_V1)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, datetime('now'))")
            conn.executescript(SCHEMA_V2)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, datetime('now'))")
            conn.executescript(SCHEMA_V3)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (3, datetime('now'))")
            conn.executescript(SCHEMA_V4)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (4, datetime('now'))")
            conn.executescript(SCHEMA_V5)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (5, datetime('now'))")
            conn.executescript(SCHEMA_V6)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (6, datetime('now'))")
            signal_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(signals)")}
            if "validated_at" not in signal_columns:
                conn.execute("ALTER TABLE signals ADD COLUMN validated_at TEXT")
            if "published_at" not in signal_columns:
                conn.execute("ALTER TABLE signals ADD COLUMN published_at TEXT")
            intent_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(exit_intents)")}
            if "validated_at" not in intent_columns:
                conn.execute("ALTER TABLE exit_intents ADD COLUMN validated_at TEXT")
            if "published_at" not in intent_columns:
                conn.execute("ALTER TABLE exit_intents ADD COLUMN published_at TEXT")
            conn.execute(
                "UPDATE signals SET validated_at=COALESCE(validated_at, generated_at), published_at=COALESCE(published_at, generated_at)"
            )
            conn.execute(
                "UPDATE exit_intents SET validated_at=COALESCE(validated_at, created_at), published_at=COALESCE(published_at, created_at)"
            )
            conn.executescript(SCHEMA_V7)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (7, datetime('now'))")
            cycle_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(position_cycles)")}
            if "manual_stop_price" not in cycle_columns:
                conn.execute("ALTER TABLE position_cycles ADD COLUMN manual_stop_price REAL")
            conn.executescript(SCHEMA_V8)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (8, datetime('now'))")
            conn.executescript(SCHEMA_V9)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (9, datetime('now'))")
            fill_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(fills)")}
            if "fee_data_status" not in fill_columns:
                conn.execute(
                    "ALTER TABLE fills ADD COLUMN fee_data_status TEXT NOT NULL DEFAULT 'unknown'"
                )
            equity_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(daily_equity)")}
            if "fee_data_status" not in equity_columns:
                conn.execute(
                    "ALTER TABLE daily_equity ADD COLUMN fee_data_status TEXT NOT NULL DEFAULT 'unknown'"
                )
            if "realized_pnl_status" not in equity_columns:
                conn.execute(
                    "ALTER TABLE daily_equity ADD COLUMN realized_pnl_status TEXT NOT NULL DEFAULT 'unknown'"
                )
            conn.executescript(SCHEMA_V10)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (10, datetime('now'))")
            conn.commit()
            self._migrate_schema_v11(conn)

    def _migrate_schema_v11(self, conn: sqlite3.Connection) -> None:
        current_version = int(
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        )
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current_version} is newer than supported {SCHEMA_VERSION}"
            )
        if current_version == SCHEMA_VERSION:
            self._validate_schema_v11(conn)
            return
        if current_version != 10:
            raise RuntimeError(f"schema 11 migration requires schema 10, got {current_version}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_V11_STATEMENTS:
                conn.execute(statement)
            cycle_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(position_cycles)")
            }
            if "profit_protection_activated_at" not in cycle_columns:
                conn.execute(
                    "ALTER TABLE position_cycles ADD COLUMN profit_protection_activated_at TEXT"
                )
            if "trailing_stop_active_from" not in cycle_columns:
                conn.execute(
                    "ALTER TABLE position_cycles ADD COLUMN trailing_stop_active_from TEXT"
                )
            conn.execute(
                """INSERT INTO schema_migrations(version, applied_at)
                   VALUES(11, datetime('now'))"""
            )
            self._validate_schema_v11(conn)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    @staticmethod
    def _validate_schema_v11(conn: sqlite3.Connection) -> None:
        for table, required_columns in SCHEMA_V11_TABLES.items():
            table_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            columns = {str(row[1]) for row in table_info}
            missing = required_columns - columns
            if missing:
                raise RuntimeError(
                    f"schema 11 table {table} missing columns: {sorted(missing)}"
                )
            primary_key = tuple(
                str(row[1])
                for row in sorted(
                    (row for row in table_info if int(row[5]) > 0),
                    key=lambda row: int(row[5]),
                )
            )
            if primary_key != SCHEMA_V11_PRIMARY_KEYS[table]:
                raise RuntimeError(
                    f"schema 11 table {table} has invalid primary key: {primary_key}"
                )
        cycle_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(position_cycles)")
        }
        missing_cycle = {
            "profit_protection_activated_at", "trailing_stop_active_from",
        } - cycle_columns
        if missing_cycle:
            raise RuntimeError(
                f"schema 11 position_cycles missing columns: {sorted(missing_cycle)}"
            )
        for index_name, (expected_table, expected_columns) in (
            SCHEMA_V11_NAMED_INDEXES.items()
        ):
            index_row = conn.execute(
                """SELECT tbl_name FROM sqlite_master
                   WHERE type='index' AND name=?""",
                (index_name,),
            ).fetchone()
            actual_columns = tuple(
                str(row[2])
                for row in sorted(
                    conn.execute(
                        f'PRAGMA index_info("{index_name}")'
                    ).fetchall(),
                    key=lambda row: int(row[0]),
                )
            )
            if (
                index_row is None
                or str(index_row[0]) != expected_table
                or actual_columns != expected_columns
            ):
                raise RuntimeError(
                    f"schema 11 index {index_name} has invalid table or columns"
                )
        for table, primary_key in SCHEMA_V11_PRIMARY_KEYS.items():
            unique_sets = set()
            for index in conn.execute(f"PRAGMA index_list({table})"):
                if not int(index[2]):
                    continue
                unique_sets.add(tuple(
                    str(row[2])
                    for row in sorted(
                        conn.execute(f'PRAGMA index_info("{index[1]}")').fetchall(),
                        key=lambda row: int(row[0]),
                    )
                ))
            expected_unique = {primary_key} | SCHEMA_V11_UNIQUE_KEYS.get(
                table, set()
            )
            if unique_sets != expected_unique:
                raise RuntimeError(
                    f"schema 11 table {table} has invalid unique keys: "
                    f"{sorted(unique_sets)}"
                )
        required_foreign_keys = {
            "broker_snapshot_current": {("account_scope_id", "account_scopes", "account_scope_id")},
            "broker_position_current": {
                ("account_scope_id", "broker_snapshot_current", "account_scope_id"),
            },
            "broker_order_current": {
                ("account_scope_id", "broker_snapshot_current", "account_scope_id"),
            },
            "strategy_order_candidates": {
                ("account_scope_id", "account_scopes", "account_scope_id"),
            },
            "pre_trade_results": {
                ("account_scope_id", "strategy_order_candidates", "account_scope_id"),
                ("candidate_id", "strategy_order_candidates", "candidate_id"),
            },
            "execution_intents": {
                ("account_scope_id", "pre_trade_results", "account_scope_id"),
                ("pre_trade_result_id", "pre_trade_results", "pre_trade_result_id"),
            },
            "capacity_reservations": {
                ("account_scope_id", "execution_intents", "account_scope_id"),
                ("client_order_id", "execution_intents", "client_order_id"),
            },
        }
        for table, required in required_foreign_keys.items():
            grouped: dict[int, list[sqlite3.Row]] = {}
            for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
                grouped.setdefault(int(row[0]), []).append(row)
            actual_groups = {
                tuple(
                    (str(row[3]), str(row[2]), str(row[4]))
                    for row in sorted(rows, key=lambda item: int(item[1]))
                )
                for rows in grouped.values()
            }
            expected_groups: set[tuple[tuple[str, str, str], ...]] = set()
            if table in {
                "pre_trade_results", "execution_intents",
                "capacity_reservations",
            }:
                expected_groups.add(tuple(sorted(
                    required,
                    key=lambda item: (
                        0 if item[0] == "account_scope_id" else 1
                    ),
                )))
            else:
                expected_groups = {(item,) for item in required}
            if actual_groups != expected_groups:
                raise RuntimeError(
                    f"schema 11 table {table} has invalid foreign keys"
                )

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
                if version == 11:
                    self._validate_schema_v11(conn)
            return StoreHealth(ok=version == SCHEMA_VERSION, schema_version=version)
        except Exception as exc:
            return StoreHealth(ok=False, schema_version=0, error=str(exc))

    @staticmethod
    def _money(value: object, name: str) -> Decimal:
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be a decimal") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
        return amount

    def get_or_create_account_scope(
        self,
        conn: sqlite3.Connection,
        adapter: str,
        scope_alias: str,
    ) -> str:
        adapter = str(adapter).strip().lower()
        scope_alias = str(scope_alias).strip()
        if adapter not in {"joinquant", "qmt"}:
            raise ValueError("adapter must be joinquant or qmt")
        if not scope_alias:
            raise ValueError("scope_alias is required")
        row = conn.execute(
            """SELECT account_scope_id FROM account_scopes
               WHERE adapter=? AND scope_alias=?""",
            (adapter, scope_alias),
        ).fetchone()
        if row:
            return str(row[0])
        account_scope_id = str(uuid.uuid4())
        try:
            conn.execute(
                """INSERT INTO account_scopes(
                   account_scope_id, adapter, scope_alias, created_at
                   ) VALUES(?,?,?,datetime('now'))""",
                (account_scope_id, adapter, scope_alias),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                """SELECT account_scope_id FROM account_scopes
                   WHERE adapter=? AND scope_alias=?""",
                (adapter, scope_alias),
            ).fetchone()
            if not row:
                raise
            return str(row[0])
        return account_scope_id

    def replace_current_broker_snapshot(
        self,
        conn: sqlite3.Connection,
        snapshot: BrokerSnapshot | dict,
    ) -> str:
        normalized = BrokerSnapshot.from_dict(
            snapshot.to_dict() if isinstance(snapshot, BrokerSnapshot) else snapshot
        )
        payload_json = contract_canonical_json(normalized.to_dict())
        if len(payload_json.encode("utf-8")) > 1024 * 1024:
            raise ValueError("broker snapshot canonical payload exceeds 1 MiB")
        scope = normalized.account_scope_id
        if not conn.execute(
            "SELECT 1 FROM account_scopes WHERE account_scope_id=?", (scope,)
        ).fetchone():
            raise ValueError("unknown account_scope_id")
        current = conn.execute(
            """SELECT snapshot_id, broker_time, generated_at, snapshot_sha256
               FROM broker_snapshot_current WHERE account_scope_id=?""",
            (scope,),
        ).fetchone()
        if current:
            if current["snapshot_id"] == normalized.snapshot_id:
                if current["snapshot_sha256"] == normalized.snapshot_sha256:
                    return normalized.snapshot_id
                raise ValueError("broker snapshot ID conflicts with existing hash")
            incoming_time = (
                datetime.fromisoformat(normalized.broker_time),
                datetime.fromisoformat(normalized.generated_at),
            )
            current_time = (
                datetime.fromisoformat(str(current["broker_time"])),
                datetime.fromisoformat(str(current["generated_at"])),
            )
            if incoming_time < current_time:
                raise ValueError("stale broker snapshot")
            if incoming_time == current_time:
                raise ValueError("ambiguous broker snapshot ordering conflict")

        conn.execute(
            "DELETE FROM broker_position_current WHERE account_scope_id=?", (scope,)
        )
        conn.execute(
            "DELETE FROM broker_order_current WHERE account_scope_id=?", (scope,)
        )
        conn.execute(
            "DELETE FROM broker_snapshot_current WHERE account_scope_id=?", (scope,)
        )
        conn.execute(
            """INSERT INTO broker_snapshot_current(
               account_scope_id, snapshot_id, trade_date, broker_time, generated_at,
               snapshot_sha256, payload_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                scope, normalized.snapshot_id, normalized.trade_date,
                normalized.broker_time, normalized.generated_at,
                normalized.snapshot_sha256, payload_json,
            ),
        )
        for position in normalized.positions:
            conn.execute(
                """INSERT INTO broker_position_current(
                   account_scope_id, stock_code, total_qty, sellable_qty,
                   frozen_qty, today_buy_qty, average_cost, last_price, market_value
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    scope, position.code, position.total_qty, position.sellable_qty,
                    position.frozen_qty, position.today_buy_qty,
                    str(position.average_cost), str(position.last_price),
                    str(position.market_value),
                ),
            )
        for order in normalized.open_orders:
            conn.execute(
                """INSERT INTO broker_order_current(
                   account_scope_id, client_order_id, broker_order_id, stock_code,
                   side, target_qty, filled_qty, status, updated_at, content_sha256
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    scope, order["client_order_id"], order["broker_order_id"],
                    order["stock_code"], order["side"], order["target_qty"],
                    order["filled_qty"], order["status"], order["updated_at"],
                    canonical_sha256(order),
                ),
            )
        return normalized.snapshot_id

    def load_current_broker_snapshot(
        self,
        conn: sqlite3.Connection,
        account_scope_id: str,
    ) -> BrokerSnapshot | None:
        row = conn.execute(
            """SELECT payload_json FROM broker_snapshot_current
               WHERE account_scope_id=?""",
            (account_scope_id,),
        ).fetchone()
        return BrokerSnapshot.from_dict(json.loads(row[0])) if row else None

    @staticmethod
    def _insert_immutable_fact(
        conn: sqlite3.Connection,
        *,
        table: str,
        scope: str,
        identity_column: str,
        identity: str,
        hash_column: str,
        content_hash: str,
        statement: str,
        parameters: tuple[object, ...],
    ) -> str:
        row = conn.execute(
            f"""SELECT {hash_column} FROM {table}
                WHERE account_scope_id=? AND {identity_column}=?""",
            (scope, identity),
        ).fetchone()
        if row:
            if str(row[0]) == content_hash:
                return identity
            raise ValueError(f"{table} immutable ID conflicts with existing hash")
        conn.execute(statement, parameters)
        return identity

    def insert_strategy_order_candidate(
        self,
        conn: sqlite3.Connection,
        candidate: StrategyOrderCandidate | dict,
    ) -> str:
        record = StrategyOrderCandidate.from_dict(
            candidate.to_dict()
            if isinstance(candidate, StrategyOrderCandidate)
            else candidate
        )
        payload = contract_canonical_json(record.to_dict())
        return self._insert_immutable_fact(
            conn,
            table="strategy_order_candidates",
            scope=record.account_scope_id,
            identity_column="candidate_id",
            identity=record.candidate_id,
            hash_column="payload_sha256",
            content_hash=record.payload_sha256,
            statement="""INSERT INTO strategy_order_candidates(
                account_scope_id, candidate_id, logical_signal_id, payload_sha256,
                payload_json, created_at
                ) VALUES(?,?,?,?,?,?)""",
            parameters=(
                record.account_scope_id, record.candidate_id,
                record.logical_signal_id, record.payload_sha256, payload,
                record.signal_time,
            ),
        )

    def insert_pre_trade_result(
        self,
        conn: sqlite3.Connection,
        result: PreTradeResult | dict,
    ) -> str:
        record = PreTradeResult.from_dict(
            result.to_dict() if isinstance(result, PreTradeResult) else result
        )
        scope = record.candidate.account_scope_id
        candidate_row = conn.execute(
            """SELECT payload_sha256 FROM strategy_order_candidates
               WHERE account_scope_id=? AND candidate_id=?""",
            (scope, record.candidate_id),
        ).fetchone()
        if (
            candidate_row is None
            or str(candidate_row[0]) != record.candidate.payload_sha256
        ):
            raise ValueError("pre-trade result candidate evidence is not persisted")
        payload = contract_canonical_json(record.to_dict())
        return self._insert_immutable_fact(
            conn,
            table="pre_trade_results",
            scope=scope,
            identity_column="pre_trade_result_id",
            identity=record.pre_trade_result_id,
            hash_column="result_sha256",
            content_hash=record.result_sha256,
            statement="""INSERT INTO pre_trade_results(
                account_scope_id, pre_trade_result_id, candidate_id, allowed,
                result_sha256, payload_json, checked_at, valid_until
                ) VALUES(?,?,?,?,?,?,?,?)""",
            parameters=(
                scope, record.pre_trade_result_id, record.candidate_id,
                int(record.allowed), record.result_sha256, payload,
                record.checked_at, record.valid_until,
            ),
        )

    def insert_execution_intent(
        self,
        conn: sqlite3.Connection,
        intent: ExecutionIntent | dict,
        *,
        status: str = "READY",
    ) -> str:
        record = ExecutionIntent.from_dict(
            intent.to_dict() if isinstance(intent, ExecutionIntent) else intent
        )
        if record.account_scope_id != record.pre_trade_result.candidate.account_scope_id:
            raise ValueError("execution intent scope does not match pre-trade result")
        result_row = conn.execute(
            """SELECT result_sha256 FROM pre_trade_results
               WHERE account_scope_id=? AND pre_trade_result_id=?""",
            (record.account_scope_id, record.pre_trade_result_id),
        ).fetchone()
        if (
            result_row is None
            or str(result_row[0]) != record.pre_trade_result_sha256
        ):
            raise ValueError("execution intent pre-trade evidence is not persisted")
        payload = contract_canonical_json(record.to_dict())
        return self._insert_immutable_fact(
            conn,
            table="execution_intents",
            scope=record.account_scope_id,
            identity_column="client_order_id",
            identity=record.client_order_id,
            hash_column="intent_sha256",
            content_hash=record.intent_sha256,
            statement="""INSERT INTO execution_intents(
                account_scope_id, client_order_id, pre_trade_result_id,
                intent_sha256, submission_attempt_id, payload_json, status,
                expires_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
            parameters=(
                record.account_scope_id, record.client_order_id,
                record.pre_trade_result_id, record.intent_sha256,
                record.submission_attempt_id, payload, str(status), record.expires_at,
            ),
        )

    def compare_and_set_execution_intent_status(
        self,
        conn: sqlite3.Connection,
        account_scope_id: str,
        client_order_id: str,
        *,
        expected_status: str,
        new_status: str,
    ) -> bool:
        cursor = conn.execute(
            """UPDATE execution_intents SET status=?
               WHERE account_scope_id=? AND client_order_id=? AND status=?""",
            (new_status, account_scope_id, client_order_id, expected_status),
        )
        return cursor.rowcount == 1

    def reserve_capacity(
        self,
        conn: sqlite3.Connection,
        *,
        account_scope_id: str,
        reservation_id: str,
        client_order_id: str,
        stock_code: str,
        side: str,
        target_qty: int,
        cash_yuan: object,
        position_value_yuan: object,
        open_risk_yuan: object,
        industry: str,
        theme: str,
        uncategorized: bool,
        created_at: str,
    ) -> str:
        if int(target_qty) <= 0:
            raise ValueError("target_qty must be positive")
        cash = self._money(cash_yuan, "cash_yuan")
        position_value = self._money(position_value_yuan, "position_value_yuan")
        open_risk = self._money(open_risk_yuan, "open_risk_yuan")
        values = (
            account_scope_id, reservation_id, client_order_id, str(stock_code),
            str(side).lower(), int(target_qty), str(cash), str(position_value),
            str(open_risk), int(target_qty), str(cash), str(position_value),
            str(open_risk), str(industry), str(theme), int(bool(uncategorized)),
            "active", str(created_at),
        )
        existing = conn.execute(
            """SELECT * FROM capacity_reservations
               WHERE account_scope_id=? AND reservation_id=?""",
            (account_scope_id, reservation_id),
        ).fetchone()
        if existing:
            columns = (
                "account_scope_id", "reservation_id", "client_order_id",
                "stock_code", "side", "target_qty", "cash_yuan",
                "position_value_yuan", "open_risk_yuan", "remaining_target_qty",
                "remaining_cash_yuan", "remaining_position_value_yuan",
                "remaining_open_risk_yuan", "industry", "theme", "uncategorized",
                "status", "created_at",
            )
            if tuple(existing[name] for name in columns) == values:
                return reservation_id
            raise ValueError("capacity reservation immutable ID conflict")
        client_reservation = conn.execute(
            """SELECT reservation_id FROM capacity_reservations
               WHERE account_scope_id=? AND client_order_id=?""",
            (account_scope_id, client_order_id),
        ).fetchone()
        if client_reservation:
            raise ValueError("execution intent already has a capacity reservation")
        conn.execute(
            """INSERT INTO capacity_reservations(
               account_scope_id, reservation_id, client_order_id, stock_code,
               side, target_qty, cash_yuan, position_value_yuan, open_risk_yuan,
               remaining_target_qty, remaining_cash_yuan,
               remaining_position_value_yuan, remaining_open_risk_yuan,
               industry, theme, uncategorized, status, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        return reservation_id

    def adjust_capacity_reservation(
        self,
        conn: sqlite3.Connection,
        account_scope_id: str,
        reservation_id: str,
        *,
        remaining_target_qty: int,
        remaining_cash_yuan: object,
        remaining_position_value_yuan: object,
        remaining_open_risk_yuan: object,
    ) -> bool:
        remaining = (
            int(remaining_target_qty),
            self._money(remaining_cash_yuan, "remaining_cash_yuan"),
            self._money(
                remaining_position_value_yuan, "remaining_position_value_yuan"
            ),
            self._money(remaining_open_risk_yuan, "remaining_open_risk_yuan"),
        )
        row = conn.execute(
            """SELECT target_qty, cash_yuan, position_value_yuan, open_risk_yuan,
                      remaining_target_qty, remaining_cash_yuan,
                      remaining_position_value_yuan, remaining_open_risk_yuan
               FROM capacity_reservations
               WHERE account_scope_id=? AND reservation_id=? AND status='active'""",
            (account_scope_id, reservation_id),
        ).fetchone()
        if not row:
            return False
        original = (
            int(row[0]), Decimal(row[1]), Decimal(row[2]), Decimal(row[3]),
        )
        current = (
            int(row[4]), Decimal(row[5]), Decimal(row[6]), Decimal(row[7]),
        )
        if remaining[0] < 0 or any(
            value > limit for value, limit in zip(remaining, original)
        ):
            raise ValueError("remaining reservation values exceed original values")
        if any(value > limit for value, limit in zip(remaining, current)):
            raise ValueError("remaining reservation values cannot increase")
        cursor = conn.execute(
            """UPDATE capacity_reservations
               SET remaining_target_qty=?, remaining_cash_yuan=?,
                   remaining_position_value_yuan=?, remaining_open_risk_yuan=?
               WHERE account_scope_id=? AND reservation_id=? AND status='active'
                 AND remaining_target_qty=? AND remaining_cash_yuan=?
                 AND remaining_position_value_yuan=?
                 AND remaining_open_risk_yuan=?""",
            (
                remaining[0], str(remaining[1]), str(remaining[2]), str(remaining[3]),
                account_scope_id, reservation_id,
                current[0], str(current[1]), str(current[2]), str(current[3]),
            ),
        )
        return cursor.rowcount == 1

    def aggregate_active_reservations(
        self,
        conn: sqlite3.Connection,
        account_scope_id: str,
    ) -> dict[str, object]:
        rows = conn.execute(
            """SELECT * FROM capacity_reservations
               WHERE account_scope_id=? AND status='active'""",
            (account_scope_id,),
        ).fetchall()
        result: dict[str, object] = {
            "target_qty": 0,
            "cash_yuan": Decimal("0"),
            "position_value_yuan": Decimal("0"),
            "open_risk_yuan": Decimal("0"),
            "industry_value_yuan": {},
            "theme_value_yuan": {},
            "uncategorized_value_yuan": Decimal("0"),
        }
        for row in rows:
            value = Decimal(row["remaining_position_value_yuan"])
            result["target_qty"] += int(row["remaining_target_qty"])
            result["cash_yuan"] += Decimal(row["remaining_cash_yuan"])
            result["position_value_yuan"] += value
            result["open_risk_yuan"] += Decimal(row["remaining_open_risk_yuan"])
            if row["industry"]:
                industries = result["industry_value_yuan"]
                industries[row["industry"]] = industries.get(
                    row["industry"], Decimal("0")
                ) + value
            if row["theme"]:
                themes = result["theme_value_yuan"]
                themes[row["theme"]] = themes.get(
                    row["theme"], Decimal("0")
                ) + value
            if row["uncategorized"]:
                result["uncategorized_value_yuan"] += value
        return result

    def release_capacity_reservation(
        self,
        conn: sqlite3.Connection,
        account_scope_id: str,
        reservation_id: str,
        *,
        released_at: str,
        reason: str,
    ) -> bool:
        cursor = conn.execute(
            """UPDATE capacity_reservations
               SET status='released', released_at=?, release_reason=?
               WHERE account_scope_id=? AND reservation_id=? AND status='active'""",
            (released_at, reason, account_scope_id, reservation_id),
        )
        return cursor.rowcount == 1

    def record_strategy_run(self, conn: sqlite3.Connection, run: StrategyRunRecord) -> bool:
        cursor = conn.execute(
            """
            INSERT INTO strategy_runs(
                run_id, trade_date, started_at, strategy_version, parameters_version,
                data_status, result, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(run_id) DO NOTHING
            """,
            (
                run.run_id, run.trade_date, run.started_at, run.strategy_version,
                run.parameter_version, run.data_status, run.result,
            ),
        )
        return cursor.rowcount == 1

    def finish_strategy_run(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        result: str,
        data_status: str,
        finished_at: str,
        error_message: str | None = None,
    ) -> None:
        error = None
        if error_message:
            error = " ".join(str(error_message).split())
            error = re.sub(
                r"(?i)(token|key|secret|password)=([^&\s]+)",
                r"\1=[REDACTED]",
                error,
            )[:500]
        conn.execute(
            """UPDATE strategy_runs
               SET result=?, data_status=?, finished_at=?, error_message=?, updated_at=datetime('now')
               WHERE run_id=?""",
            (result, data_status, finished_at, error, run_id),
        )

    def upsert_gap_reentry_opportunity(self, conn: sqlite3.Connection, event: dict) -> None:
        columns = (
            "opportunity_id", "trade_date", "stock_code", "parent_signal_id",
            "new_signal_id", "state", "reason", "original_entry_price",
            "original_stop_price", "original_risk_r", "reentry_cap_price",
            "first_open_at", "first_open_price", "first_batch_id", "confirmation_count",
            "attempt_count", "planned_entry_price", "planned_stop_price",
            "planned_take_profit", "planned_qty", "order_status",
        )
        values = [event.get(name) for name in columns]
        conn.execute(
            f"""
            INSERT INTO gap_reentry_opportunities(
                {", ".join(columns)}, created_at, updated_at
            ) VALUES ({", ".join("?" for _ in columns)}, datetime('now'), datetime('now'))
            ON CONFLICT(opportunity_id) DO UPDATE SET
                new_signal_id=excluded.new_signal_id,
                state=excluded.state,
                reason=excluded.reason,
                first_open_at=COALESCE(excluded.first_open_at, gap_reentry_opportunities.first_open_at),
                first_open_price=COALESCE(excluded.first_open_price, gap_reentry_opportunities.first_open_price),
                first_batch_id=COALESCE(excluded.first_batch_id, gap_reentry_opportunities.first_batch_id),
                confirmation_count=excluded.confirmation_count,
                attempt_count=excluded.attempt_count,
                planned_entry_price=excluded.planned_entry_price,
                planned_stop_price=excluded.planned_stop_price,
                planned_take_profit=excluded.planned_take_profit,
                planned_qty=excluded.planned_qty,
                order_status=excluded.order_status,
                updated_at=datetime('now')
            """,
            values,
        )

    def get_gap_reentry_opportunity(self, opportunity_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM gap_reentry_opportunities WHERE opportunity_id=?",
                (opportunity_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_gap_reentry_signal(
        self, conn: sqlite3.Connection, opportunity_id: str, signal: dict
    ) -> None:
        conn.execute(
            """UPDATE gap_reentry_opportunities SET
                   new_signal_id=?, planned_entry_price=?, planned_stop_price=?,
                   planned_take_profit=?, planned_qty=?, updated_at=datetime('now')
               WHERE opportunity_id=?""",
            (
                signal.get("id"), signal.get("entry_price"), signal.get("stop_loss"),
                signal.get("take_profit"), signal.get("target_qty"), opportunity_id,
            ),
        )

    def get_gap_reentry_for_stock_date(self, trade_date: str, stock_code: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM gap_reentry_opportunities
                   WHERE trade_date=? AND stock_code=?
                   ORDER BY created_at DESC LIMIT 1""",
                (trade_date, stock_code),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_prior_buy_signal(self, stock_code: str, generated_before: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT signal_id, generated_at, raw_json FROM signals
                   WHERE stock_code=? AND action='buy' AND generated_at<?
                   ORDER BY generated_at DESC LIMIT 1""",
                (stock_code, generated_before),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(str(row["raw_json"]))
        result["id"] = str(result.get("id") or row["signal_id"])
        result["_ledger_generated_at"] = str(row["generated_at"])
        return result

    def record_signal(self, conn: sqlite3.Connection, signal: SignalRecord) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO signals(
                signal_id, run_id, trade_date, stock_code, jq_code, action,
                target_position, signal_price, stop_loss, take_profit, final_score,
                strategy_mode, generated_at, expires_at, raw_json, created_at,
                validated_at, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
            """,
            (
                signal.signal_id, signal.run_id, signal.trade_date, signal.code,
                signal.jq_code, signal.action, signal.position_pct, signal.signal_price,
                signal.stop_loss, signal.take_profit, signal.final_score,
                signal.strategy_mode or None, signal.generated_at, signal.expires_at,
                signal.raw_json,
                signal.validated_at or signal.generated_at,
                signal.published_at or signal.generated_at,
            ),
        )
        if cursor.rowcount == 1:
            return True
        row = conn.execute(
            """SELECT run_id, trade_date, stock_code, jq_code, action, target_position,
                      generated_at, expires_at, raw_json FROM signals WHERE signal_id = ?""",
            (signal.signal_id,),
        ).fetchone()
        old_raw = json.loads(row[8]) if row is not None else {}
        new_raw = json.loads(signal.raw_json)
        mutable = {"validated_at", "published_at"}
        if str(new_raw.get("action") or signal.action) == "sell":
            mutable.update({"price", "name"})
        old_identity = {key: value for key, value in old_raw.items() if key not in mutable}
        new_identity = {key: value for key, value in new_raw.items() if key not in mutable}
        if row is None or old_identity != new_identity:
            raise SignalConflictError(f"immutable signal conflict: {signal.signal_id}")
        conn.execute(
            "UPDATE signals SET validated_at=?, published_at=? WHERE signal_id=?",
            (
                signal.validated_at or signal.generated_at,
                signal.published_at or signal.generated_at,
                signal.signal_id,
            ),
        )
        return False

    def current_signal_parity(self, signals: list[dict]) -> tuple[int, bool]:
        """Compare only current JSON signals with ledger rows; never scan history."""
        def identity(item: dict) -> str:
            mutable = {"validated_at", "published_at"}
            if str(item.get("action") or "") == "sell":
                mutable.update({"price", "name"})
            return canonical_json({key: value for key, value in item.items() if key not in mutable})

        expected = {str(item.get("id")): identity(item) for item in signals if item.get("id")}
        if not expected:
            return 0, True
        found: dict[str, str] = {}
        ids = sorted(expected)
        with self.connect() as conn:
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT signal_id, raw_json FROM signals WHERE signal_id IN ({placeholders})", chunk
                ).fetchall()
                found.update((str(row[0]), identity(json.loads(row[1]))) for row in rows)
        return len(found), found == expected

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

    def upsert_execution_issue(
        self, conn: sqlite3.Connection, issue: dict[str, object]
    ) -> dict[str, object]:
        key = str(issue["issue_key"])
        previous = conn.execute(
            "SELECT * FROM execution_issue_state WHERE issue_key=?", (key,)
        ).fetchone()
        state = str(issue["state"])
        severity = str(issue["severity"])
        seen_at = str(issue["seen_at"])
        transitioned = previous is None or (
            str(previous["state"]) != state or str(previous["severity"]) != severity
            or previous["recovered_at"] is not None
        )
        first_seen = str(previous["first_seen_at"]) if previous else seen_at
        transition_at = seen_at if transitioned else str(previous["last_transition_at"])
        last_notified = str(previous["last_notified_at"] or "") if previous else ""
        stage_started = str(issue["stage_started_at"])
        if previous is not None and not transitioned:
            previous_details = json.loads(str(previous["details_json"] or "{}"))
            current_details = issue.get("details") or {}
            material_progress = (
                state == "PARTIAL_FILL_PENDING"
                and int(current_details.get("filled_qty") or 0)
                > int(previous_details.get("filled_qty") or 0)
            )
            if not material_progress:
                stage_started = str(previous["stage_started_at"])
        conn.execute(
            """INSERT INTO execution_issue_state(
               issue_key, object_type, object_id, state, severity, first_seen_at,
               stage_started_at, last_seen_at, last_transition_at, last_notified_at,
               recovered_at, signal_id, order_id, reconciliation_id, details_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
               ON CONFLICT(issue_key) DO UPDATE SET
               object_type=excluded.object_type, object_id=excluded.object_id,
               state=excluded.state, severity=excluded.severity,
               stage_started_at=excluded.stage_started_at, last_seen_at=excluded.last_seen_at,
               last_transition_at=excluded.last_transition_at, recovered_at=NULL,
               signal_id=excluded.signal_id, order_id=excluded.order_id,
               reconciliation_id=excluded.reconciliation_id, details_json=excluded.details_json""",
            (
                key, str(issue["object_type"]), str(issue["object_id"]), state, severity,
                first_seen, stage_started, seen_at, transition_at,
                last_notified or None, str(issue.get("signal_id") or "") or None,
                str(issue.get("order_id") or "") or None,
                str(issue.get("reconciliation_id") or "") or None,
                canonical_json(issue.get("details") or {}),
            ),
        )
        return {
            "issue_key": key,
            "previous_state": str(previous["state"]) if previous else "",
            "state": state,
            "severity": severity,
            "transitioned": transitioned,
            "reopened": bool(previous and previous["recovered_at"] is not None),
            "last_notified_at": last_notified,
            "last_transition_at": transition_at,
        }

    def mark_execution_issues_notified(
        self, conn: sqlite3.Connection, issue_keys: list[str], now: str
    ) -> None:
        keys = sorted({str(key) for key in issue_keys if str(key)})
        if not keys:
            return
        conn.executemany(
            "UPDATE execution_issue_state SET last_notified_at=? WHERE issue_key=?",
            ((now, key) for key in keys),
        )

    def recover_execution_issue(
        self, conn: sqlite3.Connection, issue_key: str, now: str
    ) -> dict[str, object] | None:
        row = conn.execute(
            "SELECT * FROM execution_issue_state WHERE issue_key=?", (issue_key,)
        ).fetchone()
        if row is None or row["recovered_at"] is not None:
            return None
        conn.execute(
            """UPDATE execution_issue_state SET state='RECOVERED', severity='INFO',
               last_seen_at=?, last_transition_at=?, recovered_at=? WHERE issue_key=?""",
            (now, now, now, issue_key),
        )
        return {
            "issue_key": issue_key, "previous_state": str(row["state"]),
            "state": "RECOVERED", "severity": "INFO", "transitioned": True,
            "reopened": False, "last_notified_at": str(row["last_notified_at"] or ""),
        }

    def reconcile_position_cycles(
        self,
        conn: sqlite3.Connection,
        positions: list[dict],
        snapshot_at: str,
    ) -> None:
        active_positions = {
            str(item.get("code") or "").strip(): item
            for item in positions
            if str(item.get("code") or "").strip() and int(float(item.get("qty") or 0)) > 0
        }
        active_codes = set(active_positions)
        current_codes = {
            str(row[0])
            for row in conn.execute(
                "SELECT stock_code FROM position_cycles WHERE status = 'active'"
            )
        }
        for code in current_codes - active_codes:
            conn.execute(
                """UPDATE position_cycles SET status='closed', closed_at=?, current_qty=0,
                   last_snapshot_at=?, updated_at=datetime('now')
                   WHERE stock_code=? AND status='active'""",
                (snapshot_at, snapshot_at, code),
            )

        for code, item in active_positions.items():
            qty = int(float(item.get("qty") or 0))
            entry_price = float(item.get("cost_price") or item.get("avg_cost") or 0)
            current_price = float(item.get("current_price") or item.get("price") or entry_price)
            stop_price = float(item.get("stop_price") or 0)
            row = conn.execute(
                "SELECT * FROM position_cycles WHERE stock_code=? AND status='active'",
                (code,),
            ).fetchone()
            if row is None:
                signal_row = conn.execute(
                    """SELECT signal_id, raw_json FROM signals
                       WHERE stock_code=? AND action='buy'
                       ORDER BY generated_at DESC, signal_id DESC LIMIT 1""",
                    (code,),
                ).fetchone()
                signal = json.loads(signal_row["raw_json"]) if signal_row is not None else {}
                signal_stop = float(signal.get("stop_loss") or 0)
                from exit_policy import validated_initial_stop_price
                stop_price = validated_initial_stop_price(
                    code, entry_price, signal_stop or stop_price,
                    float(signal.get("atr14") or item.get("atr14") or 0),
                )
                cycle_id = f"{code}-{snapshot_at.replace(' ', 'T')}-{uuid.uuid4().hex[:8]}"
                initial_r = round(max(entry_price - stop_price, 0.01), 4)
                conn.execute(
                    """INSERT INTO position_cycles(
                    position_cycle_id, stock_code, entry_signal_id, opened_at, status, mode,
                    initial_qty, current_qty, entry_price, initial_stop_price, initial_r,
                    atr14, market_state, highest_price, take_profit_stage, last_snapshot_at,
                    created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, datetime('now'), datetime('now'))""",
                    (
                        cycle_id, code, signal_row["signal_id"] if signal_row is not None else item.get("entry_signal_id"),
                        str(item.get("entry_time") or snapshot_at),
                        str(signal.get("signal_type") or item.get("mode") or "legacy_fixed"),
                        qty, qty, entry_price, stop_price, initial_r,
                        float(signal.get("atr14") or item.get("atr14") or 0),
                        str(signal.get("market_regime") or item.get("market_state") or ""),
                        max(entry_price, current_price), snapshot_at,
                    ),
                )
                continue
            target_half = int(row["initial_qty"]) // 2 // 100 * 100
            stage = max(int(row["take_profit_stage"]), int(qty <= target_half))
            added = qty > int(row["current_qty"])
            updated_entry = entry_price if added else float(row["entry_price"])
            from exit_policy import validated_initial_stop_price
            repaired_stop = max(
                float(row["initial_stop_price"]),
                validated_initial_stop_price(
                    code, updated_entry, float(row["initial_stop_price"]), float(row["atr14"]),
                ),
            )
            if repaired_stop > float(row["initial_stop_price"]):
                conn.execute(
                    """INSERT INTO control_events(event_id, action, operator, old_value, new_value,
                       reason, created_at) VALUES (?, 'repair_initial_stop', 'system:migration-v8', ?, ?,
                       '按真实持仓成本和板块最大亏损边界只上调修复', ?)""",
                    (f"stop-repair-{row['position_cycle_id']}", str(row["initial_stop_price"]), str(repaired_stop), snapshot_at),
                )
            updated_r = round(max(updated_entry - repaired_stop, 0.01), 4)
            conn.execute(
                """UPDATE position_cycles SET current_qty=?, entry_price=?, initial_stop_price=?, initial_r=?, highest_price=?,
                   take_profit_stage=?, last_snapshot_at=?, updated_at=datetime('now')
                   WHERE position_cycle_id=?""",
                (
                    qty, updated_entry, repaired_stop, updated_r, max(float(row["highest_price"]), current_price), stage,
                    snapshot_at, row["position_cycle_id"],
                ),
            )

    def set_manual_stop(
        self,
        conn: sqlite3.Connection,
        code: str,
        value: float | None,
        reason: str,
        operator: str = "portfolio_web",
        now: str = "",
    ) -> dict:
        row = conn.execute(
            "SELECT * FROM position_cycles WHERE stock_code=? AND status='active'", (code,)
        ).fetchone()
        if row is None:
            raise ValueError(f"未找到活动持仓周期：{code}")
        old = float(row["manual_stop_price"] or 0)
        new = float(value or 0)
        if new < 0:
            raise ValueError("人工止损必须大于 0")
        if new and new < float(row["initial_stop_price"]):
            raise ValueError("人工止损不能低于冻结初始止损")
        if old and new and new < old:
            raise ValueError("人工止损只允许上调；需要放宽时请先清除并重新评审")
        if not reason.strip():
            raise ValueError("修改人工止损必须填写原因")
        conn.execute(
            "UPDATE position_cycles SET manual_stop_price=?, updated_at=datetime('now') WHERE position_cycle_id=?",
            (new or None, row["position_cycle_id"]),
        )
        event_time = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO control_events(event_id, action, operator, old_value, new_value, reason, created_at)
               VALUES (?, 'set_manual_stop', ?, ?, ?, ?, ?)""",
            (f"manual-stop-{uuid.uuid4().hex}", operator, str(old or ""), str(new or ""), reason.strip(), event_time),
        )
        return {**dict(row), "manual_stop_price": new or None}

    def get_active_position_cycles(self) -> dict[str, dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM position_cycles WHERE status='active' ORDER BY stock_code"
            ).fetchall()
        return {str(row["stock_code"]): dict(row) for row in rows}

    @staticmethod
    def _signal_classification(raw_json: str) -> dict[str, str]:
        try:
            raw = json.loads(raw_json)
        except (TypeError, ValueError):
            raw = {}
        industry = str(raw.get("industry") or raw.get("sector") or "").strip()
        theme = str(raw.get("theme") or raw.get("theme_label") or raw.get("concept") or industry).strip()
        return {"industry": industry, "theme": theme}

    def get_active_position_classifications(self) -> dict[str, dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT p.stock_code, s.raw_json
                   FROM position_cycles p
                   LEFT JOIN signals s ON s.signal_id=p.entry_signal_id
                   WHERE p.status='active' ORDER BY p.stock_code"""
            ).fetchall()
        return {
            str(row["stock_code"]): self._signal_classification(row["raw_json"] or "{}")
            for row in rows
        }

    def get_pending_buy_classification_exposures(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT o.stock_code, s.raw_json
                   FROM orders o JOIN signals s ON s.signal_id=o.signal_id
                   WHERE o.action='buy' AND o.status IN ('submitting','submitted','held','open','partial')
                   ORDER BY o.stock_code, o.client_order_id"""
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                raw = {}
            result.append({
                "code": str(row["stock_code"]),
                **self._signal_classification(row["raw_json"] or "{}"),
                "position_pct": float(raw.get("position_pct") or 0),
            })
        return result

    def backup_to(self, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)

    def integrity_check(self) -> str:
        with self.connect() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row is not None else "missing"

    def reconcile_order_events(self, conn: sqlite3.Connection, events: list[dict], snapshot_at: str) -> None:
        for event in events:
            signal_id = str(event.get("id") or "").strip()
            if not signal_id:
                continue
            order_id = str(event.get("order_id") or "").strip()
            event_key = order_id or f"{signal_id}:{event.get('datetime') or snapshot_at}"
            values = (
                event_key, signal_id, order_id or None, str(event.get("code") or ""),
                str(event.get("action") or ""), event.get("target_qty"),
                abs(int(float(event.get("amount") or 0))), abs(int(float(event.get("filled") or 0))),
                str(event.get("status") or "unknown").lower(), str(event.get("reason") or ""),
                str(event.get("datetime") or snapshot_at), snapshot_at, canonical_json(event),
            )
            conn.execute(
                """INSERT INTO order_events(event_key, signal_id, order_id, stock_code, action,
                   target_qty, requested_qty, filled_qty, status, reason, event_at, snapshot_at, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_key) DO UPDATE SET filled_qty=excluded.filled_qty,
                   status=excluded.status, reason=excluded.reason, snapshot_at=excluded.snapshot_at,
                   raw_json=excluded.raw_json""",
                values,
            )

    def upsert_order(self, conn: sqlite3.Connection, order: dict) -> bool:
        row = conn.execute(
            "SELECT * FROM orders WHERE client_order_id=? OR (order_id IS NOT NULL AND order_id=?) LIMIT 1",
            (order["client_order_id"], order.get("order_id")),
        ).fetchone()
        signal_id = order.get("signal_id")
        if signal_id and conn.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone() is None:
            signal_id = None
        terminal = {"filled", "cancelled", "rejected", "risk_rejected"}
        if row is None:
            conn.execute(
                """INSERT INTO orders(
                   client_order_id, signal_id, order_id, stock_code, action, target_qty,
                   requested_qty, filled_qty, average_fill_price, status, submit_count,
                   reason, first_submitted_at, updated_at, completed_at, raw_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order["client_order_id"], signal_id, order.get("order_id"), order["stock_code"],
                    order["action"], order.get("target_qty"), order["requested_qty"], order["filled_qty"],
                    order["average_fill_price"], order["status"], order["submit_count"], order["reason"],
                    order.get("first_submitted_at"), order["updated_at"], order.get("completed_at"),
                    order["raw_json"],
                ),
            )
            client_id = str(order["client_order_id"])
            inserted = True
        else:
            client_id = str(row["client_order_id"])
            status = str(row["status"]) if str(row["status"]) in terminal else str(order["status"])
            filled_qty = max(int(row["filled_qty"]), int(order["filled_qty"]))
            conn.execute(
                """UPDATE orders SET signal_id=COALESCE(signal_id, ?), order_id=COALESCE(order_id, ?),
                   target_qty=COALESCE(?, target_qty), requested_qty=max(requested_qty, ?),
                   filled_qty=?, average_fill_price=CASE WHEN ?>0 THEN ? ELSE average_fill_price END,
                   status=?, submit_count=max(submit_count, ?), reason=?, updated_at=max(updated_at, ?),
                   completed_at=COALESCE(completed_at, ?), raw_json=? WHERE client_order_id=?""",
                (
                    signal_id, order.get("order_id"), order.get("target_qty"), order["requested_qty"],
                    filled_qty, order["average_fill_price"], order["average_fill_price"], status,
                    order["submit_count"], order["reason"], order["updated_at"], order.get("completed_at"),
                    order["raw_json"], client_id,
                ),
            )
            inserted = False
        if order.get("order_id"):
            conn.execute(
                "UPDATE fills SET client_order_id=?, signal_id=COALESCE(signal_id, ?) WHERE order_id=?",
                (client_id, signal_id, order["order_id"]),
            )
        return inserted

    def insert_fill(self, conn: sqlite3.Connection, fill: dict) -> bool:
        existing = conn.execute("SELECT * FROM fills WHERE fill_id=?", (fill["fill_id"],)).fetchone()
        keys = (
            "order_id", "stock_code", "action", "qty", "price", "commission",
            "stamp_tax", "other_fee", "filled_at", "raw_json",
        )
        expected = tuple(fill.get(key) for key in keys[:-1]) + (canonical_json(fill["raw_json"]),)
        if existing is not None:
            actual = tuple(existing[key] for key in keys[:-1]) + (canonical_json(existing["raw_json"]),)
            if actual != expected:
                raise FillConflictError(f"immutable fill conflict: {fill['fill_id']}")
            if (
                str(existing["fee_data_status"]) == "unknown"
                and str(fill.get("fee_data_status") or "unknown") == "reported"
            ):
                conn.execute(
                    "UPDATE fills SET fee_data_status='reported' WHERE fill_id=?",
                    (fill["fill_id"],),
                )
            return False
        client_id = fill.get("client_order_id")
        if client_id and conn.execute("SELECT 1 FROM orders WHERE client_order_id=?", (client_id,)).fetchone() is None:
            client_id = None
        signal_id = fill.get("signal_id")
        if signal_id and conn.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone() is None:
            signal_id = None
        conn.execute(
            """INSERT INTO fills(fill_id, client_order_id, order_id, signal_id, stock_code,
               action, qty, price, commission, stamp_tax, other_fee, filled_at, raw_json,
               fee_data_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fill["fill_id"], client_id, fill.get("order_id"), signal_id, fill["stock_code"],
                fill["action"], fill["qty"], fill["price"], fill["commission"], fill["stamp_tax"],
                fill["other_fee"], fill["filled_at"], fill["raw_json"],
                fill.get("fee_data_status") or "unknown",
            ),
        )
        return True

    def upsert_exit_intent(self, conn: sqlite3.Connection, signal_id: str, code: str,
                           target_qty: int, reason: str, created_at: str) -> bool:
        from exit_policy import exit_priority

        active = conn.execute(
            "SELECT signal_id, target_qty, reason FROM exit_intents WHERE stock_code=? AND status='active' LIMIT 1",
            (code,),
        ).fetchone()
        if active is not None and str(active["signal_id"]) != signal_id:
            old_priority = exit_priority(
                f"{active['signal_id']} {active['reason'] or ''}"
            )
            new_priority = exit_priority(f"{signal_id} {reason}")
            if new_priority < old_priority or (
                new_priority == old_priority
                and int(target_qty) >= int(active["target_qty"])
            ):
                return False
        conn.execute(
            "UPDATE exit_intents SET status='superseded', updated_at=? WHERE stock_code=? AND status='active' AND signal_id<>?",
            (created_at, code, signal_id),
        )
        conn.execute(
            """INSERT INTO exit_intents(signal_id, stock_code, target_qty, reason, status,
               remaining_qty, created_at, updated_at, validated_at, published_at)
               VALUES (?, ?, ?, ?, 'active', 0, ?, ?, ?, ?)
               ON CONFLICT(signal_id) DO UPDATE SET target_qty=excluded.target_qty,
               reason=excluded.reason, updated_at=excluded.updated_at,
               validated_at=excluded.validated_at, published_at=excluded.published_at""",
            (signal_id, code, int(target_qty), reason, created_at, created_at, created_at, created_at),
        )
        return True

    def reconcile_exit_intents(self, conn: sqlite3.Connection, positions: list[dict], snapshot_at: str) -> None:
        quantities = {str(item.get("code") or ""): int(float(item.get("qty") or 0)) for item in positions}
        for row in conn.execute("SELECT signal_id, stock_code, target_qty, reason FROM exit_intents WHERE status='active'"):
            qty = quantities.get(str(row["stock_code"]), 0)
            target = int(row["target_qty"])
            status = "completed" if qty <= target else "active"
            conn.execute(
                "UPDATE exit_intents SET status=?, remaining_qty=?, updated_at=? WHERE signal_id=?",
                (status, max(0, qty - target), snapshot_at, row["signal_id"]),
            )
            if status == "completed":
                reason = str(row["reason"] or "sell")
                days = 5 if "hard_stop" in reason else 2 if "time_stop" in reason else 1
                conn.execute(
                    """INSERT INTO trade_cooldowns(stock_code, reason, until_date, updated_at)
                       VALUES (?, ?, date(?, ?), ?) ON CONFLICT(stock_code) DO UPDATE SET
                       reason=excluded.reason, until_date=excluded.until_date, updated_at=excluded.updated_at""",
                    (row["stock_code"], reason, snapshot_at[:10], f"+{days} days", snapshot_at),
                )

    def get_open_exit_intents(self) -> dict[str, dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM exit_intents WHERE status='active'").fetchall()
        return {str(row["stock_code"]): dict(row) for row in rows}

    def confirm_market_regime(self, observed: str) -> str:
        from trade_safety import MarketRegimeState
        raw = self.get_system_state("market_regime_confirmation", "")
        data = json.loads(raw) if raw else {}
        state = MarketRegimeState(
            str(data.get("current") or "NORMAL"), str(data.get("candidate") or ""),
            int(data.get("confirmations") or 0),
        ).advance(observed)
        with self.transaction() as conn:
            self.set_system_state(conn, "market_regime_confirmation", canonical_json(state.__dict__), "confirmed scans")
        return state.current

    def is_in_cooldown(self, code: str, trade_date: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT until_date FROM trade_cooldowns WHERE stock_code=?", (code,)).fetchone()
        return row is not None and str(row[0]) >= str(trade_date)[:10]

    def active_cooldown_codes(self, trade_date: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT stock_code FROM trade_cooldowns WHERE until_date>=?", (trade_date[:10],)).fetchall()
        return {str(row[0]) for row in rows}

    def daily_activity(self, trade_date: str) -> tuple[int, int]:
        with self.connect() as conn:
            buys = conn.execute(
                "SELECT COUNT(DISTINCT stock_code) FROM signals WHERE trade_date=? AND action='buy'",
                (trade_date[:10],),
            ).fetchone()[0]
            orders = conn.execute(
                "SELECT COUNT(*) FROM order_events WHERE substr(event_at,1,10)=?",
                (trade_date[:10],),
            ).fetchone()[0]
        return int(buys), int(orders)

    def prune_execution_history(
        self, conn: sqlite3.Connection, cutoff_date: str, now: str
    ) -> dict[str, int]:
        del now
        runs = conn.execute(
            """DELETE FROM reconciliation_runs
               WHERE substr(started_at, 1, 10) < ? AND result='matched'
               AND NOT EXISTS (
                   SELECT 1 FROM reconciliation_items
                   WHERE reconciliation_items.reconciliation_id=reconciliation_runs.reconciliation_id
               )""",
            (cutoff_date[:10],),
        ).rowcount
        snapshots = conn.execute(
            """DELETE FROM account_snapshots
               WHERE trade_date < ? AND snapshot_id NOT IN (
                   SELECT snapshot_id FROM reconciliation_runs
                   WHERE result<>'matched' AND snapshot_id IS NOT NULL
               )""",
            (cutoff_date[:10],),
        ).rowcount
        strategy_runs = conn.execute(
            """DELETE FROM strategy_runs
               WHERE trade_date < ?
               AND NOT EXISTS (
                   SELECT 1 FROM signals
                   WHERE signals.run_id=strategy_runs.run_id
               )""",
            (cutoff_date[:10],),
        ).rowcount
        return {
            "account_snapshots": max(0, int(snapshots)),
            "reconciliation_runs": max(0, int(runs)),
            "strategy_runs": max(0, int(strategy_runs)),
        }
