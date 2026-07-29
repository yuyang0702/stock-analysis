from __future__ import annotations

import sqlite3
import hashlib
import json
import re
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from types import MappingProxyType
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


def order_allowed_quantity(requested_qty: object, target_qty: object) -> int:
    requested = int(requested_qty or 0)
    return requested if requested > 0 else int(target_qty or 0)

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

SCHEMA_V10_REQUIRED_COLUMNS = MappingProxyType({
    "schema_migrations": frozenset({"version", "applied_at"}),
    "strategy_runs": frozenset({
        "run_id", "trade_date", "started_at", "finished_at", "git_commit",
        "strategy_version", "parameters_version", "data_status", "result",
        "error_message", "created_at", "updated_at",
    }),
    "signals": frozenset({
        "signal_id", "run_id", "trade_date", "stock_code", "jq_code",
        "action", "target_position", "signal_price", "stop_loss",
        "take_profit", "final_score", "strategy_mode", "generated_at",
        "expires_at", "raw_json", "created_at", "validated_at",
        "published_at",
    }),
    "risk_decisions": frozenset({
        "decision_id", "signal_id", "risk_mode", "allowed",
        "hard_block_code", "shadow_codes", "cash", "total_assets",
        "position_value", "current_single_exposure",
        "projected_single_exposure", "current_portfolio_exposure",
        "projected_portfolio_exposure", "current_industry_exposure",
        "projected_industry_exposure", "daily_profit_loss",
        "account_drawdown", "turnover_rate", "snapshot_at", "raw_json",
        "decided_at",
    }),
    "system_state": frozenset({"key", "value", "updated_at", "reason"}),
    "position_cycles": frozenset({
        "position_cycle_id", "stock_code", "entry_signal_id", "opened_at",
        "closed_at", "status", "mode", "initial_qty", "current_qty",
        "entry_price", "initial_stop_price", "initial_r", "atr14",
        "market_state", "highest_price", "take_profit_stage",
        "last_snapshot_at", "created_at", "updated_at", "manual_stop_price",
    }),
    "order_events": frozenset({
        "event_key", "signal_id", "order_id", "stock_code", "action",
        "target_qty", "requested_qty", "filled_qty", "status", "reason",
        "event_at", "snapshot_at", "raw_json",
    }),
    "exit_intents": frozenset({
        "signal_id", "stock_code", "target_qty", "reason", "status",
        "remaining_qty", "created_at", "updated_at", "validated_at",
        "published_at",
    }),
    "trade_cooldowns": frozenset({
        "stock_code", "reason", "until_date", "updated_at",
    }),
    "orders": frozenset({
        "client_order_id", "signal_id", "order_id", "stock_code", "action",
        "target_qty", "requested_qty", "filled_qty", "average_fill_price",
        "status", "submit_count", "reason", "first_submitted_at",
        "updated_at", "completed_at", "raw_json",
    }),
    "fills": frozenset({
        "fill_id", "client_order_id", "order_id", "signal_id", "stock_code",
        "action", "qty", "price", "commission", "stamp_tax", "other_fee",
        "filled_at", "raw_json", "fee_data_status",
    }),
    "account_snapshots": frozenset({
        "snapshot_id", "trade_date", "generated_at", "received_at", "cash",
        "available_cash", "total_value", "position_market_value",
        "daily_turnover_pct", "daily_pnl_pct", "account_drawdown_pct",
        "template_version", "state_hash", "retained_details", "raw_json",
    }),
    "position_snapshots": frozenset({
        "snapshot_id", "stock_code", "qty", "closeable_qty", "locked_qty",
        "today_qty", "avg_cost", "price", "market_value", "pnl",
    }),
    "daily_equity": frozenset({
        "trade_date", "opening_equity", "closing_equity", "cash",
        "position_market_value", "realized_pnl", "unrealized_pnl", "fees",
        "net_deposit", "max_drawdown_pct", "first_snapshot_at",
        "last_snapshot_at", "fee_data_status", "realized_pnl_status",
    }),
    "reconciliation_runs": frozenset({
        "reconciliation_id", "mode", "snapshot_id", "started_at",
        "finished_at", "result", "severity", "difference_count",
        "control_action", "summary_json",
    }),
    "reconciliation_items": frozenset({
        "item_id", "reconciliation_id", "category", "object_id",
        "reason_code", "local_value", "platform_value", "tolerance",
        "severity", "details_json",
    }),
    "control_events": frozenset({
        "event_id", "action", "operator", "old_value", "new_value",
        "reason", "reconciliation_id", "created_at",
    }),
    "execution_issue_state": frozenset({
        "issue_key", "object_type", "object_id", "state", "severity",
        "first_seen_at", "stage_started_at", "last_seen_at",
        "last_transition_at", "last_notified_at", "recovered_at",
        "signal_id", "order_id", "reconciliation_id", "details_json",
    }),
    "gap_reentry_opportunities": frozenset({
        "opportunity_id", "trade_date", "stock_code", "parent_signal_id",
        "new_signal_id", "state", "reason", "original_entry_price",
        "original_stop_price", "original_risk_r", "reentry_cap_price",
        "first_open_at", "first_open_price", "first_batch_id",
        "confirmation_count", "attempt_count", "planned_entry_price",
        "planned_stop_price", "planned_take_profit", "planned_qty",
        "order_status", "created_at", "updated_at",
    }),
})

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
        "expires_at", "status_updated_at",
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

SCHEMA_V11_REQUIRED_COLUMNS = MappingProxyType({
    **SCHEMA_V10_REQUIRED_COLUMNS,
    **{
        table: frozenset(columns)
        for table, columns in SCHEMA_V11_TABLES.items()
    },
    "position_cycles": (
        SCHEMA_V10_REQUIRED_COLUMNS["position_cycles"]
        | frozenset({
            "profit_protection_activated_at", "trailing_stop_active_from",
        })
    ),
    "reconciliation_runs": (
        SCHEMA_V10_REQUIRED_COLUMNS["reconciliation_runs"]
        | frozenset({
            "account_scope_id", "broker_snapshot_id",
            "broker_snapshot_sha256", "snapshot_broker_time",
            "snapshot_generated_at",
        })
    ),
})

SCHEMA_REQUIRED_COLUMNS_BY_VERSION = MappingProxyType({
    10: SCHEMA_V10_REQUIRED_COLUMNS,
    11: SCHEMA_V11_REQUIRED_COLUMNS,
})

SCHEMA_V10_PRIMARY_KEYS = MappingProxyType({
    "schema_migrations": ("version",),
    "strategy_runs": ("run_id",),
    "signals": ("signal_id",),
    "risk_decisions": ("decision_id",),
    "system_state": ("key",),
    "position_cycles": ("position_cycle_id",),
    "order_events": ("event_key",),
    "exit_intents": ("signal_id",),
    "trade_cooldowns": ("stock_code",),
    "orders": ("client_order_id",),
    "fills": ("fill_id",),
    "account_snapshots": ("snapshot_id",),
    "position_snapshots": ("snapshot_id", "stock_code"),
    "daily_equity": ("trade_date",),
    "reconciliation_runs": ("reconciliation_id",),
    "reconciliation_items": ("item_id",),
    "control_events": ("event_id",),
    "execution_issue_state": ("issue_key",),
    "gap_reentry_opportunities": ("opportunity_id",),
})

SCHEMA_V10_UNIQUE_KEYS = MappingProxyType({
    "orders": frozenset({("order_id",)}),
    "gap_reentry_opportunities": frozenset({
        ("trade_date", "stock_code", "opportunity_id"),
    }),
})

