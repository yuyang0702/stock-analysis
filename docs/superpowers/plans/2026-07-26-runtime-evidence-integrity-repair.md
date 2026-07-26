# Runtime Evidence Integrity Repair Implementation Plan

> **For Codex:** Execute each task test-first. Do not modify trading, position, stop, take-profit, reconciliation-control, or JoinQuant order semantics.

**Goal:** Make every scan, signal, fee/P&L value, health result, notification failure, and login rejection produce truthful, bounded, queryable evidence.

**Architecture:** Extend the existing trading SQLite additively to schema 10, reuse the existing signal and strategy-run tables, and retain the current bounded JSON/JSONL operational files. Wrap the existing scan entry point with ledger lifecycle recording, add health dimensions without changing execution gates, and make the shared notifier byte-safe with finite retry states.

**Tech Stack:** Python 3, sqlite3, pandas, Flask, requests, unittest.

---

## Task 1: Record the complete strategy-run lifecycle

**Files:**

- Modify: `trading_store.py`
- Modify: `a_share_strategy.py`
- Test: `tests/test_trading_store.py`
- Test: `tests/test_strategy_run_ledger.py`

**Steps:**

1. Add failing store tests for `running -> success/empty/failed` updates and bounded error text.
2. Add a failing wrapper test proving an exception before signal export still produces a failed run.
3. Extend `StrategyRunRecord` and add an idempotent `finish_strategy_run`.
4. Add a small `_run_once_with_ledger` wrapper used by one-shot and daemon paths; pass its `run_id` through `run_once` to `run_joinquant_export`.
5. Run:

```bash
python -m unittest tests.test_trading_store tests.test_strategy_run_ledger
```

## Task 2: Populate structured signal columns

**Files:**

- Modify: `trading_store.py`
- Modify: `joinquant_exporter.py`
- Test: `tests/test_trading_store.py`
- Test: `tests/test_joinquant_exporter.py`

**Steps:**

1. Add failing tests for buy/sell structured price, stop, take-profit, score and mode fields.
2. Extend `SignalRecord` with optional fields while preserving existing positional-call compatibility.
3. Insert the existing columns and populate them from each immutable signal.
4. Keep duplicate/conflict semantics unchanged.
5. Run:

```bash
python -m unittest tests.test_trading_store tests.test_joinquant_exporter
```

## Task 3: Add fee and realized-P&L credibility states

**Files:**

- Modify: `trading_store.py`
- Modify: `order_ledger.py`
- Modify: `joinquant_sync.py`
- Test: `tests/test_trading_store.py`
- Test: `tests/test_joinquant_sync.py`
- Test: `tests/test_execution_ledger_integration.py`

**Steps:**

1. Add failing migration tests for schema 10 and historical `unknown` backfill.
2. Add failing normalization tests for complete versus missing fee fields.
3. Add failing ingestion tests for daily fee credibility and realized-P&L status.
4. Add schema 10 columns:
   - `fills.fee_data_status`
   - `daily_equity.fee_data_status`
   - `daily_equity.realized_pnl_status`
5. Mark fees `reported` only when all three source fields are present and parseable.
6. Keep realized P&L `unknown` unless the snapshot explicitly reports a parseable value; do not infer it from total equity.
7. Run:

```bash
python -m unittest tests.test_trading_store tests.test_joinquant_sync tests.test_execution_ledger_integration
```

## Task 4: Separate system health from session freshness

**Files:**

- Modify: `joinquant_health.py`
- Test: `tests/test_joinquant_health.py`

**Steps:**

1. Replace the old off-hours stale expectation with a failing test requiring no stale issue and no score deduction.
2. Add failing tests for trading-time stale, `freshness_status`, check-point `observation_status`, and daily `observation_day_status`.
3. Compute trading-session state before freshness issue generation.
4. Add `system_status`, `freshness_status`, `observation_status`, and bounded current-day aggregation.
5. Preserve all existing transaction-time safety issues and alert rules.
6. Run:

```bash
python -m unittest tests.test_joinquant_health
```

## Task 5: Make WeCom delivery byte-safe and retries finite

**Files:**

- Modify: `notifier.py`
- Test: `tests/test_notifier.py`
- Test: `tests/test_notifier_retry.py`

**Steps:**

1. Add failing UTF-8 payload-limit tests including Chinese truncation.
2. Add failing retry tests for:
   - `40058 -> dead`
   - temporary failure with backoff
   - fifth failure -> dead
   - already-sent dedupe removal
   - 100-item/30-day bound
3. Add a byte-safe markdown renderer capped below the platform hard limit.
4. Introduce a delivery result internal to the notifier while keeping `send_markdown() -> bool`.
5. Upgrade legacy rows on read, retry only due pending rows, retain dead rows for bounded audit, and atomically rewrite.
6. Run:

```bash
python -m unittest tests.test_notifier tests.test_notifier_retry
```

## Task 6: Reject Unicode login tokens safely

**Files:**

- Modify: `holdings_web.py`
- Test: `tests/test_holdings_web.py`

**Steps:**

1. Add a failing POST test with a non-ASCII invalid token.
2. Compare UTF-8 encoded bytes with `secrets.compare_digest`.
3. Verify valid authentication and existing cookie behavior remain unchanged.
4. Run:

```bash
python -m unittest tests.test_holdings_web
```

## Task 7: Integrate, document, and verify

**Files:**

- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-26-runtime-evidence-integrity-repair-design.md`

**Steps:**

1. Run focused tests after every task.
2. Run target compilation:

```bash
python -m py_compile trading_store.py order_ledger.py joinquant_sync.py joinquant_health.py notifier.py holdings_web.py a_share_strategy.py joinquant_exporter.py
```

3. Run the Windows-available full suite:

```bash
python -m unittest discover -s tests
```

4. Update documents from `planned` to `implemented（未推送） / not deployed / not observed / not validated`, record schema 10 and exact test evidence, and keep external deployment facts separate.
5. Run `git diff --check`, inspect the full diff, and commit intentionally.
6. Merge to `main` only after verification. Push/deploy/restart require the task’s external-action authorization.
7. Before deployment: preserve the environment hash, create and verify an online backup, run Linux full tests against an isolated test DB, run `ledger-check`, restart only the three stock services, and verify the environment hash is unchanged.
8. Restore the new backup only into an isolated temporary destination; verify SQLite integrity, schema 10 and bounded table counts. Never overwrite the live database.
