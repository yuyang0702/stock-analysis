# Point-in-Time Historical Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build an independent, deterministic A-share daily backtest that supports honest `strict` point-in-time evidence and explicitly labeled `price_core` proxy evidence without changing the existing signal-level backtest.

**Local completion evidence (2026-07-14):** Framework `implemented` and committed locally as `9f4c12d`. Pre-commit verification passed 254 platform-independent tests, 2 Linux static entrypoint tests, module compilation, and `git diff --check`. No push, deployment, server write, real 6/12-month dataset run, observation, or validation occurred.

**Architecture:** `historical_data.py` owns a separate SQLite history database, imports and quality gates. `historical_strategy.py` generates deterministic candidates without realtime calls. `historical_backtest.py` owns next-open matching, account state, walk-forward metrics, CLI and atomic outputs. Existing `exit_policy.py` supplies layered exit decisions; the formal trading ledger remains untouched.

**Tech Stack:** Python 3, standard library SQLite/CSV/hashlib/argparse, existing pandas, existing `exit_policy.py`, `unittest`, and `run_ubuntu.sh`.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-14-point-in-time-historical-backtest-design.md` exactly.
- Do not add dependencies or network downloads.
- Do not write `cache/trading/trading.db` or reuse `TradingStore` for historical data.
- Keep `backtest_engine.py` and `bash run_ubuntu.sh backtest` backward compatible.
- `strict` rejects missing, conflicting, future, survivorship-biased, or version-mixed inputs; it never fills them silently.
- `price_core` always reports `proxy_only=true` and cannot satisfy the complete-history gate.
- Default matching is close decision at T and open execution at T+1; T+1, suspensions, price limits, fees, slippage, lots and cash limits are mandatory.
- No further commit, push, deployment, timer, service restart, server write, or JoinQuant mutation without separate user authorization.

---

### Task 1: Independent History Store and Idempotent Import

**Files:**
- Create: `historical_data.py`
- Create: `tests/test_historical_data.py`

**Interfaces:**
- Produces `HistoricalStore(db_path: Path)` with `initialize()`, `transaction()`, `import_csv(dataset_id, kind, path, source, adjust)`, `dataset_counts(dataset_id)`, and `dataset_hash(dataset_id)`.
- Produces tables `dataset_manifests`, `daily_bars`, `daily_status`, `daily_universe`, `point_in_time_features`, `backtest_runs`, `backtest_equity`, and `backtest_trades`.

- [x] **Step 1: Write failing schema and import tests**

```python
store = HistoricalStore(tmp_path / "history.db")
store.initialize()
self.assertEqual(store.schema_version(), 1)
self.assertEqual(store.import_csv("d1", "bars", bars_csv, "joinquant", "raw"), 2)
self.assertEqual(store.import_csv("d1", "bars", bars_csv, "joinquant", "raw"), 0)
self.assertEqual(store.dataset_counts("d1")["daily_bars"], 2)
```

Add a conflicting replay test that changes `close` for the same `(dataset_id, trade_date, code)` and asserts `HistoricalDataConflict`; verify the original rows remain unchanged. Add a test proving a history DB can be created while a sentinel file at the formal trading DB path is byte-identical.

Add two adapter fixtures: JoinQuant English columns and AkShare Chinese columns (`日期/股票代码/开盘/最高/最低/收盘/昨收/成交量/成交额/复权因子`). Both must produce the same canonical row and manifest hash after source-specific metadata is excluded from row identity.

- [x] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_historical_data -v`

Expected: import fails because `historical_data` does not exist.

- [x] **Step 3: Implement the minimal schema and importers**

Use one schema migration and these required CSV columns:

```python
REQUIRED_COLUMNS = {
    "bars": {"trade_date", "code", "open", "high", "low", "close", "prev_close", "volume", "amount", "adjust_factor"},
    "status": {"trade_date", "code", "listed", "st", "suspended", "limit_up", "limit_down"},
    "universe": {"trade_date", "code"},
    "features": {"trade_date", "code", "feature_name", "feature_value", "event_at", "available_at"},
}
```

Canonicalize codes to six digits and dates to ISO. Store each source file SHA-256 in `dataset_manifests`. For an existing primary key, compare canonical values and raise `HistoricalDataConflict` on any difference; exact replays return zero inserted rows. Wrap each file import in `BEGIN IMMEDIATE`.

