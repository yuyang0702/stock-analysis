# Complete Trading Ledger and Reconciliation Implementation Plan

> **Status (2026-07-14):** Tasks 1–8 are `implemented（已推送）` as commit `9f4c12d`, which is included in `origin/main`. Platform-independent verification passed; the Bash execution tests remain reserved for Linux because Bash was unavailable on this Windows host. Deployment remains externally unverified, and the capability is not observed or validated.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Complete the JoinQuant simulation ledger so every signal, order, fill, account state, holding checkpoint, equity day, reconciliation difference, and manual control action is durable, idempotent, auditable, and automatically reconciled.

**Architecture:** Extend the existing SQLite database from schema version 5 to 6. `TradingStore` owns schema and transactions; `order_ledger.py` normalizes stable order/fill identities; `reconciliation.py` compares the platform snapshot with the independent local ledger; `trading_control.py` owns stop-buy, kill-switch, status, full reconciliation, and the guarded unlock wizard. The JoinQuant callback persists and reconciles SQLite before publishing the compatible latest JSON.

**Tech Stack:** Python 3 standard library, SQLite, existing Flask service, existing JoinQuant template, existing `WeComNotifier`, `unittest`, Bash/systemd integration through `run_ubuntu.sh`.

## Global Constraints

- Do not change stock selection, scoring, entry, exit, position sizing, stop-loss, take-profit, or JoinQuant matching semantics.
- Keep JoinQuant signal and account callback schema version `1`; extend fields compatibly.
- Upgrade SQLite only through idempotent schema version `6`; never fabricate historical orders, fills, fees, snapshots, or equity.
- SQLite is the formal ledger. A callback must not publish a newer compatible account JSON if its ledger transaction failed.
- `ERROR` sets `buy_enabled=0` while legal sells remain allowed. `CRITICAL` also sets `kill_switch=1`.
- Neither control state automatically recovers. Unlock requires two consistent full reconciliations on distinct snapshot IDs and an explicit human reason.
- `kill-switch-off` never restores buying; `resume-buy` is a separate action.
- Reconciliation and control notifications use the existing enterprise-WeChat retry path and never include secrets or full account details.
- Preserve at most 366 days of high-frequency successful account summaries, retained position checkpoints, and mismatch-free reconciliation runs; keep orders, fills, daily equity, control events, and mismatch evidence long-term.
- No new third-party dependency, database server, message queue, or duplicate append-only event stream.
- Do not commit, push, deploy, update the JoinQuant website, migrate the server database, or restart services without separate authorization.

---

### Task 1: Schema Version 6 and Bounded Ledger Storage

**Files:**
- Modify: `trading_store.py`
- Modify: `trading_backup.py`
- Modify: `tests/test_trading_store.py`
- Modify: `tests/test_trading_backup.py`

**Interfaces:**
- Produces `SCHEMA_VERSION = 6`.
- Produces tables `orders`, `fills`, `account_snapshots`, `position_snapshots`, `daily_equity`, `reconciliation_runs`, `reconciliation_items`, and `control_events`.
- Produces `TradingStore.prune_execution_history(conn, cutoff_date: str, now: str) -> dict[str, int]`.

- [x] **Step 1: Write the failing schema and retention tests**

Add a test that initializes a new database and asserts version `6`, the eight new tables, foreign keys, unique constraints, and indexes. Add a migration test that creates a version-5 database, inserts existing rows, calls `initialize()`, and proves the rows survive.

Add a retention test with one old successful summary/run, one old mismatched run, and one recent row:

```python
deleted = store.prune_execution_history(conn, "2025-07-14", "2026-07-14 16:00:00")
self.assertEqual(deleted["account_snapshots"], 1)
self.assertEqual(deleted["reconciliation_runs"], 1)
self.assertEqual(conn.execute(
    "SELECT COUNT(*) FROM reconciliation_runs WHERE result <> 'matched'"
).fetchone()[0], 1)
```

Extend the backup test to require all schema-6 tables in `table_counts`.

