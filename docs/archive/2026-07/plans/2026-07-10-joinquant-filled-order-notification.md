# JoinQuant Filled Order Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]) syntax for tracking.

**Goal:** Only send WeCom execution reports when a JoinQuant buy or sell order has a positive filled quantity.

**Architecture:** Keep the complete account snapshot and all order events for persistence, health checks, and ML labels. Before notifying WeCom, filter orders to actions `buy`/`sell` with numeric `filled > 0` and send a notification payload containing only those actual fills.

**Tech Stack:** Python, Flask, unittest

---

### Task 1: Lock Notification Behavior With Tests

**Files:**
- Modify: `tests/test_joinquant_signal_server.py`

- [ ] Add tests proving empty orders, zero-filled orders, failed/skipped orders do not notify.
- [ ] Add tests proving positive-filled buy and sell orders notify once.
- [ ] Add a mixed-order test proving the notification contains only actual fills.
- [ ] Run `python -m unittest tests.test_joinquant_signal_server` and confirm the new suppression tests fail before implementation.

### Task 2: Filter Notification Orders

**Files:**
- Modify: `joinquant_signal_server.py`

- [ ] Add one helper that returns only dictionary orders whose normalized action is buy/sell and whose numeric filled quantity is greater than zero.
- [ ] In the account snapshot endpoint, persist the original payload unchanged, then notify only when filtered orders exist.
- [ ] Pass a shallow payload copy with `orders` replaced by the filtered list to the notification formatter.
- [ ] Run the focused server tests and confirm they pass.

### Task 3: Update Main And Subordinate Documentation

**Files:**
- Modify: `docs/project_roadmap.md`
- Modify: `docs/live_trading_execution_plan.md`

- [ ] Document that periodic snapshots and zero-filled/failed/skipped orders do not send WeCom execution reports.
- [ ] Document that positive-filled buy, sell, and partial-fill events do send execution reports.

### Task 4: Verify And Publish

**Files:**
- Include pending test isolation fix: `tests/test_joinquant_health.py`

- [ ] Compile tracked Python files.
- [ ] Run `python -m unittest discover -s tests -p "test*.py"`.
- [ ] Run `git diff --check` and inspect the final diff.
- [ ] Commit and push to `origin/main`.
