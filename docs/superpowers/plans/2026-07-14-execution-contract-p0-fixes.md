# Execution Contract P0 Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make risk rejection, final buy plans, unfinished exits, JoinQuant position limits, and existing-holding classification enforce one durable and auditable simulation-trading contract.

**Architecture:** Add one pure buy-plan builder to `exit_policy.py`, make the scan row and signal anchor own the final execution fields, and make the exporter validate and copy those fields. Re-drive active exit intents from SQLite, enforce 5 positions/80% at both server and JoinQuant layers, and recover active holding classifications from immutable buy-signal JSON without changing schema version 6.

**Tech Stack:** Python 3 standard library, pandas, SQLite schema version 6, existing JoinQuant strategy template, `unittest`, Markdown documentation.

**Current Status (2026-07-14):** Tasks 1-6 are complete and were committed and pushed as `52b3653`. The server `/opt/stock-analysis` has fast-forwarded to that commit; the focused P0 suite passes 123/123, Python compilation and `ledger-check` pass, SQLite remains schema version 6, the environment file checksum is unchanged, and all three stock services are active. The JoinQuant “AI” strategy has persisted template version `2026-07-14.2-p0-execution-contract` while retaining its existing URL, token and runtime configuration. Status is `implemented（已推送） / deployed（服务器与 JoinQuant 模板） / not observed / not validated`; no real trading-day evidence exists for this version yet.

**2026-07-15 follow-up:** Execution timing, signal lifecycle, reconciliation state, and guarded auto recovery are implemented only in the local workspace under `docs/superpowers/plans/2026-07-15-execution-timing-reconciliation-recovery.md`; the deployed P0 baseline above is unchanged.

## Global Constraints

- Authoritative business design: `docs/superpowers/specs/2026-07-14-execution-contract-p0-fixes-design.md`.
- Keep JoinQuant signal schema version `1` and SQLite schema version `6`.
- Enforce `JOINQUANT_MAX_POSITIONS=5` and `JOINQUANT_MAX_TOTAL_POSITION_PCT=80` for actual simulation buys.
- Keep `MAX_TOTAL_POSITION_PCT=95` as an observation-ledger threshold only.
- Preserve legal sells when buying is disabled; only explicit `JOINQUANT_ALLOW_SELL=0` suppresses sell publication.
- Do not add JSONL files, per-scan history files, third-party dependencies, or a second database.
- Use test-first RED/GREEN cycles for every production behavior.
- Do not commit, push, deploy, update the JoinQuant website, migrate the server, or restart services without separate authorization.

---

### Task 1: Canonical Buy Execution Plan and Mandatory Risk Gate

**Files:**
- Modify: `exit_policy.py`
- Modify: `a_share_strategy.py`
- Modify: `joinquant_exporter.py`
- Modify: `tests/test_exit_policy.py`
- Modify: `tests/test_risk_engine.py`
- Modify: `tests/test_joinquant_exporter.py`

**Interfaces:**
- Produces `EXECUTION_PLAN_VERSION: str`.
- Produces `BuyExecutionPlan` with `entry_price`, `stop_loss`, `take_profit`, `risk_per_share`, `risk_reward`, `position_pct`, `board_type`, and `market_regime`.
- Produces `build_buy_execution_plan(*, code: str, entry_price: float, support_price: float, atr14: float, position_cap_pct: float, market_state: str) -> BuyExecutionPlan`.
- Extends scan rows with `execution_plan_version`, `execution_allowed`, and `execution_reject_reason`.

- [ ] **Step 1: Write the failing pure-plan test**

Add to `tests/test_exit_policy.py`:

```python
def test_build_buy_execution_plan_is_single_two_r_contract(self) -> None:
    plan = exit_policy.build_buy_execution_plan(
        code="600000", entry_price=10.0, support_price=9.6,
        atr14=0.2, position_cap_pct=20.0, market_state="NORMAL",
    )
    self.assertEqual(plan.version, exit_policy.EXECUTION_PLAN_VERSION)
    self.assertEqual(plan.take_profit, round(plan.entry_price + 2 * plan.risk_per_share, 2))
    self.assertEqual(plan.risk_reward, 2.0)
    self.assertGreater(plan.position_pct, 0)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_exit_policy.ExitPolicyTest.test_build_buy_execution_plan_is_single_two_r_contract -v
```

Expected: `AttributeError` because `build_buy_execution_plan` does not exist.

- [ ] **Step 3: Implement the pure execution plan**

Add to `exit_policy.py`:

```python
EXECUTION_PLAN_VERSION = "2026-07-14.2-p0-execution-contract"

@dataclass(frozen=True)
class BuyExecutionPlan:
    version: str
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_per_share: float
    risk_reward: float
    position_pct: float
    board_type: str
    market_regime: str

def build_buy_execution_plan(*, code, entry_price, support_price, atr14,
                             position_cap_pct, market_state):
    regime = market_regime(market_state)
    board = board_type(code, entry_price, atr14)
    stop = initial_stop_price(entry_price, support_price, atr14, board)
    risk = round(max(entry_price - stop, 0.0), 2)
    take = round(entry_price + 2 * risk, 2) if risk > 0 else 0.0
    position = risk_position_pct(entry_price, stop, board, position_cap_pct, regime)
    return BuyExecutionPlan(
        EXECUTION_PLAN_VERSION, round(entry_price, 2), stop, take, risk,
        2.0 if risk > 0 else 0.0, position, board, regime,
    )
```

- [ ] **Step 4: Run the pure-plan tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_exit_policy -v
```

Expected: all exit-policy tests pass.

- [ ] **Step 5: Write failing risk-gate and row-contract tests**

Add tests proving:

```python
def test_risk_bundle_exposes_current_execution_rejection(self) -> None:
    row = pd.Series({
        "code": "600000", "price": 10.0, "amount": 100_000_000,
        "support_level": 9.6, "atr14": 0.2, "trend_state": "明显破坏",
        "market_state": "NORMAL", "has_holding": False,
    })
    bundle = a_share_strategy.build_risk_bundle(row, {"state": "NORMAL"}, "")
    self.assertFalse(bundle["execution_allowed"])
    self.assertEqual(bundle["execution_plan_version"], exit_policy.EXECUTION_PLAN_VERSION)
```

and in exporter tests:

```python
def test_high_score_risk_disallowed_row_cannot_buy(self) -> None:
    row = self.buy_row(final_score=99, execution_allowed=False,
                       execution_reject_reason="趋势走弱")
    payload = self.export_payload([row])
    self.assertEqual(payload["signals"], [])
    self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_risk_disallowed"], 1)
```

Introduce test helpers `buy_row(**changes)` and `export_payload(rows, **kwargs)` so every direct buy fixture supplies a valid execution-plan version and `execution_allowed=True` unless the test changes it.

- [ ] **Step 6: Run the risk-gate tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_risk_engine tests.test_joinquant_exporter -v
```

Expected: failures because scan rows do not expose the contract and exporter ignores `execution_allowed`.

- [ ] **Step 7: Wire the plan into scan rows and exporter**

In `build_risk_bundle()`:

```python
plan = build_buy_execution_plan(
    code=clean_code(row.get("code")), entry_price=decision.entry_price,
    support_price=_float_value(row.get("support_level")),
    atr14=_float_value(row.get("atr14")),
    position_cap_pct=decision.position_pct,
    market_state=safe_text(row.get("market_state") or market_info.get("state")),
)
execution_allowed = bool(decision.allowed and plan.position_pct > 0)
```

Return the plan values plus current rejection fields. In `_buy_reject_reason()`, reject a false flag before score/position checks and reject missing/unknown plan versions as `buy_execution_plan_missing`. In `_buy_signal()`, copy the row's final plan fields; remove its independent stop/take/position calculation.