- [x] **Step 2: Run focused tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_trading_store tests.test_trading_backup -v
```

Expected: failures because schema version 6, tables, and pruning do not exist.

- [x] **Step 3: Add the minimum schema migration**

Add `SCHEMA_V6` with these keys and constraints:

```sql
orders(client_order_id PRIMARY KEY, signal_id NULL, order_id UNIQUE NULL,
       stock_code, action, target_qty, requested_qty, filled_qty, average_fill_price,
       status, submit_count, reason, first_submitted_at, updated_at, completed_at,
       raw_json)
fills(fill_id PRIMARY KEY, client_order_id NULL REFERENCES orders(client_order_id),
      order_id NULL, signal_id NULL, stock_code, action, qty, price,
      commission, stamp_tax, other_fee, filled_at, raw_json)
account_snapshots(snapshot_id PRIMARY KEY, trade_date, generated_at, received_at,
                  cash, available_cash, total_value, position_market_value,
                  daily_turnover_pct, daily_pnl_pct, account_drawdown_pct,
                  template_version, state_hash, retained_details, raw_json)
position_snapshots(snapshot_id REFERENCES account_snapshots(snapshot_id), stock_code,
                   qty, closeable_qty, locked_qty, today_qty, avg_cost, price,
                   market_value, pnl, PRIMARY KEY(snapshot_id, stock_code))
daily_equity(trade_date PRIMARY KEY, opening_equity, closing_equity, cash,
             position_market_value, realized_pnl, unrealized_pnl, fees,
             net_deposit, max_drawdown_pct, first_snapshot_at, last_snapshot_at)
reconciliation_runs(reconciliation_id PRIMARY KEY, mode, snapshot_id,
                    started_at, finished_at, result, severity,
                    difference_count, control_action, summary_json)
reconciliation_items(item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                     reconciliation_id REFERENCES reconciliation_runs(reconciliation_id),
                     category, object_id, reason_code, local_value, platform_value,
                     tolerance, severity, details_json)
control_events(event_id TEXT PRIMARY KEY, action, operator, old_value, new_value,
               reason, reconciliation_id NULL, created_at)
```

Create indexes for order status/signal/order ID, fill order/signal/time, snapshot trade date, reconciliation result/time, and reconciliation item reason.

- [x] **Step 4: Implement bounded pruning and backup facts**

Delete only:

- `position_snapshots` whose parent account snapshot is older than the cutoff;
- old `account_snapshots`;
- old `reconciliation_runs` where `result='matched'` and no items exist.

Never delete mismatches, orders, fills, daily equity, or control events. Update `trading_backup.CORE_TABLES` to include all schema-6 tables.

- [x] **Step 5: Run focused tests and verify GREEN**

Run the Task 1 command again. Expected: all tests pass.

- [x] **Step 6: Checkpoint without Git mutation**

Run `git diff --check` and `git status --short`; do not commit.

---

### Task 2: Stable Orders, Immutable Fills, and JoinQuant Trade Export

**Files:**
- Create: `order_ledger.py`
- Create: `tests/test_order_ledger.py`
- Modify: `joinquant_strategy.py`
- Modify: `tests/test_joinquant_strategy_template.py`
- Modify: `trading_store.py`

**Interfaces:**
- Produces `client_order_id(event: dict[str, object], trade_date: str, strategy_version: str) -> str`.
- Produces `fill_id(trade: dict[str, object]) -> str`.
- Produces `normalize_order(event: dict[str, object], *, trade_date: str, strategy_version: str) -> dict[str, object]`.
- Produces `normalize_fill(trade: dict[str, object], *, orders: dict[str, dict[str, object]]) -> dict[str, object]`.
- Produces `TradingStore.upsert_order(...)` and `TradingStore.insert_fill(...)`.

- [x] **Step 1: Write failing order/fill tests**

Cover:

- the approved SHA-256 client order ID;
- platform order fallback for a manual order with no signal;
- terminal order states not regressing to submitted;
- partial to filled progression;
- duplicate fill replay returning `False`;
- same fill ID with changed content raising an immutable conflict;
- missing platform fill ID using a stable deterministic fallback;
- out-of-order fill before order linking after the order arrives.

Example:

```python
self.assertTrue(store.insert_fill(conn, normalized))
self.assertFalse(store.insert_fill(conn, normalized))
with self.assertRaises(FillConflictError):
    store.insert_fill(conn, {**normalized, "price": 11.0})
