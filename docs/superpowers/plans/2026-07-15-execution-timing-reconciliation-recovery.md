# Execution Timing, Reconciliation State, and Safe Recovery Implementation Plan

> **Execution status (2026-07-15):** Tasks 1–10 plus the post-review corrections are implemented in the local workspace. Python compilation passes, the focused execution-chain suite passes 169/169, and all Windows-capable tests pass 321/321. Three `run_ubuntu.sh ledger-check` execution tests require Bash and remain pending for Linux. Status is `implemented (local workspace) / not deployed / not observed / not validated`; no commit, push, deployment, restart, server migration, or JoinQuant website update has occurred.

> **Post-review corrections:** Exit classification now consumes idless JoinQuant block/skip events; stage timing preserves first submission and material partial-fill progress; one object retains its highest-severity current issue; ordinary resolved issues recover independently while immutable ledger criticals remain sticky; transition dedupe includes severity and persisted successful-notification time supports a 30-minute ERROR reminder. Automatic ownership is ERROR-only, includes the originating control-event ID, is cancelled by every manual buy/kill-switch action, and is never created or retained for CRITICAL.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Repair the simulation execution chain so scans wake on market boundaries, signals carry honest per-item freshness, exit reconciliation models delivery/submission/fill progress, alerts are transition-based, and only reconciliation-owned stop-buy state can safely auto-recover.

**Architecture:** Upgrade the existing SQLite ledger to schema v7, add a focused `execution_state.py` domain module, and keep orchestration in the existing scheduler, exporter, JoinQuant template, reconciliation, and control modules. Runtime state remains SQLite-first and bounded; no broker, database server, or second event stream is introduced.

**Tech Stack:** Python 3 standard library, SQLite, pandas, existing Flask/JoinQuant adapters, `unittest`, Bash/systemd integration tests.

## Global Constraints

- Do not change stock selection, entry scoring, position sizing, stop-loss, take-profit, or exit-price rules.
- Automatic recovery may restore only `buy_enabled` that reconciliation itself changed from `1` to `0`.
- Never automatically disable `kill_switch`, override manual stop-buy, bypass account risk gates, or bypass `RISK_OFF`.
- Preserve signal/account callback schema version `1` and extend it compatibly.
- Preserve every schema-v6 row and never fabricate historical delivery, order, fill, or recovery evidence.
- Do not add third-party dependencies, a message broker, a database server, or an unbounded duplicate event stream.
- Do not modify or output `stock-analysis.env`, `SYNC_TOKEN`, webhook URLs, SSH keys, or other secrets.
- Do not commit, push, deploy, restart services, migrate the server, or edit the JoinQuant website without separate authorization.
- Local completion status is `implemented (local workspace) / not deployed / not observed / not validated`.

---

### Task 1: Schema v7 Lifecycle and Current Issue State

**Files:**
- Modify: `trading_store.py`
- Modify: `trading_backup.py`
- Modify: `tests/test_trading_store.py`
- Modify: `tests/test_trading_backup.py`

**Interfaces:**
- Produces `SCHEMA_VERSION = 7`.
- Extends `signals` with `validated_at` and `published_at`.
- Extends `exit_intents` with `validated_at` and `published_at`.
- Produces table `execution_issue_state`.
- Produces `TradingStore.upsert_execution_issue(conn, issue: dict[str, object]) -> dict[str, object]`.
- Produces `TradingStore.recover_execution_issue(conn, issue_key: str, now: str) -> dict[str, object] | None`.

- [x] **Step 1: Write failing migration and issue-state tests**

Add tests that initialize a new database, migrate a populated schema-v6 fixture, and prove lifecycle timestamps advance without changing immutable signal facts:

```python
self.assertEqual(store.health().schema_version, 7)
columns = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
self.assertTrue({"validated_at", "published_at"} <= columns)
self.assertEqual(conn.execute(
    "SELECT count(*) FROM signals WHERE signal_id='legacy-signal'"
).fetchone()[0], 1)
```

Add one transition test:

```python
first = store.upsert_execution_issue(conn, {
    "issue_key": "exit:s-1", "object_type": "exit_intent", "object_id": "s-1",
    "state": "SIGNAL_DELIVERY_PENDING", "severity": "INFO",
    "stage_started_at": "2026-07-15 09:30:00", "seen_at": "2026-07-15 09:31:00",
    "signal_id": "s-1", "order_id": "", "reconciliation_id": "r-1",
    "details": {"target_qty": 0},
})
repeat = store.upsert_execution_issue(conn, {**issue, "seen_at": "2026-07-15 09:31:30"})
self.assertTrue(first["transitioned"])
self.assertFalse(repeat["transitioned"])
```

