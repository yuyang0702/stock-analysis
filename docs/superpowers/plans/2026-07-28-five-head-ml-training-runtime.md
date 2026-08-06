# Five-Head ML Training and Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete ML-7 Tasks 4-10 with strict five-minute history, cost-aware labels, leakage-safe walk-forward training, immutable five-head model bundles, manual governance and deterministic L0 runtime.

**Architecture:** Existing Tasks 1-3 remain the identity, candidate-capture and independent-ML-ledger foundation. Strict history stays in `cache/backtest/history.db`; labels, predictions, model registration and permissions stay in `cache/ml/ml.db`; immutable artifacts stay in `cache/ml/models/`. Training creates transparent baselines and HistGradientBoosting challengers, but runtime begins at L0 and must preserve every rule candidate, action, quantity, stop, target and signal field.

**Tech Stack:** Python 3.11+, pandas, sqlite3, scikit-learn 1.9.0, joblib, hashlib/json/pathlib, unittest and existing run scripts.

**Status (2026-08-06):** ML-7 Tasks 4–10 are `implemented locally`; ML-7 Task 11 local full verification/document truth/security review is in progress; ML-7 Task 12 server deployment and L0 observation is not authorized and has not started. Overall: `not committed / not deployed / not observed / not validated`.

No real one-year/365-day strict dataset evidence, trustworthy or approvable trained model, human approval, active model, or server L0 evidence exists. The task steps below remain as the implementation and verification procedure; their historical checkbox text is preserved and does not override this current status checkpoint.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md`, especially section 11, section 12.3, section 14.3 and Batch C.
- Preserve implemented ML-7 Tasks 1-3 contracts: stable `CandidateSample`, complete five-minute cohorts, idempotent ML writes and capture only after the trading-ledger transaction succeeds.
- Consume Batch A `FeeSchedule`; do not introduce a second default label/backtest cost model.
- Assume Batch B retired active rule-shadow scoring. Reports compare original rules with trained models, never three active scoring systems.
- Strict means every feature has `available_at <= decision_at`; date-only checks, current-cache backfill and historical-path network calls are forbidden.
- Diagnostic training thresholds are 5,000 stock-days/120 trading days for linear baselines and 15,000 stock-days/180 trading days for gradient challengers. Neither threshold permits L0.
- L0 eligibility requires at least 365 calendar days and 240 effective trading days of strict data plus every coverage, regime, leakage, performance and hash gate in the confirmed design.
- Use at least three expanding walk-forward folds, a 10-trading-day embargo for D+10 and one sealed final 40-trading-day holdout opened once per candidate model evaluation.
- First version uses Ridge, LogisticRegression and HistGradientBoosting only. No LightGBM, CatBoost, neural network, GPU, online learning or automatic parameter approval.
- A model never controls sells, stops, T+1, tradability, absolute risk, `buy_enabled` or `kill_switch`; model failure falls back to pure rules and cannot trigger trading controls.
- Training may register a challenger. It may not approve, activate, deploy or raise permission. Every upward permission change is a hash-bound human event.
- Model paths stay within `cache/ml/models`; runtime loads only locally generated, registered, approved artifacts whose file, manifest, feature, dependency and strategy hashes match.
- ML live data targets below 1 GB/year, warns above 1 GB/year and stops new ML detail above 2 GB/year without stopping rules. Historical imports reject at 3 GB.
- Git commit, push, dependency installation, model training on external data, deployment, ML enablement, activation and service restart require separate authorization at execution time.

---

## Existing Foundation To Preserve

- `candidate_core.py`: `CandidatePoolConfig`, `build_candidate_pool`, `score_candidate_frame`.
- `ml_contracts.py`: `TimedFeature`, `CandidateSample`, `LabelRecord`, `PredictionRecord`, `ModelManifest`, `candidate_sample_id`, `canonical_hash`.
- `ml_store.py`: ML schema v1, transactions, candidates, labels, predictions, model events/runtime state, backup and integrity.
- `ml_dataset.py`: `build_candidate_samples`, `record_candidate_batch` and migration-compatible JSONL reads.
- `joinquant_exporter.export_signals(..., ml_store=None, cohort_mode="audit", cohort_interval_sec=None)` captures full cohorts without changing signals.
- `historical_data.HistoricalStore` currently has schema v1 daily history only.

## File Map

- Modify `historical_data.py`, `historical_strategy.py` and `historical_backtest.py` for strict history schema v2 and exact decision-time replay.
- Create `ml_labels.py`: next-window fill, fee components, D+3/D+5/D+10 and D+10 downside labels with maturity/quality evidence.
- Create `ml_training_data.py`: feature allowlist, leakage/version gates, stock-day weights, walk-forward/embargo/holdout and readiness reports.
- Create `ml_train.py`: fold-local preprocessing, five heads, baselines/challengers, performance gates and atomic immutable bundles.
- Create `ml_admin.py`: evidence-bound approval, activation, downgrade and rollback.
- Create `ml_runtime.py`: verified loading, inference, frozen-reference scoring, PSI/confidence and level policy.
- Create `ml_maintenance.py`: labels/train/status/backup/restore/retention commands and bounded health evidence.
- Modify `ml_contracts.py`, `ml_store.py`, `ml_dataset.py`, `a_share_strategy.py`, `joinquant_exporter.py`, `strategy_compare_report.py`, `config.py`, `requirements.txt` and `run_ubuntu.sh`.
- Create focused tests for every new module and update current historical/ML/export/report/script tests.

### Task 1: Import strict five-minute cohorts and prices (ML-7 Task 4)

**Files:**

- Modify: `historical_data.py`
- Modify: `historical_strategy.py`
- Modify: `historical_backtest.py`
- Modify: `tests/test_historical_data.py`
- Modify: `tests/test_historical_strategy.py`
- Modify: `tests/test_historical_backtest.py`
- Modify: `tests/test_historical_backtest_cli.py`

**Interfaces:**

- Produces history schema v2 tables `decision_candidates` and `candidate_prices`.
- Produces `HistoricalStore.decision_times(dataset_id, start, end) -> list[str]`.
- Produces `candidate_cohort(dataset_id, decision_at) -> list[CandidateSample]` and `candidate_price_path(dataset_id, code, start_at, end_at) -> list[dict]`.
- Produces `generate_candidates_at(store, dataset_id, decision_at, strategy_config) -> list[CandidateSample]` without network access.

- [ ] **Step 1: Write failing strict-time/import tests**

```python
def test_import_rejects_feature_available_after_decision(self):
    row = strict_candidate(decision_at="2025-01-02T10:00:00+08:00", price_available_at="2025-01-02T10:00:01+08:00")
    with self.assertRaisesRegex(HistoricalDataError, "FEATURE_FROM_FUTURE"):
        store.import_candidate_cohorts([row], manifest=MANIFEST)

