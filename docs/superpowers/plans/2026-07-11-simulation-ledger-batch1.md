# Simulation Ledger Batch 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a SQLite source-of-truth ledger and observation-only risk records without changing the signals or orders produced by the existing JoinQuant simulation flow.

**Architecture:** A focused `trading_store.py` owns SQLite schema migration and transactions. `joinquant_exporter.py` writes each strategy run, immutable signal, and observation-only risk decision to SQLite before atomically publishing the existing `signals.json`; existing JSON/JSONL consumers remain unchanged. Health and readiness checks expose ledger availability and parity while fail-closed behavior suppresses new buy publication if the ledger cannot commit.

**Tech Stack:** Python 3.10+, standard-library `sqlite3`, `dataclasses`, JSON, pandas, existing `unittest` test suite, Bash/systemd deployment script.

## Global Constraints

- `docs/project_roadmap.md` remains the unique main document; conflicts are resolved in its favor.
- The approved design is `docs/superpowers/specs/2026-07-11-simulation-stability-ledger-design.md`.
- Batch 1 must not alter candidate selection, `final_score`, buy/sell rules, target position percentages, or JoinQuant order behavior.
- `RISK_MODE=OBSERVE` is the only supported Batch 1 mode; soft limits are recorded and never suppress a valid existing signal.
- SQLite is the formal ledger at `cache/trading/trading.db`; existing JSON/JSONL output remains compatible.
- SQLite must use WAL, foreign keys, and a 5000 ms busy timeout.
- A failed ledger transaction must prevent publication of new buy signals; valid sell signals remain publishable.
- No new third-party dependency is allowed for Batch 1.
- Every schema or interface change is test-first and committed separately.

---

## File Structure

- Create `trading_store.py`: SQLite connection, schema migration, transactions, strategy runs, immutable signals, risk decisions, and system-state access.
- Create `tests/test_trading_store.py`: migration, idempotency, rollback, and persistence tests.
- Create `pre_trade_check.py`: observation-only portfolio limit evaluation with structured results.
- Create `tests/test_pre_trade_check.py`: soft-warning and hard-error separation tests.
- Modify `config.py`: database path, risk mode, and approved observation thresholds.
- Modify `tests/test_config_env.py`: environment parsing coverage.
- Modify `joinquant_exporter.py`: ledger-first dual write and sell-only fail-closed fallback.
- Modify `tests/test_joinquant_exporter.py`: parity, idempotency, and ledger failure behavior.
- Modify `joinquant_health.py`: ledger availability and JSON/SQLite signal parity checks.
- Modify `tests/test_joinquant_health.py`: healthy, unavailable, and mismatch cases.
- Modify `joinquant_readiness_report.py`: report ledger readiness before simulation start.
- Modify `tests/test_joinquant_readiness_report.py`: readiness failure coverage.
- Modify `run_ubuntu.sh`: environment defaults, database directory creation, and a `ledger-check` command.
- Modify `tests/test_joinquant_linux_script.py`: deployment script assertions.
- Modify `docs/project_roadmap.md`: mark Batch 1 implemented only after all verification passes.
- Modify `docs/live_trading_execution_plan.md`: record Batch 1 deployment and one-day observation gate.

---

### Task 1: SQLite Store and Schema Migration

**Files:**
- Create: `trading_store.py`
- Create: `tests/test_trading_store.py`

**Interfaces:**
- Produces: `TradingStore(db_path: Path)`, `TradingStore.initialize() -> None`, `TradingStore.transaction() -> ContextManager[sqlite3.Connection]`, `TradingStore.health() -> StoreHealth`.
- Produces: schema version `1` with tables `schema_migrations`, `strategy_runs`, `signals`, `risk_decisions`, and `system_state`.
- Consumes: only Python standard-library modules.

- [ ] **Step 1: Write the failing migration and PRAGMA tests**

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_store import TradingStore


class TradingStoreTest(unittest.TestCase):
    def test_initialize_creates_version_one_schema_and_pragmas(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.connect() as conn:
                version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(version, 1)
            self.assertTrue(
                {"strategy_runs", "signals", "risk_decisions", "system_state"}.issubset(tables)
            )
            self.assertEqual(foreign_keys, 1)
            self.assertEqual(busy_timeout, 5000)
```

- [ ] **Step 2: Run the focused test and verify the missing-module failure**

Run: `python -m unittest tests.test_trading_store.TradingStoreTest.test_initialize_creates_version_one_schema_and_pragmas -v`

Expected: `ModuleNotFoundError: No module named 'trading_store'`.

- [ ] **Step 3: Implement connection setup and schema version 1**

```python
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoreHealth:
    ok: bool
    schema_version: int
    error: str = ""


class TradingStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_V1)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, datetime('now'))"
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
            return StoreHealth(ok=version == SCHEMA_VERSION, schema_version=version)
        except Exception as exc:
            return StoreHealth(ok=False, schema_version=0, error=str(exc))