- [x] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_trading_store tests.test_trading_backup -v
```

Expected: failures for schema version 7, missing columns/table, and missing store methods.

- [x] **Step 3: Add the idempotent schema-v7 migration**

Add `SCHEMA_V7` with additive columns and this bounded table:

```sql
ALTER TABLE signals ADD COLUMN validated_at TEXT;
ALTER TABLE signals ADD COLUMN published_at TEXT;
ALTER TABLE exit_intents ADD COLUMN validated_at TEXT;
ALTER TABLE exit_intents ADD COLUMN published_at TEXT;
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
```

Because SQLite has no `ADD COLUMN IF NOT EXISTS`, add a helper that checks `PRAGMA table_info` before each `ALTER TABLE`. Backfill only lifecycle timestamps that are already factual:

```sql
UPDATE signals
SET validated_at=COALESCE(validated_at, generated_at),
    published_at=COALESCE(published_at, generated_at);
UPDATE exit_intents
SET validated_at=COALESCE(validated_at, created_at),
    published_at=COALESCE(published_at, created_at);
```

- [x] **Step 4: Implement bounded issue upsert and recovery**

`upsert_execution_issue` must preserve `first_seen_at`, change `last_transition_at` only when state/severity changes or a recovered issue reopens, clear `recovered_at` on reopen, and return:

```python
{
    "issue_key": issue_key,
    "previous_state": previous_state,
    "state": state,
    "severity": severity,
    "transitioned": previous_state != state or previous_severity != severity,
    "reopened": previous_recovered_at is not None,
    "last_notified_at": previous_last_notified_at,
}
```

`recover_execution_issue` sets `state='RECOVERED'`, `severity='INFO'`, and `recovered_at=now` exactly once. A repeated recovery returns `None`.

- [x] **Step 5: Update backup facts and run GREEN tests**

Add `execution_issue_state` to `trading_backup.CORE_TABLES`, then rerun Task 1 tests. Expected: all pass.

- [x] **Step 6: Checkpoint without Git mutation**

Run `git diff --check` and `git status --short`; do not commit.

---

### Task 2: Boundary-aligned Runtime Scheduler

**Files:**
- Modify: `a_share_strategy.py`
- Modify: `tests/test_paper_trading_market_time.py`

**Interfaces:**
- Produces `resolve_runtime_phase(now: datetime | None = None) -> str` with `closed`.
- Produces `next_runtime_wake(now: datetime, phase: str, interval: int, jitter: int = 0) -> datetime`.
- Produces `sleep_until(wake_at: datetime) -> None`.

- [x] **Step 1: Write failing boundary tests**

Add exact phase assertions and boundary caps:

```python
self.assertEqual(resolve_runtime_phase(datetime(2026, 7, 15, 0, 12)), "closed")
self.assertEqual(resolve_runtime_phase(datetime(2026, 7, 15, 9, 14, 59)), "closed")
self.assertEqual(resolve_runtime_phase(datetime(2026, 7, 15, 9, 15)), "pre")
self.assertEqual(resolve_runtime_phase(datetime(2026, 7, 15, 9, 30)), "intraday")
self.assertEqual(resolve_runtime_phase(datetime(2026, 7, 15, 12, 0)), "lunch")
self.assertEqual(resolve_runtime_phase(datetime(2026, 7, 15, 13, 0)), "intraday")
self.assertEqual(resolve_runtime_phase(datetime(2026, 7, 15, 15, 0, 1)), "after")
self.assertEqual(
    next_runtime_wake(datetime(2026, 7, 15, 9, 29, 50), "pre", 300, 30),
    datetime(2026, 7, 15, 9, 30),
)
```

Patch `run_once` and `sleep_until` to prove a midnight daemon does not scan and a completed pre run sleeps to 09:30.

- [x] **Step 2: Run focused scheduler tests and verify RED**

```powershell
python -m unittest tests.test_paper_trading_market_time -v
```

Expected: `closed` and wake helpers are missing.

- [x] **Step 3: Implement phase and wake calculation**

Use local `datetime` and existing holiday configuration. The core calculation must cap intraday intervals at 11:30/15:00 and return the next trading-day 09:15 for `closed`/completed `after`:

```python
def resolve_runtime_phase(now=None):
    now = now or datetime.now()
    hhmmss = now.strftime("%H:%M:%S")
    if hhmmss < "09:15:00":
        return "closed"
    if hhmmss < "09:30:00":
        return "pre"
    if hhmmss <= "11:30:00" or "13:00:00" <= hhmmss <= "15:00:00":
        return "intraday"
    if hhmmss < "13:00:00":
        return "lunch"
    return "after"