def test_historical_candidate_path_never_calls_live_provider(self):
    with patch("historical_strategy.fetch_live_quotes", side_effect=AssertionError("network")):
        rows = generate_candidates_at(store, "dataset-1", DECISION_AT, CONFIG)
    self.assertTrue(rows)
```

Cover manifest/table hashes, exact timezone timestamps, duplicate same-content idempotency, conflicting replay, missing cohort members, market/strategy/parameter/feature versions, price OHLCV availability, suspended intervals, current-cache rejection, 3 GB preflight refusal and implementation hash including the new tables.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_historical_data tests.test_historical_strategy tests.test_historical_backtest tests.test_historical_backtest_cli -v`

Expected: history schema remains v1 and exact `decision_at` APIs are absent.

- [ ] **Step 3: Implement additive history schema v2**

```sql
CREATE TABLE decision_candidates(
  dataset_id TEXT NOT NULL, sample_id TEXT NOT NULL,
  trade_date TEXT NOT NULL, decision_at TEXT NOT NULL, code TEXT NOT NULL,
  features_json TEXT NOT NULL, feature_times_json TEXT NOT NULL,
  selected INTEGER NOT NULL, rejection_stage TEXT NOT NULL,
  rejection_code TEXT NOT NULL, strategy_version TEXT NOT NULL,
  parameter_version TEXT NOT NULL, feature_schema_version TEXT NOT NULL,
  market_regime TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  PRIMARY KEY(dataset_id, sample_id)
);
CREATE INDEX idx_decision_cohort ON decision_candidates(dataset_id, decision_at, code);
CREATE TABLE candidate_prices(
  dataset_id TEXT NOT NULL, code TEXT NOT NULL, bar_at TEXT NOT NULL,
  available_at TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL, paused INTEGER NOT NULL,
  limit_up REAL, limit_down REAL, adjustment_version TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  PRIMARY KEY(dataset_id, code, bar_at)
);
```

