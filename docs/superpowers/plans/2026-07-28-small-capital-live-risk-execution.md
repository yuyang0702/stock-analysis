# Small-Capital Live Risk and Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace amount-based and observation-only buy preparation with versioned costs, exact board-lot sizing, enforceable pre-trade checks, atomic reservations and correct one-lot profit protection.

**Architecture:** Pure contracts and sizing functions calculate an exact quantity from a frozen account/quote/rule snapshot. A single `BEGIN IMMEDIATE` admission transaction rereads controls and current broker state, records the complete decision, reserves capacity, creates an immutable `ExecutionIntent`, and creates one READY order. JoinQuant continues as the simulator executor, but receives exact quantities and never fabricates live-ready evidence from an empty portfolio.

**Tech Stack:** Python 3.11+, dataclasses, Decimal, sqlite3, pandas, existing unittest suite and JoinQuant template.

**Status:** `planned / not implemented / not deployed / not observed / not validated`.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md`, especially sections 6, 7, 12.1, 13, 14.1 and Batch A.
- This is the first new trading migration: move `cache/trading/trading.db` from schema 10 to schema 11. Batch B and Batch D consume this schema and must not reuse version 11.
- Preserve all five previously deployed execution-correctness P0 rules, hard stop, T+1, exit-intent ownership, sell priority, 5-position/80% limits and classification exposure gates.
- The frozen chain is `StrategyOrderCandidate -> PreTradeResult -> ExecutionIntent`; a candidate has no final quantity, and an intent exists only after an allowed pre-trade result.
- `pre_trade_check(...)` is pure. It does not write SQLite, create an order, send a signal or mutate its arguments.
- Existing hard safety blocks apply in both `observe` and `enforce`; `observe` may only soften the new economic and migration-period policy gates.
- Buy checks use real current account state, open orders and active reservations. Legal sells must not be blocked by `buy_enabled=0`, buy capacity or buy economics.
- All buy orders have an exact integer `target_qty` aligned to `InstrumentRules.buy_qty_step`; platforms may not round a target value.
- QMT real-account preparation requires a positive Yuan per-trade risk cap. JoinQuant migration may omit it only with an explicit warning in evidence.
- A 100-share position reaching `+2R` does not sell; it activates profit protection. `take_profit_stage` advances only after a real partial-reduction fill.
- Actual broker-reported fees remain final facts. Estimates, backtests, ML labels and sizing all use one frozen `FeeSchedule` version.
- Git commit, push, server deployment, environment change, JoinQuant website update and restart require separate authorization at execution time.

---

## File Map

- Create `execution_contracts.py`: `FeeSchedule`, `InstrumentRules`, `QuoteSnapshot`, `BrokerSnapshot`, `StrategyOrderCandidate`, `PreTradeResult`, `ExecutionIntent`, stable logical signal identity, normalized JSON and hashes.
- Create `position_sizing.py`: discrete board-lot allocation, full round-trip fees, stop/gap losses and economic-order decisions.
- Create `execution_admission.py`: one-transaction decision, reservation, intent and READY-order admission.
- Modify `pre_trade_check.py`: keep the compatibility observation API but add the pure unified enforcement API.
- Modify `trading_store.py`: schema 11 current broker state, candidates, results, intents, reservations and profit-protection fields.
- Modify `joinquant_sync.py`: atomically replace the current normalized broker snapshot while retaining existing history facts.
- Modify `joinquant_exporter.py` and `a_share_strategy.py`: use admission and publish exact quantities from SQLite state.
- Modify `joinquant_strategy.py`: use exact target quantities and remove the buy fallback to `order_target_value()`.
- Modify `exit_policy.py`, `gap_reentry.py` and position-cycle reconciliation for one-lot/gap semantics.
- Modify `backtest_engine.py`, `historical_backtest.py` and `paper_trading.py` to consume the same fee schedule.
- Modify `trading_backup.py` for schema 11 table counts and isolated restore checks.
- Add focused tests named in each task and retain all existing execution, reconciliation and template regressions.

### Task 1: Add versioned fees, instrument rules and immutable contracts

**Files:**

- Create: `execution_contracts.py`
- Create: `tests/test_execution_contracts.py`
- Modify: `backtest_engine.py`
- Modify: `historical_backtest.py`
- Modify: `paper_trading.py`
- Modify: `config.py`
- Modify: `tests/test_backtest_engine.py`
- Modify: `tests/test_historical_backtest.py`
- Modify: `tests/test_paper_trading.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**