```

`SCHEMA_V1` must define exact primary keys, foreign keys, timestamps, `raw_json TEXT NOT NULL`, `UNIQUE(signal_id)`, and indexes on trade date, signal action, run ID, and decision signal ID as specified in the approved design.

- [ ] **Step 4: Add rollback and repeat-initialization tests**

```python
    def test_transaction_rolls_back_all_rows_on_error(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with store.transaction() as conn:
                    conn.execute(
                        "INSERT INTO system_state(key, value, updated_at) VALUES (?, ?, datetime('now'))",
                        ("buy_enabled", "1"),
                    )
                    raise RuntimeError("boom")
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM system_state").fetchone()[0]
            self.assertEqual(count, 0)

    def test_initialize_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            store.initialize()
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(count, 1)
```

- [ ] **Step 5: Run store tests**

Run: `python -m unittest tests.test_trading_store -v`

Expected: all `TradingStoreTest` cases pass.

- [ ] **Step 6: Commit the store foundation**

```bash
git add trading_store.py tests/test_trading_store.py
git commit -m "feat: add simulation trading ledger"
```

---

### Task 2: Strategy Runs, Immutable Signals, and System State

**Files:**
- Modify: `trading_store.py`
- Modify: `tests/test_trading_store.py`

**Interfaces:**
- Consumes: `TradingStore.transaction()` from Task 1.
- Produces: `record_strategy_run(conn, run: StrategyRunRecord) -> None`.
- Produces: `record_signal(conn, signal: SignalRecord) -> bool`, returning `True` only for a new signal.
- Produces: `set_system_state(conn, key: str, value: str, reason: str) -> None` and `get_system_state(key: str, default: str = "") -> str`.

- [ ] **Step 1: Write failing persistence and idempotency tests**

```python
    def test_signal_insert_is_immutable_and_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            run = StrategyRunRecord(
                run_id="run-1", trade_date="2026-07-11", started_at="2026-07-11 09:30:00",
                strategy_version="git:abc", parameter_version="risk-observe-v1",
            )
            signal = SignalRecord(
                signal_id="sig-1", run_id="run-1", trade_date="2026-07-11",
                code="600000", jq_code="600000.XSHG", action="buy",
                position_pct=10.0, generated_at="2026-07-11 09:31:00",
                expires_at="2026-07-11 09:51:00", raw_json='{"id":"sig-1"}',
            )
            with store.transaction() as conn:
                store.record_strategy_run(conn, run)
                self.assertTrue(store.record_signal(conn, signal))
                self.assertFalse(store.record_signal(conn, signal))
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM signals WHERE signal_id='sig-1'").fetchone()[0]
            self.assertEqual(count, 1)
```

- [ ] **Step 2: Run the test and verify undefined record types**

Run: `python -m unittest tests.test_trading_store.TradingStoreTest.test_signal_insert_is_immutable_and_idempotent -v`

Expected: import or attribute failure for `StrategyRunRecord`/`SignalRecord`.

- [ ] **Step 3: Add frozen record dataclasses and transactional methods**

Implement frozen dataclasses with the exact fields used above. Use `INSERT ... ON CONFLICT(run_id) DO UPDATE` only for run completion metadata. Use `INSERT OR IGNORE` for signals, never update `raw_json` after insertion, and return `cursor.rowcount == 1`.

- [ ] **Step 4: Add system-state audit tests and implementation**

```python
    def test_system_state_records_latest_value_and_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            store = TradingStore(Path(tmp) / "trading.db")
            store.initialize()
            with store.transaction() as conn:
                store.set_system_state(conn, "buy_enabled", "0", "ledger unavailable")
            self.assertEqual(store.get_system_state("buy_enabled"), "0")
```

The implementation must upsert `system_state` with `updated_at` and `reason`; it must not silently discard the reason.

- [ ] **Step 5: Run tests and commit**

Run: `python -m unittest tests.test_trading_store -v`

Expected: all tests pass.

```bash
git add trading_store.py tests/test_trading_store.py
git commit -m "feat: persist strategy runs and signals"
```

---

### Task 3: Observation-Only Risk Evaluation

**Files:**
- Create: `pre_trade_check.py`
- Create: `tests/test_pre_trade_check.py`
- Modify: `config.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**
- Produces: `RiskLimits`, `PortfolioState`, `RiskCheckResult` frozen dataclasses.
- Produces: `evaluate_observation(signal: Mapping[str, Any], portfolio: PortfolioState, limits: RiskLimits) -> RiskCheckResult`.
- Produces: config constants matching every Batch 1 threshold in Global Constraints.

- [ ] **Step 1: Write failing observation-mode tests**

```python
import unittest

from pre_trade_check import PortfolioState, RiskLimits, evaluate_observation


class PreTradeCheckTest(unittest.TestCase):
    def test_soft_limit_warnings_do_not_block_signal(self) -> None:
        result = evaluate_observation(
            {"action": "buy", "position_pct": 40, "sector": "半导体"},
            PortfolioState(
                total_position_pct=90, cash_reserve_pct=10,
                sector_exposure_pct={"半导体": 30}, new_positions_today=10,
                orders_today=50, daily_turnover_pct=190,
                daily_pnl_pct=-6, account_drawdown_pct=-16,
            ),
            RiskLimits(),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.hard_blocks, ())
        self.assertIn("SINGLE_POSITION_LIMIT", result.soft_warnings)
        self.assertIn("TOTAL_POSITION_LIMIT", result.soft_warnings)
        self.assertIn("DAILY_LOSS_WARNING", result.soft_warnings)

    def test_invalid_signal_is_a_hard_block(self) -> None:
        result = evaluate_observation(
            {"action": "buy", "position_pct": 10, "price": 0},
            PortfolioState.empty(),
            RiskLimits(),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.hard_blocks, ("INVALID_ORDER_INPUT",))
```

- [ ] **Step 2: Run tests and verify the missing-module failure**

Run: `python -m unittest tests.test_pre_trade_check -v`

Expected: `ModuleNotFoundError: No module named 'pre_trade_check'`.

- [ ] **Step 3: Implement immutable inputs and deterministic warnings**

`RiskLimits` defaults must be `30, 95, 5, 60, 10, 50, 200, 5, 15`. `RiskCheckResult` must contain `allowed`, `hard_blocks`, `soft_warnings`, and `metrics`. Warning ordering must be stable so reports and tests remain deterministic.

- [ ] **Step 4: Add config environment tests**

Extend `tests/test_config_env.py` to set `RISK_MODE`, all observation thresholds, `TRADING_DB_FILE`, reload `config`, and assert parsed values and `Path` handling. Assert the default `RISK_MODE` is exactly `observe`.

- [ ] **Step 5: Add config constants**

Add `TRADING_DB_FILE = Path(_env_text("TRADING_DB_FILE", str(CACHE_DIR / "trading" / "trading.db")))`, environment key constants, and typed defaults for every approved threshold. Reject unsupported risk modes during configuration load rather than silently enabling blocking behavior.

- [ ] **Step 6: Run tests and commit**

Run: `python -m unittest tests.test_pre_trade_check tests.test_config_env -v`

Expected: all tests pass.

```bash
git add pre_trade_check.py config.py tests/test_pre_trade_check.py tests/test_config_env.py
git commit -m "feat: add observation-only risk checks"
```

---

### Task 4: Ledger-First Signal Export with Compatible JSON

**Files:**
- Modify: `joinquant_exporter.py:145-199`
- Modify: `tests/test_joinquant_exporter.py`

**Interfaces:**
- Consumes: `TradingStore`, `StrategyRunRecord`, `SignalRecord`, and `evaluate_observation`.
- Produces: unchanged `export_signals(...) -> dict[str, Any]` return contract and unchanged JSON schema version `1`.
- Produces: diagnostic fields `ledger_ok`, `ledger_signal_count`, `ledger_error`, and `buy_publication_blocked` under the existing diagnostics object.

- [ ] **Step 1: Write a failing parity test**

Add a test that exports one buy and one sell to a temporary JSON path and temporary database, then asserts:

```python
self.assertEqual([item["id"] for item in payload["signals"]], ["run-1-600000-buy-0", "run-1-000001-sell-1"])
with store.connect() as conn:
    db_ids = [row[0] for row in conn.execute("SELECT signal_id FROM signals ORDER BY signal_id")]
self.assertEqual(db_ids, sorted(item["id"] for item in payload["signals"]))
self.assertEqual(payload["diagnostics"]["ledger_ok"], True)
```

- [ ] **Step 2: Run the parity test and verify it fails because no ledger rows exist**

Run: `python -m unittest tests.test_joinquant_exporter.JoinQuantExporterTest.test_ledger_and_json_signal_ids_match -v`

Expected: failure showing an empty SQLite signal set or missing diagnostic key.

- [ ] **Step 3: Add optional store injection without changing current callers**

Extend `export_signals` with `store: TradingStore | None = None`. When omitted, initialize a store from `app_config.TRADING_DB_FILE`. Build the final payload first, evaluate observation warnings, and insert the run, signals, and decisions in one SQLite transaction before replacing the JSON temporary file.

- [ ] **Step 4: Write failing ledger-error behavior tests**

Use a fake store whose transaction context raises `sqlite3.OperationalError("database is locked")`. Assert:

```python
self.assertEqual([item["action"] for item in payload["signals"]], ["sell"])
self.assertFalse(payload["diagnostics"]["ledger_ok"])
self.assertTrue(payload["diagnostics"]["buy_publication_blocked"])
self.assertIn("database is locked", payload["diagnostics"]["ledger_error"])
```

Also assert that an export containing only buys publishes an empty `signals` list rather than reusing a prior buy file.

- [ ] **Step 5: Implement sell-only fail-closed fallback**

On ledger failure, remove buys from the payload, retain valid sells, set the explicit diagnostic fields, atomically replace the JSON file, and let the caller continue to notification/health handling. Do not catch errors from JSON temporary-file replacement; a failed compatible publication must remain visible to the service supervisor.

- [ ] **Step 6: Run exporter and ML compatibility tests**

Run: `python -m unittest tests.test_joinquant_exporter tests.test_ml_dataset tests.test_joinquant_notification -v`

Expected: all tests pass and existing schema-version assertions remain unchanged.

- [ ] **Step 7: Commit exporter integration**

```bash
git add joinquant_exporter.py tests/test_joinquant_exporter.py
git commit -m "feat: dual write joinquant signals to ledger"
```

---

### Task 5: Ledger Health and Readiness Gates

**Files:**
- Modify: `joinquant_health.py:218-410`
- Modify: `tests/test_joinquant_health.py`
- Modify: `joinquant_readiness_report.py`
- Modify: `tests/test_joinquant_readiness_report.py`

**Interfaces:**
- Consumes: `TradingStore.health()` and signal IDs from SQLite/JSON.
- Produces: health fields `ledger_ok`, `ledger_schema_version`, `ledger_signal_count`, `json_signal_count`, `ledger_json_parity`, and `ledger_error`.
- Produces: issue codes `ledger_unavailable` and `ledger_json_signal_mismatch`.

- [ ] **Step 1: Add failing healthy-parity test**

Create temporary database and signal JSON with the same two IDs, call the existing health builder, and assert:

```python
self.assertTrue(result["ledger_ok"])
self.assertTrue(result["ledger_json_parity"])
self.assertNotIn("ledger_unavailable", result["issue_codes"])
self.assertNotIn("ledger_json_signal_mismatch", result["issue_codes"])
```

- [ ] **Step 2: Add failing unavailable and mismatch tests**

Assert an unreadable database adds `ledger_unavailable` and an ID mismatch adds `ledger_json_signal_mismatch`. During Batch 1 both are critical during continuous trading hours and informational outside trading hours only when no fresh executable buy signals exist.

- [ ] **Step 3: Implement bounded ledger checks**

Read only the current payload's signal IDs and matching SQLite IDs; do not scan the complete historical table. Include the schema version and sanitized error text in the Markdown report.

- [ ] **Step 4: Add readiness coverage and implementation**

Extend readiness tests so a missing/uninitialized ledger fails readiness with the exact line `SQLite 交易账本: 未就绪`, while an initialized version-1 database reports `SQLite 交易账本: 正常`.

- [ ] **Step 5: Run health/readiness tests and commit**

Run: `python -m unittest tests.test_joinquant_health tests.test_joinquant_readiness_report -v`

Expected: all tests pass.

```bash
git add joinquant_health.py joinquant_readiness_report.py tests/test_joinquant_health.py tests/test_joinquant_readiness_report.py
git commit -m "feat: monitor simulation ledger health"
```

---

### Task 6: Linux Deployment Controls and Full Batch Verification

**Files:**
- Modify: `run_ubuntu.sh:41-48,260-285,650-715`
- Modify: `tests/test_joinquant_linux_script.py`
- Modify: `docs/project_roadmap.md`
- Modify: `docs/live_trading_execution_plan.md`

**Interfaces:**
- Consumes: `TRADING_DB_FILE`, `RISK_MODE`, approved observation thresholds, and `TradingStore.health()`.
- Produces: `bash run_ubuntu.sh ledger-check` with exit `0` only for schema version `1` and a writable transaction probe.
- Produces: installation defaults matching the approved design.

- [ ] **Step 1: Add failing script-content tests**

Extend `tests/test_joinquant_linux_script.py` to assert the script contains:

```python
self.assertIn("ledger-check", text)
self.assertIn('set_env "RISK_MODE" "observe"', text)
self.assertIn('set_env "MAX_TOTAL_POSITION_PCT" "95"', text)
self.assertIn('set_env "ACCOUNT_SNAPSHOT_MAX_AGE_SEC" "300"', text)
self.assertIn('mkdir -p "${APP_DIR}/cache/trading"', text)
```

- [ ] **Step 2: Run the script test and verify missing strings**

Run: `python -m unittest tests.test_joinquant_linux_script -v`

Expected: failures for missing Batch 1 deployment controls.

- [ ] **Step 3: Add environment defaults and ledger-check command**

The command must run a short Python expression importing `config` and `TradingStore`, initialize the store, perform a transaction that writes and removes a probe `system_state` key, print schema/health, and return nonzero on any exception. It must not delete or recreate an existing database.

- [ ] **Step 4: Run all unit tests**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`

Expected: all existing and new tests pass.

- [ ] **Step 5: Run syntax and repository checks**

Run: `python -m py_compile trading_store.py pre_trade_check.py joinquant_exporter.py joinquant_health.py joinquant_readiness_report.py config.py`

Expected: no output and exit code `0`.

Run: `bash -n run_ubuntu.sh`

Expected: no output and exit code `0`.

Run: `git diff --check`

Expected: no output and exit code `0`.

- [ ] **Step 6: Update document status without overstating deployment**

In `docs/project_roadmap.md`, change Batch 1 from “设计已批准、待实现” to “代码已实现、待服务器部署和 1 个交易日双写观察” only after Step 4 and Step 5 pass. In `docs/live_trading_execution_plan.md`, record the exact test command, database location, observation mode, and one-day acceptance criteria. Keep the five-batch design document subordinate to the main roadmap.

- [ ] **Step 7: Commit Batch 1 completion**

```bash
git add run_ubuntu.sh tests/test_joinquant_linux_script.py docs/project_roadmap.md docs/live_trading_execution_plan.md
git commit -m "docs: prepare simulation ledger rollout"
```

- [ ] **Step 8: Server deployment checkpoint**

Deploy using the existing documented Git pull and `run_ubuntu.sh` workflow, then run:

```bash
bash run_ubuntu.sh ledger-check
bash run_ubuntu.sh readiness
bash run_ubuntu.sh health
```

Expected: ledger schema version `1`, readiness reports SQLite normal, health has no `ledger_unavailable` or `ledger_json_signal_mismatch`.

- [ ] **Step 9: One-trading-day observation checkpoint**

At market close, compare every current JSON signal ID with SQLite, confirm JoinQuant orders are unchanged from pre-Batch-1 behavior, confirm no duplicate signals, and archive the health/readiness reports. Do not start the Batch 2 order-state-machine plan until this checkpoint passes.

---

## Plan Self-Review Result

- Spec coverage: Batch 1 covers ledger schema, immutable signals, observation risk, dual write, ledger failure behavior, health, readiness, deployment defaults, and the one-day gate.
- Deferred by approved batch boundary: order/fill state machine, reconciliation tables and service, HMAC migration, kill-switch commands, and 20-day strategy validation reports belong to Batches 2-5 and must receive separate implementation plans after Batch 1 observation.
- Type consistency: later tasks consume the exact `TradingStore`, record dataclasses, and `RiskCheckResult` interfaces introduced earlier.
- Dependency check: implementation uses only the Python standard library and existing project packages.