Validate each feature timestamp individually before insertion, keep daily history compatibility, include new rows/manifests in dataset hash and refuse any history replay that tries to call AkShare/current cache. Import through a temporary transaction with predicted size plus WAL reserve checked against the 3 GB hard cap.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_historical_data tests.test_historical_strategy tests.test_historical_backtest tests.test_historical_backtest_cli -v`

Expected: strict imports/replays are deterministic, future data fails closed and old daily tests remain green.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add historical_data.py historical_strategy.py historical_backtest.py tests/test_historical_data.py tests/test_historical_strategy.py tests/test_historical_backtest.py tests/test_historical_backtest_cli.py
git commit -m "feat: import strict five minute ml cohorts"
```

### Task 2: Build mature cost-aware labels (ML-7 Task 5)

**Files:**

- Create: `ml_labels.py`
- Create: `tests/test_ml_labels.py`
- Modify: `ml_contracts.py`
- Modify: `ml_store.py`
- Modify: `tests/test_ml_contracts.py`
- Modify: `tests/test_ml_store.py`
- Modify: `ml_dataset.py`
- Modify: `tests/test_ml_dataset.py`

**Interfaces:**

- Consumes Batch A `FeeSchedule` by explicit version.
- Produces `LabelPolicy`, `LabelOutcome` and `build_labels(store, ml_store, dataset_id, as_of, fee_schedule, policy) -> LabelBuildResult`.
- Migrates independent ML schema v1 to v2 with per-horizon maturity, gross/cost/net components, downside path flags and quality reasons.

- [ ] **Step 1: Write failing fill/maturity/downside tests**

```python
def test_horizons_mature_independently_and_keep_fee_components(self):
    outcome = label_sample(SAMPLE, as_of=trade_day_after(5), fee_schedule=FEES)
    self.assertIsNotNone(outcome.ret_3d_net)
    self.assertIsNotNone(outcome.ret_5d_net)
    self.assertIsNone(outcome.ret_10d_net)
    self.assertEqual(outcome.cost_version, FEES.version)
    self.assertEqual(outcome.net_cost, outcome.buy_cost + outcome.sell_cost + outcome.slippage_cost)

def test_downside_is_positive_loss_and_marks_blocked_exit(self):
    outcome = label_path(entry_ref=D("10"), lows=[D("9"), D("8")], limit_down_blocked=True)
    self.assertGreater(outcome.downside_loss, 0)
    self.assertEqual(outcome.exit_blocked, 1)
```

Cover next reasonable fill window, planned-price limit, no-fill, opening limit-up, suspension carry-forward, limit-down path, D+3/5/10 trading days, buy/sell minimum commission, stamp/other fees, gross/net decomposition, adjustment failures, no official path, idempotent partial maturity and conflicting facts.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_ml_labels tests.test_ml_contracts tests.test_ml_store tests.test_ml_dataset -v`

Expected: label builder/schema-v2 fields are absent.

- [ ] **Step 3: Implement exact label definitions**

For each strict candidate, compute fill from the next reasonable market window without assuming an impossible limit/suspension execution. Preserve all quality failures in denominator tables. For each valid path:

```python
net_mark_return_t = low_t / entry_ref - 1 - reference_buy_cost_rate - reference_sell_cost_rate(low_t)
downside_loss = max(0, -min(net_mark_return_t for t in path))
```

Carry the last official close through suspension and set `paused_path=1`; include limit-down marks and set `exit_blocked=1`. Train return/downside heads only from complete filled labels and fill head from all eligible mature candidates. Never overwrite an existing mature fact with null or a different hash.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_ml_labels tests.test_ml_contracts tests.test_ml_store tests.test_ml_dataset -v`