- Produces `FeeSchedule.estimate(side, price, qty) -> FeeBreakdown` and `estimate_round_trip(entry_price, exit_price, qty) -> RoundTripCost`.
- Produces `InstrumentRules.validate_order(side, qty, price) -> tuple[str, ...]`.
- Produces `QuoteSnapshot`, `BrokerPosition`, `BrokerSnapshot`, `StrategyOrderCandidate`, `PreTradeResult` and `ExecutionIntent` frozen records.
- Produces `canonical_json(value) -> str`, `canonical_sha256(value) -> str`, `logical_signal_id(...) -> str` and `client_order_id(account_scope_id, adapter, logical_signal_id, pre_trade_result_id, exact_order, submission_attempt_id) -> str`.

- [ ] **Step 1: Write failing fee and contract tests**

```python
def test_round_trip_applies_each_minimum_commission_and_sell_tax(self):
    fees = FeeSchedule(
        version="sim-v1", effective_from="2026-01-01",
        buy_commission_rate=Decimal("0.0003"), sell_commission_rate=Decimal("0.0003"),
        buy_minimum_commission_yuan=Decimal("5"),
        sell_minimum_commission_yuan=Decimal("5"), stamp_tax_rate=Decimal("0.0005"),
        transfer_fee_rate=Decimal("0.00001"), other_fee_rate=Decimal("0"),
        buy_slippage_rate=Decimal("0.001"), sell_slippage_rate=Decimal("0.001"),
    )
    result = fees.estimate_round_trip(Decimal("10"), Decimal("11"), 100)
    self.assertEqual(result.buy.commission_yuan, Decimal("5.00"))
    self.assertEqual(result.sell.commission_yuan, Decimal("5.00"))
    self.assertGreater(result.sell.stamp_tax_yuan, 0)

def test_instrument_rules_reject_non_board_lot_buy(self):
    rules = InstrumentRules.a_share("600000", buy_min_qty=100, buy_qty_step=100)
    self.assertEqual(rules.validate_order("buy", 150, Decimal("10")), ("BUY_QTY_STEP_INVALID",))

def test_logical_signal_identity_is_stable_and_not_run_scoped(self):
    value = logical_signal_id("scope-uuid", "2026-07-28", "main", "s1", "000001", "buy", "breakout")
    self.assertEqual(value, logical_signal_id("scope-uuid", "2026-07-28", "main", "s1", "000001", "buy", "breakout"))
    self.assertEqual(len(value), 20)

def test_client_order_id_is_idempotent_but_new_attempt_is_distinct(self):
    first = client_order_id("scope", "joinquant", "logical", "risk-1", EXACT_ORDER, "candidate-1")
    self.assertEqual(first, client_order_id("scope", "joinquant", "logical", "risk-1", EXACT_ORDER, "candidate-1"))
    self.assertNotEqual(first, client_order_id("scope", "joinquant", "logical", "risk-2", EXACT_ORDER, "manual-reissue-event-1"))
```

Also test non-finite/negative fields, missing versions, effective dates, price ticks, zero-share sells, odd-lot sell allowance, rule freshness/hash and canonical content conflicts.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_execution_contracts tests.test_backtest_engine tests.test_historical_backtest tests.test_paper_trading tests.test_config_env -v`

Expected: `execution_contracts` is missing and fee totals differ across engines.

- [ ] **Step 3: Implement Decimal-based contracts and one configured schedule**

```python
@dataclass(frozen=True)
class FeeSchedule:
    version: str
    effective_from: str
    buy_commission_rate: Decimal
    sell_commission_rate: Decimal
    buy_minimum_commission_yuan: Decimal
    sell_minimum_commission_yuan: Decimal
    stamp_tax_rate: Decimal
    transfer_fee_rate: Decimal
    other_fee_rate: Decimal
    buy_slippage_rate: Decimal
    sell_slippage_rate: Decimal