```

- [x] **Step 2: Run focused tests and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_order_ledger tests.test_trading_store -v
```

Expected: module/functions missing.

- [x] **Step 3: Implement minimum normalization and state protection**

Use only `hashlib`, existing canonical JSON, and explicit status ranks. Terminal states are `filled`, `cancelled`, `rejected`, and `risk_rejected`; a later snapshot may add fill quantity but may not move a terminal order back to an active status.

Manual platform orders use `manual:<order_id>` as `client_order_id` and leave `signal_id` null. A missing fill ID hashes `order_id|stock_code|action|filled_at|qty|price`.

- [x] **Step 4: Export platform trades from JoinQuant**

Add `_platform_trade_events()` using `get_trades()`. Emit `trade_id`, `order_id`, mapped `signal_id`, `code`, `jq_code`, `action`, `amount`, `price`, `commission`, `stamp_tax`, `other_fee`, and `datetime`. Fields unavailable from the platform remain zero/empty; do not estimate fees.

Set snapshot `trades` to `_platform_trade_events()` and bump `STRATEGY_TEMPLATE_VERSION` to a new explicit ledger version.

- [x] **Step 5: Run focused tests and verify GREEN**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_order_ledger tests.test_trading_store tests.test_joinquant_strategy_template -v
```

- [x] **Step 6: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 3: Atomic Snapshot Ingestion, Checkpoints, and Daily Equity

**Files:**
- Modify: `joinquant_sync.py`
- Modify: `trading_store.py`
- Modify: `tests/test_joinquant_sync.py`

**Interfaces:**
- Produces `snapshot_id(payload: dict[str, object]) -> str`.
- Produces `persist_account_snapshot(store: TradingStore, conn, payload: dict[str, object], *, received_at: str) -> dict[str, object]`.
- Produces `should_retain_details(conn, payload: dict[str, object]) -> bool`.
- Produces `ingest_snapshot_payload(payload: dict[str, object], store: TradingStore, *, received_at: str, mode: str = "incremental") -> dict[str, object]`.

- [x] **Step 1: Write failing ingestion tests**

Test identical callback replay, changed state, an hourly checkpoint, closing checkpoint, daily equity first/last values, position market-value balance, and transaction rollback when fill ingestion fails.

```python
first = ingest_snapshot_payload(payload, store, received_at="2026-07-14 10:00:01")
repeat = ingest_snapshot_payload(payload, store, received_at="2026-07-14 10:00:02")
self.assertEqual(first["snapshot_id"], repeat["snapshot_id"])
self.assertEqual(count("fills"), 1)
self.assertEqual(count("position_snapshots"), 1)
```

- [x] **Step 2: Run focused tests and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_joinquant_sync -v
```

- [x] **Step 3: Implement canonical snapshot identity and detail retention**

Hash only canonical platform state plus `generated_at`; exclude server `received_at`. Retain details when the state hash differs from the last retained snapshot, minute is `00`, or local time is at/after `15:00`. Account summary upsert is idempotent; retained positions use the snapshot ID composite key.

- [x] **Step 4: Update daily equity and run once-daily pruning**

Upsert the first and last equity of each trade date. Sum reported commissions/taxes/fees without inventing missing values. Use `system_state.execution_history_last_pruned` so pruning runs at most once per local date with cutoff `date - 366 days`.

- [x] **Step 5: Keep existing sync outputs compatible**

`sync_account_snapshot()` still atomically writes the portfolio JSON and migration report, but delegates ledger writes to `ingest_snapshot_payload`. Repeated server callback plus timer sync must remain idempotent.