Expected: ML schema v2 migrates idempotently and label components/maturity/quality match frozen formulas.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add ml_labels.py ml_contracts.py ml_store.py ml_dataset.py tests/test_ml_labels.py tests/test_ml_contracts.py tests/test_ml_store.py tests/test_ml_dataset.py
git commit -m "feat: build cost aware ml labels"
```

### Task 3: Build leakage-safe training frames and splits (ML-7 Task 6)

**Files:**

- Create: `ml_training_data.py`
- Create: `tests/test_ml_training_data.py`

**Interfaces:**

- Produces `TrainingFrame`, `DataReadiness`, `WalkForwardFold` and `TrainingSplits`.
- Produces `build_training_frame(candidates, labels, feature_allowlist) -> TrainingFrame`.
- Produces `build_ml_splits(frame, holdout_days=40, folds=3, embargo_days=10) -> TrainingSplits`.
- Produces `validate_training_data(frame, splits, require_l0) -> DataReadiness`.

- [ ] **Step 1: Write failing leakage, weight and readiness tests**

```python
def test_each_stock_day_has_total_weight_one(self):
    frame = build_training_frame(repeated_rows_for_one_stock_day(5), LABELS, ALLOWLIST)
    self.assertAlmostEqual(frame.rows["sample_weight"].sum(), 1.0)

def test_holdout_and_embargo_never_overlap_training_labels(self):
    splits = build_ml_splits(FRAME, holdout_days=40, folds=3, embargo_days=10)
    for fold in splits.walk_forward:
        self.assertLess(max(fold.train_dates), min(fold.test_dates) - trading_days(10))
    self.assertTrue(set(splits.holdout_dates).isdisjoint(splits.all_development_dates))
```

Cover future feature time, code/name/shadow/future-result forbidden fields, schema/version mixing, non-finite values, reciprocal stock-day weights, three folds, sealed 40 days, diagnostic thresholds, 365/240 L0 threshold, fill 99%, horizon/downside 90%, quality-failure denominators and each regime 10 days/500 stock-days.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_ml_training_data -v`

Expected: training-data module is absent.

- [ ] **Step 3: Implement a frozen allowlist and explicit gates**

Drop no failed rows before denominator metrics. Validate feature `available_at`, schema and versions before constructing matrices. Give each row `1 / rows_for_same_code_trade_date` weight. Build folds only by ordered trading date, reserve the final 40 dates before fold construction and enforce a 10-date gap between development train/test label boundaries.

Return diagnostic and L0 readiness separately. `require_l0=False` may permit a diagnostic report at lower thresholds; it never writes an approvable status.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_ml_training_data -v`

Expected: every leakage/version/coverage failure has a stable reason and all split/weight invariants pass.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add ml_training_data.py tests/test_ml_training_data.py
git commit -m "feat: build leakage safe ml training data"
```

### Task 4: Train five heads and write immutable bundles (ML-7 Task 7)

**Files:**

- Create: `ml_train.py`
- Create: `tests/test_ml_train.py`
- Modify: `ml_contracts.py`
- Modify: `ml_store.py`
- Modify: `requirements.txt`
- Modify: `tests/test_ml_contracts.py`
- Modify: `tests/test_ml_store.py`

**Interfaces:**

- Produces `TrainingConfig`, `HeadMetrics`, `ModelEvaluation`, `ModelBundleManifest` and `train_challenger(frame, splits, config, output_dir) -> TrainingResult`.
- Registers rejected challengers as evidence but marks only a fully passing candidate `approvable_l0`.
- Writes artifact contents to a temporary directory, verifies every hash, then atomically renames to `cache/ml/models/<model_id>/`.

- [ ] **Step 1: Write failing determinism, holdout and performance-gate tests**

```python
def test_same_data_config_and_seed_produce_same_model_identity(self):
    first = train_challenger(FRAME, SPLITS, CONFIG, root / "models")
    second = train_challenger(FRAME, SPLITS, CONFIG, root / "models")
    self.assertEqual(first.model_id, second.model_id)
    self.assertEqual(first.manifest_sha256, second.manifest_sha256)

def test_failed_gate_registers_rejected_bundle_not_approvable(self):
    result = train_challenger(ANTI_SIGNAL_FRAME, SPLITS, CONFIG, root / "models")
    self.assertEqual(result.status, "rejected")
    self.assertIn("D5_OOF_RANK_GATE", result.failed_gates)
```

