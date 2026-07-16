# Trained Shadow Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an auditable five-minute candidate dataset, strict historical training pipeline, versioned multi-head shadow model, manual model governance, and safe L0 runtime that cannot change existing trading behavior.

**Architecture:** Reuse the live candidate frame, historical SQLite, trading ledger, and existing report/backup patterns. Store live ML facts in a separate `cache/ml/ml.db`, store strict historical candidate cohorts in `cache/backtest/history.db`, train small scikit-learn pipelines into immutable model bundles, and route every runtime output through a deterministic permission policy. L0 is the only initially enabled level; higher levels exist behind hash-bound manual approval and remain disabled until their observation gates are met.

**Tech Stack:** Python 3.11+, pandas, SQLite, scikit-learn 1.9.0, joblib, hashlib/json/pathlib, existing unittest suite and `run_ubuntu.sh`.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-15-trained-shadow-model-design.md` exactly.
- Preserve `final_score`, current signal selection, buy/sell decisions, target positions, exits and hard risk rules while L0 is active.
- Keep `shadow_score.py` as a deterministic comparator; do not use `enhanced_score`, `shadow_rank` or `shadow_adjust_score` as first-version model inputs.
- Use one new training dependency only: `scikit-learn==1.9.0`; verify Python is at least 3.11 before changing `requirements.txt`.
- Never write high-frequency ML rows to `cache/trading/trading.db`.
- `price_core` remains proxy-only and cannot train or approve the final model.
- Exact strict rule: every feature must satisfy `available_at <= decision_at`; date-only comparison is insufficient.
- Primary label is D+5 return after buy/sell costs and slippage; D+3, D+10, MAE and fill probability are auxiliary labels.
- Weekly automation may create a challenger and evidence only. It cannot approve, activate, deploy, restart or raise a permission level.
- Model failure falls back to pure rules and never changes `buy_enabled`, `kill_switch` or sell capability.
- Live ML DB target is below 1 GB/year; warn above 1 GB/year and stop new ML detail above 2 GB/year without affecting trading.
- Historical DB target remains below 2 GB/year and rejects imports at 3 GB.
- No secret, account identifier, private URL, token, webhook or environment-file content may enter samples, models, logs or reports.
- Git commit, push, deployment, dependency installation, environment change and service restart require separate user authorization at execution time.

## File Map

- Create `candidate_core.py`: shared pure candidate-pool and final-score calculations used by live and strict history paths.
- Create `ml_contracts.py`: immutable timed-feature, candidate, label, prediction and model-manifest records plus stable hashes.
- Create `ml_store.py`: independent ML SQLite schema, transactions, bounded queries, model state and backup primitive.
- Modify `ml_dataset.py`: live candidate conversion, JSONL compatibility and DB-first review reads.
- Modify `joinquant_exporter.py`: capture every candidate and stable rejection reason without changing exported signals.
- Modify `a_share_strategy.py`: reuse shared candidate scoring and call shadow runtime after the rule score is complete.
- Modify `historical_data.py`: schema v2 strict five-minute cohort and candidate-price imports.
- Modify `historical_strategy.py`: exact `decision_at` candidate reads while retaining daily compatibility.
- Modify `historical_backtest.py`: complete implementation hash and strict candidate-cohort evidence.
- Create `ml_labels.py`: cost-aware historical/live label generation and maturity tracking.
- Create `ml_training_data.py`: feature allowlist, leakage gates, weights, walk-forward and sealed holdout splits.
- Create `ml_train.py`: baselines, gradient models, evaluation, immutable bundle creation and challenger CLI.
- Create `ml_admin.py`: hash-bound approve/activate/downgrade/rollback commands and permission state machine.
- Create `ml_runtime.py`: safe model loading, inference, deterministic score/filter/position mapping and rule fallback.
- Create `ml_maintenance.py`: ML backup, integrity, retention dry-run/apply and status.
- Modify `strategy_compare_report.py`: original vs rule-shadow vs trained-shadow comparison from bounded DB queries.
- Modify `config.py`, `requirements.txt`, `run_ubuntu.sh`: paths, disabled-by-default gates, CLI routes and bounded timers.
- Add focused tests named in each task; update current tests only where a contract intentionally changes.

---

### Task 1: Shared Candidate Core and Immutable ML Contracts

**Files:**
- Create: `candidate_core.py`
- Create: `ml_contracts.py`
- Create: `tests/test_candidate_core.py`
- Create: `tests/test_ml_contracts.py`
- Modify: `a_share_strategy.py`
- Modify: `historical_strategy.py`
- Modify: `tests/test_historical_strategy.py`

**Interfaces:**
- Produces `CandidatePoolConfig(mode, min_price, min_amount, limit)`.
- Produces `build_candidate_pool(frame, config) -> pandas.DataFrame` and `score_candidate_frame(frame) -> pandas.DataFrame`.
- Produces `TimedFeature(value, available_at)`, `CandidateSample`, `LabelRecord`, `PredictionRecord`, `ModelManifest`.
- Produces `candidate_sample_id(sample) -> str` and `canonical_hash(value) -> str`.

- [ ] **Step 1: Write failing contract and behavior tests**

```python
def test_shared_pool_keeps_top_30_and_reproduces_live_score(self):
    rows = pd.DataFrame([
        {"code": f"{i:06d}", "name": "普通股", "price": 10, "amount": 1e8 + i,
         "pct_chg": 4 + i / 100, "turnover": 2 + i / 100, "score": 70 + i / 10,
         "news_score": i % 3}
        for i in range(40)
    ])
    pool = build_candidate_pool(rows, CandidatePoolConfig("intraday", 2, 5e7, 30))
    scored = score_candidate_frame(pool)
    self.assertEqual(len(scored), 30)
    expected = scored["score"] + scored["news_score"] * 1.2
    expected += scored["pct_chg"].rank(pct=True) * 5
    expected += scored["turnover"].rank(pct=True) * 2
    pd.testing.assert_series_equal(scored["final_score"], expected, check_names=False)