```

For an order, compute `notional = price * qty`, commission as `max(side_minimum_commission_yuan, notional * side_commission_rate)` when quantity is positive, stamp tax on sells only, and transfer/other fees on their configured sides. Slippage is a separate cash component using the side-specific rate; every component is rounded once to Fen and the unrounded inputs remain in the result hash. Round-trip cost calls the buy calculation at entry and the sell calculation independently at the planned stop/gap/target price, so each side's explicit minimum commission applies independently.

Use canonical JSON identities exactly:

```python
def logical_signal_id(account_scope_id, trade_date, strategy_id, strategy_version, code, side, setup_type):
    return canonical_sha256({
        "account_scope_id": account_scope_id, "trade_date": trade_date,
        "strategy_id": strategy_id, "strategy_version": strategy_version,
        "code": code, "side": side, "setup_type": setup_type,
    })[:20]

def client_order_id(account_scope_id, adapter, logical_signal_id, pre_trade_result_id, exact_order, submission_attempt_id):
    return canonical_sha256({
        "account_scope_id": account_scope_id, "adapter": adapter,
        "logical_signal_id": logical_signal_id,
        "pre_trade_result_id": pre_trade_result_id,
        "exact_order": exact_order,
        "submission_attempt_id": submission_attempt_id,
    })[:32]
```

For a first attempt, `submission_attempt_id` is the stable candidate ID. A future reissue uses the explicit manual reissue control-event ID, a fresh pre-trade result and therefore a new client order ID.

Centralize the simulation default currently represented by historical backtest values: commission `0.0003`, minimum commission `5`, sell stamp tax `0.0005`, and slippage `0.001`; keep transfer/other fees explicit even when zero. Expose environment keys and a mandatory `fee_schedule_version`. Real QMT admission later refuses an absent or `simulation-only` version rather than assuming the simulation default.

Replace engine-local calculations with adapters around `FeeSchedule`; keep old constructor fields as deprecated compatibility inputs only when they can be converted to a complete explicit schedule. Report the schedule version and component fees in every result.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_execution_contracts tests.test_backtest_engine tests.test_historical_backtest tests.test_paper_trading tests.test_config_env -v`

Expected: identical explicit schedule/price/quantity inputs produce identical fee components in all three engines.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add execution_contracts.py config.py backtest_engine.py historical_backtest.py paper_trading.py tests/test_execution_contracts.py tests/test_config_env.py tests/test_backtest_engine.py tests/test_historical_backtest.py tests/test_paper_trading.py
git commit -m "feat: add versioned execution fee contracts"
```

### Task 2: Solve exact board-lot quantity and economics

**Files:**

- Create: `position_sizing.py`
- Create: `tests/test_position_sizing.py`
- Modify: `gap_reentry.py`
- Modify: `tests/test_gap_reentry.py`

**Interfaces:**

- Produces `SizingPolicy`, `CapacityBudget`, `SizingDecision` and `allocate_buy_quantity(...) -> SizingDecision`.
- Consumes an explicit per-trade Yuan cap and a separate remaining portfolio open-risk budget.
- Produces stable reasons including `NO_BOARD_LOT`, `INVALID_STOP_DISTANCE`, `PER_TRADE_RISK_EXCEEDED`, `PORTFOLIO_OPEN_RISK_EXCEEDED`, `CASH_CAPACITY_EXCEEDED`, `ECONOMIC_EDGE_INSUFFICIENT` and `FEE_SCHEDULE_REQUIRED`.

- [ ] **Step 1: Write failing discrete-search and gap-budget tests**

```python
def test_allocator_searches_down_by_lot_with_minimum_fee(self):
    result = allocate_buy_quantity(
        entry_price=D("10"), stop_price=D("9.5"), gap_price=D("9.0"),
        rules=RULES_100, fees=FEES, equity=D("50000"), available_cash=D("12000"),
        risk_pct=D("0.01"), risk_cap_yuan=D("300"),
        capacity=CapacityBudget(max_qty=1300, remaining_open_risk_yuan=D("500")),
        expected_gross_return=D("0.03"), max_cost_edge_ratio=D("0.35"),
    )
    self.assertEqual(result.target_qty % 100, 0)
    self.assertLessEqual(result.worst_case_loss_yuan, D("300"))

def test_gap_one_lot_checks_trade_and_portfolio_budgets_separately(self):
    result = minimum_lot_position(per_trade_risk_yuan=D("80"), remaining_open_risk_yuan=D("200"), lot_loss_yuan=D("100"))
    self.assertFalse(result.allowed)
    self.assertIn("PER_TRADE_RISK_EXCEEDED", result.reasons)
    self.assertNotIn("PORTFOLIO_OPEN_RISK_EXCEEDED", result.reasons)