- [ ] **Step 8: Run focused tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_exit_policy tests.test_risk_engine tests.test_joinquant_exporter -v
```

Expected: all tests pass.

- [ ] **Step 9: Checkpoint without Git mutation**

Run `git diff --check` and inspect `git status --short`; do not commit.

---

### Task 2: Versioned Signal Anchors and Display/Signal/Ledger Equality

**Files:**
- Modify: `a_share_strategy.py`
- Modify: `tests/test_theme_and_anchor.py`
- Modify: `tests/test_joinquant_exporter.py`

**Interfaces:**
- Consumes `EXECUTION_PLAN_VERSION` and the Task 1 row contract.
- Produces a signal anchor that freezes final price/position fields but never freezes `execution_allowed`.
- Produces `format_execution_plan_text(row: Mapping[str, Any], holding: bool = False) -> str`.

- [ ] **Step 1: Write failing anchor tests**

Add tests that seed an old cache payload and then assert:

```python
self.assertEqual(result["signal_anchor_plan_version"], EXECUTION_PLAN_VERSION)
self.assertNotEqual(result["stop_loss"], old_cache_stop)
```

Add a second test where a current row has `execution_allowed=False` but cached `buy_state="已到买点"`; assert the returned row remains disallowed and displays `不建议介入`.

- [ ] **Step 2: Run the anchor tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_theme_and_anchor -v
```

Expected: failures because anchor cache has no execution-plan version and restores cached buy state.

- [ ] **Step 3: Make anchor reuse version-aware**

Change `build_signal_anchor_bundle()` so `use_cache` requires:

```python
cached.get("execution_plan_version") == EXECUTION_PLAN_VERSION
```

Cache only the final plan fields and classification anchor. Do not restore `execution_allowed`, `execution_reject_reason`, `buy_state`, `buy_reason`, or current risk reason from cache. When current execution is disallowed, preserve current `不建议介入` text.

- [ ] **Step 4: Rebuild display text after anchoring**

After applying `anchor_frame` in both normal and intraday watch paths, regenerate `risk_plan` from the anchored row fields:

```python
result["risk_plan"] = result.apply(
    lambda row: format_execution_plan_text(row, bool(row.get("has_holding"))), axis=1,
)
```

Use the same row fields in console and enterprise-WeChat renderers.

- [ ] **Step 5: Write and run the cross-layer equality test**

Build one scan row, export it with a temporary `TradingStore`, and assert exact equality for `entry_price`, `stop_loss`, `take_profit`, and `position_pct` among the anchored row, JSON signal, and decoded `signals.raw_json`.

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_theme_and_anchor tests.test_joinquant_exporter -v
```

Expected: all tests pass.

- [ ] **Step 6: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 3: Durable Exit-Intent Re-Drive and Priority Protection

**Files:**
- Modify: `a_share_strategy.py`
- Modify: `exit_policy.py`
- Modify: `trading_store.py`
- Modify: `tests/test_holding_stop_loss.py`
- Modify: `tests/test_trading_store.py`

**Interfaces:**
- Extends `merge_holding_stop_loss_rows(..., exit_intents: dict[str, dict[str, Any]] | None = None)`.
- Produces `exit_priority(reason_or_action: str) -> int` in `exit_policy.py`.
- Makes `TradingStore.upsert_exit_intent(...) -> bool` return whether the new intent became active.

- [ ] **Step 1: Write failing re-drive tests**

Add to `tests/test_holding_stop_loss.py`:

```python
def test_open_hard_stop_intent_republishes_after_price_recovers(self) -> None:
    intents = {"600000": {
        "signal_id": "cycle-hard_stop-0", "stock_code": "600000",
        "target_qty": 0, "reason": "hard_stop", "status": "active",
    }}
    result = merge_holding_stop_loss_rows(
        pd.DataFrame(), pd.DataFrame(), self.portfolio(price=10.2, stop=9.0),
        exit_intents=intents,
    )
    self.assertEqual(result.iloc[0]["exit_signal_id"], "cycle-hard_stop-0")
    self.assertEqual(result.iloc[0]["target_qty"], 0)