def test_candidate_hash_is_stable_and_future_feature_is_rejected(self):
    sample = CandidateSample.from_values(
        source="strict", dataset_id="d1", decision_at="2025-01-02T10:00:00+08:00",
        code="600000", strategy_version="s1", parameter_version="p1",
        feature_schema_version="f1",
        features={"price": TimedFeature(10.0, "2025-01-02T09:59:00+08:00")},
        selected=False, rejection_stage="score", rejection_code="buy_low_score",
    )
    self.assertEqual(candidate_sample_id(sample), candidate_sample_id(sample))
    with self.assertRaisesRegex(ValueError, "FEATURE_FROM_FUTURE"):
        CandidateSample.from_values(
            source="strict", dataset_id="d1", decision_at="2025-01-02T10:00:00+08:00",
            code="600000", strategy_version="s1", parameter_version="p1",
            feature_schema_version="f1",
            features={"price": TimedFeature(10.0, "2025-01-02T10:01:00+08:00")},
            selected=False, rejection_stage="score", rejection_code="buy_low_score",
        )
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_candidate_core tests.test_ml_contracts tests.test_historical_strategy -v`

Expected: imports fail because `candidate_core` and `ml_contracts` do not exist.

- [ ] **Step 3: Implement the shared pure functions and records**

```python
@dataclass(frozen=True)
class CandidatePoolConfig:
    mode: str
    min_price: float
    min_amount: float
    limit: int

def score_candidate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["final_score"] = pd.to_numeric(result["score"], errors="coerce").fillna(0)
    result["final_score"] += pd.to_numeric(result.get("news_score", 0), errors="coerce").fillna(0) * 1.2
    result["final_score"] += pd.to_numeric(result["pct_chg"], errors="coerce").rank(pct=True).fillna(0) * 5
    if "turnover" in result:
        result["final_score"] += pd.to_numeric(result["turnover"], errors="coerce").rank(pct=True).fillna(0) * 2
    return result

def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

`CandidateSample.from_values` must normalize the code to six digits, require timezone-aware ISO timestamps, reject any timed feature later than `decision_at`, exclude names/account data from identity, and derive `sample_id` from the identity fields in the spec. Replace the two duplicated live/historical score formulas with `score_candidate_frame`; keep `a_share_strategy.build_pool` as a compatibility wrapper around `build_candidate_pool`.

- [ ] **Step 4: Run focused and regression tests**

Run: `python -m unittest tests.test_candidate_core tests.test_ml_contracts tests.test_historical_strategy tests.test_shadow_score -v`

Expected: all tests pass and historical candidate order remains deterministic.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: no whitespace errors. Do not commit without separate authorization; suggested commit is `refactor: share candidate scoring contracts`.

---

### Task 2: Independent ML SQLite Store and Model State

**Files:**
- Create: `ml_store.py`
- Create: `tests/test_ml_store.py`
- Modify: `config.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**
- Produces `MlStore(path, max_bytes=2_000_000_000)` with `initialize`, `transaction`, `record_candidates`, `upsert_labels`, `record_predictions`, `register_model`, `record_model_event`, `runtime_state`, `compare_and_swap_runtime`, `backup_to`, `integrity_check`.
- Produces schema version 1 and the tables `ml_candidate_samples`, `ml_labels`, `ml_predictions`, `ml_models`, `ml_model_events`, `ml_runtime_state`.

- [ ] **Step 1: Write failing schema, idempotency and isolation tests**

```python
def test_ml_store_is_idempotent_and_does_not_touch_trading_db(self):
    trading = self.root / "cache" / "trading" / "trading.db"
    trading.parent.mkdir(parents=True)
    trading.write_bytes(b"ledger-sentinel")
    store = MlStore(self.root / "cache" / "ml" / "ml.db")
    store.initialize()
    self.assertEqual(store.schema_version(), 1)
    self.assertEqual(store.record_candidates([self.sample]), 1)
    self.assertEqual(store.record_candidates([self.sample]), 0)
    self.assertEqual(trading.read_bytes(), b"ledger-sentinel")

def test_conflicting_sample_rolls_back(self):
    store.record_candidates([self.sample])
    changed = replace(self.sample, rejection_code="different")
    with self.assertRaises(MlDataConflict):
        store.record_candidates([changed])
    self.assertEqual(store.counts()["ml_candidate_samples"], 1)
```

Also test foreign keys, prediction uniqueness `(sample_id, model_id)`, immutable model hash, CAS failure, concurrent busy timeout, size refusal, online backup and restored counts.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_ml_store tests.test_config_env -v`

Expected: `ml_store` import fails and ML DB configuration fields are absent.

- [ ] **Step 3: Implement schema and bounded APIs**

Use WAL, foreign keys, a 5-second busy timeout and explicit transactions. Read the live mutable WAL database through normal read-only WAL-aware connections; never use `immutable=1`. Apply `ML_DB_MAX_BYTES` to logical main-database capacity plus reserved WAL data growth, while monitoring but excluding SQLite's fixed SHM coordination file from the data quota. Full schema reservation is required only for first creation or migration; repeated initialization of an already valid schema is an idempotent validation. The core schema must include:

```sql
CREATE TABLE ml_candidate_samples(
  sample_id TEXT PRIMARY KEY, source TEXT NOT NULL, dataset_id TEXT NOT NULL,
  trade_date TEXT NOT NULL, decision_at TEXT NOT NULL, code TEXT NOT NULL,
  strategy_version TEXT NOT NULL, parameter_version TEXT NOT NULL,
  feature_schema_version TEXT NOT NULL, features_json TEXT NOT NULL,
  selected INTEGER NOT NULL, rejection_stage TEXT NOT NULL,
  rejection_code TEXT NOT NULL, final_action TEXT NOT NULL,
  universe_hash TEXT NOT NULL, market_data_version TEXT NOT NULL,
  code_hash TEXT NOT NULL, generator_hash TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_ml_candidates_date_code ON ml_candidate_samples(trade_date, code, decision_at);
CREATE TABLE ml_labels(
  sample_id TEXT PRIMARY KEY REFERENCES ml_candidate_samples(sample_id),
  label_version TEXT NOT NULL, label_source TEXT NOT NULL, cost_version TEXT NOT NULL,
  fill_label INTEGER, fill_delay_sec REAL, fill_price REAL,
  ret_3d_net REAL, ret_5d_net REAL, ret_10d_net REAL,
  mfe_10d REAL, mae_10d REAL, hit_stop INTEGER, hit_take INTEGER,
  actual_net_pnl REAL, market_data_sha256 TEXT NOT NULL, matured_at TEXT NOT NULL
);
CREATE TABLE ml_predictions(
  sample_id TEXT NOT NULL REFERENCES ml_candidate_samples(sample_id),
  model_id TEXT NOT NULL, expected_ret_3d REAL, expected_ret_5d REAL,
  expected_ret_10d REAL, downside_risk REAL, fill_probability REAL,
  ml_score REAL, ml_filter INTEGER, position_multiplier REAL, confidence REAL,
  created_at TEXT NOT NULL, PRIMARY KEY(sample_id, model_id)
);
CREATE TABLE ml_models(
  model_id TEXT PRIMARY KEY, parent_model_id TEXT, status TEXT NOT NULL,
  artifact_path TEXT NOT NULL, artifact_sha256 TEXT NOT NULL UNIQUE,
  manifest_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE ml_model_events(
  event_id TEXT PRIMARY KEY, model_id TEXT NOT NULL REFERENCES ml_models(model_id),
  action TEXT NOT NULL, old_level INTEGER NOT NULL, new_level INTEGER NOT NULL,
  artifact_sha256 TEXT NOT NULL, reason TEXT NOT NULL, operator TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE ml_runtime_state(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), active_model_id TEXT,
  permission_level INTEGER NOT NULL CHECK(permission_level BETWEEN 0 AND 3),
  updated_at TEXT NOT NULL
);
```

This is the pre-deployment schema-v1 contract, not a deployed migration: ML v1 has not yet been deployed or observed, so the first real database must be created with the five auditable `final_action`/provenance columns above. `record_candidates` must write and conflict-check them, and online backup/restore must preserve them.

Add `ML_DB_FILE`, `ML_MODEL_DIR`, `ML_DB_MAX_BYTES`, `ML_TRAINED_SHADOW_ENABLE=False`, `ML_PERMISSION_LEVEL_MAX=0` and `ML_INFERENCE_TIMEOUT_SEC=1.0` to `config.py`. Defaults must not activate a model.

- [ ] **Step 4: Run focused tests**

Run: `python -m unittest tests.test_ml_store tests.test_config_env -v`

Expected: schema, backup, capacity and CAS tests pass.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: add independent ml ledger`.

---

### Task 3: Capture Every Live Five-Minute Candidate

**Files:**
- Modify: `ml_dataset.py`
- Modify: `joinquant_exporter.py`
- Modify: `tests/test_ml_dataset.py`
- Modify: `tests/test_joinquant_exporter.py`

**Interfaces:**
- Produces `build_candidate_samples(rows, decisions, context) -> list[CandidateSample]`.
- Produces `record_candidate_batch(rows, decisions, context, store) -> int`.
- Preserves `append_signal_samples` for migration compatibility.

- [ ] **Step 1: Write failing full-cohort tests**

```python
def test_export_records_selected_and_rejected_candidates(self):
    rows = pd.DataFrame([
        self.row("600000", final_score=90),
        self.row("600001", final_score=70),
        self.row("600002", final_score=95, execution_allowed=False),
    ])
    store = MlStore(self.root / "ml.db")
    export_signals(rows, run_id="r1", trade_date="2026-07-15",
                   output_path=self.root / "signals.json", ml_store=store)
    saved = store.candidates_for_window("2026-07-15", "2026-07-15")
    self.assertEqual(len(saved), 3)
    self.assertEqual({row.rejection_code for row in saved}, {"", "buy_low_score", "buy_risk_disallowed"})
    self.assertEqual(sum(row.selected for row in saved), 1)
```

Add a repeated-export idempotency test, a changed-row conflict test, and an ML write failure test proving exported JSON signals are byte-equivalent to the no-ML path.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_ml_dataset tests.test_joinquant_exporter -v`

Expected: `ml_store` argument and full-cohort records are absent.

- [ ] **Step 3: Implement one candidate decision stream**

In `export_signals`, append one decision record for every ordered row immediately after `_buy_reject_reason` is calculated:

```python
candidate_decisions.append({
    "code": clean_code(row.get("code")),
    "selected": not bool(buy_reject_reason),
    "rejection_stage": rejection_stage(buy_reject_reason),
    "rejection_code": buy_reject_reason,
})
```

`rejection_stage` must be a stable pure mapping: empty reason to `selected`, score threshold to `score`, existing tradability reasons to `tradability`, existing portfolio/capacity/risk reasons to `risk`, and publication/execution reasons to `execution`; unknown non-empty reasons are rejected as data-contract errors rather than silently regrouped. Only intraday five-minute batches are training-eligible; other modes may be retained with their cohort mode for audit but are excluded by the training allowlist.

`selected` records the rule decision before trading-ledger controls. After the ledger transaction and its publication filters, set an independent stable `final_action`: `buy_published`, `sell_published`, `rule_rejected`, `buy_blocked_kill_switch`, `buy_blocked_disabled`, `buy_blocked_ledger_error`, `sell_blocked_kill_switch`, `sell_blocked_disabled`, or `sell_rejected_no_holding` as the real path requires. Sell rows and any batch affected by ledger/control blocking are audit-only (`training_eligible=false`); ordinary rejected buy candidates remain eligible in a valid intraday five-minute batch. Never persist ledger exception text in a sample.