```

The implementation must explicitly handle the 11:30–13:00 gap so the first intraday condition cannot misclassify it. `next_runtime_wake` must add jitter only when the jittered time remains before the current phase boundary.

- [x] **Step 4: Replace fixed off-phase sleeps**

In the daemon loop:

```python
if runtime_phase in {"closed", "lunch"}:
    sleep_until(next_runtime_wake(now, runtime_phase, cfg.interval, cfg.jitter))
    continue
if auto_daemon and runtime_phase in {"pre", "after"} and stage_key == last_stage_key:
    sleep_until(next_runtime_wake(now, runtime_phase, cfg.interval, cfg.jitter))
    continue
```

After every successful/failed iteration, calculate the next wake from the current phase instead of calling the existing fixed `sleep_with_jitter` for non-intraday phases.

- [x] **Step 5: Run scheduler tests and verify GREEN**

Run Task 2 tests plus `tests.test_signal_watchlist`. Expected: pass.

- [x] **Step 6: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 3: Signal Lifecycle Publication and Immutable Ledger Identity

**Files:**
- Modify: `joinquant_exporter.py`
- Modify: `trading_store.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `tests/test_trading_store.py`
- Modify: `tests/test_joinquant_export_runtime.py`

**Interfaces:**
- Extends every signal with `created_at`, `validated_at`, and `published_at`.
- Extends `SignalRecord` with `validated_at` and `published_at`.
- Produces `signal_identity(signal: dict[str, object]) -> str` or equivalent canonical immutable comparison.

- [x] **Step 1: Write failing lifecycle tests**

Test a new buy, a stable repeated sell, and an illegal immutable mutation:

```python
first = json.loads(export_signals(rows, run_id="r1", now="2026-07-15 09:30:00").read_text())
second = json.loads(export_signals(rows, run_id="r2", now="2026-07-15 09:35:00").read_text())
self.assertEqual(first["signals"][0]["created_at"], second["signals"][0]["created_at"])
self.assertEqual(second["signals"][0]["validated_at"], "2026-07-15 09:35:00")
self.assertEqual(second["signals"][0]["published_at"], second["generated_at"])
```

Use the existing exporter clock-patching pattern if adding a public `now` parameter would widen the production API unnecessarily.

- [x] **Step 2: Run focused exporter/store tests and verify RED**

```powershell
python -m unittest tests.test_joinquant_exporter tests.test_joinquant_export_runtime tests.test_trading_store -v
```

- [x] **Step 3: Add lifecycle timestamps to payload construction**

Capture publication time once per export. New buy signals use that value for all three fields. Stable sell intents obtain the original creation time from the existing ledger row and use current publication time for validation/publication:

```python
signal.update({
    "created_at": original_created_at or published_at,
    "validated_at": published_at,
    "published_at": published_at,
})
```

Only include a stable exit in the payload when the current run actually revalidated the holding and active exit condition.

- [x] **Step 4: Separate immutable signal facts from lifecycle updates**

Compare immutable canonical fields under an existing signal ID:

```python
IMMUTABLE_SIGNAL_FIELDS = (
    "id", "code", "jq_code", "action", "target_qty", "position_pct",
    "entry_price", "stop_loss", "take_profit", "execution_plan_version",
    "signal_type", "created_at",
)
```

On a matching identity, advance only `validated_at` and `published_at`. Preserve the original `generated_at`/creation evidence and immutable raw identity. On a mismatch, raise `SignalConflictError`.

- [x] **Step 5: Update exit-intent lifecycle**

`upsert_exit_intent` preserves `created_at`, advances `validated_at` and `published_at`, and does not let `reconcile_exit_intents` overwrite lifecycle validation time. Position reconciliation may still advance its general `updated_at`.

- [x] **Step 6: Run focused tests and verify GREEN**

Run Task 3 tests. Expected: pass with no existing signal parity regression.

- [x] **Step 7: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 4: JoinQuant Freshness and Runtime-State Self-healing