Keep source mapping as two constant dictionaries, not a plugin system. Unknown source or missing required mapped field raises `HistoricalDataValidationError`.

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest tests.test_historical_data -v`

Expected: all Task 1 tests pass.

- [x] **Step 5: Checkpoint without Git mutation**

Run: `git diff --check`

Do not commit.

---

### Task 2: Quality Gate and Strict/Proxy Contracts

**Files:**
- Modify: `historical_data.py`
- Modify: `tests/test_historical_data.py`

**Interfaces:**
- Produces `QualityIssue(code, count, examples)` and `QualityReport(dataset_id, mode, accepted, proxy_only, coverage, excluded_features, issues, input_hash)`.
- Produces `validate_dataset(store, dataset_id, start, end, mode, required_features) -> QualityReport`.

- [x] **Step 1: Write failing quality tests**

Cover:

```python
strict = validate_dataset(store, "d1", "2025-01-01", "2025-12-31", "strict", STRICT_FEATURES)
self.assertFalse(strict.accepted)
self.assertIn("MISSING_POINT_IN_TIME_FEATURES", [i.code for i in strict.issues])

proxy = validate_dataset(store, "d1", "2025-01-01", "2025-12-31", "price_core", STRICT_FEATURES)
self.assertTrue(proxy.accepted)
self.assertTrue(proxy.proxy_only)
self.assertIn("news_score", proxy.excluded_features)
```

Also reject `available_at` after the decision date, bars outside the dated universe, invalid OHLC, missing status, duplicate conflicts, missing adjustment factors across an ex-rights change, and mixed source/adjust declarations. Input row order must not change `input_hash`.

- [x] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_historical_data -v`

Expected: `validate_dataset` import or assertions fail.

- [x] **Step 3: Implement indexed quality queries**

Set:

```python
STRICT_FEATURES = {
    "score", "news_score", "pct_chg", "turnover", "position_pct",
    "entry_price", "stop_loss", "take_profit", "atr14", "support_level",
    "strategy_mode", "market_regime", "industry", "theme",
}
```

For `strict`, require 100% coverage for bars, status, universe and enabled features. For `price_core`, require bars/status/universe, calculate the actual excluded strict features, and always set `proxy_only=True`. Bound each issue example list to ten entries and avoid reading raw source files after import.

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest tests.test_historical_data -v`

Expected: all Task 1–2 tests pass.

- [x] **Step 5: Checkpoint without Git mutation**

Run: `git diff --check`

Do not commit.

---

### Task 3: Point-in-Time Candidate Generator

**Files:**
- Create: `historical_strategy.py`
- Create: `tests/test_historical_strategy.py`
- Modify: `historical_data.py`

**Interfaces:**
- Consumes `HistoricalStore.daily_slice(dataset_id, trade_date)` and `HistoricalStore.history_until(dataset_id, code, trade_date, limit)`.
- Produces `Candidate(code, score, position_pct, entry_price, stop_loss, take_profit, atr14, mode, market_regime, industry, theme, evidence)`.
- Produces `generate_daily_candidates(store, dataset_id, trade_date, *, mode, parameter_version, min_score=75) -> list[Candidate]`.

- [x] **Step 1: Write failing strategy tests**

For strict mode, insert complete point-in-time features and prove the current score aggregation is reproduced:

```python
expected = base_score + news_score * 1.2 + pct_rank * 5 + turnover_rank * 2
self.assertEqual(candidates[0].score, expected)
```

Prove `available_at` after the decision date is never returned. For `price_core`, provide 30 prior bars and verify candidates are deterministic, contain `evidence["proxy_only"] is True`, and use only bars/status/universe. Shuffle insertion order and assert identical candidates. Verify suspended, ST, insufficient-history, invalid-price and limit-up candidates are excluded with stable diagnostic counts.

- [x] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_historical_strategy -v`

Expected: module import fails.

- [x] **Step 3: Implement strict aggregation and minimal price core**

Strict mode reads declared features and applies the existing top-level aggregation exactly. Price core computes MA5/10/20, ATR14, 20-day high, amount percentile and daily-return percentile from data through T only. Its base rule is explicit and frozen:

```python
trend = close > ma5 > ma10 > ma20
breakout = close >= prior_20d_high
base_score = 70 + 10 * trend + 8 * breakout + 5 * pct_rank + 2 * amount_rank
```