After the trading-ledger transaction and before JSON publication, call `record_candidate_batch` inside its own `try/except (MlDataConflict, sqlite3.Error, OSError, ValueError)`. The context must use the payload `generated_at` as `decision_at`, include strategy/parameter/feature versions, and mark live-derived feature times as that observed runtime timestamp. Keep the old JSONL call for published signals until the migration observation gate passes.

The live `parameter_version` suffix must hash a non-secret snapshot containing `min_score`, `enforce_execution_contract`, all `_buy_reject_reason` configuration limits and its fixed thresholds; persist the same snapshot as a timed batch feature. It must not contain account identifiers, tokens, URLs, webhooks or environment content. The cached implementation hash must list `a_share_strategy.py`, `candidate_core.py`, `joinquant_exporter.py`, `ml_dataset.py`, `trade_safety.py`, `exit_policy.py`, `execution_contract.py` and `config.py`; a currently absent listed file contributes an explicit missing-file sentinel so its later appearance changes the hash. A repeated `run_id` is a replay and reuses the immutable first ledger `started_at` as the ML decision time.

- [ ] **Step 4: Run focused and export-contract tests**

Run: `python -m unittest tests.test_ml_dataset tests.test_joinquant_exporter tests.test_execution_contract -v`

Expected: every candidate is stored once and exported trading signals remain unchanged.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: capture complete candidate cohorts`.

---

### Task 4: Strict Five-Minute Historical Candidate Imports

**Files:**
- Modify: `historical_data.py`
- Modify: `historical_strategy.py`
- Modify: `historical_backtest.py`
- Modify: `tests/test_historical_data.py`
- Modify: `tests/test_historical_strategy.py`
- Modify: `tests/test_historical_backtest_cli.py`

**Interfaces:**
- Raises historical schema version to 2.
- Adds import kinds `decision_candidates` and `candidate_prices`.
- Produces `HistoricalStore.decision_times`, `candidate_cohort`, `next_candidate_price`.
- Produces `generate_candidates_at(store, dataset_id, decision_at, parameter_version) -> list[Candidate]`.

- [ ] **Step 1: Write failing migration and exact-time tests**

```python
def test_strict_candidate_import_rejects_feature_after_decision(self):
    features = {"price": {"value": 10, "available_at": "2025-01-02T10:01:00+08:00"}}
    path = self.write_candidate_csv(decision_at="2025-01-02T10:00:00+08:00", features=features)
    with self.assertRaises(HistoricalDataValidationError) as raised:
        store.import_csv("d1", "decision_candidates", path, "joinquant", "raw")
    self.assertIn("FEATURE_FROM_FUTURE", str(raised.exception))

def test_implementation_hash_covers_shared_core_and_exit_policy(self):
    paths = _implementation_paths()
    self.assertIn(Path("candidate_core.py"), {path.name for path in paths})
    self.assertIn(Path("exit_policy.py"), {path.name for path in paths})
```

Also test migration from schema 1, exact replay idempotency, conflicting JSON rollback, manifest/generator/universe hashes, chronological cohorts, next five-minute price, 3 GB refusal and daily API compatibility.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_historical_data tests.test_historical_strategy tests.test_historical_backtest_cli -v`

Expected: schema remains 1 and new import kinds are unknown.

- [ ] **Step 3: Implement schema v2 and exact-time reads**

Add:

```sql
CREATE TABLE decision_candidates(
  dataset_id TEXT NOT NULL, decision_at TEXT NOT NULL, trade_date TEXT NOT NULL,
  code TEXT NOT NULL, selected INTEGER NOT NULL, rejection_stage TEXT NOT NULL,
  rejection_code TEXT NOT NULL, strategy_version TEXT NOT NULL,
  parameter_version TEXT NOT NULL, feature_schema_version TEXT NOT NULL,
  generator_hash TEXT NOT NULL, universe_hash TEXT NOT NULL,
  features_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  PRIMARY KEY(dataset_id, decision_at, code)
);
CREATE TABLE candidate_prices(
  dataset_id TEXT NOT NULL, bar_at TEXT NOT NULL, code TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL NOT NULL, amount REAL NOT NULL, suspended INTEGER NOT NULL,
  limit_up REAL NOT NULL, limit_down REAL NOT NULL,
  PRIMARY KEY(dataset_id, bar_at, code)
);
```

Canonical candidate CSV columns are `decision_at,trade_date,code,selected,rejection_stage,rejection_code,strategy_version,parameter_version,feature_schema_version,generator_hash,universe_hash,features_json`. Validate every nested feature timestamp against exact `decision_at`. The historical path reads imported cohorts, never current caches or network calls. Extend `_implementation_hash` to include `candidate_core.py`, `exit_policy.py`, `execution_contract.py`, `historical_data.py`, `historical_strategy.py` and `historical_backtest.py`.

- [ ] **Step 4: Run historical tests**

Run: `python -m unittest tests.test_historical_data tests.test_historical_strategy tests.test_historical_backtest tests.test_historical_backtest_cli -v`

