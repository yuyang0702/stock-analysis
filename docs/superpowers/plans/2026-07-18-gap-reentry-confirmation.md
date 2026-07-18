# Gap Reentry Confirmation Implementation Plan

状态：Tasks 1–7 已在 `feature/gap-reentry-confirmation` 完成并通过最终复查；待合并和另行授权部署。`GAP_REENTRY_ENABLE` 保持默认关闭。最终复查补齐精确100股下单、当前价费用缓冲、部分成交撤余单、交易控制拦截不误标发布，以及无效风险单位状态一致性。

执行记录：Tasks 1–7 的首轮实现合并提交为 `11e887e`；下列逐任务提交命令是原计划检查点，实际未拆成七个提交。最终复查修正另行提交，所有状态仍只表示本地功能分支 `implemented`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an auditable, risk-limited JoinQuant simulation entry path for stocks that gap above a prior plan and become tradable after a limit-up opens.

**Architecture:** Put the deterministic state transition and price/lot calculations in a focused `gap_reentry.py` module. Persist one mutable event row per stock/day/opportunity in schema 9, feed confirmed rows through the existing versioned execution-plan/export path, and retain JoinQuant as the final price/order guard.

**Tech Stack:** Python 3.11-compatible standard library, pandas, SQLite, unittest, existing strategy/exporter/ledger modules.

## Global Constraints

- Status remains `planned` until code and tests are complete; deployment, observation, and validation remain separate.
- A prior signal is reference-only; confirmation always creates a new signal ID.
- Locked limit-up orders are never queued.
- Reentry price must not exceed `original_entry_price + 0.5 * original_risk_r`.
- Confirmation requires two distinct valid trading scans at least five effective trading minutes apart.
- A reseal resets confirmation; at most two opening attempts per stock/day.
- Do not start after 14:45 or publish after 14:50.
- `RISK_OFF`, buy disable, kill switch, health/reconciliation gates, existing positions/orders, and portfolio risk remain hard blocks.
- A 100-share exception is allowed only when cash and every existing risk limit permit it.
- Store one event row per opportunity; do not add per-scan JSONL or unbounded files.
- Do not add third-party dependencies or persist credentials.

---

### Task 1: Deterministic Reentry Rules

**Files:**
- Create: `gap_reentry.py`
- Create: `tests/test_gap_reentry.py`

**Interfaces:**
- Produces: `GapReentryInput`, `GapReentryDecision`, `evaluate_gap_reentry()`, `reentry_cap_price()`, and `effective_trading_minutes()`.

- [x] **Step 1: Write failing calculation and state tests**

```python
def test_locked_limit_is_observed_without_buying():
    result = evaluate_gap_reentry(_case(at_limit=True))
    assert result.state == "LOCKED_LIMIT"
    assert result.allowed is False

def test_two_distinct_scans_confirm_below_half_r_cap():
    result = evaluate_gap_reentry(_case(
        original_entry_price=74.72, original_stop_price=69.49,
        first_open_at="2026-07-17 10:00:00", now="2026-07-17 10:05:00",
        first_open_price=76.90, price=76.95, confirmation_count=1,
    ))
    assert result.state == "OPEN_CONFIRMED"
    assert result.allowed is True

def test_reentry_above_half_r_cap_is_rejected():
    assert evaluate_gap_reentry(_case(price=77.35)).reason == "gap_reentry_too_far"
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_gap_reentry -v`
Expected: FAIL because `gap_reentry` does not exist.

- [x] **Step 3: Implement immutable inputs and pure evaluator**

```python
@dataclass(frozen=True)
class GapReentryDecision:
    state: str
    reason: str
    allowed: bool = False
    cap_price: float = 0.0
    confirmation_count: int = 0
    attempt_count: int = 0

def reentry_cap_price(entry: float, stop: float) -> float:
    risk = entry - stop
    return entry + 0.5 * risk if risk > 0 else 0.0
```

Implement fail-closed checks in this order: invalid parent, hard risk block, time boundary, locked limit, attempt limit, cap, reseal, falling more than 1%, distinct/effective scan interval, confirmation.

- [x] **Step 4: Add edge tests**

Cover lunch effective time, 14:45/14:50, `RISK_OFF`, stale quote, reseal reset, third opening, duplicate batch ID, and invalid original R.

- [x] **Step 5: Run tests and commit**

Run: `python -m unittest tests.test_gap_reentry -v`
Expected: PASS.

```bash
git add gap_reentry.py tests/test_gap_reentry.py
git commit -m "feat: add gap reentry state rules"
```

### Task 2: Schema 9 Opportunity Ledger

**Files:**
- Modify: `trading_store.py`
- Modify: `tests/test_trading_store.py`

**Interfaces:**
- Consumes: decision fields from Task 1.
- Produces: `TradingStore.upsert_gap_reentry_opportunity()` and `TradingStore.get_gap_reentry_opportunity()`.

