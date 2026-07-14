from __future__ import annotations

import sqlite3
import json
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 6


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
        if cursor.rowcount == 1:
            return True
        row = conn.execute(
            """SELECT run_id, trade_date, stock_code, jq_code, action, target_position,
                      generated_at, expires_at, raw_json FROM signals WHERE signal_id = ?""",
            (signal.signal_id,),
        ).fetchone()
        expected = (
            signal.run_id, signal.trade_date, signal.code, signal.jq_code, signal.action,
            signal.position_pct, signal.generated_at, signal.expires_at, canonical_json(signal.raw_json),
        )
        actual = tuple(row[:8]) + (canonical_json(row[8]),) if row is not None else ()
        if actual != expected:
            raise SignalConflictError(f"immutable signal conflict: {signal.signal_id}")
        return False

    def current_signal_parity(self, signals: list[dict]) -> tuple[int, bool]:
        """Compare only current JSON signals with ledger rows; never scan history."""
        expected = {str(item.get("id")): canonical_json(item) for item in signals if item.get("id")}
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
                found.update((str(row[0]), canonical_json(row[1])) for row in rows)
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
            if stop_price <= 0 and entry_price > 0:
                stop_price = round(entry_price * 0.93, 2)
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
                if signal_stop > 0:
                    stop_price = signal_stop
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
            updated_r = round(max(updated_entry - float(row["initial_stop_price"]), 0.01), 4)
            conn.execute(
                """UPDATE position_cycles SET current_qty=?, entry_price=?, initial_r=?, highest_price=?,
                   take_profit_stage=?, last_snapshot_at=?, updated_at=datetime('now')
                   WHERE position_cycle_id=?""",
                (
                    qty, updated_entry, updated_r, max(float(row["highest_price"]), current_price), stage,
                    snapshot_at, row["position_cycle_id"],
                ),
            )

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
            return False
        client_id = fill.get("client_order_id")
        if client_id and conn.execute("SELECT 1 FROM orders WHERE client_order_id=?", (client_id,)).fetchone() is None:
            client_id = None
        signal_id = fill.get("signal_id")
        if signal_id and conn.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,)).fetchone() is None:
            signal_id = None
        conn.execute(
            """INSERT INTO fills(fill_id, client_order_id, order_id, signal_id, stock_code,
               action, qty, price, commission, stamp_tax, other_fee, filled_at, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fill["fill_id"], client_id, fill.get("order_id"), signal_id, fill["stock_code"],
                fill["action"], fill["qty"], fill["price"], fill["commission"], fill["stamp_tax"],
                fill["other_fee"], fill["filled_at"], fill["raw_json"],
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
               remaining_qty, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', 0, ?, ?)
               ON CONFLICT(signal_id) DO UPDATE SET target_qty=excluded.target_qty,
               reason=excluded.reason, updated_at=excluded.updated_at""",
            (signal_id, code, int(target_qty), reason, created_at, created_at),
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
        return {
            "account_snapshots": max(0, int(snapshots)),
            "reconciliation_runs": max(0, int(runs)),
        }
