from __future__ import annotations

import sqlite3
import json
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 5


class SignalConflictError(RuntimeError):
    """Raised when an immutable signal ID is reused for different content."""


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

    def upsert_exit_intent(self, conn: sqlite3.Connection, signal_id: str, code: str,
                           target_qty: int, reason: str, created_at: str) -> None:
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