SCHEMA_V10_NAMED_INDEXES = MappingProxyType({
    "idx_strategy_runs_trade_date": (
        "strategy_runs", ("trade_date",), False, None,
    ),
    "idx_signals_action": ("signals", ("action",), False, None),
    "idx_signals_run_id": ("signals", ("run_id",), False, None),
    "idx_risk_decisions_signal_id": (
        "risk_decisions", ("signal_id",), False, None,
    ),
    "idx_position_cycles_active_code": (
        "position_cycles", ("stock_code",), True, "status = 'active'",
    ),
    "idx_position_cycles_status": (
        "position_cycles", ("status",), False, None,
    ),
    "idx_order_events_signal": (
        "order_events", ("signal_id",), False, None,
    ),
    "idx_order_events_status": (
        "order_events", ("status",), False, None,
    ),
    "idx_exit_intents_active_code": (
        "exit_intents", ("stock_code",), True, "status = 'active'",
    ),
    "idx_orders_signal": ("orders", ("signal_id",), False, None),
    "idx_orders_status": ("orders", ("status",), False, None),
    "idx_fills_order": ("fills", ("order_id",), False, None),
    "idx_fills_signal": ("fills", ("signal_id",), False, None),
    "idx_fills_time": ("fills", ("filled_at",), False, None),
    "idx_account_snapshots_trade_date": (
        "account_snapshots", ("trade_date", "generated_at"), False, None,
    ),
    "idx_reconciliation_runs_time": (
        "reconciliation_runs", ("started_at",), False, None,
    ),
    "idx_reconciliation_runs_result": (
        "reconciliation_runs", ("result", "severity"), False, None,
    ),
    "idx_reconciliation_items_reason": (
        "reconciliation_items", ("reason_code", "severity"), False, None,
    ),
    "idx_execution_issue_state_active": (
        "execution_issue_state",
        ("recovered_at", "severity", "last_seen_at"),
        False,
        None,
    ),
    "idx_gap_reentry_trade_state": (
        "gap_reentry_opportunities", ("trade_date", "state"), False, None,
    ),
    "idx_gap_reentry_code_date": (
        "gap_reentry_opportunities", ("stock_code", "trade_date"), False, None,
    ),
})

SCHEMA_V10_FOREIGN_KEYS = MappingProxyType({
    "signals": frozenset({
        (("run_id", "strategy_runs", "run_id", "NO ACTION"),),
    }),
    "risk_decisions": frozenset({
        (("signal_id", "signals", "signal_id", "NO ACTION"),),
    }),
    "orders": frozenset({
        (("signal_id", "signals", "signal_id", "NO ACTION"),),
    }),
    "fills": frozenset({
        (("signal_id", "signals", "signal_id", "NO ACTION"),),
        (("client_order_id", "orders", "client_order_id", "SET NULL"),),
    }),
    "position_snapshots": frozenset({
        (("snapshot_id", "account_snapshots", "snapshot_id", "CASCADE"),),
    }),
    "reconciliation_runs": frozenset({
        (("snapshot_id", "account_snapshots", "snapshot_id", "SET NULL"),),
    }),
    "reconciliation_items": frozenset({
        ((
            "reconciliation_id", "reconciliation_runs",
            "reconciliation_id", "CASCADE",
        ),),
    }),
    "control_events": frozenset({
        ((
            "reconciliation_id", "reconciliation_runs",
            "reconciliation_id", "SET NULL",
        ),),
    }),
})


def required_schema_columns(version: int) -> MappingProxyType:
    try:
        return SCHEMA_REQUIRED_COLUMNS_BY_VERSION[int(version)]
    except KeyError as exc:
        raise RuntimeError(f"unsupported schema version: {version}") from exc