- [x] **Step 6: Run focused tests and verify GREEN**

Run the Task 3 command plus `tests.test_trading_store`.

- [x] **Step 7: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 4: Reconciliation Engine and Automatic Controls

**Files:**
- Create: `reconciliation.py`
- Create: `tests/test_reconciliation.py`
- Create: `trading_control.py`
- Create: `tests/test_trading_control.py`
- Modify: `joinquant_sync.py`
- Modify: `trading_store.py`

**Interfaces:**
- Produces `ReconciliationDifference(category, object_id, reason_code, local_value, platform_value, tolerance, severity, details)`.
- Produces `ReconciliationResult(reconciliation_id, result, severity, differences, control_action, snapshot_id)`.
- Produces `reconcile_snapshot(store, conn, payload, *, snapshot_id: str, mode: str, now: str) -> ReconciliationResult`.
- Produces `apply_reconciliation_control(store, conn, result, *, operator: str = "system") -> list[str]`.
- Produces `unlock_eligibility(store, *, now: str) -> tuple[bool, list[str]]`.

- [x] **Step 1: Write failing reconciliation tests**

Cover matched cash/asset balance, tolerance boundary, position-cycle mismatch, sellable/frozen mismatch, order/fill aggregate mismatch, unknown order, unknown fill, manual trade, exit-intent mismatch, duplicate/conflicting fill, and severity-to-control mapping.

Test that two matched full runs must reference distinct snapshot IDs:

```python
ok, reasons = unlock_eligibility(store, now="2026-07-14 10:05:00")
self.assertFalse(ok)
self.assertIn("TWO_DISTINCT_FULL_RECONCILIATIONS_REQUIRED", reasons)
```

- [x] **Step 2: Run focused tests and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_reconciliation tests.test_trading_control -v
```

- [x] **Step 3: Implement pure comparisons and stable reason codes**

Use explicit codes including:

```text
ACCOUNT_BALANCE_MISMATCH
POSITION_QTY_MISMATCH
POSITION_SELLABLE_MISMATCH
ORDER_MISSING_LOCAL
ORDER_MISSING_PLATFORM
ORDER_FILL_QTY_MISMATCH
FILL_MISSING_LOCAL
EXIT_INTENT_MISMATCH
LEDGER_INTEGRITY_FAILURE
IMMUTABLE_FILL_CONFLICT
```

Store only non-matching items. Matched runs contain summary counts but no item rows.

- [x] **Step 4: Apply controls in the same transaction**

`ERROR` writes `buy_enabled=0`; `CRITICAL` writes both `buy_enabled=0` and `kill_switch=1`. Record a `control_events` row only when a value actually changes. Never auto-enable either state.

- [x] **Step 5: Integrate reconciliation after all snapshot rows update**

`ingest_snapshot_payload` must update cycles/orders/fills/account/positions first, reconcile second, apply controls third, and commit all together.

- [x] **Step 6: Run focused tests and verify GREEN**

Run Task 4 tests plus Task 2 and Task 3 tests.

- [x] **Step 7: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 5: Ledger-First Callback and Enterprise-WeChat Reconciliation Alerts

**Files:**
- Modify: `joinquant_signal_server.py`
- Modify: `tests/test_joinquant_signal_server.py`
- Modify: `reconciliation.py`
- Modify: `tests/test_reconciliation.py`

**Interfaces:**
- Extends `create_app(..., store: TradingStore | None = None)`.
- Produces `build_reconciliation_markdown(result: ReconciliationResult, controls: dict[str, str]) -> str`.
- Produces `notify_reconciliation(result, controls, *, notifier: WeComNotifier) -> bool`.

- [x] **Step 1: Write failing callback tests**

Prove:

- SQLite rows exist before the latest account JSON is replaced;
- a locked/failed SQLite transaction returns `503` and leaves the old JSON unchanged;
- replay is idempotent;
- `ERROR`, `CRITICAL`, severity escalation, recovery-ready, and manual control changes notify once;
- notification failure enters the existing retry queue and does not roll back ledger state;
- notification content excludes token/webhook/full raw account JSON.

- [x] **Step 2: Run focused tests and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_joinquant_signal_server tests.test_reconciliation -v
```