```

Also test an active partial target at current quantity 800/target 500, completed target at quantity 500, and a fresh hard stop overriding an active take-profit target.

- [ ] **Step 2: Run holding-exit tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_holding_stop_loss -v
```

Expected: `TypeError` because `exit_intents` is not accepted or no sell row is emitted.

- [ ] **Step 3: Merge active intent and fresh decision deterministically**

For each active holding:

```python
intent = exit_intents.get(code)
intent_pending = intent and int(qty) > int(intent.get("target_qty") or 0)
```

Construct the persisted candidate using its stable ID, target, and reason. Compare it with the fresh `evaluate_exit()` candidate using the documented priority. Keep the persisted candidate unless the fresh candidate is higher priority. Emit no persisted row after the target is reached.

- [ ] **Step 4: Write failing store downgrade tests**

Add tests that create an active hard stop and then call `upsert_exit_intent()` with a time stop or take-profit target; assert the hard stop remains active. Add the inverse case where hard stop supersedes take profit.

- [ ] **Step 5: Run store tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_trading_store.TradingStoreTest.test_higher_priority_exit_supersedes_prior_intent_for_same_stock -v
```

Expected: new downgrade assertion fails because the current method supersedes every different signal ID.

- [ ] **Step 6: Enforce priority in `upsert_exit_intent`**

Read the current active row first. Return `False` without changing it when the new action has lower priority, or when it weakens a same-priority target. Otherwise supersede the old row, upsert the new row, and return `True`.

- [ ] **Step 7: Load exit intents in the main export path**

When loading active cycles in `run_once()`, also load:

```python
open_exit_intents = trading_store.get_open_exit_intents()
```

Pass it to `merge_holding_stop_loss_rows()`. On SQLite read failure, block new buying at export time and retain the existing fixed-stop degradation path for sells.

- [ ] **Step 8: Run focused tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_holding_stop_loss tests.test_trading_store tests.test_execution_ledger_integration -v
```

Expected: all tests pass.

- [ ] **Step 9: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 4: Enforce Buy/Sell Switches, Five Positions, and 80% Total Exposure

**Files:**
- Modify: `a_share_strategy.py`
- Modify: `joinquant_exporter.py`
- Modify: `joinquant_strategy.py`
- Modify: `config.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `tests/test_joinquant_strategy_template.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**
- Extends `export_signals(..., allow_sell: bool = True, current_position_count: int = 0)`.
- Produces `load_pending_buy_codes(path: Path = app_config.JOINQUANT_ACCOUNT_FILE) -> set[str]`.
- Updates JoinQuant template version to `2026-07-14.2-p0-execution-contract`.

- [ ] **Step 1: Write failing server-limit tests**

Add tests proving:

```python
payload = self.export_payload([self.buy_row()], current_position_count=5)
self.assertEqual(payload["diagnostics"]["reject_reasons"]["buy_max_positions"], 1)
```

Use `current_position_pct=79` and a valid 2% final plan to assert `buy_total_position_limit`. Add `allow_sell=False` and assert a holding sell is absent with `sell_disabled` diagnostics. Add `allow_buy=False` with one sell and assert the sell remains.