**Files:**
- Modify: `joinquant_strategy.py`
- Modify: `config.py`
- Modify: `tests/test_joinquant_strategy_template.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**
- Produces `_ensure_runtime_state(context) -> None` in the JoinQuant template.
- Changes `_signal_is_fresh(signal)` to prefer per-signal lifecycle time.
- Bumps the explicit template version in both template and server config.

- [x] **Step 1: Write failing static and executable template tests**

Require self-healing calls and timestamp precedence:

```python
self.assertIn("def _ensure_runtime_state(context):", text)
self.assertIn('signal.get("validated_at")', text)
self.assertIn('signal.get("created_at")', text)
self.assertIn('getattr(g, "signal_generated_at", "")', text)
self.assertIn("_ensure_runtime_state(context)\n    fetch_and_execute(context)", text)
```

Execute the template helpers with a fake `g` missing `order_signal_ids`, then call `_record_order` and assert the mapping is created without raising.

- [x] **Step 2: Run focused template/config tests and verify RED**

```powershell
python -m unittest tests.test_joinquant_strategy_template tests.test_config_env -v
```

- [x] **Step 3: Implement non-destructive state initialization**

Use `getattr` and initialize only absent/invalid containers:

```python
def _ensure_runtime_state(context):
    if not isinstance(getattr(g, "signals", None), list):
        g.signals = []
    if not isinstance(getattr(g, "executed_signal_ids", None), set):
        g.executed_signal_ids = set()
    if not isinstance(getattr(g, "order_events", None), list):
        g.order_events = []
    if not isinstance(getattr(g, "order_signal_ids", None), dict):
        g.order_signal_ids = {}
    # initialize missing metric scalars from the current portfolio only
```

Call it at every public callback and immediately before order/snapshot state use.

- [x] **Step 4: Implement per-signal freshness**

```python
text = (
    signal.get("validated_at")
    or signal.get("created_at")
    or getattr(g, "signal_generated_at", "")
    or ""
)
age = datetime.now() - datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
return timedelta(0) <= age <= timedelta(
    minutes=int(signal.get("max_age_min") or MAX_SIGNAL_AGE_MIN)
)
```

Future timestamps must not be treated as fresh.

- [x] **Step 5: Bump and align the template version**

Use one exact new value in `joinquant_strategy.py`, `config.py`, and tests:

```text
2026-07-15.1-execution-state-recovery
```

- [x] **Step 6: Run focused tests and verify GREEN**

Run Task 4 tests. Expected: pass.

- [x] **Step 7: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 5: Effective Trading Time and Exit Execution Classification

**Files:**
- Create: `execution_state.py`
- Create: `tests/test_execution_state.py`
- Modify: `reconciliation.py`
- Modify: `tests/test_reconciliation.py`

**Interfaces:**
- Produces `trading_minutes_between(start: str, end: str, holidays: set[str]) -> float`.
- Produces `exit_family(reason: str) -> str`.
- Produces `classify_exit_execution(intent: dict, orders: list[dict], platform_qty: int, now: str, holidays: set[str]) -> dict[str, object]`.

- [x] **Step 1: Write failing effective-time tests**

Cover open, lunch, overnight, weekend, and configured holiday:

```python
self.assertEqual(trading_minutes_between(
    "2026-07-15 11:29:00", "2026-07-15 13:01:00", set()
), 2.0)
self.assertEqual(trading_minutes_between(
    "2026-07-15 14:59:00", "2026-07-16 09:31:00", set()
), 2.0)
self.assertEqual(trading_minutes_between(
    "2026-07-17 14:59:00", "2026-07-20 09:31:00", set()
), 2.0)
```

- [x] **Step 2: Write failing classifier tests**

Create table-driven cases for delivery pending, stale, submitted, partial fill, target reached, T+1, suspended, limit down, and submit unknown. Assert the four family thresholds exactly.

- [x] **Step 3: Run new module tests and verify RED**

```powershell
python -m unittest tests.test_execution_state -v
```

- [x] **Step 4: Implement the pure domain module**

Use no I/O and no database access. Threshold constants:

```python
EXIT_THRESHOLDS = {
    "hard_stop": (1, 2, 3),
    "protective_stop": (2, 3, 5),
    "time_stop": (3, 5, 10),
    "take_profit": (5, 10, 15),
}
```

Market-block reasons map to WARNING states without time escalation. Target reached returns a recovered/completed result. Other active states use effective minutes to assign pending/INFO/WARNING/ERROR.

- [x] **Step 5: Integrate classification into reconciliation**

Replace the unconditional active-intent quantity mismatch with classification. `_text` must preserve zero:

```python
def _text(value):
    return "" if value is None else str(value).strip()