- [x] **Step 3: Persist and reconcile before compatible JSON publication**

Validate the callback, call `ingest_snapshot_payload`, commit, then `_write_json`. If ledger ingest fails, append a bounded API error summary, attempt a deduplicated critical notification, return `503`, and do not update ML labels or compatible JSON.

- [x] **Step 4: Implement compact notification rules**

Use dedupe key `reconciliation:<reason_code>:<object_id>:<control_state>`. Include reconciliation ID, time, severity, bounded affected-object list, control state, and these checks:

```text
bash run_ubuntu.sh trading-status
bash run_ubuntu.sh reconcile
bash run_ubuntu.sh unlock
```

- [x] **Step 5: Run focused tests and verify GREEN**

Run Task 5 tests plus existing notification tests.

- [x] **Step 6: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 6: Trading Control CLI and Guarded `run_ubuntu.sh` Menu

**Files:**
- Modify: `trading_control.py`
- Modify: `tests/test_trading_control.py`
- Modify: `run_ubuntu.sh`
- Modify: `tests/test_joinquant_linux_script.py`

**Interfaces:**
- Produces CLI commands `status`, `reconcile`, `stop-buy`, `resume-buy`, `kill-switch-on`, `kill-switch-off`, and `unlock`.
- Produces Bash commands `trading-status`, `reconcile`, `stop-buy`, `resume-buy`, `kill-switch-on`, `kill-switch-off`, and `unlock`.

- [x] **Step 1: Write failing CLI and static-menu tests**

Test required `--reason`, stale expected-state rejection, audit rows, distinct-reconciliation unlock gate, kill-switch-off without buy resume, and successful two-step unlock. Static Bash tests must assert the submenu labels and all command routes.

- [x] **Step 2: Run focused tests and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_trading_control tests.test_joinquant_linux_script -v
```

- [x] **Step 3: Implement non-interactive control commands**

Use `getpass.getuser()` for the operator and require non-empty reason. `resume-buy` and `kill-switch-off` accept an expected old update timestamp/value; mismatch returns nonzero without changing state. Every change records `control_events` and sends a notification after commit.

- [x] **Step 4: Implement the interactive unlock wizard**

The wizard must:

1. refuse non-TTY use;
2. display current control values and latest mismatch summary;
3. run one full reconciliation against the latest account snapshot;
4. require another prior matched full run on a distinct snapshot;
5. require a non-empty reason and exact confirmation;
6. turn off `kill_switch` first if active;
7. require a second confirmation before enabling buying;
8. record and notify each state change separately.

No `--force` or hidden bypass flag is allowed.

- [x] **Step 5: Add the Bash submenu and routes**

Add:

```text
交易控制与自动对账
1. 查看交易控制状态
2. 执行完整对账
3. 交易解锁向导
4. 手动停止新买入
5. 手动开启 KILL_SWITCH
0. 返回
```

Keep the direct commands documented in `usage()` and do not attach them to any timer.

- [x] **Step 6: Run focused tests and verify GREEN**

Run Task 6 tests. If Windows cannot run the three Bash execution tests, run the static test plus all platform-independent tests and reserve execution tests for Linux.

- [x] **Step 7: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 7: Health, Readiness, Backup, and End-to-End Evidence

**Files:**
- Modify: `joinquant_health.py`
- Modify: `joinquant_readiness_report.py`
- Modify: `tests/test_joinquant_health.py`
- Modify: `tests/test_joinquant_readiness_report.py`
- Modify: `tests/test_trading_backup.py`
- Create: `tests/test_execution_ledger_integration.py`

**Interfaces:**
- Health output adds schema, latest reconciliation, mismatch counts, control state, snapshot/order/fill counts, and recovery-ready status.
- Readiness fails when schema is not 6, a control is active, latest full reconciliation is not matched, or unresolved critical evidence exists.

- [x] **Step 1: Write failing health/readiness tests**

Cover healthy/mismatch/critical/recovery-ready states, off-hours alert semantics, schema mismatch, active stop-buy, active kill switch, and bounded report fields.

- [x] **Step 2: Write the failing end-to-end test**

Use Flask test client and a temporary database:

```text
signal already in ledger
→ partial order/trade callback
→ filled callback replayed twice
→ one immutable fill
→ correct order aggregate
→ account/position checkpoint
→ matched reconciliation
→ daily equity row
```

Add a mismatch callback that sets stop-buy and a later pair of distinct matched full reconciliations that makes the account eligible for manual unlock without automatically changing state.

- [x] **Step 3: Run focused tests and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_joinquant_health tests.test_joinquant_readiness_report tests.test_execution_ledger_integration -v
```