```

Cover 100/200-share steps, low/high prices, invalid stops, non-finite values, full buy/sell minimum fees, sell tax, both slippages, planned stop versus gap loss, slots/industry/theme/cash caps and an edge swallowed by fees.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_position_sizing tests.test_gap_reentry -v`

Expected: allocator is absent and current gap one-lot logic conflates the two budgets.

- [ ] **Step 3: Implement descending integer-step search**

```python
for qty in range(max_qty_aligned, rules.buy_min_qty - 1, -rules.buy_qty_step):
    costs = fees.estimate_round_trip(entry_price, stop_price, qty)
    stop_loss = planned_stop_loss(entry_price, stop_price, qty, costs)
    gap_loss = gap_scenario_loss(entry_price, gap_price, qty, fees)
    if all_constraints_pass(qty, max(stop_loss, gap_loss), costs, budgets, economics):
        return SizingDecision.allowed(qty, ...)
return SizingDecision.rejected(reasons)
```

Calculate percentage risk and Yuan cap independently, then use their minimum. Record planned-stop and gap loss separately. The economic layer can only reduce quantity or reject; it cannot exceed the rule/capacity upper bound. With ML disabled/L0/L1, expected edge comes only from frozen rule targets.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_position_sizing tests.test_gap_reentry -v`

Expected: all matrix cases return an exact, auditable lot or a stable rejection; no closed-form fee approximation remains.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add position_sizing.py gap_reentry.py tests/test_position_sizing.py tests/test_gap_reentry.py
git commit -m "feat: allocate exact small-capital order quantities"
```

### Task 3: Persist current broker state and schema 11 execution facts

**Files:**

- Modify: `trading_store.py`
- Modify: `joinquant_sync.py`
- Modify: `trading_backup.py`
- Modify: `tests/test_trading_store.py`
- Modify: `tests/test_joinquant_sync.py`
- Modify: `tests/test_trading_backup.py`

**Interfaces:**

- Produces `replace_current_broker_snapshot(conn, snapshot) -> str`.
- Produces `load_current_broker_snapshot(conn, account_scope_id) -> BrokerSnapshot | None`.
- Produces immutable candidate/result/intent inserts and active reservation aggregation/release methods.
- Adds `position_cycles.profit_protection_activated_at` and `position_cycles.trailing_stop_active_from`.

- [ ] **Step 1: Write failing migration, replacement and rollback tests**

```python
def test_snapshot_replacement_is_atomic_and_account_scoped(self):
    store.replace_current_broker_snapshot(conn, snapshot("s1", positions=[position("600000", 100)]))
    with self.assertRaises(ValueError):
        store.replace_current_broker_snapshot(conn, invalid_snapshot("s2"))
    loaded = store.load_current_broker_snapshot(conn, "scope-1")
    self.assertEqual(loaded.snapshot_id, "s1")
    self.assertEqual(loaded.positions[0].code, "600000")

def test_empty_database_and_schema_10_upgrade_reach_11_idempotently(self):
    store.initialize(); store.initialize()
    self.assertEqual(store.health().schema_version, 11)
```