Expected: schema v2 tests and all legacy daily backtest tests pass.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: import strict five minute cohorts`.

---

### Task 5: Cost-Aware D+3/D+5/D+10, Risk and Fill Labels

**Files:**
- Create: `ml_labels.py`
- Create: `tests/test_ml_labels.py`
- Modify: `ml_dataset.py`
- Modify: `strategy_compare_report.py`
- Modify: `tests/test_strategy_compare_report.py`

**Interfaces:**
- Produces `LabelCostModel(commission_rate=0.0003, min_commission=5, stamp_tax_rate=0.001, slippage_bps=10)`.
- Produces `label_historical_candidate(store, sample, costs) -> LabelRecord`.
- Produces `update_mature_labels(ml_store, history_store, trading_store, as_of) -> LabelUpdateResult`.

- [ ] **Step 1: Write failing hand-computable label tests**

```python
def test_next_bar_fill_and_net_returns_are_cost_aware(self):
    label = label_historical_candidate(self.history, self.sample, LabelCostModel())
    self.assertEqual(label.fill_label, 1)
    self.assertEqual(label.fill_price, 10.01)
    self.assertAlmostEqual(label.ret_5d_net, self.hand_calculated_ret5, places=6)
    self.assertEqual(label.label_source, "strict_counterfactual_v1")

def test_limit_up_sets_no_fill_and_null_return(self):
    label = label_historical_candidate(self.limit_up_history, self.sample, LabelCostModel())
    self.assertEqual(label.fill_label, 0)
    self.assertIsNone(label.ret_5d_net)
```

Cover suspension, missing next bar, D+10 maturity, MFE/MAE, stop/take hits, minimum commission, actual JoinQuant fill source, idempotent update and “D+5 present but D+10 missing” continuation.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_ml_labels tests.test_ml_dataset tests.test_strategy_compare_report -v`

Expected: `ml_labels` is absent and D+10 remains unfilled.

- [ ] **Step 3: Implement pure label calculations and bounded update**

```python
def net_return(entry: float, exit_: float, qty: int, costs: LabelCostModel) -> float:
    buy = entry * qty
    sell = exit_ * qty
    buy_fee = max(buy * costs.commission_rate, costs.min_commission)
    sell_fee = max(sell * costs.commission_rate, costs.min_commission)
    sell_fee += sell * costs.stamp_tax_rate
    return (sell - sell_fee - buy - buy_fee) / (buy + buy_fee)
```

Use the next candidate bar after `decision_at` as the counterfactual fill only when tradable and below the buy limit. Count D+3/D+5/D+10 by A-share trading dates after fill. Use actual ledger fills only for `label_source="joinquant_actual_v1"`; never overwrite strict counterfactual labels with a different semantic. Replace the current `ret_5d` early exit with per-label maturity checks.

- [ ] **Step 4: Run label and report tests**

Run: `python -m unittest tests.test_ml_labels tests.test_ml_dataset tests.test_strategy_compare_report -v`

Expected: costs and all maturity boundaries are reproducible.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: add mature ml outcome labels`.

---

### Task 6: Leakage-Safe Training Dataset and Temporal Splits

**Files:**
- Create: `ml_training_data.py`
- Create: `tests/test_ml_training_data.py`

**Interfaces:**
- Produces `TrainingFrame(features, labels, weights, metadata)`.
- Produces `build_training_frame(history_store, ml_store, start, end) -> TrainingFrame`.
- Produces `build_ml_splits(trade_dates, holdout_days=40, n_splits=3, gap_days=10) -> MlSplits`.
- Produces `validate_training_data(frame) -> DataQualityResult`.

- [ ] **Step 1: Write failing split, weight and leakage tests**

```python
def test_three_walk_forward_splits_have_ten_day_gap_and_sealed_holdout(self):
    dates = make_trade_dates(250)
    splits = build_ml_splits(dates, holdout_days=40, n_splits=3, gap_days=10)
    self.assertEqual(len(splits.walk_forward), 3)
    self.assertEqual(len(splits.holdout_dates), 40)
    for fold in splits.walk_forward:
        self.assertEqual(trading_day_distance(fold.train_end, fold.validation_start), 11)
        self.assertTrue(set(fold.validation_dates).isdisjoint(splits.holdout_dates))

def test_stock_day_weights_sum_to_one(self):
    frame = build_fixture_with_repeated_intraday_stock()
    weighted = assign_stock_day_weights(frame)
    self.assertAlmostEqual(weighted.groupby(["trade_date", "code"])["weight"].sum().iloc[0], 1.0)
```

Also reject shadow-score inputs, stock code/name inputs, missing label source, future timestamps, duplicate conflicts, mixed strategy/parameter/feature versions and insufficient one-class fill labels.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_ml_training_data -v`

Expected: module import fails.

- [ ] **Step 3: Implement the allowlist and date-only split builder**

```python
FORBIDDEN_MODEL_FEATURES = {
    "code", "name", "enhanced_score", "shadow_adjust_score",
    "shadow_rank", "shadow_rank_change", "shadow_reason",
}

def assign_stock_day_weights(frame: pd.DataFrame) -> pd.Series:
    counts = frame.groupby(["trade_date", "code"])["sample_id"].transform("count")
    return 1.0 / counts.astype(float)
```

Reserve the final 40 trading dates before building the three expanding validation folds. Apply the 10-trading-day gap after each training range. Return explicit date lists and a split hash. Do not fit encoders, imputers or thresholds in this module; later tasks receive only fold-specific raw frames.

- [ ] **Step 4: Run training-data tests**

Run: `python -m unittest tests.test_ml_training_data -v`

Expected: all split, leakage and weighting tests pass.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: build leakage safe ml dataset`.

---

### Task 7: Baselines, Multi-Head Models and Immutable Challenger Bundle

**Files:**
- Modify: `requirements.txt`
- Create: `ml_train.py`
- Create: `tests/test_ml_train.py`
- Modify: `ml_store.py`
- Modify: `tests/test_ml_store.py`

**Interfaces:**
- Produces `train_challenger(training_frame, splits, output_dir) -> ModelManifest`.
- Produces `evaluate_bundle(bundle, frame, splits) -> ModelEvaluation`.
- Writes `cache/ml/models/<model_id>/bundle.joblib` and `manifest.json` atomically.

- [ ] **Step 1: Verify dependency compatibility before editing requirements**

Run: `python -c "import sys; print(sys.version); assert sys.version_info >= (3, 11)"`

Expected: exit 0. If it fails, stop and amend the approved spec with a compatible pinned scikit-learn version; do not select a version silently.

- [ ] **Step 2: Add the pinned dependency and write failing deterministic model tests**

Add exactly `scikit-learn==1.9.0` to `requirements.txt`.

```python
def test_training_is_deterministic_and_writes_hash_bound_bundle(self):
    first = train_challenger(self.frame, self.splits, self.output)
    second = train_challenger(self.frame, self.splits, self.output)
    self.assertEqual(first.model_id, second.model_id)
    self.assertEqual(first.artifact_sha256, second.artifact_sha256)
    self.assertEqual(first.permission_level, 0)