Cover no holdout access during fit/tuning, fold-local preprocessing, maximum three parameter configurations, return Ridge baselines, Logistic fill baseline, constant 0.8 downside baseline, HGB quantile loss, OOF references, residual q80, D5 challenger-vs-Ridge difference P90, PSI bins/categories/frequencies, dependency hashes, corrupt temporary publish rollback and existing identical bundle reuse.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_ml_train tests.test_ml_contracts tests.test_ml_store -v`

Expected: trainer is absent and scikit-learn is not pinned.

- [ ] **Step 3: Implement baselines, challengers and exact gates**

Pin `scikit-learn==1.9.0`. Train three return regressors for D+3/D+5/D+10, one `HistGradientBoostingRegressor(loss="quantile", quantile=0.8)` downside head and one calibrated fill classifier. Fit encoders/imputers within each fold only.

The manifest must contain code/feature/label/data/split/cost/dependency hashes, all parameters, OOF prediction reference arrays, residual q80 values, D5 challenger-minus-Ridge absolute-difference P90, numeric decile bins, categorical `OTHER/MISSING` sets, 0.5 PSI pseudocount and training frequencies.

Apply every confirmed performance gate: D+5 rank sign/Top20%, holdout, downside/fill monotonicity, counterfactual return/drawdown, D+3/D+10 nonnegative limits, MAE/pinball/Brier baselines, residual interval 75%-85% coverage and fill calibration error at most 0.10. Any failed gate produces a registered rejected challenger only.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_ml_train tests.test_ml_contracts tests.test_ml_store -v`

Expected: deterministic bundles, atomic publish and all pass/reject cases match exact reasons.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add ml_train.py ml_contracts.py ml_store.py requirements.txt tests/test_ml_train.py tests/test_ml_contracts.py tests/test_ml_store.py
git commit -m "feat: train immutable five head challengers"
```

### Task 5: Govern approval, permission and rollback (ML-7 Task 8)

**Files:**

- Create: `ml_admin.py`
- Create: `tests/test_ml_admin.py`
- Modify: `ml_store.py`
- Modify: `ml_contracts.py`
- Modify: `tests/test_ml_store.py`
- Modify: `tests/test_ml_contracts.py`

**Interfaces:**

- Produces `PermissionEvidence`, `PermissionGateResult` and `evaluate_permission_gate(level, model, evidence) -> PermissionGateResult`.
- Produces explicit CLI actions `approve`, `activate`, `downgrade` and `rollback`, each requiring expected current model/level for CAS.
- Binds approval to artifact hash, strategy version, evidence window and evidence hash.

- [ ] **Step 1: Write failing conjunctive-gate and CAS tests**

```python
def test_days_alone_cannot_promote_l1(self):
    result = evaluate_permission_gate(1, MODEL, evidence(valid_days=25, mature_d5=80))
    self.assertFalse(result.allowed)
    self.assertIn("MATURE_D5_200_REQUIRED", result.reasons)

def test_automatic_process_can_downgrade_but_not_promote(self):
    self.assertTrue(admin.auto_downgrade(model_id=MODEL.id, reason="HASH_MISMATCH"))
    with self.assertRaises(PermissionDenied):
        admin.auto_promote(model_id=MODEL.id, level=1)
```

Cover L0 5 days/500 predictions, L1 20/200 D+5, L2 40/300 D+10/30 filters, L3 60/30 closed cycles, availability 99%, fault 1%, PSI 0.10, behavior invariants 100%, version reset, insufficient matured labels, evidence hash conflict, separate approve/activate and rollback to registered parent.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_ml_admin tests.test_ml_store tests.test_ml_contracts -v`

Expected: admin/evidence gate is absent.

- [ ] **Step 3: Implement evidence-bound manual governance**

Use 2,000 trading-day block bootstrap samples with random seed 7 and one-sided 90% intervals. Require mean improvement of at least 0.10 percentage points and lower bound at least zero for the level-specific rule baseline; require maximum drawdown non-degradation where specified. An approval records no runtime change. Activation uses CAS and refuses an unapproved hash. Any version, coverage, performance or integrity break automatically downgrades to L0/off and invalidates the observation window; recovery requires a new human approval.

