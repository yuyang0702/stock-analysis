# Pandas Holding Series Health Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore complete intraday scans when a current holding enters the candidate pool.

**Architecture:** Preserve the existing `pd.Series` holding payload and correct the shared risk engine's presence check. Prove the production traceback with one focused regression test before changing production code, then verify the full suite without changing strategy behavior.

**Tech Stack:** Python 3.12, pandas, unittest

---

### Task 1: Reproduce And Fix The Holding Presence Check

**Files:**
- Modify: `tests/test_risk_engine.py`
- Modify: `risk_engine.py:309`

- [x] **Step 1: Write the failing regression test**

Add this test to `RiskEngineTest`:

```python
def test_build_risk_decision_accepts_series_holding(self) -> None:
    holding = pd.Series({"price": 10.0, "cost": 9.5})

    decision = build_risk_decision(
        self.row,
        self.market_info,
        profile=build_strategy_profile("short"),
        holding=holding,
    )

    self.assertIsNotNone(decision)
```

- [x] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m unittest tests.test_risk_engine.RiskEngineTest.test_build_risk_decision_accepts_series_holding -v
```

Expected: `ERROR` with `ValueError: The truth value of a Series is ambiguous` at `risk_engine.py`.

- [x] **Step 3: Apply the minimal root-cause fix**

Change only the presence check in `build_risk_decision`:

```python
if holding is not None:
```

- [x] **Step 4: Run the focused test and risk engine suite**

Run:

```powershell
python -m unittest tests.test_risk_engine.RiskEngineTest.test_build_risk_decision_accepts_series_holding -v
python -m unittest tests.test_risk_engine -v
```

Expected: both commands exit 0 and all listed tests pass.

### Task 2: Verify And Record The Local State

**Files:**
- Modify: `docs/superpowers/specs/2026-07-23-pandas-holding-series-health-fix-design.md`
- Modify: `docs/project_handoff.md`

- [x] **Step 1: Run syntax and available local test verification**

Run:

```powershell
python -m py_compile risk_engine.py a_share_strategy.py
python -m unittest discover -s tests -v
```

Expected: compilation exits 0. Run the complete suite once to expose environment exclusions, then run all
locally executable modules. On this Windows host, 3 `run_ubuntu.sh ledger-check` tests require Bash and
remain pending for Linux; all other tests must pass.

- [x] **Step 2: Update documentation status precisely**

Set the incident spec to:

```text
implemented / not deployed / not observed / not validated
```

Add a dated handoff entry recording the production traceback, the local fix, test evidence, and the fact that server deployment and live observation remain pending.

- [x] **Step 3: Check the resulting diff**

Run:

```powershell
git diff --check
git status --short --branch
git diff -- risk_engine.py tests/test_risk_engine.py docs/superpowers/specs/2026-07-23-pandas-holding-series-health-fix-design.md docs/project_handoff.md
```

Expected: no whitespace errors; only the planned code, test, and documentation files plus this plan are changed.

- [x] **Step 4: Stop before repository or server mutations**

Do not commit, push, deploy, edit server configuration, or restart services without separate user authorization.

## Deployment Evidence

The user separately authorized commit, merge, push, Linux verification, deployment, and restart.
Commit `2cb90485290e75883379dada2b934637d87ffa37` was pushed to `origin/main` and
fast-forwarded on the server with a bundle verified on both sides after GitHub TLS failures.
The production SQLite backup passed integrity checking; Linux tests passed 441/441, compilation
and schema 9 `ledger-check` passed, the environment hash stayed unchanged, and all three services
were active without warnings after restart. The 13:00 live scan processed a current holding and
refreshed scan outputs and `signals.json` at 13:05:58. Status is
`implemented / deployed / observed / not validated`.