def test_holdout_is_evaluated_only_after_configuration_is_frozen(self):
    result = train_challenger(self.frame, self.splits, self.output)
    self.assertEqual(result.search_inputs_hash, hash_without_holdout(self.frame, self.splits))
    self.assertTrue(result.holdout_metrics["evaluated_after_freeze"])
```

Also test three fixed configurations maximum, train-fold preprocessing, unseen categories, linear baseline comparison, positive-rank gates, top-20% return, risk monotonicity, fill calibration, no-worse drawdown, damaged output rollback, no model registration on failure, and deterministic reruns reusing the already-published matching model rather than rewriting its artifact directory.

- [ ] **Step 3: Run tests and verify RED**

Run: `python -m unittest tests.test_ml_train tests.test_ml_store -v`

Expected: `ml_train` import fails.

- [ ] **Step 4: Implement fold-local pipelines and bundle publication**

Build numeric/categorical preprocessing inside each scikit-learn `Pipeline`. Train:

```python
heads = {
    "ret_3d": HistGradientBoostingRegressor(random_state=7),
    "ret_5d": HistGradientBoostingRegressor(random_state=7),
    "ret_10d": HistGradientBoostingRegressor(random_state=7),
    "downside": HistGradientBoostingRegressor(loss="quantile", quantile=0.8, random_state=7),
    "fill": HistGradientBoostingClassifier(random_state=7),
}
baselines = {
    "ret_5d": Ridge(alpha=1.0),
    "fill": LogisticRegression(max_iter=1000, random_state=7),
}
```

Use sample weights. Freeze the winning configuration before evaluating the sealed holdout. Persist fold dispersion, training feature distributions and missing-value coverage needed for runtime confidence/drift checks. The canonical manifest must include Python, NumPy, pandas, scikit-learn and joblib versions. Derive `model_id` from dataset/split/code/config hashes. Write to a temporary directory, compute SHA-256, write canonical manifest, then atomically rename the directory and register it as `challenger`; if that exact model already exists, verify and reuse it without rewriting. Never change runtime state.

- [ ] **Step 5: Run model tests**

Run: `python -m unittest tests.test_ml_train tests.test_ml_training_data tests.test_ml_store -v`

Expected: deterministic artifacts and all validation gates pass on fixtures.

- [ ] **Step 6: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: train versioned shadow challengers`.

---

### Task 8: Manual Approval, Permission Levels and Rollback

**Files:**
- Create: `ml_admin.py`
- Create: `tests/test_ml_admin.py`
- Modify: `ml_store.py`
- Modify: `tests/test_ml_store.py`

**Interfaces:**
- CLI commands `approve`, `activate`, `downgrade`, `rollback`, `status`.
- Produces `approve_model(model_id, sha256, level, reason, operator)`.
- Produces `activate_model(model_id, sha256, expected_model, expected_level)` using CAS.

- [ ] **Step 1: Write failing authorization and state-machine tests**

```python
def test_approve_requires_exact_hash_reason_and_observation_gate(self):
    with self.assertRaises(ModelGateError):
        approve_model(self.store, "m1", "wrong", 1, "证据充分", "user")
    with self.assertRaises(ModelGateError):
        approve_model(self.store, "m1", self.sha, 1, "", "user")

def test_cas_prevents_stale_activation_and_rollback_is_audited(self):
    approve_model(self.store, "m1", self.sha, 0, "进入影子", "user")
    activate_model(self.store, "m1", self.sha, None, 0)
    with self.assertRaises(ModelStateConflict):
        activate_model(self.store, "m2", self.sha2, None, 0)
    rollback_model(self.store, to_model=None, expected_model="m1", reason="模型文件异常")
    self.assertIsNone(self.store.runtime_state().active_model_id)
```

Cover L1 20 days, L2 40 days/D+10, L3 60 days/30 closed cycles, changed artifact invalidating approval, one active model, immutable events, automatic downgrade never automatic upgrade, and no commands that edit environment files or restart services.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_ml_admin tests.test_ml_store -v`

Expected: admin module and gates are absent.

- [ ] **Step 3: Implement explicit hash-bound commands**

```text
python ml_admin.py approve --model MODEL --sha256 HASH --level 0 --reason TEXT
python ml_admin.py activate --model MODEL --sha256 HASH --expect-model none --expect-level 0
python ml_admin.py downgrade --to-level 0 --expect-model MODEL --reason TEXT
python ml_admin.py rollback --to-model none --expect-model MODEL --reason TEXT
python ml_admin.py status
```

`approve` writes an immutable event but does not activate. `activate` requires an existing approval with the same model hash and target level. `downgrade` may only reduce permission. No code path may raise permission without an explicit approval event. Sanitize operator to a non-sensitive local identifier.

- [ ] **Step 4: Run admin tests**

Run: `python -m unittest tests.test_ml_admin tests.test_ml_store -v`

Expected: all state and CAS tests pass.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: govern shadow model permissions`.

---

### Task 9: Safe Runtime Inference and L0 Integration

**Files:**
- Create: `ml_runtime.py`
- Create: `tests/test_ml_runtime.py`
- Modify: `a_share_strategy.py`
- Modify: `tests/test_a_share_strategy.py`
- Modify: `joinquant_exporter.py`
- Modify: `tests/test_joinquant_exporter.py`