- [x] **Step 1: Write failing schema and idempotency tests**

```python
def test_schema_v9_upserts_one_gap_opportunity_per_identity(self):
    store.initialize()
    with store.transaction() as conn:
        store.upsert_gap_reentry_opportunity(conn, event)
        store.upsert_gap_reentry_opportunity(conn, {**event, "state": "OPEN_CONFIRMED"})
    row = store.get_gap_reentry_opportunity("gap-20260717-000001")
    self.assertEqual(row["state"], "OPEN_CONFIRMED")
```

Assert schema version 9, required indexes, no duplicate row, and no secret fields.

- [x] **Step 2: Run focused test and verify RED**

Run: `python -m unittest tests.test_trading_store.TradingStoreTest.test_schema_v9_upserts_one_gap_opportunity_per_identity -v`
Expected: FAIL with missing method/schema.

- [x] **Step 3: Add schema and minimal APIs**

Add `SCHEMA_V9`, set `SCHEMA_VERSION = 9`, initialize migration idempotently, and use:

```sql
CREATE TABLE gap_reentry_opportunities (
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
```

Add indexes on `(trade_date, state)` and `(stock_code, trade_date)`.

- [x] **Step 4: Verify migration, health, backup table count and regression**

Run: `python -m unittest tests.test_trading_store -v`
Expected: PASS with schema version 9.

- [x] **Step 5: Commit**

```bash
git add trading_store.py tests/test_trading_store.py
git commit -m "feat: persist gap reentry opportunities"
```

### Task 3: Candidate Evaluation and New Execution Contract