def _normalize_index_predicate(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([=<>!]+)\s*", r"\1", text)
    return text


def _index_predicate(conn: sqlite3.Connection, index_name: str) -> str | None:
    row = conn.execute(
        """SELECT sql FROM sqlite_master
           WHERE type='index' AND name=?""",
        (index_name,),
    ).fetchone()
    match = re.search(
        r"\bwhere\b(.*)$",
        str(row[0] or "") if row is not None else "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _normalize_index_predicate(match.group(1)) if match else None


def _index_key_signature(
    conn: sqlite3.Connection,
    index_name: str,
) -> tuple[tuple[str, bool, str], ...]:
    return tuple(
        (
            str(row[2]),
            bool(row[3]),
            str(row[4] or "BINARY").upper(),
        )
        for row in sorted(
            (
                row
                for row in conn.execute(
                    f'PRAGMA index_xinfo("{index_name}")'
                )
                if int(row[5]) == 1
            ),
            key=lambda row: int(row[0]),
        )
    )


def _unique_index_signatures(
    conn: sqlite3.Connection,
    table: str,
) -> tuple[
    tuple[tuple[tuple[str, bool, str], ...], bool, str | None], ...
]:
    signatures = []
    for index in conn.execute(f"PRAGMA index_list({table})"):
        if not int(index[2]) or str(index[3]) == "pk":
            continue
        index_name = str(index[1])
        partial = bool(index[4])
        signatures.append((
            _index_key_signature(conn, index_name),
            partial,
            _index_predicate(conn, index_name) if partial else None,
        ))
    return tuple(sorted(signatures, key=repr))


def _primary_key_index_signature(
    conn: sqlite3.Connection,
    table: str,
    table_info: list[sqlite3.Row],
) -> tuple[tuple[str, bool, str], ...]:
    indexes = [
        row for row in conn.execute(f"PRAGMA index_list({table})")
        if str(row[3]) == "pk"
    ]
    if indexes:
        if len(indexes) != 1:
            return ()
        return _index_key_signature(conn, str(indexes[0][1]))
    primary_columns = [row for row in table_info if int(row[5]) > 0]
    if (
        len(primary_columns) == 1
        and str(primary_columns[0][2]).strip().upper() == "INTEGER"
    ):
        return ((str(primary_columns[0][1]), False, "BINARY"),)
    return ()


def _expected_unique_index_signatures(
    table: str,
    table_unique_keys: object,
    named_indexes: object,
) -> tuple[
    tuple[tuple[tuple[str, bool, str], ...], bool, str | None], ...
]:
    signatures = [
        (
            tuple((str(column), False, "BINARY") for column in columns),
            False,
            None,
        )
        for columns in table_unique_keys.get(table, ())
    ]
    for _, (
        index_table, columns, unique, where,
    ) in named_indexes.items():
        if index_table == table and unique:
            signatures.append((
                tuple(
                    (str(column), False, "BINARY")
                    for column in columns
                ),
                where is not None,
                _normalize_index_predicate(where),
            ))
    return tuple(sorted(signatures, key=repr))


def validate_schema_contract(conn: sqlite3.Connection, version: int) -> None:
    required = required_schema_columns(version)
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = set(required) - tables
    if missing_tables:
        raise RuntimeError(
            f"schema {version} missing required tables: {sorted(missing_tables)}"
        )
    for table, required_columns in required.items():
        table_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        actual_columns = {str(row[1]) for row in table_info}
        missing_columns = required_columns - actual_columns
        if missing_columns:
            raise RuntimeError(
                f"schema {version} table {table} missing columns: "
                f"{sorted(missing_columns)}"
            )
        if table in SCHEMA_V10_PRIMARY_KEYS:
            actual_primary_key = tuple(
                str(row[1])
                for row in sorted(
                    (row for row in table_info if int(row[5]) > 0),
                    key=lambda row: int(row[5]),
                )
            )
            if actual_primary_key != SCHEMA_V10_PRIMARY_KEYS[table]:
                raise RuntimeError(
                    f"schema {version} table {table} has invalid primary key"
                )
            expected_primary_index = tuple(
                (column, False, "BINARY")
                for column in SCHEMA_V10_PRIMARY_KEYS[table]
            )
            if _primary_key_index_signature(
                conn, table, table_info,
            ) != expected_primary_index:
                raise RuntimeError(
                    f"schema {version} table {table} has invalid primary key index"
                )
            actual_unique_indexes = _unique_index_signatures(conn, table)
            expected_unique_indexes = _expected_unique_index_signatures(
                table,
                SCHEMA_V10_UNIQUE_KEYS,
                SCHEMA_V10_NAMED_INDEXES,
            )
            if actual_unique_indexes != expected_unique_indexes:
                expected_names = sorted(
                    name
                    for name, (
                        index_table, _, unique, _,
                    ) in SCHEMA_V10_NAMED_INDEXES.items()
                    if index_table == table and unique
                )
                raise RuntimeError(
                    f"schema {version} table {table} has invalid unique "
                    f"indexes: {expected_names}"
                )
            grouped: dict[int, list[sqlite3.Row]] = {}
            for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
                grouped.setdefault(int(row[0]), []).append(row)
            actual_foreign_keys = {
                tuple(
                    (
                        str(row[3]), str(row[2]), str(row[4]), str(row[6]),
                    )
                    for row in sorted(rows, key=lambda item: int(item[1]))
                )
                for rows in grouped.values()
            }
            expected_foreign_keys = SCHEMA_V10_FOREIGN_KEYS.get(
                table, frozenset(),
            )
            if version >= 11 and table == "reconciliation_runs":
                expected_foreign_keys = expected_foreign_keys | frozenset({
                    ((
                        "account_scope_id", "account_scopes",
                        "account_scope_id", "NO ACTION",
                    ),),
                })
            if actual_foreign_keys != expected_foreign_keys:
                raise RuntimeError(
                    f"schema {version} table {table} has invalid foreign keys"
                )
    for index_name, (
        expected_table, expected_columns, expected_unique, expected_where,
    ) in SCHEMA_V10_NAMED_INDEXES.items():
        index_row = conn.execute(
            """SELECT tbl_name, sql FROM sqlite_master
               WHERE type='index' AND name=?""",
            (index_name,),
        ).fetchone()
        actual_columns = _index_key_signature(conn, index_name)
        expected_key = tuple(
            (str(column), False, "BINARY")
            for column in expected_columns
        )
        table_index = None
        if index_row is not None:
            table_index = next(
                (
                    row for row in conn.execute(
                        f"PRAGMA index_list({expected_table})"
                    )
                    if str(row[1]) == index_name
                ),
                None,
            )
        actual_where = _index_predicate(conn, index_name)
        normalized_expected_where = _normalize_index_predicate(expected_where)
        if (
            index_row is None
            or str(index_row[0]) != expected_table
            or actual_columns != expected_key
            or table_index is None
            or bool(table_index[2]) != expected_unique
            or bool(table_index[4]) != (expected_where is not None)
            or actual_where != normalized_expected_where
        ):
            raise RuntimeError(
                f"schema {version} index {index_name} is invalid"
            )

SCHEMA_V11_NAMED_INDEXES = {
    "idx_candidates_scope_signal": (
        "strategy_order_candidates",
        ("account_scope_id", "logical_signal_id"),
        False,
        None,
    ),
    "idx_execution_intents_scope_status": (
        "execution_intents", ("account_scope_id", "status"), False, None,
    ),
    "idx_reservations_scope_status": (
        "capacity_reservations", ("account_scope_id", "status"), False, None,
    ),
    "idx_broker_orders_scope_status": (
        "broker_order_current", ("account_scope_id", "status"), False, None,
    ),
}

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

SCHEMA_V11_FOREIGN_KEYS = MappingProxyType({
    "account_scopes": frozenset(),
    "broker_snapshot_current": frozenset({
        (("account_scope_id", "account_scopes", "account_scope_id", "NO ACTION"),),
    }),
    "broker_position_current": frozenset({
        (("account_scope_id", "account_scopes", "account_scope_id", "NO ACTION"),),
        ((
            "account_scope_id", "broker_snapshot_current",
            "account_scope_id", "CASCADE",
        ),),
    }),
    "broker_order_current": frozenset({
        (("account_scope_id", "account_scopes", "account_scope_id", "NO ACTION"),),
        ((
            "account_scope_id", "broker_snapshot_current",
            "account_scope_id", "CASCADE",
        ),),
    }),
    "strategy_order_candidates": frozenset({
        (("account_scope_id", "account_scopes", "account_scope_id", "NO ACTION"),),
    }),
    "pre_trade_results": frozenset({
        (("account_scope_id", "account_scopes", "account_scope_id", "NO ACTION"),),
        (
            (
                "account_scope_id", "strategy_order_candidates",
                "account_scope_id", "NO ACTION",
            ),
            (
                "candidate_id", "strategy_order_candidates",
                "candidate_id", "NO ACTION",
            ),
        ),
    }),
    "execution_intents": frozenset({
        (("account_scope_id", "account_scopes", "account_scope_id", "NO ACTION"),),
        (
            (
                "account_scope_id", "pre_trade_results",
                "account_scope_id", "NO ACTION",
            ),
            (
                "pre_trade_result_id", "pre_trade_results",
                "pre_trade_result_id", "NO ACTION",
            ),
        ),
    }),
    "capacity_reservations": frozenset({
        (("account_scope_id", "account_scopes", "account_scope_id", "NO ACTION"),),
        (
            (
                "account_scope_id", "execution_intents",
                "account_scope_id", "NO ACTION",
            ),
            (
                "client_order_id", "execution_intents",
                "client_order_id", "NO ACTION",
            ),
        ),
    }),
})

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
           REFERENCES account_scopes(account_scope_id),
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
           REFERENCES account_scopes(account_scope_id),
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
       FOREIGN KEY(account_scope_id)
           REFERENCES account_scopes(account_scope_id),
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
       status_updated_at TEXT NOT NULL,
       PRIMARY KEY(account_scope_id, client_order_id),
       UNIQUE(account_scope_id, pre_trade_result_id),
       FOREIGN KEY(account_scope_id)
           REFERENCES account_scopes(account_scope_id),
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
       FOREIGN KEY(account_scope_id)
           REFERENCES account_scopes(account_scope_id),
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
    def commit(self) -> None:
        self._trading_store_new_intents = None
        super().commit()

    def rollback(self) -> None:
        self._trading_store_new_intents = None
        super().rollback()

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
                if current_version == 10:
                    validate_schema_contract(conn, 10)
                    self._migrate_schema_v11(conn)
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
            validate_schema_contract(conn, 10)
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
            reconciliation_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(reconciliation_runs)")
            }
            reconciliation_additions = {
                "account_scope_id": (
                    "TEXT REFERENCES account_scopes(account_scope_id)"
                ),
                "broker_snapshot_id": "TEXT",
                "broker_snapshot_sha256": "TEXT",
                "snapshot_broker_time": "TEXT",
                "snapshot_generated_at": "TEXT",
            }
            for column, declaration in reconciliation_additions.items():
                if column not in reconciliation_columns:
                    conn.execute(
                        f"ALTER TABLE reconciliation_runs "
                        f"ADD COLUMN {column} {declaration}"
                    )
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
        validate_schema_contract(conn, 11)
        for table in SCHEMA_V11_TABLES:
            table_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
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
            expected_primary_index = tuple(
                (column, False, "BINARY")
                for column in SCHEMA_V11_PRIMARY_KEYS[table]
            )
            if _primary_key_index_signature(
                conn, table, table_info,
            ) != expected_primary_index:
                raise RuntimeError(
                    f"schema 11 table {table} has invalid primary key index"
                )
        for index_name, (
            expected_table, expected_columns, expected_unique, expected_where,
        ) in (
            SCHEMA_V11_NAMED_INDEXES.items()
        ):
            index_row = conn.execute(
                """SELECT tbl_name FROM sqlite_master
                   WHERE type='index' AND name=?""",
                (index_name,),
            ).fetchone()
            actual_columns = _index_key_signature(conn, index_name)
            expected_key = tuple(
                (str(column), False, "BINARY")
                for column in expected_columns
            )
            table_index = next(
                (
                    row
                    for row in conn.execute(
                        f"PRAGMA index_list({expected_table})"
                    )
                    if str(row[1]) == index_name
                ),
                None,
            )
            if (
                index_row is None
                or str(index_row[0]) != expected_table
                or actual_columns != expected_key
                or table_index is None
                or bool(table_index[2]) != expected_unique
                or bool(table_index[4]) != (expected_where is not None)
                or _index_predicate(
                    conn, index_name,
                ) != _normalize_index_predicate(expected_where)
            ):
                raise RuntimeError(
                    f"schema 11 index {index_name} is invalid"
                )
        for table in SCHEMA_V11_PRIMARY_KEYS:
            actual_unique = _unique_index_signatures(conn, table)
            expected_unique = _expected_unique_index_signatures(
                table,
                SCHEMA_V11_UNIQUE_KEYS,
                SCHEMA_V11_NAMED_INDEXES,
            )
            if actual_unique != expected_unique:
                raise RuntimeError(
                    f"schema 11 table {table} has invalid unique indexes"
                )
        for table, expected_groups in SCHEMA_V11_FOREIGN_KEYS.items():
            grouped: dict[int, list[sqlite3.Row]] = {}
            for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
                grouped.setdefault(int(row[0]), []).append(row)
            actual_groups = {
                tuple(
                    (
                        str(row[3]), str(row[2]), str(row[4]), str(row[6]),
                    )
                    for row in sorted(rows, key=lambda item: int(item[1]))
                )
                for rows in grouped.values()
            }
            if actual_groups != expected_groups:
                raise RuntimeError(
                    f"schema 11 table {table} has invalid foreign keys"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn._trading_store_transaction_generation = 0

            def track_transaction_boundary(statement: str) -> None:
                text = statement.lstrip()
                while text.startswith(("--", "/*")):
                    if text.startswith("--"):
                        text = text.partition("\n")[2].lstrip()
                    else:
                        _, marker, text = text.partition("*/")
                        if not marker:
                            return
                        text = text.lstrip()
                words = text.upper().split(None, 2)
                if not words or words[:2] in (["ROLLBACK", "TO"],):
                    return
                if words[0] in {"BEGIN", "COMMIT", "END", "ROLLBACK"}:
                    conn._trading_store_transaction_generation += 1

            conn.set_trace_callback(track_transaction_boundary)
            conn.execute("BEGIN IMMEDIATE")
            conn._trading_store_transaction_owner = id(self)
            conn._trading_store_owner_generation = (
                conn._trading_store_transaction_generation
            )
            conn._trading_store_new_intents = set()
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
            finally:
                conn._trading_store_new_intents = None
                conn._trading_store_transaction_owner = None
                conn._trading_store_owner_generation = None

    def health(self) -> StoreHealth:
        version = 0
        try:
            with self.connect() as conn:
                version = int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)
                conn.execute("SELECT 1").fetchone()
                if version == 11:
                    self._validate_schema_v11(conn)
            return StoreHealth(ok=version == SCHEMA_VERSION, schema_version=version)
        except Exception as exc:
            return StoreHealth(ok=False, schema_version=version, error=str(exc))

    @staticmethod
    def _money(value: object, name: str) -> Decimal:
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be a decimal") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
        return amount

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value, "f").rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _quantity(
        value: object,
        name: str,
        *,
        positive: bool,
    ) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        try:
            quantity = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not quantity.is_finite() or quantity != quantity.to_integral_value():
            raise ValueError(f"{name} must be a finite integer")
        if quantity < 0 or (positive and quantity == 0):
            qualifier = "positive" if positive else "nonnegative"
            raise ValueError(f"{name} must be {qualifier}")
        return int(quantity)

    @staticmethod
    def _required_text(value: object, name: str) -> str:
        if isinstance(value, bool):
            raise ValueError(f"{name} is required")
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{name} is required")
        return text

    @staticmethod
    def _aware_timestamp(value: object, name: str) -> str:
        text = TradingStore._required_text(value, name)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be a valid timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone")
        return parsed.isoformat()

    @staticmethod
    def _timestamp_instant(value: object, name: str) -> datetime:
        text = TradingStore._required_text(value, name)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be a valid timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.astimezone(timezone.utc)

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
            account_scope_id = str(row[0])
            if adapter == "joinquant" and scope_alias == "primary":
                self._adopt_legacy_execution_issues(conn, account_scope_id)
            return account_scope_id
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
            account_scope_id = str(row[0])
        if adapter == "joinquant" and scope_alias == "primary":
            self._adopt_legacy_execution_issues(conn, account_scope_id)
        return account_scope_id

    @staticmethod
    def _adopt_legacy_execution_issues(
        conn: sqlite3.Connection, account_scope_id: str,
    ) -> None:
        prefix = f"scope:{account_scope_id}:"
        severity_rank = {
            "INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3,
        }
        legacy_rows = conn.execute(
            """SELECT * FROM execution_issue_state
               WHERE issue_key NOT LIKE 'scope:%'"""
        ).fetchall()
        for legacy in legacy_rows:
            scoped_key = prefix + str(legacy["issue_key"])
            scoped = conn.execute(
                """SELECT * FROM execution_issue_state WHERE issue_key=?""",
                (scoped_key,),
            ).fetchone()
            if scoped is None:
                conn.execute(
                    """UPDATE execution_issue_state SET issue_key=?
                       WHERE issue_key=?""",
                    (scoped_key, legacy["issue_key"]),
                )
                continue
            winner = max(
                (scoped, legacy),
                key=lambda row: (
                    row["recovered_at"] is None,
                    severity_rank.get(str(row["severity"]), -1),
                    str(row["last_seen_at"]),
                ),
            )
            first_seen = min(
                str(scoped["first_seen_at"]), str(legacy["first_seen_at"]),
            )
            stage_started = min(
                str(scoped["stage_started_at"]),
                str(legacy["stage_started_at"]),
            )
            last_seen = max(
                str(scoped["last_seen_at"]), str(legacy["last_seen_at"]),
            )
            notified = max(
                str(scoped["last_notified_at"] or ""),
                str(legacy["last_notified_at"] or ""),
            )
            conn.execute(
                """UPDATE execution_issue_state SET
                   object_type=?, object_id=?, state=?, severity=?,
                   first_seen_at=?, stage_started_at=?, last_seen_at=?,
                   last_transition_at=?, last_notified_at=?, recovered_at=?,
                   signal_id=?, order_id=?, reconciliation_id=?, details_json=?
                   WHERE issue_key=?""",
                (
                    winner["object_type"], winner["object_id"],
                    winner["state"], winner["severity"], first_seen,
                    stage_started, last_seen, winner["last_transition_at"],
                    notified or None, winner["recovered_at"],
                    winner["signal_id"], winner["order_id"],
                    winner["reconciliation_id"], winner["details_json"],
                    scoped_key,
                ),
            )
            conn.execute(
                """DELETE FROM execution_issue_state WHERE issue_key=?""",
                (legacy["issue_key"],),
            )

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
        if (
            not conn.in_transaction
            or getattr(
                conn, "_trading_store_transaction_owner", None,
            ) != id(self)
        ):
            raise ValueError(
                "broker snapshot replacement requires store-owned BEGIN IMMEDIATE"
            )
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
        if status != "READY":
            raise ValueError("execution intent initial status must be READY")
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
        existed = conn.execute(
            """SELECT 1 FROM execution_intents
               WHERE account_scope_id=? AND client_order_id=?""",
            (record.account_scope_id, record.client_order_id),
        ).fetchone() is not None
        payload = contract_canonical_json(record.to_dict())
        identity = self._insert_immutable_fact(
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
                expires_at, status_updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
            parameters=(
                record.account_scope_id, record.client_order_id,
                record.pre_trade_result_id, record.intent_sha256,
                record.submission_attempt_id, payload, str(status), record.expires_at,
                record.pre_trade_result.checked_at,
            ),
        )
        if (
            not existed
            and getattr(
                conn, "_trading_store_transaction_owner", None,
            ) == id(self)
            and isinstance(
                getattr(conn, "_trading_store_new_intents", None), set,
            )
        ):
            conn._trading_store_new_intents.add((
                record.account_scope_id,
                record.client_order_id,
            ))
        return identity

    def compare_and_set_execution_intent_status(
        self,
        conn: sqlite3.Connection,
        account_scope_id: str,
        client_order_id: str,
        *,
        expected_status: str,
        new_status: str,
        transitioned_at: str,
    ) -> bool:
        account_scope_id = self._required_text(
            account_scope_id, "account_scope_id",
        )
        client_order_id = self._required_text(
            client_order_id, "client_order_id",
        )
        expected_status = self._required_text(
            expected_status, "expected_status",
        ).upper()
        new_status = self._required_text(new_status, "new_status").upper()
        transitioned_at = self._aware_timestamp(
            transitioned_at, "transitioned_at",
        )
        transitions = {
            "READY": {"SUBMITTING", "EXPIRED"},
            "SUBMITTING": {
                "SUBMITTED", "REJECTED", "NOT_SUBMITTED", "SUBMIT_UNKNOWN",
            },
            "SUBMITTED": {
                "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED",
            },
            "PARTIALLY_FILLED": {
                "PARTIALLY_FILLED", "FILLED", "CANCELLED",
            },
            "EXPIRED": set(),
            "NOT_SUBMITTED": set(),
            "REJECTED": set(),
            "CANCELLED": set(),
            "FILLED": set(),
            "SUBMIT_UNKNOWN": {
                "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED",
                "REJECTED", "NOT_SUBMITTED",
            },
        }
        if expected_status not in transitions or new_status not in transitions:
            raise ValueError("unknown execution intent status")
        if new_status not in transitions[expected_status]:
            raise ValueError(
                "invalid execution intent status transition"
            )
        current = conn.execute(
            """SELECT status, status_updated_at, expires_at FROM execution_intents
               WHERE account_scope_id=? AND client_order_id=?""",
            (account_scope_id, client_order_id),
        ).fetchone()
        if current is not None and str(current["status"]).upper() == expected_status:
            if self._timestamp_instant(
                transitioned_at, "transitioned_at",
            ) < self._timestamp_instant(
                current["status_updated_at"], "status_updated_at",
            ):
                raise ValueError("execution intent transition time cannot regress")
            expires_at = self._timestamp_instant(
                current["expires_at"], "expires_at",
            )
            transition_instant = self._timestamp_instant(
                transitioned_at, "transitioned_at",
            )
            if (
                expected_status == "READY"
                and new_status == "SUBMITTING"
                and transition_instant >= expires_at
            ):
                raise ValueError("expired execution intent cannot be submitted")
            if (
                expected_status == "READY"
                and new_status == "EXPIRED"
                and transition_instant < expires_at
            ):
                raise ValueError("execution intent cannot expire before expires_at")
        cursor = conn.execute(
            """UPDATE execution_intents SET status=?, status_updated_at=?
               WHERE account_scope_id=? AND client_order_id=? AND status=?""",
            (
                new_status, transitioned_at, account_scope_id,
                client_order_id, expected_status,
            ),
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
        account_scope_id = self._required_text(
            account_scope_id, "account_scope_id",
        )
        reservation_id = self._required_text(reservation_id, "reservation_id")
        client_order_id = self._required_text(
            client_order_id, "client_order_id",
        )
        stock_code = self._required_text(stock_code, "stock_code")
        side = self._required_text(side, "side").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        target_qty = self._quantity(target_qty, "target_qty", positive=True)
        if not isinstance(uncategorized, bool):
            raise ValueError("uncategorized must be a bool")
        industry = self._required_text(industry, "industry")
        theme = self._required_text(theme, "theme")
        expected_uncategorized = "__UNCATEGORIZED__" in {industry, theme}
        if uncategorized != expected_uncategorized:
            raise ValueError(
                "uncategorized must match normalized industry/theme"
            )
        created_at = self._aware_timestamp(created_at, "created_at")
        cash = self._money(cash_yuan, "cash_yuan")
        position_value = self._money(position_value_yuan, "position_value_yuan")
        open_risk = self._money(open_risk_yuan, "open_risk_yuan")
        evidence = conn.execute(
            """SELECT i.payload_json, i.intent_sha256, i.status,
                      i.expires_at, i.status_updated_at,
                      p.result_sha256, c.payload_sha256
               FROM execution_intents AS i
               JOIN pre_trade_results AS p
                 ON p.account_scope_id=i.account_scope_id
                AND p.pre_trade_result_id=i.pre_trade_result_id
               JOIN strategy_order_candidates AS c
                 ON c.account_scope_id=p.account_scope_id
                AND c.candidate_id=p.candidate_id
               WHERE i.account_scope_id=? AND i.client_order_id=?""",
            (account_scope_id, client_order_id),
        ).fetchone()
        if evidence is None:
            raise ValueError("capacity reservation requires execution intent")
        try:
            intent = ExecutionIntent.from_dict(
                json.loads(str(evidence["payload_json"]))
            )
        except Exception as exc:
            raise ValueError(
                "capacity reservation execution intent evidence is invalid"
            ) from exc
        result = intent.pre_trade_result
        candidate = result.candidate
        if (
            intent.intent_sha256 != str(evidence["intent_sha256"])
            or result.result_sha256 != str(evidence["result_sha256"])
            or candidate.payload_sha256 != str(evidence["payload_sha256"])
        ):
            raise ValueError(
                "capacity reservation signed evidence does not match ledger"
            )
        if (
            intent.account_scope_id != account_scope_id
            or intent.client_order_id != client_order_id
            or intent.code != stock_code
            or intent.side != side
            or intent.order_qty != target_qty
        ):
            raise ValueError(
                "capacity reservation does not match execution intent"
            )
        if (
            candidate.industry != industry
            or candidate.theme != theme
            or candidate.uncategorized != uncategorized
        ):
            raise ValueError(
                "capacity reservation classification does not match candidate"
            )
        if side == "buy":
            fee = result.execution_fee
            if fee is None or (
                position_value != fee.notional_yuan
                or cash != fee.notional_yuan + fee.total_yuan
                or open_risk != result.per_trade_risk_yuan
            ):
                raise ValueError(
                    "buy reservation amounts do not match signed evidence"
                )
        elif any(value != 0 for value in (cash, position_value, open_risk)):
            raise ValueError("sell reservation amounts must be zero")
        values = (
            account_scope_id, reservation_id, client_order_id, stock_code,
            side, target_qty, str(cash), str(position_value),
            str(open_risk), target_qty, str(cash), str(position_value),
            str(open_risk), industry, theme, int(uncategorized),
            "active", created_at,
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
                "position_value_yuan", "open_risk_yuan", "industry", "theme",
                "uncategorized", "created_at",
            )
            original_values = (
                account_scope_id, reservation_id, client_order_id, stock_code,
                side, target_qty, str(cash), str(position_value),
                str(open_risk), industry, theme, int(uncategorized), created_at,
            )
            if tuple(existing[name] for name in columns) == original_values:
                return reservation_id
            raise ValueError("capacity reservation immutable ID conflict")
        if str(evidence["status"]).upper() != "READY":
            raise ValueError("capacity reservation requires READY execution intent")
        created_instant = self._timestamp_instant(created_at, "created_at")
        if created_instant < self._timestamp_instant(
            evidence["status_updated_at"], "status_updated_at",
        ) or created_instant > self._timestamp_instant(
            evidence["expires_at"], "expires_at",
        ):
            raise ValueError(
                "capacity reservation creation time is outside intent validity"
            )
        new_intents = getattr(conn, "_trading_store_new_intents", None)
        if (
            not conn.in_transaction
            or getattr(conn, "_trading_store_transaction_owner", None) != id(self)
            or getattr(conn, "_trading_store_owner_generation", None)
            != getattr(conn, "_trading_store_transaction_generation", None)
            or not new_intents
            or (account_scope_id, client_order_id) not in new_intents
        ):
            raise ValueError(
                "capacity reservation must be created in the same transaction "
                "as its execution intent"
            )
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
        cumulative_filled_qty: int,
    ) -> bool:
        account_scope_id = self._required_text(
            account_scope_id, "account_scope_id",
        )
        reservation_id = self._required_text(reservation_id, "reservation_id")
        cumulative_filled_qty = self._quantity(
            cumulative_filled_qty, "cumulative_filled_qty", positive=True,
        )
        row = conn.execute(
            """SELECT r.client_order_id, r.stock_code, r.side, r.target_qty,
                      r.cash_yuan,
                      r.position_value_yuan, r.open_risk_yuan,
                      r.remaining_target_qty, r.remaining_cash_yuan,
                      r.remaining_position_value_yuan,
                      r.remaining_open_risk_yuan, i.status AS intent_status,
                      i.payload_json
               FROM capacity_reservations AS r
               JOIN execution_intents AS i
                 ON i.account_scope_id=r.account_scope_id
                AND i.client_order_id=r.client_order_id
               WHERE r.account_scope_id=? AND r.reservation_id=?
                 AND r.status='active'""",
            (account_scope_id, reservation_id),
        ).fetchone()
        if not row:
            return False
        if str(row["intent_status"]).upper() != "PARTIALLY_FILLED":
            raise ValueError(
                "capacity reservation adjustment requires PARTIALLY_FILLED intent"
            )
        client_order_id = str(row["client_order_id"])
        try:
            intent = ExecutionIntent.from_dict(
                json.loads(str(row["payload_json"]))
            )
        except Exception as exc:
            raise ValueError(
                "capacity reservation execution intent evidence is invalid"
            ) from exc
        if (
            intent.account_scope_id != account_scope_id
            or intent.client_order_id != client_order_id
            or intent.code != str(row["stock_code"])
            or intent.side != str(row["side"])
            or intent.order_qty != int(row["target_qty"])
        ):
            raise ValueError(
                "capacity reservation does not match execution intent"
            )
        broker_order = conn.execute(
            """SELECT broker_order_id, stock_code, side, target_qty,
                      filled_qty, status
               FROM broker_order_current
               WHERE account_scope_id=? AND client_order_id=?""",
            (account_scope_id, client_order_id),
        ).fetchone()
        if broker_order is None:
            raise ValueError(
                "capacity reservation adjustment requires current broker order"
            )
        if (
            str(broker_order["stock_code"]) != intent.code
            or str(broker_order["side"]).lower() != intent.side
            or int(broker_order["target_qty"]) != intent.order_qty
            or str(broker_order["status"]).lower()
            not in {"partially_filled", "pending_cancel"}
            or int(broker_order["filled_qty"]) != cumulative_filled_qty
        ):
            raise ValueError(
                "current broker order does not match partial-fill evidence"
            )
        order = conn.execute(
            """SELECT order_id, stock_code, action, filled_qty,
                      requested_qty, target_qty
               FROM orders WHERE client_order_id=?""",
            (client_order_id,),
        ).fetchone()
        order_filled = 0
        linked_order_ids = {
            str(broker_order["broker_order_id"] or "").strip()
        }
        if order is not None:
            if (
                str(order["stock_code"]) != intent.code
                or str(order["action"]).lower() != intent.side
                or order_allowed_quantity(
                    order["requested_qty"], order["target_qty"],
                ) != intent.order_qty
            ):
                raise ValueError(
                    "legacy order does not match execution intent"
                )
            legacy_order_id = str(order["order_id"] or "").strip()
            if (
                legacy_order_id
                and broker_order["broker_order_id"]
                and legacy_order_id != str(broker_order["broker_order_id"])
            ):
                raise ValueError(
                    "legacy and broker order identities conflict"
                )
            linked_order_ids.add(legacy_order_id)
            order_filled = int(order["filled_qty"] or 0)
        linked_order_ids.discard("")
        fills = conn.execute(
            """SELECT stock_code, action, qty, order_id
               FROM fills WHERE client_order_id=?
                  OR order_id=?
                  OR order_id IN (
                      SELECT order_id FROM orders
                      WHERE client_order_id=? AND order_id IS NOT NULL
                  )""",
            (
                client_order_id,
                str(broker_order["broker_order_id"] or ""),
                client_order_id,
            ),
        ).fetchall()
        for fill in fills:
            if (
                str(fill["stock_code"]) != intent.code
                or str(fill["action"]).lower() != intent.side
                or (
                    linked_order_ids
                    and fill["order_id"]
                    and str(fill["order_id"]) not in linked_order_ids
                )
            ):
                raise ValueError("fill identity conflicts with execution intent")
        fill_filled = sum(int(fill["qty"] or 0) for fill in fills)
        evidenced_filled = max(
            int(broker_order["filled_qty"]), order_filled, fill_filled,
        )
        if cumulative_filled_qty != evidenced_filled:
            raise ValueError(
                "cumulative_filled_qty does not match persisted order/fill evidence"
            )
        target_qty = int(row["target_qty"])
        if cumulative_filled_qty >= target_qty:
            raise ValueError(
                "PARTIALLY_FILLED cumulative quantity must be below target quantity"
            )
        remaining_qty = target_qty - cumulative_filled_qty
        ratio = Decimal(remaining_qty) / Decimal(target_qty)
        original = (
            target_qty,
            Decimal(row["cash_yuan"]),
            Decimal(row["position_value_yuan"]),
            Decimal(row["open_risk_yuan"]),
        )
        current = (
            int(row["remaining_target_qty"]),
            Decimal(row["remaining_cash_yuan"]),
            Decimal(row["remaining_position_value_yuan"]),
            Decimal(row["remaining_open_risk_yuan"]),
        )
        remaining = (
            remaining_qty,
            *((value * ratio).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING,
            ) for value in original[1:]),
        )
        if any(value > limit for value, limit in zip(remaining, current)):
            raise ValueError("remaining reservation values cannot increase")
        remaining_text = tuple(
            self._decimal_text(value) for value in remaining[1:]
        )
        cursor = conn.execute(
            """UPDATE capacity_reservations
               SET remaining_target_qty=?, remaining_cash_yuan=?,
                   remaining_position_value_yuan=?, remaining_open_risk_yuan=?
               WHERE account_scope_id=? AND reservation_id=? AND status='active'
                 AND remaining_target_qty=? AND remaining_cash_yuan=?
                 AND remaining_position_value_yuan=?
                 AND remaining_open_risk_yuan=?""",
            (
                remaining[0], *remaining_text,
                account_scope_id, reservation_id,
                current[0], str(current[1]), str(current[2]), str(current[3]),
            ),
        )
        return cursor.rowcount == 1

    def list_active_reservations(
        self,
        conn: sqlite3.Connection,
        account_scope_id: str,
    ) -> list[dict[str, object]]:
        account_scope_id = self._required_text(
            account_scope_id, "account_scope_id",
        )
        rows = conn.execute(
            """SELECT r.*, i.status AS intent_status
               FROM capacity_reservations AS r
               JOIN execution_intents AS i
                 ON i.account_scope_id=r.account_scope_id
                AND i.client_order_id=r.client_order_id
               WHERE r.account_scope_id=? AND r.status='active'
               ORDER BY r.reservation_id""",
            (account_scope_id,),
        ).fetchall()
        return [{
            "account_scope_id": str(row["account_scope_id"]),
            "reservation_id": str(row["reservation_id"]),
            "client_order_id": str(row["client_order_id"]),
            "code": str(row["stock_code"]),
            "side": str(row["side"]),
            "intent_status": str(row["intent_status"]),
            "original_target_qty": int(row["target_qty"]),
            "original_cash_yuan": Decimal(row["cash_yuan"]),
            "original_position_value_yuan": Decimal(
                row["position_value_yuan"]
            ),
            "original_open_risk_yuan": Decimal(row["open_risk_yuan"]),
            "remaining_target_qty": int(row["remaining_target_qty"]),
            "remaining_cash_yuan": Decimal(row["remaining_cash_yuan"]),
            "remaining_position_value_yuan": Decimal(
                row["remaining_position_value_yuan"]
            ),
            "remaining_open_risk_yuan": Decimal(
                row["remaining_open_risk_yuan"]
            ),
            "industry": str(row["industry"]),
            "theme": str(row["theme"]),
            "uncategorized": bool(row["uncategorized"]),
        } for row in rows]

    def aggregate_active_reservations(
        self,
        conn: sqlite3.Connection,
        account_scope_id: str,
    ) -> dict[str, object]:
        rows = self.list_active_reservations(conn, account_scope_id)
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
            value = row["remaining_position_value_yuan"]
            result["target_qty"] += int(row["remaining_target_qty"])
            result["cash_yuan"] += row["remaining_cash_yuan"]
            result["position_value_yuan"] += value
            result["open_risk_yuan"] += row["remaining_open_risk_yuan"]
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
        reconciliation_id: str | None = None,
    ) -> bool:
        account_scope_id = self._required_text(
            account_scope_id, "account_scope_id",
        )
        reservation_id = self._required_text(reservation_id, "reservation_id")
        released_at = self._aware_timestamp(released_at, "released_at")
        reason = self._required_text(reason, "reason")
        row = conn.execute(
            """SELECT i.client_order_id, i.status AS intent_status,
                      i.expires_at, i.status_updated_at, i.payload_json
               FROM capacity_reservations AS r
               JOIN execution_intents AS i
                 ON i.account_scope_id=r.account_scope_id
                AND i.client_order_id=r.client_order_id
               WHERE r.account_scope_id=? AND r.reservation_id=?
                 AND r.status='active'""",
            (account_scope_id, reservation_id),
        ).fetchone()
        if row is None:
            return False
        intent_status = str(row["intent_status"]).upper()
        if intent_status in {
            "SUBMITTING", "SUBMIT_UNKNOWN", "SUBMITTED", "PARTIALLY_FILLED",
        }:
            raise ValueError(
                f"{intent_status} capacity reservation cannot be released"
            )
        client_order_id = str(row["client_order_id"])
        release_instant = self._timestamp_instant(released_at, "released_at")
        if intent_status in {"READY", "EXPIRED"}:
            expires_at = self._timestamp_instant(row["expires_at"], "expires_at")
            if release_instant < expires_at:
                raise ValueError(
                    "READY capacity reservation cannot be released before expiry"
                )
            if intent_status == "READY":
                raise ValueError(
                    "READY intent must transition to EXPIRED before release"
                )
            if conn.execute(
                "SELECT 1 FROM orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone() is not None:
                raise ValueError(
                    "EXPIRED capacity release conflicts with submission evidence"
                )
            if conn.execute(
                "SELECT 1 FROM fills WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone() is not None:
                raise ValueError(
                    "EXPIRED capacity release conflicts with fill evidence"
                )
            if conn.execute(
                """SELECT 1 FROM broker_order_current
                   WHERE account_scope_id=? AND client_order_id=?""",
                (account_scope_id, client_order_id),
            ).fetchone() is not None:
                raise ValueError(
                    "EXPIRED capacity release conflicts with current broker order"
                )
            broker_snapshot = conn.execute(
                """SELECT broker_time, generated_at
                   FROM broker_snapshot_current WHERE account_scope_id=?""",
                (account_scope_id,),
            ).fetchone()
            if broker_snapshot is None:
                raise ValueError(
                    "EXPIRED capacity release requires current broker snapshot"
                )
            broker_time = self._timestamp_instant(
                broker_snapshot["broker_time"], "broker_snapshot.broker_time",
            )
            snapshot_generated_at = self._timestamp_instant(
                broker_snapshot["generated_at"],
                "broker_snapshot.generated_at",
            )
            if broker_time < expires_at or snapshot_generated_at < expires_at:
                raise ValueError(
                    "current broker snapshot predates execution intent expiry"
                )
            if release_instant < max(broker_time, snapshot_generated_at):
                raise ValueError(
                    "capacity release predates current broker snapshot"
                )
        elif intent_status in {
            "NOT_SUBMITTED", "REJECTED", "CANCELLED", "FILLED",
        }:
            reconciliation_id = self._required_text(
                reconciliation_id, "reconciliation_id",
            )
            reconciliation = conn.execute(
                """SELECT account_scope_id, broker_snapshot_id,
                          broker_snapshot_sha256, snapshot_broker_time,
                          snapshot_generated_at, finished_at
                   FROM reconciliation_runs
                   WHERE reconciliation_id=? AND mode='full'
                     AND result='matched' AND difference_count=0""",
                (reconciliation_id,),
            ).fetchone()
            if reconciliation is None:
                raise ValueError(
                    "terminal capacity release requires matched full reconciliation"
                )
            required_reconciliation_evidence = (
                reconciliation["account_scope_id"],
                reconciliation["broker_snapshot_id"],
                reconciliation["broker_snapshot_sha256"],
                reconciliation["snapshot_broker_time"],
                reconciliation["snapshot_generated_at"],
            )
            if any(
                value is None or not str(value).strip()
                for value in required_reconciliation_evidence
            ):
                raise ValueError(
                    "terminal capacity release requires broker snapshot evidence"
                )
            if str(reconciliation["account_scope_id"]) != account_scope_id:
                raise ValueError(
                    "matched reconciliation account scope does not match reservation"
                )
            if conn.execute(
                """SELECT 1 FROM broker_order_current
                   WHERE account_scope_id=? AND client_order_id=?""",
                (account_scope_id, client_order_id),
            ).fetchone() is not None:
                raise ValueError(
                    "terminal capacity release conflicts with current broker order"
                )
            order = conn.execute(
                """SELECT stock_code, action, status, filled_qty,
                          requested_qty, target_qty, updated_at
                   FROM orders WHERE client_order_id=?""",
                (client_order_id,),
            ).fetchone()
            if intent_status == "NOT_SUBMITTED":
                if order is not None:
                    raise ValueError(
                        "NOT_SUBMITTED capacity release conflicts with order evidence"
                    )
                if conn.execute(
                    "SELECT 1 FROM fills WHERE client_order_id=?",
                    (client_order_id,),
                ).fetchone() is not None:
                    raise ValueError(
                        "NOT_SUBMITTED capacity release conflicts with fill evidence"
                    )
                evidence_at = str(row["status_updated_at"])
            else:
                intent = ExecutionIntent.from_dict(
                    json.loads(str(row["payload_json"]))
                )
                expected_order_statuses = {
                    "REJECTED": {
                        "rejected", "risk_rejected", "failed", "skipped",
                    },
                    "CANCELLED": {"cancelled"},
                    "FILLED": {"filled"},
                }
                order_status = str(order["status"]).lower() if order else "missing"
                if order is None or order_status not in expected_order_statuses[
                    intent_status
                ]:
                    raise ValueError(
                        f"{intent_status} intent does not match terminal order "
                        f"status {order_status}"
                    )
                order_qty = order_allowed_quantity(
                    order["requested_qty"], order["target_qty"],
                )
                if (
                    str(order["stock_code"]) != intent.code
                    or str(order["action"]).lower() != intent.side
                    or order_qty != intent.order_qty
                    or int(order["filled_qty"] or 0) > intent.order_qty
                ):
                    raise ValueError(
                        "terminal order does not match execution intent"
                    )
                if (
                    intent_status == "FILLED"
                    and int(order["filled_qty"] or 0) < order_qty
                ):
                    raise ValueError(
                        "FILLED terminal order quantity is incomplete"
                    )
                if (
                    intent_status == "REJECTED"
                    and int(order["filled_qty"] or 0) != 0
                ):
                    raise ValueError(
                        "REJECTED terminal order cannot contain fills"
                    )
                evidence_at = max(
                    (str(order["updated_at"]), str(row["status_updated_at"])),
                    key=lambda value: self._timestamp_instant(
                        value, "terminal evidence timestamp",
                    ),
                )
            terminal_evidence_at = self._timestamp_instant(
                evidence_at, "terminal evidence timestamp",
            )
            snapshot_broker_at = self._timestamp_instant(
                reconciliation["snapshot_broker_time"],
                "reconciliation.snapshot_broker_time",
            )
            snapshot_generated_at = self._timestamp_instant(
                reconciliation["snapshot_generated_at"],
                "reconciliation.snapshot_generated_at",
            )
            reconciliation_at = self._timestamp_instant(
                reconciliation["finished_at"], "reconciliation.finished_at",
            )
            if (
                snapshot_broker_at < terminal_evidence_at
                or snapshot_generated_at < terminal_evidence_at
                or reconciliation_at < max(
                    terminal_evidence_at,
                    snapshot_broker_at,
                    snapshot_generated_at,
                )
            ):
                raise ValueError(
                    "matched reconciliation predates terminal execution evidence"
                )
            if reconciliation_at > release_instant:
                raise ValueError(
                    "capacity release predates matched reconciliation"
                )
            current_snapshot = conn.execute(
                """SELECT snapshot_id, snapshot_sha256, broker_time, generated_at
                   FROM broker_snapshot_current WHERE account_scope_id=?""",
                (account_scope_id,),
            ).fetchone()
            if current_snapshot is None:
                raise ValueError(
                    "terminal capacity release requires current broker snapshot"
                )
            current_evidence = (
                str(current_snapshot["snapshot_id"]),
                str(current_snapshot["snapshot_sha256"]),
                str(current_snapshot["broker_time"]),
                str(current_snapshot["generated_at"]),
            )
            reconciliation_evidence = (
                str(reconciliation["broker_snapshot_id"]),
                str(reconciliation["broker_snapshot_sha256"]),
                str(reconciliation["snapshot_broker_time"]),
                str(reconciliation["snapshot_generated_at"]),
            )
            if current_evidence != reconciliation_evidence:
                raise ValueError(
                    "matched reconciliation does not describe current broker snapshot"
                )
        else:
            raise ValueError(
                f"unknown execution intent status cannot be released: {intent_status}"
            )
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
        rows = conn.execute(
            """SELECT * FROM orders
               WHERE client_order_id=?
                  OR (order_id IS NOT NULL AND order_id=?)""",
            (order["client_order_id"], order.get("order_id")),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("order identity resolves to multiple ledger rows")
        row = rows[0] if rows else None
        if row is not None:
            incoming_order_id = str(order.get("order_id") or "")
            existing_order_id = str(row["order_id"] or "")
            quantity_drift = False
            for field in ("requested_qty", "target_qty"):
                existing_qty = self._quantity(
                    row[field] or 0, f"existing {field}", positive=False,
                )
                incoming_qty = self._quantity(
                    order.get(field) or 0,
                    f"incoming {field}",
                    positive=False,
                )
                if (
                    existing_qty > 0
                    and incoming_qty > 0
                    and existing_qty != incoming_qty
                ):
                    quantity_drift = True
            if (
                str(row["client_order_id"]) != str(order["client_order_id"])
                or str(row["stock_code"]) != str(order["stock_code"])
                or str(row["action"]) != str(order["action"])
                or quantity_drift
                or (
                    incoming_order_id
                    and existing_order_id
                    and incoming_order_id != existing_order_id
                )
            ):
                raise ValueError("order identity conflict")
        signal_id = order.get("signal_id")
        if signal_id and conn.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone() is None:
            signal_id = None
        terminal = {
            "filled", "cancelled", "rejected", "risk_rejected", "failed",
            "skipped",
        }
        non_fill_terminal = {
            "rejected", "risk_rejected", "failed", "skipped",
        }
        incoming_status = str(order["status"]).lower()
        incoming_filled_qty = int(order["filled_qty"] or 0)
        existing_status = str(row["status"]).lower() if row is not None else ""
        existing_filled_qty = int(row["filled_qty"] or 0) if row is not None else 0
        incoming_allowed_qty = order_allowed_quantity(
            order.get("requested_qty"), order.get("target_qty"),
        )
        if incoming_status not in terminal and incoming_allowed_qty > 0:
            if incoming_filled_qty >= incoming_allowed_qty:
                incoming_status = "filled"
                order["completed_at"] = order["updated_at"]
            elif incoming_filled_qty > 0:
                incoming_status = "partial"
            elif incoming_status in {"partial", "partially_filled"}:
                incoming_status = "submitted"
            order["status"] = incoming_status
        if (
            incoming_status == "filled"
            and incoming_allowed_qty > 0
            and incoming_filled_qty < incoming_allowed_qty
        ):
            raise ValueError("filled order quantity is incomplete")
        if (
            incoming_status in non_fill_terminal
            and max(incoming_filled_qty, existing_filled_qty) > 0
        ) or (
            existing_status in non_fill_terminal
            and incoming_filled_qty > existing_filled_qty
        ):
            raise ValueError("terminal order cannot gain filled quantity")
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
            filled_qty = max(int(row["filled_qty"]), int(order["filled_qty"]))
            allowed_qty = order_allowed_quantity(
                row["requested_qty"], row["target_qty"],
            )
            if (
                existing_status in {"cancelled", "filled"}
                and filled_qty > existing_filled_qty
                and not (
                    existing_status == "cancelled"
                    and incoming_status == "filled"
                    and allowed_qty > 0
                    and filled_qty >= allowed_qty
                )
            ):
                raise ValueError(
                    "terminal order cannot gain filled quantity"
                )
            active_rank = {
                "unknown": 0,
                "new": 1,
                "open": 1,
                "held": 1,
                "submitted": 1,
                "partial": 2,
                "partially_filled": 2,
                "pending_cancel": 2,
            }
            if existing_status in terminal:
                status = existing_status
                if (
                    existing_status == "cancelled"
                    and incoming_status == "filled"
                    and allowed_qty > 0
                    and filled_qty >= allowed_qty
                ):
                    status = "filled"
            elif incoming_status in terminal:
                status = incoming_status
            elif active_rank.get(incoming_status, 0) < active_rank.get(
                existing_status, 0,
            ):
                status = existing_status
            else:
                status = incoming_status
            incoming_instant = self._timestamp_instant(
                order["updated_at"], "incoming order updated_at",
            )
            existing_instant = self._timestamp_instant(
                row["updated_at"], "existing order updated_at",
            )
            metadata_stale = (
                incoming_instant < existing_instant
                or incoming_filled_qty < existing_filled_qty
                or incoming_status != status
            )
            updated_at = (
                row["updated_at"] if metadata_stale else order["updated_at"]
            )
            average_fill_price = (
                row["average_fill_price"]
                if metadata_stale or float(order["average_fill_price"] or 0) <= 0
                else order["average_fill_price"]
            )
            reason = row["reason"] if metadata_stale else order["reason"]
            raw_json = row["raw_json"] if metadata_stale else order["raw_json"]
            completed_at = row["completed_at"] or (
                updated_at if status in terminal else order.get("completed_at")
            )
            conn.execute(
                """UPDATE orders SET signal_id=COALESCE(signal_id, ?), order_id=COALESCE(order_id, ?),
                   target_qty=COALESCE(?, target_qty), requested_qty=max(requested_qty, ?),
                   filled_qty=?, average_fill_price=?,
                   status=?, submit_count=max(submit_count, ?), reason=?, updated_at=?,
                   completed_at=?, raw_json=? WHERE client_order_id=?""",
                (
                    signal_id, order.get("order_id"), order.get("target_qty"), order["requested_qty"],
                    filled_qty, average_fill_price, status,
                    order["submit_count"], reason, updated_at, completed_at,
                    raw_json, client_id,
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
            "stamp_tax", "other_fee",
        )
        expected = tuple(fill.get(key) for key in keys)
        if existing is not None:
            actual = tuple(existing[key] for key in keys)
            same_time = self._timestamp_instant(
                existing["filled_at"], "existing fill filled_at",
            ) == self._timestamp_instant(fill["filled_at"], "incoming fill filled_at")
            same_raw = canonical_json(existing["raw_json"]) == canonical_json(
                fill["raw_json"]
            )
            if actual != expected or not same_time or not same_raw:
                raise FillConflictError(f"immutable fill conflict: {fill['fill_id']}")
            if str(existing["filled_at"]) != str(fill["filled_at"]):
                conn.execute(
                    "UPDATE fills SET filled_at=? WHERE fill_id=?",
                    (fill["filled_at"], fill["fill_id"]),
                )
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