Use existing `exit_policy.board_type`, `initial_stop_price`, `risk_position_pct`, and `market_regime`. Sort output by `(-score, code)`.

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest tests.test_historical_strategy tests.test_exit_policy -v`

Expected: both suites pass.

- [x] **Step 5: Checkpoint without Git mutation**

Run: `git diff --check`

Do not commit.

---

### Task 4: Daily Event Engine and A-Share Matching

**Files:**
- Create: `historical_backtest.py`
- Create: `tests/test_historical_backtest.py`

**Interfaces:**
- Consumes `generate_daily_candidates` and `exit_policy.evaluate_exit`.
- Produces `HistoricalBacktestConfig`, `HistoricalPosition`, `PendingOrder`, `HistoricalBacktestResult`, and `run_historical_backtest(store, dataset_id, start, end, config) -> HistoricalBacktestResult`.

- [x] **Step 1: Write failing matching tests**

Cover one behavior per test:

- close signal on T executes at T+1 open, never T close;
- 100-share lot rounding and deterministic score/code cash allocation;
- commission, minimum commission, stamp tax and 10bp buy/sell slippage;
- T+1 blocks same-day sell;
- suspension blocks both sides;
- open at/above limit-up blocks buy and open at/below limit-down blocks sell;
- hard-stop gap sells at tradable open, not the stop price;
- when daily low and high touch both stop and take-profit, hard stop wins;
- sells process before buys;
- unfilled orders expire after the day;
- an adjustment-factor change keeps position market value/cost continuity; strict mode rejects a missing factor at that boundary;
- identical shuffled inputs produce byte-equivalent trades/equity.

Example assertion:

```python
result = run_historical_backtest(store, "d1", "2025-01-02", "2025-02-28", config)
self.assertEqual(result.trades[0].decision_date, "2025-01-02")
self.assertEqual(result.trades[0].trade_date, "2025-01-03")
self.assertEqual(result.trades[0].price, round(next_open * 1.001, 4))
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_historical_backtest -v`

Expected: module import fails.

- [x] **Step 3: Implement the smallest deterministic engine**

Iterate `store.trade_dates()` once. At each date: apply the ratio between current and prior adjustment factor to position quantity/cost, execute prior pending sells then buys at open, update high-water marks, evaluate conservative OHLC exits with `PositionExitState`, create next-day candidates at close, and mark equity at close. Use `Decimal` only for final fee rounding if float assertions expose drift; otherwise keep the existing project float convention.

Record stable reason codes including `SUSPENDED`, `LIMIT_UP_BUY_BLOCKED`, `LIMIT_DOWN_SELL_BLOCKED`, `T_PLUS_ONE`, `INSUFFICIENT_CASH`, `LOT_TOO_SMALL`, `HARD_STOP`, `TAKE_PROFIT_1`, `TRAILING_STOP`, and `TIME_STOP`.

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest tests.test_historical_backtest tests.test_exit_policy -v`

Expected: all matching and exit tests pass.

- [x] **Step 5: Checkpoint without Git mutation**

Run: `git diff --check`

Do not commit.

---

### Task 5: Metrics, Walk-Forward Windows, and Parameter Comparison

**Files:**
- Modify: `historical_backtest.py`
- Modify: `tests/test_historical_backtest.py`

**Interfaces:**
- Produces `BacktestMetrics`, `WalkForwardWindow`, `compute_metrics(equity, trades)`, `group_metrics(trades, fields)`, `build_walk_forward_windows(trade_dates, count=3)`, `sensitivity_matrix(result_factory, base_config)`, and `compare_results(baseline, candidate) -> dict`.

- [x] **Step 1: Write failing metrics and isolation tests**

Use small hand-computable equity/trade fixtures and assert net return, annualized return, max drawdown, volatility, Calmar, win rate, average win/loss, Profit Factor, 5% tail loss, turnover, average holding days, and results after dropping the top three profits.

Assert three validation windows are non-overlapping, ordered, and each training end precedes validation start. Assert holdout dates are absent from candidate-ranking inputs. A baseline/candidate comparison with different dataset hash, window, fees, slippage, capital, strategy version or parameter-family count must return `COMPARISON_CONTRACT_MISMATCH`.

Assert grouping by `strategy_mode`, `market_regime`, score band, industry and theme yields bounded dictionaries and an explicit `unknown` bucket. Assert sensitivity runs cover zero/base/double slippage, base/double fees, conservative same-bar ordering and price-limit blocked counts while retaining the same dataset/window hash.

- [x] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_historical_backtest -v`

Expected: missing metrics/window functions or assertion failures.

- [x] **Step 3: Implement pure calculations**

Keep calculations free of file and current-date access. `compare_results` accepts completed result objects, verifies shared contracts, and returns per-window and aggregate deltas. It never writes parameter approval state. The top-three robustness calculation removes the three largest positive closed-trade PnLs and recomputes net profit and Profit Factor. `group_metrics` limits each dimension to the top 20 stable keys plus `other`; `sensitivity_matrix` varies only execution assumptions, never candidate selection or parameter approval state.

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest tests.test_historical_backtest -v`

Expected: all Task 4–5 tests pass.

- [x] **Step 5: Checkpoint without Git mutation**

Run: `git diff --check`

Do not commit.

---

### Task 6: Run Persistence, CLI, Atomic Reports, and Linux Routes

**Files:**
- Modify: `historical_data.py`
- Modify: `historical_backtest.py`
- Modify: `run_ubuntu.sh`
- Create: `tests/test_historical_backtest_cli.py`
- Modify: `tests/test_joinquant_linux_script.py`