Do not expose admin actions through automated timers or HTTP services.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_ml_admin tests.test_ml_store tests.test_ml_contracts -v`

Expected: all layer gates are conjunctive, upward actions require human CLI evidence and CAS prevents stale changes.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add ml_admin.py ml_store.py ml_contracts.py tests/test_ml_admin.py tests/test_ml_store.py tests/test_ml_contracts.py
git commit -m "feat: govern ml permissions with evidence gates"
```

### Task 6: Add verified inference and L0 production wiring (ML-7 Task 9)

**Files:**

- Create: `ml_runtime.py`
- Create: `tests/test_ml_runtime.py`
- Modify: `a_share_strategy.py`
- Modify: `joinquant_exporter.py`
- Modify: `ml_store.py`
- Modify: `config.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**

- Produces `load_active_bundle(store, model_dir, expected_versions) -> LoadedBundle | None`.
- Produces `frozen_midrank_pct(x, reference)`, `compute_feature_psi(...)`, `compute_confidence(...)`, `predict_candidate_frame(...)` and `apply_model_policy(...)`.
- Persists one prediction per `(sample_id, model_id)` and deterministic `ml_score`, `ml_filter`, multiplier and reasons.

- [ ] **Step 1: Write failing frozen-score, PSI and permission tests**

```python
def test_score_is_independent_of_current_batch_members(self):
    one = score_prediction(PREDICTION, bundle=FROZEN_BUNDLE, current_batch=[PREDICTION])
    many = score_prediction(PREDICTION, bundle=FROZEN_BUNDLE, current_batch=[PREDICTION, *OTHER_ROWS])
    self.assertEqual(one.ml_score, many.ml_score)

def test_l0_is_field_for_field_trading_equivalent(self):
    disabled = export_with_model(enabled=False)
    l0 = export_with_model(enabled=True, level=0)
    self.assertEqual(l0, disabled)
    self.assertEqual(count_predictions(model_id=MODEL_ID), count_expected_candidates())
```

Cover midrank ties/out-of-range/empty references, numeric/categorical/missing PSI with 0.5 pseudocount, 20 batches/200 rows insufficiency, coverage/disagreement/drift minimum, confidence below 0.60, coverage below 95%, non-finite output, timeout, path escape, hash/schema/dependency mismatch, L1 order-only, L2 delete-only, L3 `0.8/0.9/1.0/1.1`, hard-risk recheck and no sell changes.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_ml_runtime tests.test_joinquant_exporter tests.test_config_env -v`

Expected: runtime module is absent and exporter has no trained-model observation path.

- [ ] **Step 3: Implement frozen formulas and fail-open-to-rules behavior**

```python
return_component = frozen_midrank_pct(pred_d5, manifest.d5_oof_predictions)
risk_component = 100 - frozen_midrank_pct(pred_downside, manifest.downside_oof_predictions)
fill_component = 100 * clip(calibrated_fill_probability, 0, 1)
ml_score = 0.60 * return_component + 0.30 * risk_component + 0.10 * fill_component

coverage = provided_required_features / total_required_features
disagreement = max(0, 1 - abs(challenger_d5 - ridge_d5) / max(manifest.d5_difference_p90, 1e-6))
drift = max(0, 1 - max_feature_psi / 0.25)
confidence = clip(min(coverage, disagreement, drift), 0, 1)
```

PSI uses the manifest's merged numeric deciles or categorical `OTHER/MISSING` buckets, adds 0.5 to every training/current count, normalizes and takes maximum feature PSI. Insufficient drift, low coverage/confidence, non-finite output or any verification failure keeps raw audit predictions when safe but forces `ml_filter=0` and multiplier `1.0`.

At L0, write predictions/counterfactuals only. L1 stable-sorts rule-eligible rows by `ml_score`; L2 only deletes on the frozen residual/fill bounds; L3 maps score bands to the frozen multipliers after L2. Re-run Batch A economics and every hard risk after any model reduction. ML exceptions return the original rule frame and never alter trading controls or sells.

Freeze the L2 and L3 decisions exactly:

```python
ml_filter = int(
    pred_d5 + d5_residual_q80 <= 0
    or max(0, pred_downside - downside_residual_q80) >= manifest.downside_oof_prediction_p80
    or min(1, fill_probability + fill_residual_q80) < 0.60
)
position_multiplier = (
    0.8 if ml_score < 40 else
    0.9 if ml_score < 60 else
    1.0 if ml_score < 80 else
    1.1
)
```

For L2/L3 counterfactuals and any later authorized live use, restore predicted gross return from the bundle's reference fee basis and calculate the account layer after exact quantity allocation:

```python
conditional_net_pnl_yuan = order_notional * predicted_gross_return - actual_round_trip_cost_yuan
unconditional_expected_net_pnl_yuan = fill_probability * conditional_net_pnl_yuan - (1 - fill_probability) * no_fill_cost_yuan
conservative_edge_yuan = confidence * max(unconditional_expected_net_pnl_yuan, 0) + min(unconditional_expected_net_pnl_yuan, 0)
conservative_price_downside_rate = max(0, predicted_downside_loss + downside_residual_q80 - reference_round_trip_cost_rate)
filled_downside_yuan = order_notional * conservative_price_downside_rate + actual_round_trip_cost_yuan
```

Use first-version `no_fill_cost_yuan=0`. Hard risk uses `max(rule_stop_loss_yuan, filled_downside_yuan)` without multiplying by fill probability or confidence. The model can only reduce quantity; market labels never read account equity or cash.

- [ ] **Step 4: Run GREEN and rule regressions**

Run: `python -m unittest tests.test_ml_runtime tests.test_joinquant_exporter tests.test_execution_admission tests.test_pre_trade_check tests.test_exit_policy tests.test_config_env -v`

Expected: L0 trading equivalence is 100%; higher-level counterfactual invariants hold and all failure cases fall back to rules.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add ml_runtime.py a_share_strategy.py joinquant_exporter.py ml_store.py config.py tests/test_ml_runtime.py tests/test_joinquant_exporter.py tests/test_config_env.py
git commit -m "feat: integrate deterministic ml runtime"
```

### Task 7: Close the model evidence and maintenance loop (ML-7 Task 10)

**Files:**

- Create: `ml_maintenance.py`
- Create: `tests/test_ml_maintenance.py`
- Modify: `strategy_compare_report.py`
- Modify: `tests/test_strategy_compare_report.py`
- Modify: `run_ubuntu.sh`
- Modify: `tests/test_joinquant_linux_script.py`
- Modify: `config.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**

- Produces commands `ml-labels`, `ml-train`, `ml-model-status`, `ml-backup`, `ml-restore-check`, `ml-retention-dry-run` and `ml-retention-apply`.
- Produces bounded rule-versus-trained-model daily/weekly reports with data, drift, model, permission and trading-equivalence evidence.

- [ ] **Step 1: Write failing report/backup/automation tests**

```python
def test_report_compares_rules_with_trained_model_only(self):
    report = build_strategy_compare_report(STORE, start=START, end=END)
    self.assertIn("原规则策略", report.markdown)
    self.assertIn("训练模型", report.markdown)
    self.assertNotIn("规则影子", report.markdown)

def test_automated_commands_do_not_expose_admin_promotion(self):
    installed = Path("run_ubuntu.sh").read_text(encoding="utf-8")
    self.assertNotIn("ml_admin.py approve", installed_timer_commands(installed))
    self.assertNotIn("ml_admin.py activate", installed_timer_commands(installed))
```

Cover bounded query windows, D+3/5/10/downside/fill metrics, label coverage/failures, PSI/confidence, model/manifest hashes, L0 equivalence, weekly challenger registration only, daily mature labels, online backup SHA/integrity, isolated restore counts, 7/4/12 retention and apply requiring a verified backup.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_ml_maintenance tests.test_strategy_compare_report tests.test_joinquant_linux_script tests.test_config_env -v`

Expected: maintenance commands and trained-model-only report are absent.

- [ ] **Step 3: Implement bounded operations**

Read reports from bounded SQLite queries, never full unbounded JSONL scans. Daily jobs mature available labels and generate L0 evidence; Friday jobs may train/register a challenger but cannot approve/activate. Store backups separately for ML and history DBs with hashes, integrity and table counts. `retention-apply` must reference a successfully verified current backup and never delete an active model or facts inside governance windows.

When ML detail reaches 2 GB, stop candidate/prediction detail with an explicit ML health state while pure rule trading continues. Reject history imports before they can cross 3 GB.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_ml_maintenance tests.test_strategy_compare_report tests.test_joinquant_linux_script tests.test_config_env -v`