**Interfaces:**
- Produces `load_active_bundle(store, model_dir) -> LoadedBundle | None`.
- Produces `predict_candidate_frame(frame, bundle) -> pandas.DataFrame`.
- Produces `apply_model_policy(frame, permission_level) -> pandas.DataFrame`.

- [ ] **Step 1: Write failing fallback and zero-impact tests**

```python
def test_l0_adds_predictions_without_changing_rule_columns_or_order(self):
    before = self.frame.copy(deep=True)
    after = apply_trained_shadow(self.frame, self.bundle, permission_level=0)
    pd.testing.assert_frame_equal(after[before.columns], before)
    self.assertIn("ml_score", after)
    self.assertEqual(list(after.code), list(before.code))

def test_bad_hash_or_timeout_returns_original_frame(self):
    for bundle in (self.bad_hash_bundle, self.timeout_bundle):
        after = apply_trained_shadow(self.frame, bundle, permission_level=0)
        pd.testing.assert_frame_equal(after[self.frame.columns], self.frame)
        self.assertEqual(after.attrs["ml_status"], "fallback_rules")
```

Also test finite outputs, feature mismatch, path traversal rejection, `ml_score` formula, `ml_filter`, multiplier clipping, confidence degradation from missing coverage/model disagreement/feature drift, no suggestion when confidence is below threshold, L1 ordering only among rule-eligible rows, L2 only removing buys, L3 remaining within its approved narrow multiplier range, automatic downgrade without automatic upgrade, and all existing hard-risk rejects remaining rejected.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_ml_runtime tests.test_a_share_strategy tests.test_joinquant_exporter -v`

Expected: runtime module is absent.

- [ ] **Step 3: Implement verified loading and deterministic policy**

```python
def deterministic_outputs(expected_ret_5d, downside, fill_probability):
    return_pct = pd.Series(expected_ret_5d).rank(pct=True) * 100
    risk_pct = (1 - pd.Series(downside).rank(pct=True)) * 100
    ml_score = 0.60 * return_pct + 0.30 * risk_pct + 0.10 * np.asarray(fill_probability) * 100
    return ml_score.clip(0, 100)
```

Compute `confidence` deterministically from feature coverage, cross-head/fold disagreement and drift against the training distributions stored in the manifest; clip it to `[0, 1]`. Below the configured confidence threshold, retain raw predictions for audit but set filter/position suggestions to neutral and prohibit any L1-L3 effect. Track consecutive inference/hash/schema/drift failures in ML runtime state; the health evaluator may CAS-downgrade to L0 or disable the active model, but no automated path may upgrade permission, edit trading controls or trigger `kill_switch`.

Verify resolved artifact paths remain under `ML_MODEL_DIR`, the file SHA matches the active DB row, the manifest feature schema matches, and dependency versions match. Add inference after `apply_shadow_scores` and before any optional ML policy. When `ML_TRAINED_SHADOW_ENABLE` is false or maximum permission is 0, only L0 prediction columns and DB prediction rows are allowed; existing sort keys and exported signals remain untouched.

- [ ] **Step 4: Run runtime and full execution-contract tests**

Run: `python -m unittest tests.test_ml_runtime tests.test_a_share_strategy tests.test_joinquant_exporter tests.test_execution_contract tests.test_exit_policy -v`

Expected: L0 is byte/column equivalent for existing trading inputs and failures fall back safely.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: run trained model in shadow mode`.

---

### Task 10: Three-Way Reports, Weekly Challenger and ML Maintenance

**Files:**
- Create: `ml_maintenance.py`
- Create: `tests/test_ml_maintenance.py`
- Modify: `ml_train.py`
- Modify: `strategy_compare_report.py`
- Modify: `tests/test_strategy_compare_report.py`
- Modify: `run_ubuntu.sh`
- Modify: `tests/test_joinquant_linux_script.py`

**Interfaces:**
- CLI routes `ml-labels`, `ml-train`, `ml-model-status`, `ml-backup`, `ml-retention-status`, `ml-retention-dry-run`, `ml-retention-apply`.
- Timers run label maturity after close and challenger training on Friday only; no admin command is timer-accessible.

- [ ] **Step 1: Write failing report, timer and retention tests**

```python
def test_report_compares_three_scores_without_claiming_deployment(self):
    report = build_three_way_report(self.fixture_rows)
    self.assertIn("原策略 final_score", report)
    self.assertIn("规则影子 enhanced_score", report)
    self.assertIn("训练影子 ml_score", report)
    self.assertIn("planned / not deployed", report)

def test_retention_never_deletes_approved_or_active_models(self):
    plan = build_retention_plan(self.store, now=self.now)
    self.assertNotIn(self.active_model_path, plan.delete_paths)
    self.assertNotIn(self.approved_model_path, plan.delete_paths)
```

Static Linux tests must assert Friday training, daily label maturity, no training during trading hours, no `ml_admin.py` in any service, no L1–L3 activation timer, and no changes to token/webhook installation lines.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_ml_maintenance tests.test_strategy_compare_report tests.test_joinquant_linux_script -v`

Expected: maintenance routes/timers and three-way report are absent.

- [ ] **Step 3: Implement bounded reports and maintenance**

Use bounded date queries from `ml.db`; do not scan all history every five minutes. Reports must include confidence coverage and runtime health/downgrade events without claiming they are trading outcomes. `ml_maintenance.py backup` must use SQLite online backup, SHA-256, `PRAGMA integrity_check`, schema/count manifest and 7 daily/4 weekly/12 monthly retention. `retention-dry-run` lists exact paths/rows and bytes; `retention-apply` requires an explicit verified backup manifest and is not installed as an automatic timer.

Install services with these schedules:

```text
stock-ml-labels.timer: Mon..Fri 16:10
stock-ml-train.timer: Fri 18:00
stock-ml-backup.timer: daily 19:00
```

The training service runs only `ml_train.py train`; it cannot invoke `ml_admin.py`. The report must show original/rule-shadow/trained-shadow D+3/D+5/D+10, MAE, fill rate, label coverage, model ID and permission level.

- [ ] **Step 4: Run ops/report tests**

Run: `python -m unittest tests.test_ml_maintenance tests.test_strategy_compare_report tests.test_joinquant_linux_script -v`

Expected: all retention, safety and timer tests pass.

- [ ] **Step 5: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `feat: operate shadow model evidence loop`.

---

### Task 11: Documentation Truth, Full Verification and Security Review

**Files:**
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-15-trained-shadow-model-design.md`
- Modify: `docs/superpowers/plans/2026-07-15-trained-shadow-model.md`