**Files:**
- Modify: `joinquant_exporter.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `config.py`

**Interfaces:**
- Consumes: `evaluate_gap_reentry()` and opportunity ledger.
- Produces: confirmed rows tagged `entry_path=gap_reentry`, `parent_signal_id`, original anchors, cap, attempt and confirmation evidence.

- [x] **Step 1: Write failing exporter tests**

Test that ordinary chasing is still rejected, a qualifying confirmed gap row bypasses only `buy_chasing`, `RISK_OFF` still blocks, and the emitted signal has a new ID plus parent metadata.

```python
self.assertEqual(signal["entry_path"], "gap_reentry")
self.assertEqual(signal["parent_signal_id"], "old-signal")
self.assertNotEqual(signal["id"], "old-signal")
```

- [x] **Step 2: Run focused tests and verify RED**

Run: `python -m unittest tests.test_joinquant_exporter.JoinQuantExporterTest.test_confirmed_gap_reentry_creates_new_signal -v`
Expected: FAIL because confirmed gap rows are rejected as `buy_chasing`.

- [x] **Step 3: Add disabled-by-default configuration**

```python
GAP_REENTRY_ENABLE_DEFAULT = env_bool("GAP_REENTRY_ENABLE", False)
GAP_REENTRY_MAX_R_MULTIPLIER = 0.5
GAP_REENTRY_MAX_OPEN_ATTEMPTS = 2
GAP_REENTRY_MAX_OBSERVATION_DROP_PCT = 1.0
```

- [x] **Step 4: Integrate the narrow exception**

Only suppress `buy_chasing` and `buy_too_small_for_board_lot` when the row is a confirmed, enabled gap-reentry row. Re-run all remaining score, market, cash, exposure, risk, position/order, stop and take-profit checks. Add all new rejection reasons to `_REJECTION_STAGES`.

- [x] **Step 5: Rebuild the execution plan**

Use the confirmed current price, a stop no lower than the original stop, current ATR/support, one-third normal position, and existing `build_buy_execution_plan()`. Reject invalid stop or reward/risk. `_buy_signal()` must copy the audit fields without changing the parent signal.

- [x] **Step 6: Run exporter regression and commit**

Run: `python -m unittest tests.test_joinquant_exporter -v`
Expected: PASS.

```bash
git add config.py joinquant_exporter.py tests/test_joinquant_exporter.py
git commit -m "feat: export confirmed gap reentry signals"
```

### Task 4: Minimum Board-Lot Risk Exception

**Files:**
- Modify: `joinquant_exporter.py`
- Modify: `tests/test_joinquant_exporter.py`

**Interfaces:**
- Produces: exact `target_qty=100` only when the one-lot cash and risk checks pass.

- [x] **Step 1: Write failing one-lot tests**

Cover allowed 100 shares, insufficient cash, open-risk excess, sector/theme/total exposure excess, and `CAUTION` reduced budget.

- [x] **Step 2: Verify RED**

Run: `python -m unittest tests.test_joinquant_exporter.JoinQuantExporterTest.test_gap_reentry_allows_one_lot_within_risk_budget -v`
Expected: FAIL with `buy_too_small_for_board_lot`.

- [x] **Step 3: Implement the minimum calculation**

Compute one-lot position percentage and risk from account value, current entry, effective stop, fees/slippage reserve, and existing exposure. Never alter the stop to make 100 shares fit. Set both `target_qty=100` and the truthful derived `position_pct`.

- [x] **Step 4: Verify and commit**

Run: `python -m unittest tests.test_joinquant_exporter -v`
Expected: PASS.

```bash
git add joinquant_exporter.py tests/test_joinquant_exporter.py
git commit -m "feat: enforce safe one lot gap entries"
```

### Task 5: JoinQuant Final Guard and Partial-Fill Safety

**Files:**
- Modify: `joinquant_strategy.py`
- Modify: `tests/test_joinquant_strategy_template.py`

**Interfaces:**
- Consumes: signal audit fields and cap.
- Produces: final rejection reasons for reseal/price movement and no resubmission after a partial fill.

- [x] **Step 1: Write failing template tests**

Test locked limit, actual price above cap, existing/open order, partial fill followed by reseal, and same-stock/day duplicate signal.

- [x] **Step 2: Verify RED**

Run: `python -m unittest tests.test_joinquant_strategy_template -v`
Expected: FAIL because the template does not inspect `reentry_cap_price`.

- [x] **Step 3: Add final checks**

For `entry_path=gap_reentry`, reject when actual price is at/above limit or above cap. Once any quantity fills, cancel/stop the remaining target and mark the opportunity non-repeatable for that day. Preserve all current sell handling.

- [x] **Step 4: Verify and commit**

Run: `python -m unittest tests.test_joinquant_strategy_template -v`
Expected: PASS.

```bash
git add joinquant_strategy.py tests/test_joinquant_strategy_template.py
git commit -m "feat: guard gap entries at execution"
```

### Task 6: Notifications, Review Labels, and Operational Commands

**Files:**
- Modify: `a_share_strategy.py`
- Modify: `tests/test_alert_markdown.py`
- Modify: `joinquant_health.py`
- Modify: `tests/test_joinquant_health.py`
- Modify: `trading_backup.py`
- Modify: `tests/test_trading_backup.py`

**Interfaces:**
- Produces: state-change-only messages and opportunity counts in health/backup evidence.

- [x] **Step 1: Write failing presentation and backup tests**

Assert observation text never says “已到买点”, confirmed text does, server time is present, states deduplicate, and backup manifests include the new table count.

- [x] **Step 2: Verify RED**

Run: `python -m unittest tests.test_alert_markdown tests.test_joinquant_health tests.test_trading_backup -v`
Expected: FAIL on missing labels/table count.

- [x] **Step 3: Implement minimal output changes**

Map stable states to the six approved Chinese messages. Reuse the existing notifier timestamp and dedupe path. Add the table to health/backup counts without a new report file.

- [x] **Step 4: Verify and commit**

Run: `python -m unittest tests.test_alert_markdown tests.test_joinquant_health tests.test_trading_backup -v`
Expected: PASS.

```bash
git add a_share_strategy.py joinquant_health.py trading_backup.py tests/test_alert_markdown.py tests/test_joinquant_health.py tests/test_trading_backup.py
git commit -m "feat: report gap reentry lifecycle"
```

### Task 7: Full Verification and Documentation State

**Files:**
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-18-gap-reentry-confirmation-design.md`
- Modify: this plan

**Interfaces:**
- Produces: accurate `implemented / not deployed / not observed / not validated` state after code passes.

- [x] **Step 1: Run syntax and focused tests**

```bash
python -m py_compile gap_reentry.py trading_store.py joinquant_exporter.py joinquant_strategy.py a_share_strategy.py
python -m unittest tests.test_gap_reentry tests.test_trading_store tests.test_joinquant_exporter tests.test_joinquant_strategy_template tests.test_signal_lifecycle tests.test_alert_markdown tests.test_joinquant_health tests.test_trading_backup -v
```

Expected: all pass.

- [x] **Step 2: Run full regression**

Run: `python -m unittest discover -s tests -v`
Observed on Windows after final review: 435 total, 432 passed; the same 3 Linux-script tests could not start because Bash is unavailable.

- [x] **Step 3: Verify storage and Git hygiene**

```bash
git diff --check
git status --short --branch
```

Run `bash run_ubuntu.sh ledger-check` against an isolated test database on Linux before deployment. Do not touch the production database during local review.

- [x] **Step 4: Update statuses and checkboxes**

Document only local facts as `implemented`; keep server, JoinQuant, observation and validation statuses negative. Record schema 9 growth/backup policy and leave the enable switch off.

- [x] **Step 5: Commit final implementation state**

```bash
git add docs gap_reentry.py trading_store.py joinquant_exporter.py joinquant_strategy.py a_share_strategy.py config.py tests
git commit -m "docs: record gap reentry implementation"
```

Do not push or deploy without a new explicit authorization.
