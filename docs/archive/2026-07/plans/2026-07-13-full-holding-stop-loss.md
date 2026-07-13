# Full-Holding Stop-Loss Implementation Plan

> **For agentic workers:** Execute inline in the current workspace. Use test-driven development and verify each red/green transition.

**Goal:** Generate a JoinQuant sell signal when any synced holding crosses its existing stop price, even when the stock is absent from the strategy candidate pool.

**Architecture:** Add one pure helper in `a_share_strategy.py` that builds stop-loss rows from the full synced portfolio and the already-fetched spot frame, then merges them into the existing export source with stop-loss precedence by stock code. Reuse the existing exporter, ledger and notification paths without changing their contracts.

**Tech Stack:** Python, pandas, unittest.

## Global Constraints

- Do not change stop-loss percentages or recalculate stop prices.
- Do not add take-profit, trailing-stop, time-stop or partial-sell behavior.
- Do not add persistence, dependencies, database tables or JoinQuant template changes.
- Local completion is `implemented`, not `deployed`, `observed` or `validated`.

---

### Task 1: Full-holding stop-loss rows

**Files:**
- Modify: `tests/test_holding_stop_loss.py`
- Modify: `a_share_strategy.py`

**Interface:**
- Produce: `merge_holding_stop_loss_rows(source: pd.DataFrame, spot: pd.DataFrame, portfolio: dict[str, dict[str, Any]]) -> pd.DataFrame`

- [ ] Add a failing unit test where a holding absent from `source` has spot price at or below `stop_price`; assert one `stop_loss` row is returned with holding metadata.
- [ ] Run `python -m unittest tests.test_holding_stop_loss -v` and confirm failure because the helper does not exist.
- [ ] Implement the smallest helper that validates holding status, quantity, code, price and existing stop price; prefer spot price and fall back to snapshot price.
- [ ] Add focused cases for no trigger, snapshot fallback, invalid holdings and replacement of a same-code candidate row.
- [ ] Run `python -m unittest tests.test_holding_stop_loss -v` and confirm all cases pass.

### Task 2: Main-flow integration and status documentation

**Files:**
- Modify: `a_share_strategy.py`
- Modify: `docs/project_roadmap.md`

- [ ] Before `run_joinquant_export`, pass the selected export source through `merge_holding_stop_loss_rows(export_source, spot, portfolio_positions)`.
- [ ] Add an integration assertion showing the merged stop-loss row is exported as `action=sell` and no same-code buy remains.
- [ ] Run the focused stop-loss and exporter tests.
- [ ] Update the roadmap to mark the local capability `implemented`, with server deployment and real-trading-day observation explicitly pending.
- [ ] Run the complete unittest suite and `git diff --check`.