**Interfaces:**
- Produces one consistent post-code status: `implemented / not deployed / not observed / not validated` unless independent deployment evidence exists.

- [ ] **Step 1: Update status without overstating external facts**

Document exact files, schema versions, commands, default-disabled L0, dependency version, storage limits and test evidence. Keep real one-year strict import, trained model performance, server deployment and valid trading-day observation as unproven until evidence exists.

- [ ] **Step 2: Run compilation and focused ML tests**

Run:

```powershell
python -m py_compile candidate_core.py ml_contracts.py ml_store.py ml_labels.py ml_training_data.py ml_train.py ml_admin.py ml_runtime.py ml_maintenance.py
python -m unittest tests.test_candidate_core tests.test_ml_contracts tests.test_ml_store tests.test_ml_dataset tests.test_ml_labels tests.test_ml_training_data tests.test_ml_train tests.test_ml_admin tests.test_ml_runtime tests.test_ml_maintenance tests.test_strategy_compare_report -v
```

Expected: compilation and every focused test pass.

- [ ] **Step 3: Run historical and trading regression tests**

Run:

```powershell
python -m unittest tests.test_historical_data tests.test_historical_strategy tests.test_historical_backtest tests.test_historical_backtest_cli tests.test_joinquant_exporter tests.test_execution_contract tests.test_exit_policy tests.test_reconciliation -v
```

Expected: all pass; no existing trading contract changes under ML disabled/L0.

- [ ] **Step 4: Run the full platform-independent suite**

Run:

```powershell
$modules = Get-ChildItem tests\test_*.py | Where-Object { $_.BaseName -ne 'test_joinquant_linux_script' } | ForEach-Object { 'tests.' + $_.BaseName }
python -m unittest $modules -v
python -m unittest tests.test_joinquant_linux_script -v
git diff --check
git status --short --branch
```

Expected: all Windows-compatible tests and Linux static tests pass; Bash execution tests remain reserved for Linux if Bash is unavailable.

- [ ] **Step 5: Perform manual security and permission review**

Check that model paths cannot escape `cache/ml/models`, model loading verifies hashes, no pickle/joblib file can be selected without a registered hash, automated services cannot call admin commands, logs/reports omit environment values, ML failures cannot call trading control, and L0 preserves exported signal JSON.

- [ ] **Step 6: Review checkpoint**

Run: `git diff --check`

Expected: clean. Suggested authorized commit: `docs: record trained shadow model implementation`.

---

### Task 12: Separately Authorized Server Deployment and L0 Observation

**Files:**
- No additional source files unless deployment evidence exposes a defect.
- Runtime-only paths: `/opt/stock-analysis/cache/ml/`, `/opt/stock-analysis-backups/`, server virtual environment and systemd units generated by `run_ubuntu.sh`.

**Interfaces:**
- Produces deployment evidence only; it does not validate model performance or enable L1–L3.

- [ ] **Step 1: Obtain explicit deployment authorization**

Authorization must separately cover dependency installation, Git pull/bundle deployment, ML DB creation, timer installation, environment changes and service restart. It must prohibit displaying or modifying existing tokens except the explicit ML enable/level keys.

- [ ] **Step 2: Record pre-deployment evidence**

Run on the server after authorization:

```bash
cd /opt/stock-analysis
git status --short --branch
git rev-parse HEAD
sha256sum stock-analysis.env > /tmp/stock-env-before.sha256
bash run_ubuntu.sh backup
bash run_ubuntu.sh ledger-check
```

Expected: clean worktree, successful trading backup and healthy ledger.

- [ ] **Step 3: Deploy code and dependency without changing secrets**

Fast-forward to the authorized SHA, install the pinned requirements in the existing project virtual environment, initialize `ml.db`, run focused tests, ML integrity/backup and existing ledger check. Do not print `stock-analysis.env` or secret values.

- [ ] **Step 4: Enable L0 only after explicit configuration approval**

Set only:

```text
ML_TRAINED_SHADOW_ENABLE=1
ML_PERMISSION_LEVEL_MAX=0
```

Leave every existing URL, token, webhook and trading control value unchanged. A model still requires hash-bound L0 approval and activation; code deployment alone must produce no model effect.

- [ ] **Step 5: Restart and verify**

Restart only the authorized stock services. Compare the environment before/after with a redacted verifier that reports changed key names and value hashes only, never values; the changed-key set must equal exactly `ML_TRAINED_SHADOW_ENABLE` and `ML_PERMISSION_LEVEL_MAX`. Then verify services are active, schema/integrity checks pass, the expected model hash loads, and no startup ERROR contains secrets.

- [ ] **Step 6: Observe before any higher permission**

On the first valid trading day, compare ML disabled versus L0 rule signal IDs, actions, order quantities, stops, targets and exported JSON. Require exact trading equivalence, complete candidate/prediction rows and bounded inference time. Keep status `deployed / observed` only after actual evidence; L1 remains blocked until at least 20 valid trading days and manual approval.