- [ ] **Step 2: Run exporter tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_joinquant_exporter -v
```

Expected: missing arguments/reasons or incorrect 95% behavior.

- [ ] **Step 3: Enforce authoritative JoinQuant configuration**

In exporter checks use:

```python
app_config.JOINQUANT_MAX_POSITIONS_DEFAULT
app_config.JOINQUANT_MAX_TOTAL_POSITION_PCT_DEFAULT
```

Increment `current_position_count` and position percentage after each accepted candidate. Keep `MAX_TOTAL_POSITION_PCT` only in `RiskLimits` observation records.

- [ ] **Step 4: Wire config switches and pending buys**

In `run_joinquant_export()` compute:

```python
allow_buy = app_config.JOINQUANT_ALLOW_BUY_DEFAULT and is_a_share_trading_time()
allow_sell = app_config.JOINQUANT_ALLOW_SELL_DEFAULT
pending_buy_codes = load_pending_buy_codes()
current_position_count = len(positions) + len(pending_buy_codes - set(positions))
```

Pass both flags and the count to the exporter. Existing `pending_buy_position_pct` remains part of current total exposure.

- [ ] **Step 5: Write failing JoinQuant-template tests**

Assert the template contains:

```python
self.assertIn("MAX_POSITIONS = 5", text)
self.assertIn("MAX_TOTAL_POSITION_PCT = 80.0", text)
self.assertIn('return False, "max_positions"', text)
self.assertIn('return False, "total_position_limit"', text)
```

Update the expected template version in both template and config tests.

- [ ] **Step 6: Run template tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_joinquant_strategy_template tests.test_config_env -v
```

Expected: missing `MAX_POSITIONS`, missing current exposure check, and old template version.

- [ ] **Step 7: Add platform-side second checks**

Before a JoinQuant buy order:

```python
if len(context.portfolio.positions) >= MAX_POSITIONS:
    return False, "max_positions"
current_pct = sum(float(pos.value or 0) for pos in context.portfolio.positions.values()) \
    / float(context.portfolio.total_value or 1) * 100
if current_pct + float(signal.get("position_pct") or 0) > MAX_TOTAL_POSITION_PCT:
    return False, "total_position_limit"
```

Bump `STRATEGY_TEMPLATE_VERSION` and `JOINQUANT_TEMPLATE_VERSION` to the execution-contract version.

- [ ] **Step 8: Run focused tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_joinquant_exporter tests.test_joinquant_strategy_template tests.test_config_env -v
```

Expected: all tests pass.

- [ ] **Step 9: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 5: Restore Existing-Holding Industry and Theme Exposure

**Files:**
- Modify: `trading_store.py`
- Modify: `a_share_strategy.py`
- Modify: `joinquant_exporter.py`
- Modify: `tests/test_trading_store.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `tests/test_holding_stop_loss.py`

**Interfaces:**
- Produces `TradingStore.get_active_position_classifications() -> dict[str, dict[str, str]]`.
- Produces normalized helpers for row industry/theme, with `theme_label` accepted as the canonical scan fallback.
- Adds `industry` and `theme` to every buy signal `raw_json`.

- [ ] **Step 1: Write failing classification recovery test**

Create a buy signal whose raw JSON contains `industry="银行"` and `theme="中特估"`, reconcile a new active position cycle, and assert:

```python
self.assertEqual(store.get_active_position_classifications()["600000"], {
    "industry": "银行", "theme": "中特估",
})
```

- [ ] **Step 2: Run the store test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_trading_store -v
```

Expected: `AttributeError` because the bounded active-cycle query does not exist.

- [ ] **Step 3: Implement the bounded active classification query**

Join only active cycles to their entry signals:

```sql
SELECT pc.stock_code, s.raw_json
FROM position_cycles pc
LEFT JOIN signals s ON s.signal_id=pc.entry_signal_id
WHERE pc.status='active'
```

Decode only those rows, normalize `industry` and `theme`, and return empty strings for missing legacy values.

- [ ] **Step 4: Write failing exposure tests**

Add exporter tests with an existing 24% bank holding and a bank candidate, and an existing 15% AI-theme holding plus a 10% AI candidate. Assert `buy_sector_limit` and `buy_theme_limit`. Add a legacy unknown holding and assert its exposure is placed in `__UNCATEGORIZED__`, preventing another unknown allocation above 10%.

- [ ] **Step 5: Run exposure tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_joinquant_exporter tests.test_holding_stop_loss -v
```