Also test snapshot hashes/newness, current positions/open orders, no account credential fields, candidate/result conflicts, reservation uniqueness, backup manifests and isolated restore counts.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_trading_store tests.test_joinquant_sync tests.test_trading_backup -v`

Expected: schema is 10 and the current-state/reservation tables do not exist.

- [ ] **Step 3: Add schema 11 tables and idempotent migration**

```sql
CREATE TABLE account_scopes(
  account_scope_id TEXT PRIMARY KEY, adapter TEXT NOT NULL,
  scope_alias TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(adapter, scope_alias)
);
CREATE TABLE broker_snapshot_current(
  account_scope_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL UNIQUE,
  trade_date TEXT NOT NULL, broker_time TEXT NOT NULL, generated_at TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE broker_position_current(
  account_scope_id TEXT NOT NULL, stock_code TEXT NOT NULL,
  total_qty INTEGER NOT NULL, sellable_qty INTEGER NOT NULL,
  frozen_qty INTEGER NOT NULL, today_buy_qty INTEGER NOT NULL,
  PRIMARY KEY(account_scope_id, stock_code)
);
CREATE TABLE broker_order_current(
  account_scope_id TEXT NOT NULL, client_order_id TEXT NOT NULL,
  broker_order_id TEXT, stock_code TEXT NOT NULL, side TEXT NOT NULL,
  target_qty INTEGER NOT NULL, filled_qty INTEGER NOT NULL,
  status TEXT NOT NULL, updated_at TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  PRIMARY KEY(account_scope_id, client_order_id)
);
CREATE TABLE strategy_order_candidates(
  candidate_id TEXT PRIMARY KEY, logical_signal_id TEXT NOT NULL,
  account_scope_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE pre_trade_results(
  pre_trade_result_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE,
  allowed INTEGER NOT NULL, result_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL, checked_at TEXT NOT NULL, valid_until TEXT NOT NULL
);
CREATE TABLE execution_intents(
  client_order_id TEXT PRIMARY KEY, pre_trade_result_id TEXT NOT NULL UNIQUE,
  account_scope_id TEXT NOT NULL, intent_sha256 TEXT NOT NULL,
  submission_attempt_id TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE capacity_reservations(
  reservation_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE,
  account_scope_id TEXT NOT NULL, stock_code TEXT NOT NULL, side TEXT NOT NULL,
  target_qty INTEGER NOT NULL, cash_yuan TEXT NOT NULL,
  position_value_yuan TEXT NOT NULL, open_risk_yuan TEXT NOT NULL,
  industry TEXT NOT NULL, theme TEXT NOT NULL, uncategorized INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL, released_at TEXT, release_reason TEXT
);
```

At first adapter registration, generate and persist a random UUID account scope; never derive it from an account number, Token, webhook, URL or rotating secret. Private QMT configuration may later map its raw account to this UUID without storing the raw account in Linux facts. Use normalized child current-state tables for capacity queries and bounded JSON only for audit reconstruction. `joinquant_sync` replaces all current child rows in the same transaction as the snapshot header, while existing account/order/fill history remains unchanged. Extend backup manifests and restore checks before incrementing `schema_migrations` to 11.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_trading_store tests.test_joinquant_sync tests.test_trading_backup -v`

Expected: empty and v10 databases reach schema 11, failed replacement rolls back, and backup/restore table counts match.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add trading_store.py joinquant_sync.py trading_backup.py tests/test_trading_store.py tests/test_joinquant_sync.py tests/test_trading_backup.py
git commit -m "feat: persist broker state and execution reservations"
```

### Task 4: Implement pure unified pre-trade checks

**Files:**

- Modify: `pre_trade_check.py`
- Modify: `tests/test_pre_trade_check.py`
- Modify: `tests/test_portfolio_validation.py`
- Modify: `tests/test_trade_safety.py`

**Interfaces:**

- Produces `RiskPolicy` and `ReservationView`.
- Produces `pre_trade_check(candidate, broker_snapshot, quote, instrument_rules, system_state, risk_policy, reservations) -> PreTradeResult`.
- Preserves `evaluate_observation(...)` only as a compatibility wrapper for historical observe reports, never as the admission path.

- [ ] **Step 1: Write failing hard/soft and buy/sell matrix tests**

```python
def test_observe_does_not_soften_existing_hard_blocks(self):
    result = pre_trade_check(CANDIDATE, stale_snapshot(), QUOTE, RULES, controls(), policy(mode="observe"), no_reservations())
    self.assertFalse(result.allowed)
    self.assertIn("ACCOUNT_SNAPSHOT_STALE", result.hard_blocks)

def test_buy_disabled_does_not_block_valid_stop_sell(self):
    result = pre_trade_check(SELL_CANDIDATE, SNAPSHOT, QUOTE, RULES, controls(buy_enabled=False), policy(mode="enforce"), reservations())
    self.assertTrue(result.allowed)
```

Cover missing snapshot/rules/fees, signal age, duplicate order, unique exit ownership, T+1, suspension/limit state, price protection, 5 positions, 80% total, single stock, industry/theme/uncategorized/open-risk, cash, daily orders/turnover/loss/drawdown and `kill_switch` semantics.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_pre_trade_check tests.test_portfolio_validation tests.test_trade_safety -v`

Expected: current module only has observation structures and cannot enforce the matrix.

- [ ] **Step 3: Implement a side-effect-free decision pipeline**

Normalize inputs first, accumulate stable hard blocks and warnings in deterministic order, call `allocate_buy_quantity` only for buys, and include exact approved quantity, target holding, cash/position/classification/open-risk projections, fee components, stop/gap losses, all snapshot/version IDs, `checked_at`, `valid_until` and result hash. In observe mode, failed new economic soft gates become warnings, but every pre-existing safety gate remains a hard block.

- [ ] **Step 4: Run GREEN and old P0 regressions**

Run: `python -m unittest tests.test_pre_trade_check tests.test_portfolio_validation tests.test_trade_safety tests.test_risk_engine tests.test_execution_state -v`

Expected: all hard blocks are stable in both modes, valid sell exits remain available, and no function writes storage.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add pre_trade_check.py tests/test_pre_trade_check.py tests/test_portfolio_validation.py tests/test_trade_safety.py
git commit -m "feat: enforce unified pre-trade decisions"
```

### Task 5: Admit candidates and reserve capacity atomically

**Files:**

- Create: `execution_admission.py`
- Create: `tests/test_execution_admission.py`
- Modify: `order_ledger.py`
- Modify: `tests/test_order_ledger.py`
- Modify: `tests/test_execution_ledger_integration.py`

**Interfaces:**

- Produces `AdmissionRequest` and `AdmissionResult`.
- Produces `admit_candidate(store, request, now) -> AdmissionResult`.
- Produces `expire_ready_intents(store, now) -> int` and terminal-evidence-based reservation release.

- [ ] **Step 1: Write failing concurrency, idempotency and rollback tests**

```python
def test_two_buyers_cannot_spend_the_same_capacity(self):
    results = run_concurrently(lambda code: admit_candidate(store, request(code), NOW), ["600000", "600001"])
    self.assertEqual(sum(result.allowed for result in results), 1)
    self.assertEqual(count_active_reservations(store), 1)

def test_rejection_records_decision_but_not_intent_or_order(self):
    result = admit_candidate(store, request_with_stale_snapshot(), NOW)
    self.assertFalse(result.allowed)
    self.assertEqual(count_rows("pre_trade_results"), 1)
    self.assertEqual(count_rows("execution_intents"), 0)
    self.assertEqual(count_rows("capacity_reservations"), 0)
```

Also test same ID/same hash idempotency, same ID/different hash conflict, transaction failure rollback, `BEGIN IMMEDIATE` contention, READY expiry release, no release for `SUBMITTING/SUBMIT_UNKNOWN/SUBMITTED/PARTIALLY_FILLED`, partial-fill adjustment and sell exit ownership.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_execution_admission tests.test_order_ledger tests.test_execution_ledger_integration -v`

Expected: admission module is absent and concurrent decisions can see the same capacity.

- [ ] **Step 3: Implement the single transaction**

```text
BEGIN IMMEDIATE
read current controls and broker snapshot
aggregate holdings, open orders and active reservations
insert/conflict-check StrategyOrderCandidate
call pure pre_trade_check
insert/conflict-check PreTradeResult
if rejected: COMMIT
insert exact capacity reservation
derive immutable ExecutionIntent and client_order_id
insert READY order with normalized intent hash
COMMIT
```

An explicit `client_order_id` from the intent takes precedence; keep legacy derivation only for old JoinQuant events. Release is driven by authoritative terminal order/reconciliation facts, except never-submitted READY expiry. No empty portfolio is accepted for live-ready admission.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_execution_admission tests.test_order_ledger tests.test_execution_ledger_integration tests.test_trading_control -v`

Expected: one concurrent candidate wins, all content conflicts fail closed, and existing order/fill controls remain intact.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add execution_admission.py order_ledger.py tests/test_execution_admission.py tests/test_order_ledger.py tests/test_execution_ledger_integration.py
git commit -m "feat: reserve execution capacity atomically"
```

### Task 6: Publish exact-quantity JoinQuant intents

**Files:**

- Modify: `joinquant_exporter.py`
- Modify: `a_share_strategy.py`
- Modify: `joinquant_strategy.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `tests/test_joinquant_export_runtime.py`
- Modify: `tests/test_joinquant_strategy_template.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**

- `joinquant_exporter.export_signals(...)` consumes admitted intents and publishes `target_qty`, `target_position`, `client_order_id`, snapshot/version references and expiry.
- `joinquant_strategy.py` executes the exact `target_qty` with `order_target`, then reports the same `client_order_id`.

- [ ] **Step 1: Write failing exact-quantity and empty-state tests**

```python
def test_buy_signal_is_backed_by_admitted_exact_quantity(self):
    payload = export_with_snapshot(equity=50_000, cash=20_000)
    buy = next(item for item in payload["signals"] if item["action"] == "buy")
    self.assertEqual(buy["target_qty"] % 100, 0)
    self.assertTrue(buy["client_order_id"])
    self.assertTrue(load_intent(buy["client_order_id"]))

def test_template_has_no_amount_based_buy_fallback(self):
    text = Path("joinquant_strategy.py").read_text(encoding="utf-8")
    self.assertNotIn("order_target_value(", text)
```

Cover snapshot refresh, all-buy rejection on ledger failure while sells remain published, sell priority, stale intent, duplicate plan, existing 5/80/classification gates and byte-equivalent behavior for paths not affected by sizing.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_joinquant_exporter tests.test_joinquant_export_runtime tests.test_joinquant_strategy_template tests.test_config_env -v`

Expected: exporter still uses observation evidence/hand accumulation and template retains amount-based behavior.

- [ ] **Step 3: Wire the production path**

Build candidates from rule decisions, call `admit_candidate` per candidate, publish only admitted immutable intents, and serialize each exact integer quantity. Remove `PortfolioState.empty()` from admission evidence. In the template, recheck current cash/price/holding and reject mismatches; never resize or switch to target value. Keep existing exits able to publish when buy admission or storage fails.

- [ ] **Step 4: Run GREEN and template regressions**

Run: `python -m unittest tests.test_joinquant_exporter tests.test_joinquant_export_runtime tests.test_joinquant_strategy_template tests.test_execution_ledger_integration tests.test_signal_lifecycle -v`

Expected: every ordinary buy has a persisted intent and exact quantity; sell behavior and current JoinQuant callback format remain compatible.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add joinquant_exporter.py a_share_strategy.py joinquant_strategy.py tests/test_joinquant_exporter.py tests/test_joinquant_export_runtime.py tests/test_joinquant_strategy_template.py tests/test_config_env.py
git commit -m "feat: publish exact-quantity JoinQuant intents"
```

### Task 7: Correct one-lot and odd-lot `+2R` semantics

**Files:**

- Modify: `exit_policy.py`
- Modify: `trading_store.py`
- Modify: `a_share_strategy.py`
- Modify: `tests/test_exit_policy.py`
- Modify: `tests/test_trading_store.py`
- Modify: `tests/test_holding_stop_loss.py`
- Modify: `tests/test_execution_ledger_integration.py`

**Interfaces:**

- Produces `first_take_profit_target_qty(initial_qty, qty_step) -> int` using ceiling-half semantics.
- Produces transactional `activate_profit_protection(...)` with `trailing_stop_active_from` set to the next decision batch.
- Advances `take_profit_stage` only from confirmed partial-reduction fills.

- [ ] **Step 1: Write failing 100/300/500 and stage tests**

```python
def test_first_take_profit_targets_preserve_at_least_half(self):
    self.assertEqual(first_take_profit_target_qty(100, 100), 100)
    self.assertEqual(first_take_profit_target_qty(300, 100), 200)
    self.assertEqual(first_take_profit_target_qty(500, 100), 300)

def test_one_lot_activates_protection_next_batch_without_advancing_stage(self):
    decision = evaluate_exit(position(initial_qty=100, qty=100, price_at_2r=True), NOW_BATCH)
    self.assertEqual(decision.action, "activate_profit_protection")
    self.assertEqual(load_cycle()["take_profit_stage"], 0)
    self.assertGreater(load_cycle()["trailing_stop_active_from"], NOW_BATCH)
```

Also test duplicate scans, no same-batch trailing exit, 300-share sell 100/retain 200, 500-share sell 200/retain 300, hard stop/full exits, partial fill before stage, confirmed reduction after stage and no duplicate exit intent.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_exit_policy tests.test_trading_store tests.test_holding_stop_loss tests.test_execution_ledger_integration -v`

Expected: current floor arithmetic turns 100 to zero and over-sells 300 shares.

- [ ] **Step 3: Implement discrete target and independent protection state**

```python
def first_take_profit_target_qty(initial_qty: int, qty_step: int) -> int:
    minimum_remaining = (initial_qty + 1) // 2
    return min(initial_qty, ((minimum_remaining + qty_step - 1) // qty_step) * qty_step)
```

At `+2R`, a target equal to current quantity writes protection activation, high-water mark and next-batch activation in the same transaction but creates no sell intent. A real reduction fill updates the position cycle and then advances stage. Hard stop, effective trailing stop, time stop and market-risk exits retain their priority and may clear the position.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_exit_policy tests.test_trading_store tests.test_holding_stop_loss tests.test_execution_ledger_integration -v`

Expected: exact discrete targets, no duplicate intent, no same-batch activation/exit and fill-driven stage progression.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add exit_policy.py trading_store.py a_share_strategy.py tests/test_exit_policy.py tests/test_trading_store.py tests/test_holding_stop_loss.py tests/test_execution_ledger_integration.py
git commit -m "fix: preserve odd-lot profit protection semantics"
```

### Task 8: Verify, document and prepare deployment evidence

**Files:**

- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-11-simulation-stability-ledger-design.md`
- Modify: `docs/superpowers/specs/2026-07-13-layered-exit-risk-management-design.md`
- Modify: `docs/superpowers/plans/2026-07-13-layered-exit-risk-management.md`
- Modify: `docs/superpowers/specs/2026-07-14-execution-contract-p0-fixes-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-execution-contract-p0-fixes.md`
- Modify: `docs/superpowers/specs/2026-07-18-gap-reentry-confirmation-design.md`
- Modify: `docs/superpowers/plans/2026-07-18-gap-reentry-confirmation.md`
- Modify: `docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md`
- Modify: `linux_deploy.md`

- [ ] **Step 1: Run target compilation and focused safety suites**

```powershell
python -m py_compile execution_contracts.py position_sizing.py execution_admission.py pre_trade_check.py trading_store.py joinquant_sync.py order_ledger.py joinquant_exporter.py a_share_strategy.py joinquant_strategy.py exit_policy.py gap_reentry.py backtest_engine.py historical_backtest.py paper_trading.py
python -m unittest tests.test_execution_contracts tests.test_position_sizing tests.test_execution_admission tests.test_pre_trade_check tests.test_trading_store tests.test_joinquant_sync tests.test_order_ledger tests.test_joinquant_exporter tests.test_joinquant_export_runtime tests.test_joinquant_strategy_template tests.test_exit_policy tests.test_gap_reentry tests.test_execution_ledger_integration tests.test_trading_control tests.test_reconciliation tests.test_trading_backup -v
```

Expected: all focused tests pass and the old five P0 invariants remain green.

- [ ] **Step 2: Run the complete local suite and diff checks**

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
git diff --check
git status --short --branch
```

Expected: all platform-independent tests pass and only intentional source/test/document files are changed.

- [ ] **Step 3: Update documents truthfully**

Record schema 11, exact quantity, atomic admission and profit-protection implementation as `implemented / not deployed / not observed / not validated`. Keep old P0 as already deployed but not automatically validated. State that changed sizing/exit semantics require strict backtest and representative JoinQuant trading-day evidence before real money.

- [ ] **Step 4: Commit only after separate authorization**

```bash
git add docs/project_roadmap.md docs/project_handoff.md docs/live_trading_execution_plan.md docs/codex_simulation_observation_plan.md docs/data_storage_policy.md docs/superpowers/specs docs/superpowers/plans linux_deploy.md
git commit -m "docs: record small-capital execution implementation"
```

## Deployment And Observation Boundary

Implementation alone does not authorize deployment. A separately authorized deployment must preserve the private environment hash, create and verify an online schema-10 backup before migration, run Linux focused/full tests against isolated databases, migrate to schema 11, run `ledger-check`, create a post-migration backup, perform an isolated restore and restart only authorized stock services. The JoinQuant website template is deployed only after its actual editor content and reported template version match.

The new sizing and `+2R` behavior become `observed` only after real simulation-session evidence. They become `validated` only after strict historical comparison and representative completed position cycles; tests and a non-trading-day deployment cannot substitute for those facts.