Expected: reports and maintenance are bounded, backups restore, automation cannot promote and capacity failure leaves trading unchanged.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add ml_maintenance.py strategy_compare_report.py run_ubuntu.sh config.py tests/test_ml_maintenance.py tests/test_strategy_compare_report.py tests/test_joinquant_linux_script.py tests/test_config_env.py
git commit -m "feat: operate ml evidence loop"
```

### Task 8: Verify Batch C and align documents (ML-7 Task 11 — in progress locally)

**Files:**

- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-15-trained-shadow-model-design.md`
- Modify: `docs/superpowers/plans/2026-07-15-trained-shadow-model.md`
- Modify: `docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md`

- [ ] **Step 1: Run target compilation and focused ML tests**

```powershell
python -m py_compile historical_data.py historical_strategy.py historical_backtest.py ml_contracts.py ml_store.py ml_dataset.py ml_labels.py ml_training_data.py ml_train.py ml_admin.py ml_runtime.py ml_maintenance.py strategy_compare_report.py a_share_strategy.py joinquant_exporter.py
python -m unittest tests.test_historical_data tests.test_historical_strategy tests.test_historical_backtest tests.test_historical_backtest_cli tests.test_ml_contracts tests.test_ml_store tests.test_ml_dataset tests.test_ml_labels tests.test_ml_training_data tests.test_ml_train tests.test_ml_admin tests.test_ml_runtime tests.test_ml_maintenance tests.test_strategy_compare_report tests.test_joinquant_exporter -v
```

Expected: all Task 1-10 focused tests pass.

- [ ] **Step 2: Run trading regressions and the complete local suite**

```powershell
python -m unittest tests.test_execution_admission tests.test_pre_trade_check tests.test_execution_ledger_integration tests.test_execution_state tests.test_exit_policy tests.test_reconciliation tests.test_trading_control -v
python -m unittest discover -s tests -p "test_*.py" -v
git diff --check
git status --short --branch
```

Expected: ML disabled/L0 leaves all rule and trading tests unchanged and the full suite passes.

- [ ] **Step 3: Perform the mandatory security/type/permission review**

Verify model paths cannot escape `cache/ml/models`, external pickle/joblib cannot be loaded, all hashes/versions match before inference, automated services contain no approval/activation path, environment/account values do not enter samples/reports, function/type names match this plan, and L0 signal equivalence is field-for-field.

- [ ] **Step 4: Update status truthfully**

Record Tasks 4-10 as `implemented locally / not committed / not deployed / not observed / not validated`; keep Task 11 `in progress` until the full local verification and security review finish. State separately whether a diagnostic model was produced. Without one year of strict data and all performance gates, no model is `approvable_l0`; without human approval, activation, deployment and live candidates, there is no active model or server L0 evidence.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add docs/project_roadmap.md docs/project_handoff.md docs/live_trading_execution_plan.md docs/codex_simulation_observation_plan.md docs/data_storage_policy.md docs/superpowers/specs/2026-07-15-trained-shadow-model-design.md docs/superpowers/plans/2026-07-15-trained-shadow-model.md docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md
git commit -m "docs: record five-head ml implementation"
```

## Deployment And Observation Boundary (ML-7 Task 12 — not authorized / not started)

Code completion does not install scikit-learn, import external strict data, train a credible model or enable L0. A separately authorized Linux deployment must preserve existing private configuration, back up and verify trading/ML/history databases separately, install the pinned dependency, run the Linux full suite, migrate ML/history stores, restore each backup into isolated destinations and restart only authorized services.

L0 activation additionally requires one year/240 days of strict data, all coverage/regime/performance gates, an immutable registered bundle and explicit hash-bound approval. L0 becomes observed only after at least five valid trading days and 500 real predictions with 100% trading equivalence. L1/L2/L3 remain separately authorized and require the full 20/40/60-day evidence gates; no simulated unit test or elapsed calendar time substitutes for them.