```

Persist non-complete execution states as reconciliation differences using stable state codes. Structural contradictions may still use `EXIT_INTENT_MISMATCH`. Keep existing account, position, order, fill, and CRITICAL comparisons unchanged.

- [x] **Step 6: Run focused tests and verify GREEN**

```powershell
python -m unittest tests.test_execution_state tests.test_reconciliation -v
```

- [x] **Step 7: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 6: Transition-based Reconciliation Notifications

**Files:**
- Modify: `reconciliation.py`
- Modify: `joinquant_sync.py`
- Modify: `joinquant_signal_server.py`
- Modify: `trading_store.py`
- Modify: `tests/test_reconciliation.py`
- Modify: `tests/test_joinquant_sync.py`
- Modify: `tests/test_joinquant_signal_server.py`

**Interfaces:**
- Extends `ReconciliationResult` with `transitions: list[dict[str, object]]`.
- Produces `persist_issue_transitions(store, conn, result, now) -> list[dict[str, object]]`.
- Extends `notify_reconciliation` to handle transition/recovery/reminder semantics.

- [x] **Step 1: Write failing transition notification tests**

Prove:

```python
self.assertEqual(first.transitions[0]["transition"], "OPENED")
self.assertEqual(replay.transitions, [])
self.assertEqual(warning.transitions[0]["transition"], "ESCALATED")
self.assertEqual(recovered.transitions[0]["state"], "RECOVERED")
```

Use a fake notifier to assert unchanged ERROR is silent before 30 minutes, sends one reminder at 30 minutes, and repeated recovery is silent.

- [x] **Step 2: Run focused tests and verify RED**

```powershell
python -m unittest tests.test_reconciliation tests.test_joinquant_sync tests.test_joinquant_signal_server -v
```

- [x] **Step 3: Persist transitions in the snapshot transaction**

After `reconcile_snapshot` creates differences and before controls apply:

```python
result.transitions = persist_issue_transitions(store, conn, result, now)
```

Recover previously active execution issues absent from the current result only when their underlying intent is completed or the current platform/ledger evidence explicitly resolves them. Do not infer recovery from a missing/failed snapshot.

- [x] **Step 4: Implement notification decision rules**

Send immediately for OPENED WARNING/ERROR/CRITICAL, ESCALATED, state change, and RECOVERED. INFO/PENDING may be stored without a message. For unchanged ERROR:

```python
reminder_due = last_notified_at is None or now - last_notified_at >= timedelta(minutes=30)
```

After a successful send, update `last_notified_at` in a separate short ledger transaction. Notification failure uses the existing retry queue and does not mark the transition notified.

- [x] **Step 5: Keep execution reports idempotent**

Do not change the existing “newly inserted fills only” execution-report path. Reconciliation transition messages and fill execution messages must use distinct dedupe keys.

- [x] **Step 6: Run focused tests and verify GREEN**

Run Task 6 tests. Expected: pass.

- [x] **Step 7: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 7: Reconciliation-owned Stop and Safe Automatic Buy Resume

**Files:**
- Modify: `trading_control.py`
- Modify: `joinquant_sync.py`
- Modify: `joinquant_signal_server.py`
- Modify: `tests/test_trading_control.py`
- Modify: `tests/test_execution_ledger_integration.py`
- Modify: `tests/test_joinquant_signal_server.py`

**Interfaces:**
- Produces `auto_resume_eligibility(store, conn, *, now: str, required_template: str) -> tuple[bool, list[str], dict[str, object]]`.
- Produces `apply_automatic_buy_recovery(store, conn, result, *, now: str, required_template: str) -> dict[str, object] | None`.
- Uses bounded system-state key `reconciliation_auto_resume_owner`.

- [x] **Step 1: Write failing ownership tests**

Assert ownership is created only on a real `ERROR` reconciliation `1 -> 0` transition and includes the control-event ID plus control generation. Assert repeated ERROR does not replace its origin, and CRITICAL never creates or retains ownership.

- [x] **Step 2: Write failing eligibility/race tests**

Cover:

```python
self.assertIn("TWO_DISTINCT_POST_STOP_MATCHES_REQUIRED", reasons)
self.assertIn("KILL_SWITCH_ACTIVE", reasons)
self.assertIn("MANUAL_CONTROL_SUPERSEDED", reasons)
self.assertIn("UNRESOLVED_EXECUTION_ERROR", reasons)
self.assertIn("SUBMIT_UNKNOWN_PRESENT", reasons)
self.assertIn("TEMPLATE_VERSION_MISMATCH", reasons)
```

Add a successful case with two consecutive distinct fresh post-stop snapshots and a failed compare-and-set case after changing `buy_enabled.updated_at`.

- [x] **Step 3: Run focused control tests and verify RED**

```powershell
python -m unittest tests.test_trading_control tests.test_execution_ledger_integration -v
```

- [x] **Step 4: Record reconciliation ownership**

When `_set_control` changes buy state for reconciliation, store compact canonical JSON:

```python
{
    "owner": "reconciliation",
    "reconciliation_id": result.reconciliation_id,
    "control_event_id": event_id,
    "expected_value": "0",
    "expected_updated_at": updated_at,
    "stopped_at": now,
}
```

Never create or retain ownership on CRITICAL, regardless of the prior buy or kill-switch state. Preserve the critical reconciliation/control evidence and require explicit human recovery.

Only an explicit manual `resume-buy` after the existing reconciliation eligibility checks may mark sticky `LEDGER_INTEGRITY_FAILURE` or `IMMUTABLE_FILL_CONFLICT` issue state recovered. Keep the critical reconciliation items and manual control event as durable history, and never alter `kill_switch` as part of that acknowledgment.

- [x] **Step 5: Give manual controls precedence**

Any manual `buy_enabled` or `kill_switch` action clears the owner key. If `stop-buy` asserts `0` while already `0`, insert a `hold_buy_disabled` control event and return a meaningful result instead of silently preserving auto-resume ownership.

- [x] **Step 6: Implement strict eligibility and compare-and-set recovery**

Require two latest consecutive post-stop matched runs with distinct snapshots, freshness <= 600 seconds, current template equal to the required version, no active ERROR/CRITICAL issue, no `submit_unknown`, no unresolved ledger critical, no kill switch, and exact control generation.

On success in the same transaction:

```python
UPDATE system_state
SET value='1', updated_at=:now, reason=:reason
WHERE key='buy_enabled' AND value='0' AND updated_at=:expected_updated_at;
```

Require `rowcount == 1`, insert one `auto_resume_buy` control event, and clear the owner marker. Never alter `kill_switch`.

- [x] **Step 7: Integrate recovery after reconciliation controls**

In snapshot ingestion, apply stop controls first and evaluate auto recovery only for a matched reconciliation. Return a `control_transition` record so the server sends one post-commit recovery notification.

- [x] **Step 8: Run focused tests and verify GREEN**

Run Task 7 tests plus `tests.test_joinquant_sync`. Expected: pass.

- [x] **Step 9: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 8: Health, Readiness, Backup, and End-to-end Evidence

**Files:**
- Modify: `joinquant_health.py`
- Modify: `joinquant_readiness_report.py`
- Modify: `tests/test_joinquant_health.py`
- Modify: `tests/test_joinquant_readiness_report.py`
- Modify: `tests/test_execution_ledger_integration.py`

**Interfaces:**
- Health exposes schema 7, active issue counts, latest issue transition, and auto-resume ownership/readiness.
- Readiness rejects old template/schema, unresolved execution ERROR/CRITICAL, or ambiguous control ownership.

- [x] **Step 1: Write failing health/readiness tests**

Add bounded assertions for healthy, active WARNING, active ERROR, recovery eligible, manual hold, kill-switch, schema mismatch, and old-template states. Do not expose raw details JSON or secrets.

- [x] **Step 2: Extend the end-to-end test**

Exercise:

```text
hard-stop exit created and published
-> first fresh snapshot has no order (pending)
-> stale order response escalates
-> fresh revalidation produces submitted order
-> partial fill
-> target reached
-> two distinct matched snapshots
-> reconciliation-owned buy stop auto-resumes exactly once
```

Add a parallel manual-hold path that never auto-resumes.

- [x] **Step 3: Run focused tests and verify RED**

```powershell
python -m unittest tests.test_joinquant_health tests.test_joinquant_readiness_report tests.test_execution_ledger_integration -v
```

- [x] **Step 4: Add indexed bounded metrics**

Use the `execution_issue_state` active index and latest control/reconciliation rows only. Do not scan full `raw_json`, full account history, or all fills.

- [x] **Step 5: Run focused tests and verify GREEN**

Run Task 8 tests plus `tests.test_trading_backup`. Expected: pass.

- [x] **Step 6: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 9: Active Documentation Alignment

**Files:**
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-11-simulation-stability-ledger-design.md`
- Modify: `docs/superpowers/specs/2026-07-14-execution-contract-p0-fixes-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-execution-contract-p0-fixes.md`
- Modify: `docs/superpowers/specs/2026-07-14-notification-review-idempotency-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-notification-review-idempotency.md`
- Modify: `docs/superpowers/specs/2026-07-15-execution-timing-reconciliation-recovery-design.md`
- Modify: `docs/superpowers/plans/2026-07-15-execution-timing-reconciliation-recovery.md`
- Modify: `linux_deploy.md`