Expected: existing position exposure is zero because position JSON lacks classification.

- [ ] **Step 6: Persist and restore classification**

In `_buy_signal()` add:

```python
"industry": normalized_industry(row),
"theme": normalized_theme(row),
```

In `run_joinquant_export()`, enrich each current position using the active-cycle classification. For legacy gaps, use the already loaded industry cache; use the stable industry label as conservative theme fallback, otherwise assign both dimensions to `__UNCATEGORIZED__`.

- [ ] **Step 7: Accumulate current, pending, and accepted exposure**

Build sector/theme dictionaries from current market values, then let the existing ordered exporter add each accepted signal. Unknown candidates and holdings use the same `__UNCATEGORIZED__` key and 10% cap.

- [ ] **Step 8: Run focused tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_trading_store tests.test_joinquant_exporter tests.test_holding_stop_loss -v
```

Expected: all tests pass.

- [ ] **Step 9: Checkpoint without Git mutation**

Run `git diff --check`; do not commit.

---

### Task 6: Documentation, Regression Verification, and Status Boundary

**Files:**
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-13-layered-exit-risk-management-design.md`
- Modify: `docs/superpowers/plans/2026-07-13-layered-exit-risk-management.md`
- Modify: `docs/superpowers/specs/2026-07-14-execution-contract-p0-fixes-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-execution-contract-p0-fixes.md`
- Modify: `linux_deploy.md`

**Interfaces:**
- Initially marks the five fixes `implemented / not deployed / not observed / not validated` after tests pass; deployment status is promoted only from separate server and JoinQuant evidence. The 2026-07-14 deployment evidence now supports `implemented（已推送） / deployed / not observed / not validated`.
- Adds the new spec and plan to the main document's active subdocument index.

- [ ] **Step 1: Run focused Python compilation**

Run:

```powershell
.venv\Scripts\python.exe -m py_compile exit_policy.py risk_engine.py a_share_strategy.py joinquant_exporter.py trading_store.py joinquant_sync.py
```

Expected: exit code 0 with no output.

- [ ] **Step 2: Run the complete P0 regression set**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_exit_policy tests.test_risk_engine tests.test_theme_and_anchor tests.test_joinquant_exporter tests.test_holding_stop_loss tests.test_trading_store tests.test_joinquant_sync tests.test_execution_ledger_integration tests.test_joinquant_strategy_template tests.test_config_env -v
```

Expected: all tests pass with zero failures and errors.

- [ ] **Step 3: Update active documentation**

Document the exact enforced contract:

```text
risk allowed is mandatory
one final execution plan
unfinished exit intents re-drive
JoinQuant maximum 5 positions / 80% total exposure
existing holdings count toward industry/theme/unknown exposure
```

Keep server and JoinQuant deployment explicitly unverified. Do not claim observed or validated evidence.

- [ ] **Step 4: Document storage impact**

Record that SQLite remains schema version 6, classification is stored only in existing immutable signal JSON, active-cycle joins are bounded, and no new file-growth stream is introduced.

- [ ] **Step 5: Run full platform-independent tests**

Run:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

Expected: every platform-independent test passes. If Bash execution tests alone fail because Bash is unavailable, record their exact names and reserve them for Linux; do not claim Linux verification.

- [ ] **Step 6: Run final repository checks**

Run:

```powershell
git diff --check
git status --short --branch
git diff --stat
```

Expected: no whitespace errors; only the approved code, tests, spec, plan, and active documentation are modified.

- [ ] **Step 7: Review the five P0 requirements line by line**

Confirm each requirement has a failing-then-passing test, production entry point, diagnostic reason, documentation statement, and explicit deployment/observation boundary.

- [ ] **Step 8: Stop before Git and deployment operations**

Report modified files, exact test totals, remaining Linux/server/JoinQuant checks, and worktree status. Do not commit, push, deploy, migrate, restart, or edit secrets.