- [x] **Step 4: Add bounded ledger metrics**

Use indexed latest/count queries only. Do not scan raw JSON or full history in health/readiness jobs. Preserve non-trading-time stale-notification semantics.

- [x] **Step 5: Run focused tests and verify GREEN**

Run Task 7 tests plus backup tests.

- [x] **Step 6: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 8: Active Documentation and Full Verification

**Files:**
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-11-simulation-stability-ledger-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-complete-trading-ledger-reconciliation.md`
- Modify: `docs/superpowers/specs/2026-07-14-sqlite-backup-recovery-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-sqlite-backup-recovery.md`
- Modify: `linux_deploy.md`

**Interfaces:**
- Produces one consistent `implemented（已推送） / deployment externally unverified / not observed / not validated` status for the new code.
- Keeps server and JoinQuant state explicitly unverified until separately inspected and deployed.

- [x] **Step 1: Update documentation status and active index**

Mark complete ledger and automatic reconciliation as `implemented` only after code tests pass, and as `implemented（已推送）` only after the Git push is confirmed. Correct stale Git statements while preserving that the server and JoinQuant need fresh external verification.

- [x] **Step 2: Update storage and backup contracts**

Document actual schema-6 table growth, 366-day hot retention, long-term evidence, updated backup core counts, and restore compatibility. Do not claim the backup timer or recovery drill is deployed.

- [x] **Step 3: Update Codex read-only evidence**

Allow the auditor to read reconciliation/control status and reports. Keep it prohibited from running reconciliation, changing controls, using the unlock wizard, deploying, or restarting.

- [x] **Step 4: Run focused verification**

```powershell
.venv\Scripts\python.exe -m py_compile trading_store.py order_ledger.py reconciliation.py trading_control.py joinquant_sync.py joinquant_signal_server.py trading_backup.py
.venv\Scripts\python.exe -m unittest tests.test_trading_store tests.test_order_ledger tests.test_joinquant_sync tests.test_reconciliation tests.test_trading_control tests.test_joinquant_signal_server tests.test_joinquant_health tests.test_joinquant_readiness_report tests.test_trading_backup tests.test_execution_ledger_integration -v
```

- [x] **Step 5: Run full local verification**

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
git diff --check
git status --short --branch
```

Expected: every platform-independent test passes. If Bash execution fails only because Bash is unavailable, record the exact methods and rerun them later on Linux; do not claim Linux or server validation.

- [x] **Step 6: Review requirements line by line**

Confirm schema migration, immutable fills, callback ordering, reconciliation categories, controls, notifications, unlock menu, retention, backup facts, health/readiness, failure injection, and documentation each have passing evidence.

- [x] **Step 7: Stop before Git or deployment operations**

Report modified files, exact verification output, remaining Linux/server/JoinQuant checks, and current Git status. This implementation checkpoint was followed by separate authorization for commit `9f4c12d` and its Git push, which are complete. Server migration, JoinQuant website state, service state, and live observation remain externally unverified and require separate evidence or authorization.