**Interfaces:**
- Produces one consistent local-only implementation status.
- Keeps Codex auditor read-only and preserves deployment/secret boundaries.

- [x] **Step 1: Update authoritative status and active-document index**

Record schema 7, execution state, notification transitions, and guarded recovery as:

```text
implemented (local workspace) / not deployed / not observed / not validated
```

Do not describe uncommitted work as pushed or present on `origin/main`.

- [x] **Step 2: Resolve stale subordinate statuses**

Correct the older layered-exit, notification, execution-contract, and ledger documents where they still describe already verified `52b3653` deployment as pending. Preserve their historical implementation scope and point to the new spec for the 2026-07-15 follow-up.

- [x] **Step 3: Update storage and permission contracts**

Document schema-v7 columns/table, one-row-per-current-issue growth, retained transition evidence, schema-v6 restore migration, and unchanged 366-day high-frequency retention. Keep Codex prohibited from reconciliation writes, auto-resume actions, deployment, restart, and JoinQuant edits unless separately authorized in a future task.

- [x] **Step 4: Update operational documentation without claiming deployment**

Document the eventual backup/migrate/test/restart/template-update sequence, including preservation of URL/token/runtime configuration, but label every external operation unperformed.

- [x] **Step 5: Check documentation consistency**

Run:

```powershell
rg -n "schema[_ -]?version.?6|schema 6|未部署|not deployed|implemented|deployed|observed|validated" docs linux_deploy.md
git diff --check
```

Review every match in active documents and retain intentional historical statements only.

---

### Task 10: Full Verification and Local Handoff

**Files:**
- Modify only files required by failures directly caused by Tasks 1–9.

**Interfaces:**
- Produces exact platform-independent verification evidence and a bounded list of Linux/server/JoinQuant checks still required.

- [x] **Step 1: Run syntax verification**

```powershell
python -m py_compile a_share_strategy.py execution_state.py trading_store.py trading_backup.py joinquant_exporter.py joinquant_strategy.py reconciliation.py trading_control.py joinquant_sync.py joinquant_signal_server.py joinquant_health.py joinquant_readiness_report.py
```

Expected: exit code 0.

- [x] **Step 2: Run the focused execution-chain suite**

```powershell
python -m unittest tests.test_paper_trading_market_time tests.test_trading_store tests.test_trading_backup tests.test_joinquant_exporter tests.test_joinquant_export_runtime tests.test_joinquant_strategy_template tests.test_execution_state tests.test_reconciliation tests.test_trading_control tests.test_joinquant_sync tests.test_joinquant_signal_server tests.test_joinquant_health tests.test_joinquant_readiness_report tests.test_execution_ledger_integration -v
```

Expected: all platform-independent tests pass.

- [x] **Step 3: Run the complete local suite**

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: all platform-independent tests pass. Any Bash-only skip must identify the exact test and remain pending for Linux.

- [x] **Step 4: Run diff and repository checks**

```powershell
git diff --check
git status --short --branch
git diff --stat
```

Expected: no whitespace errors; only intended local source/test/doc changes; no secrets, cache, database, backup, or output artifacts.

- [x] **Step 5: Review requirements line by line**

Map every design section to a passing test or an explicit external pending item. Confirm `planned / implemented / deployed / observed / validated` labels are not conflated.

- [x] **Step 6: Stop before Git or external operations**

Report changed files, exact test totals, local-only status, and required future Linux/server/JoinQuant verification. Do not commit, push, deploy, restart, migrate the server, or modify the JoinQuant website.