**Interfaces:**
- Produces CLI commands `import`, `validate`, `run`, and `compare`.
- Produces atomic files `historical_backtest_latest.md`, `historical_backtest_quality.json`, `historical_backtest_equity.csv`, `historical_backtest_trades.csv`, and `historical_backtest_compare.json`.
- Produces Bash routes `historical-backtest` and `historical-backtest-validate` with no timer.
- Produces `HistoricalStore.prune_runs(keep_complete=20)`, preserving runs with `pinned=1` and deleting dependent equity/trades transactionally.

- [x] **Step 1: Write failing CLI and persistence tests**

Run `main([...])` against a temporary DB/output directory. Assert rejected strict validation exits nonzero and writes only quality evidence; accepted proxy run writes all four run outputs with `proxy_only=true`; identical run input/config reuses the same `run_id`; changed code/config hash gets a new ID; simulated output replacement failure leaves previous files intact; `backtest_runs` never changes a sentinel formal trading DB.

Insert 22 completed runs plus one pinned baseline, call `prune_runs(20)`, and assert only the oldest two unpinned runs and their dependent rows are deleted. Failed and pinned runs remain available for diagnosis.

Static Bash tests assert both routes, help text and no systemd timer reference.

- [x] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_historical_backtest_cli tests.test_joinquant_linux_script.JoinQuantLinuxScriptTest.test_run_script_is_the_single_linux_entrypoint -v`

Expected: CLI subcommands/routes are absent.

- [x] **Step 3: Implement CLI and atomic publication**

Use `argparse` subparsers. Derive `run_id` from dataset hash, date range, mode, strategy version, parameter version, fees, slippage and capital. Insert run/equity/trades in one history-DB transaction, then publish temporary output files with `Path.replace`. Set run status `complete` only after DB rows exist; bounded failures use `failed` and a 240-character sanitized error. Invoke `prune_runs(20)` only after a successful run; do not delete failed or pinned evidence.

Add routes:

```bash
historical-backtest) shift; run_foreground historical_backtest.py run "$@" ;;
historical-backtest-validate) shift; run_foreground historical_backtest.py validate "$@" ;;
```

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest tests.test_historical_backtest_cli tests.test_joinquant_linux_script.JoinQuantLinuxScriptTest.test_run_script_is_the_single_linux_entrypoint -v`

Expected: all CLI/static tests pass.

- [x] **Step 5: Checkpoint without Git mutation**

Run: `git diff --check`

Do not commit.

---

### Task 7: Documentation Status and Full Verification

**Files:**
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-14-point-in-time-historical-backtest-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-point-in-time-historical-backtest.md`
- Modify: `linux_deploy.md`

**Interfaces:**
- Produces one consistent status: local framework `implemented`; no imported real 6/12-month dataset means `not observed / not validated`; server remains `not deployed`.

- [x] **Step 1: Update active document relationships and truth labels**

Add the spec/plan to the roadmap index. Replace “complete history backtest planned” only after code verification with “framework implemented locally; strict data run not observed or validated.” Keep the existing signal-level backtest documented separately. State that `price_core` is proxy evidence and cannot satisfy Batch G.

- [x] **Step 2: Update storage and auditor boundaries**

Document `cache/backtest/history.db`, 2GB/year target, 3GB import refusal, no automatic deletion, separate backup/rebuild contract and no writes to the formal trading ledger. Codex may read quality/report/run summaries but may not import data, run comparisons, approve parameters, deploy or clean history data during automatic review.

- [x] **Step 3: Run focused compile and tests**

Run:

```powershell
python -m py_compile historical_data.py historical_strategy.py historical_backtest.py
python -m unittest tests.test_historical_data tests.test_historical_strategy tests.test_historical_backtest tests.test_historical_backtest_cli tests.test_backtest_engine tests.test_exit_policy -v
```

Expected: all pass.

- [x] **Step 4: Run all platform-independent tests and Linux static tests**

Run:

```powershell
$modules = Get-ChildItem tests\test_*.py | Where-Object { $_.BaseName -ne 'test_joinquant_linux_script' } | ForEach-Object { 'tests.' + $_.BaseName }
python -m unittest $modules -v
python -m unittest tests.test_joinquant_linux_script.JoinQuantLinuxScriptTest.test_run_script_is_the_single_linux_entrypoint tests.test_joinquant_linux_script.JoinQuantLinuxScriptTest.test_old_linux_entrypoints_are_removed -v
git diff --check
git status --short --branch
```

Expected: all platform-independent and Linux static tests pass. Record the three Bash execution tests as Linux-reserved if Bash remains unavailable on Windows.

- [x] **Step 5: Verify status without Git mutation**

Confirm that implementation was committed locally only after separate authorization; no push, deployment, server write, service restart, timer installation, real-data observation or validation occurred. Mark this plan locally complete only after every preceding command has fresh passing evidence.
