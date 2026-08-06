# Transactional Notification Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace run-specific WeCom dedupe and JSON retry files with a bounded transactional SQLite outbox, stable business event identities, quiet transition-based alerts and retired rule-shadow messaging.

**Architecture:** Producers enqueue one immutable `NotificationEvent` in the same trading-database transaction as its source fact. A separate worker claims rows through SQLite compare-and-swap leases, performs the stateless WeCom HTTP call, and records sent/retry/dead/cancelled state; HTTP response loss remains explicitly at-least-once. Stable account, signal, plan, issue and control identities survive process restart, Token/URL rotation and changing scan `run_id` values.

**Tech Stack:** Python 3.11+, dataclasses, sqlite3, requests, hashlib/json, existing A-share trading calendar, unittest and systemd timers.

**Status (2026-08-05):** Batch B Tasks 1–7 的代码实现已完成，当前为 `implemented（本地功能分支，未提交） / not deployed / not observed / not validated`。schema 12 基础合同、来源事务内生产者、稳定计划/TTL、容量控制、CRITICAL 180 交易分钟复报、规则影子退役、systemd 路由、`ledger-check` 通知健康字段和带事件键校验的人工解除 CLI 均已在本地实现；专项测试与 Windows 平台无关测试通过，全量 908 项中仅 3 项因当前环境缺少 `bash.exe` 无法启动，Task 7 的 Linux 复验仍待完成。提交、推送、部署、真实通知观察和 20 日验证仍未完成。

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md`, especially sections 9, 10, 12.2, 13, 14.2 and Batch B.
- Execute after Batch A. This plan migrates trading schema 11 to schema 12; Batch D consumes schema 12 and must not reuse that version.
- A trading fact and its outbox event share one SQLite transaction. New producers may not commit a fact and then call `send_markdown()`.
- Delivery is application-level at-least-once, usually once, not exactly-once. An HTTP-success/response-loss ambiguity must remain visible.
- Stable event identity excludes actual send time, current `run_id`, webhook URL, Token, account number, volatile quote fields and rendered message truncation.
- Only unresolved CRITICAL issues repeat at 180, 360, 540 and later A-share trading-minute boundaries. Ordinary ERROR never repeats without a transition.
- Five-minute scans with no material event, empty plans, normal reconciliation and L0 predictions remain silent.
- Legal sells continue when notification storage or capacity fails. The `NOTIFICATION_CAPACITY` owner can stop new buys but can only release its own unchanged control generation.
- Normal active limit is 1,000 rows/4 MiB; high-priority reserve is 5,000 rows/20 MiB; dead detail is 1,000 rows/4 MiB. High priority reaches control warning at 80%.
- WeCom rendered payload remains below 4,000 UTF-8 bytes and is split only on event boundaries.
- Old `wecom_notify_state.json` and `notify_failed_queue.jsonl` are read only by one explicit audit command; they are not an active queue and are never automatically deleted.
- Rule-based shadow scoring is retired from active calculation, display and weekly notifications. Historical fields remain read-only compatible and do not imply a trained model exists.
- Git commit, push, environment changes, deployment, timer/service changes and restart require separate authorization at execution time.

---

## File Map

- Create `notification_outbox.py`: event records, stable IDs, TTL, hashes, capacity calculation and trading-minute reminder helpers.
- Create `notification_worker.py`: lease worker, retry/dead/cancel/compact flow and CLI.
- Modify `trading_store.py`: schema 12 account scopes, logical plans, outbox, enqueue gaps, issue incident state and store primitives.
- Modify `notifier.py`: retain byte-safe stateless HTTP transport; remove active persistence responsibility from new producers.
- Modify `notify_retry.py`: compatibility wrapper for the new worker and explicit one-time legacy audit only.
- Modify fact producers in `joinquant_sync.py`, `joinquant_signal_server.py`, `reconciliation.py`, `trading_control.py`, `joinquant_health.py`, `trading_backup.py`, `a_share_strategy.py` and `joinquant_exporter.py`.
- Modify `holdings_web.py` and `strategy_compare_report.py` to retire rule-shadow display/weekly messages.
- Delete `shadow_score.py` and `tests/test_shadow_score.py` only after all active imports and assertions are removed.
- Modify `run_ubuntu.sh` and systemd text tests to run one outbox worker every five minutes and disable the old retry/rule-shadow weekly routes.
- Extend `trading_backup.py` manifests and restore checks for schema 12.
- Create `tests/test_notification_outbox.py`, `tests/test_notification_worker.py` and `tests/test_notification_producers.py`; update existing notifier, alert, reconciliation, signal and report tests.

### Task 1: Add stable notification identities and schema 12 primitives

**Files:**

- Create: `notification_outbox.py`
- Create: `tests/test_notification_outbox.py`
- Modify: `trading_store.py`
- Modify: `tests/test_trading_store.py`
- Modify: `trading_backup.py`
- Modify: `tests/test_trading_backup.py`

**Interfaces:**

- Produces frozen `NotificationEvent`, `OutboxRecord`, `EnqueueResult` and `CapacitySnapshot`.
- Consumes and re-exports Batch A `execution_contracts.logical_signal_id(...)`; it must not define a second identity formula.
- Produces `plan_version(...) -> str`, `notification_event_key(...) -> str` and `event_payload_sha256(...) -> str`.
- Produces `TradingStore.enqueue_notification(conn, event, now) -> EnqueueResult` plus claim/complete/fail/cancel/gap/capacity primitives used by later tasks.

The exact event-key formats are:

```text
{adapter}:{account_scope}:buy-plan:{trade_date}:{logical_signal_id}:{plan_version}
{adapter}:{account_scope}:exit:{position_cycle_id}:{exit_intent_id}:{stage}
{adapter}:{account_scope}:fill:{fill_id}
{adapter}:{account_scope}:order-terminal:{client_order_id}:{status}:{reason_code}
{adapter}:{account_scope}:issue:{issue_key}:{incident_id}:{transition_seq}:{transition}:{severity}
{adapter}:{account_scope}:issue:{issue_key}:{incident_id}:reminder:{reminder_seq}
{adapter}:{account_scope}:control:{control_event_id}
{adapter}:{account_scope}:pre:{trade_date}
{adapter}:{account_scope}:close:{trade_date}
{adapter}:{account_scope}:weekly:{iso_week}
```

- [x] **Step 1: Write failing stability, conflict and migration tests**

```python
def test_logical_signal_id_ignores_run_id_and_token_rotation(self):
    first = logical_signal_id("scope", "2026-07-28", "main", "s1", "000001", "buy", "breakout")
    second = logical_signal_id("scope", "2026-07-28", "main", "s1", "000001", "buy", "breakout")
    self.assertEqual(first, second)
    self.assertEqual(len(first), 20)

def test_same_event_key_with_different_payload_is_a_conflict(self):
    store.enqueue_notification(conn, event("fill:f1", payload={"qty": 100}), NOW)
    with self.assertRaises(NotificationConflict):
        store.enqueue_notification(conn, event("fill:f1", payload={"qty": 200}), NOW)
```

Also test schema 11 to 12 migration, empty database initialization, duplicate same-hash idempotency, payload/title/body byte limits, account scope persistence, tombstone uniqueness, enqueue gap uniqueness and backup/restore counts.

- [x] **Step 2: Run RED**

Run: `python -m unittest tests.test_notification_outbox tests.test_trading_store tests.test_trading_backup -v`

Expected: notification domain types and schema 12 tables are absent.

- [x] **Step 3: Implement the frozen event contract and schema**

```python
@dataclass(frozen=True)
class NotificationEvent:
    event_key: str
    account_scope_id: str
    adapter: Literal["joinquant", "qmt"]
    event_type: str
    object_type: str
    object_id: str
    source_fact_id: str
    priority: Literal["normal", "high"]
    payload_version: int
    occurred_at: str
    expires_at: str | None
    title: str
    body: str
    payload: Mapping[str, object]
    metadata: Mapping[str, object]
```

```sql
CREATE TABLE notification_outbox(
  event_key TEXT PRIMARY KEY, account_scope_id TEXT NOT NULL,
  adapter TEXT NOT NULL CHECK(adapter IN ('joinquant','qmt')),
  event_type TEXT NOT NULL, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
  source_fact_id TEXT NOT NULL, priority TEXT NOT NULL CHECK(priority IN ('normal','high')),
  payload_version INTEGER NOT NULL, payload_sha256 TEXT NOT NULL,
  payload_json TEXT, title TEXT, body TEXT, body_sha256 TEXT,
  metadata_json TEXT, state TEXT NOT NULL
    CHECK(state IN ('pending','leased','sent','dead','cancelled')),
  lease_owner TEXT, lease_until TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL,
  expires_at TEXT, sent_at TEXT, cancel_requested_at TEXT, cancel_reason TEXT,
  terminal_at TEXT, last_error_code TEXT, last_error TEXT
);
CREATE INDEX idx_notification_due
  ON notification_outbox(state, next_attempt_at, priority, created_at);
CREATE INDEX idx_notification_object
  ON notification_outbox(object_type, object_id, state);
CREATE TABLE notification_enqueue_gaps(
  event_key TEXT PRIMARY KEY, account_scope_id TEXT NOT NULL,
  adapter TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
  source_fact_id TEXT NOT NULL, priority TEXT NOT NULL,
  reason TEXT NOT NULL, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL,
  resolved_at TEXT, resolution TEXT
);
CREATE TABLE logical_signal_plans(
  account_scope_id TEXT NOT NULL, trade_date TEXT NOT NULL,
  logical_signal_id TEXT NOT NULL, frozen_valid_until TEXT NOT NULL,
  current_plan_version TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(account_scope_id, trade_date, logical_signal_id)
);
```

Add `incident_id`, `transition_seq`, `critical_trading_minutes`, `critical_last_counted_minute` and `next_reminder_seq` to `execution_issue_state`. Consume the permanent account scope created by Batch A and fail closed on an unknown scope; do not derive or rotate it in notification code. Tombstones remain in the original row with payload/body/metadata cleared, never freeing `event_key`.

- [x] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_notification_outbox tests.test_trading_store tests.test_trading_backup -v`

Expected: stable hashes, idempotency/conflict checks, schema 12 and restored counts all pass.

Local evidence (2026-08-01): initial RED ran 94 focused tests with the expected missing module/schema 12 failures. Final focused `notification_outbox + trading_store + trading_backup` suite passed `110/110`; `py_compile` and `git diff --check` passed. Independent spec and adversarial/Ponytail reviews found no remaining P0/P1/P2. Coverage includes canonical event keys, same-key/different-body conflict after tombstone compaction, recursive immutability, UTC comparisons, lease-generation CAS, one begin-attempt per lease, outbox/gap mutual exclusion and atomic repair, full bounded byte accounting, dead tombstone exclusion, sensitive-field rejection, non-empty outbox/gap backup counts and `foreign_key_check`. This is local implementation evidence only; no commit, push, deployment, observation or validation occurred.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add notification_outbox.py trading_store.py trading_backup.py tests/test_notification_outbox.py tests/test_trading_store.py tests/test_trading_backup.py
git commit -m "feat: add transactional notification outbox"
```

### Task 2: Implement leased delivery, bounded retry and one-time legacy audit

**Files:**

- Create: `notification_worker.py`
- Create: `tests/test_notification_worker.py`
- Modify: `notifier.py`
- Modify: `notify_retry.py`
- Modify: `tests/test_notifier_retry.py`

**Interfaces:**

- Produces `WeComNotifier.deliver_markdown(title, body, sent_at) -> DeliveryResult` without local queue/dedupe writes.
- Produces `run_once(store, transport, worker_id, now, limit=50, lease_seconds=120) -> WorkerResult`.
- Produces `legacy_audit(store, state_file, queue_file, now) -> LegacyAuditResult`, guarded by one persisted completion marker.

- [x] **Step 1: Write failing worker lease, retry and audit tests**

```python
def test_only_current_lease_owner_can_complete(self):
    claimed = store.claim_notifications(worker_id="w1", now=NOW, limit=1, lease_seconds=120)
    attempt = store.begin_notification_attempt(claimed[0].event_key, "w1", claimed[0].lease_until, NOW)
    self.assertEqual(attempt, 1)
    self.assertFalse(store.complete_notification(event_key=claimed[0].event_key, worker_id="w2", sent_at=LATER, expected_lease_until=claimed[0].lease_until))
    self.assertTrue(store.complete_notification(event_key=claimed[0].event_key, worker_id="w1", sent_at=LATER, expected_lease_until=claimed[0].lease_until))

def test_legacy_audit_is_explicit_and_runs_once(self):
    first = legacy_audit(store, state_file, queue_file, NOW)
    second = legacy_audit(store, state_file, queue_file, LATER)
    self.assertTrue(first.completed)
    self.assertEqual(second.code, "LEGACY_AUDIT_ALREADY_COMPLETED")
```

Cover expired leases, worker crash, send-before-expiry reread, cancellation, temporary backoff, permanent 4xx, `40058`, fifth failure to dead, byte-safe Chinese truncation, response-loss ambiguity, due-order priority, sent-at rendering and compacted tombstones.

- [x] **Step 2: Run RED**

Run: `python -m unittest tests.test_notification_worker tests.test_notifier_retry -v`

Expected: worker is absent and notifier still writes JSON queue/state.

- [x] **Step 3: Implement the worker state machine**

```text
claim pending or expired leased rows with CAS
-> reread expiry, cancel request and current object state
-> cancel stale/replaced rows
-> render business time plus actual send time below 4000 UTF-8 bytes
-> issue one HTTP request
-> success: sent
-> temporary error: pending with bounded backoff
-> permanent 4xx or fifth failure: dead
-> missing response after request: pending with ambiguous-delivery metadata
```

Keep `send_markdown()` only as a temporary compatibility wrapper that calls transport directly and emits a deprecation warning; no migrated producer may call it. The explicit legacy audit turns provably sent old keys into tombstones, imports only provably unsent/still-valid rows with deterministic keys, and converts ambiguous/expired/40058/max-attempt entries into non-sendable audit rows. It never runs at startup and never deletes old files.

- [x] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_notification_worker tests.test_notifier_retry -v`

Expected: lease ownership, bounded retry/dead, TTL/cancel and once-only audit all pass without active JSON persistence.

Local evidence (2026-08-01): the initial RED failed because `notification_worker` did not exist. Final Task 2 contract suites passed `46/46`; coverage includes real elapsed-time refresh across HTTP, sub-second TTL and expired leases, lease ABA, fifth-attempt cutoff, 300/900/1800/3600 retry, permanent 4xx/`40058`, byte-safe explicit send time, sticky attempt-bound ambiguity, out-of-order callbacks, NaN/invalid-time/deep-JSON isolation, high-priority ordering, explicit bounded legacy audit, dry-run without database creation or migration, no replay, no secret persistence and unchanged legacy-file hashes. Final adversarial review reported PASS with no P0/P1/P2. This is local implementation evidence only; no commit, push, deployment, observation or validation occurred.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add notification_worker.py notifier.py notify_retry.py tests/test_notification_worker.py tests/test_notifier_retry.py
git commit -m "feat: deliver notifications from sqlite outbox"
```

### Task 3: Enqueue ledger and control events in source transactions

**Files:**

- Create: `tests/test_notification_producers.py`
- Modify: `joinquant_sync.py`
- Modify: `joinquant_signal_server.py`
- Modify: `reconciliation.py`
- Modify: `trading_control.py`
- Modify: `joinquant_health.py`
- Modify: `trading_backup.py`
- Modify: `tests/test_joinquant_sync.py`
- Modify: `tests/test_joinquant_signal_server.py`
- Modify: `tests/test_reconciliation.py`
- Modify: `tests/test_trading_control.py`
- Modify: `tests/test_joinquant_health.py`

**Interfaces:**

- Enqueues stable high-priority fill, exit, order-terminal, issue-transition, issue-reminder and control events in the existing fact transaction.
- Uses event keys frozen in design section 9.2.
- Produces one outbox event per reconciliation/issue transition rather than one summary hash.

- [x] **Step 1: Write failing atomicity and transition tests**

```python
def test_fill_and_notification_commit_or_roll_back_together(self):
    with self.assertRaises(NotificationConflict):
        ingest_fill_with_conflicting_event(store, FILL)
    self.assertEqual(count_rows("fills"), 0)
    self.assertEqual(count_rows("notification_outbox"), 0)

def test_control_event_and_notification_share_transaction(self):
    event_id = stop_buy_for_test(store)
    row = find_outbox(object_type="control_event", object_id=event_id)
    self.assertEqual(row.source_fact_id, event_id)
```

Cover each fill ID, order terminal state/reason, multiple reconciliation transitions, recovery cancellation, immutable conflict, control event, backup failure issue, direct-send absence and a legal sell continuing when enqueue writes a gap.

- [x] **Step 2: Run RED**

Run: `python -m unittest tests.test_notification_producers tests.test_joinquant_sync tests.test_joinquant_signal_server tests.test_reconciliation tests.test_trading_control tests.test_joinquant_health tests.test_trading_backup -v`

Expected: producers still send after commits and cannot prove fact/outbox atomicity.

- [x] **Step 3: Move each producer into its fact transaction**

Build immutable events from persisted IDs, enqueue before transaction commit, and remove post-commit `send_markdown()` calls. For high-priority hard-cap failures, write `notification_enqueue_gaps` in the same transaction and preserve the business fact. Make recovery enqueue one concise transition and request cancellation of unsent old transition/reminder rows.

- [x] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_notification_producers tests.test_joinquant_sync tests.test_joinquant_signal_server tests.test_reconciliation tests.test_trading_control tests.test_joinquant_health tests.test_trading_backup -v`

Expected: source facts reconcile exactly to outbox/gap rows and no migrated path uses the legacy queue.

Local GREEN evidence (2026-08-01): the expanded Task 3 producer/outbox, sync, admission, callback, reconciliation, control, health, backup, retry and worker suite passed `258/258`. Regressions cover atomic fill/order-terminal/exit/issue/control enqueue, repaired-gap replay, stable terminal facts, immutable fill/order conflicts, legal-sell preservation at capacity, legacy cumulative-fill compatibility without late-real-fill renotification, bounded canonical exit stages, secret redaction and Asia/Shanghai rendering. Independent specification and quality reviews reported no remaining P0/P1/P2; the final adversarial recheck is pending. This is local implementation evidence only, not a Git commit, deployment, real notification observation or strategy validation.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add joinquant_sync.py joinquant_signal_server.py reconciliation.py trading_control.py joinquant_health.py trading_backup.py tests/test_notification_producers.py tests/test_joinquant_sync.py tests/test_joinquant_signal_server.py tests/test_reconciliation.py tests/test_trading_control.py tests/test_joinquant_health.py
git commit -m "feat: enqueue trading notifications atomically"
```

### Task 4: Add stable plans, TTL and sent-based review eligibility

**Files:**

- Modify: `a_share_strategy.py`
- Modify: `joinquant_exporter.py`
- Modify: `tests/test_signal_watchlist.py`
- Modify: `tests/test_joinquant_notification.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `tests/test_alert_markdown.py`

**Interfaces:**

- Freezes first `frozen_valid_until` per trade date/logical signal and increments `plan_version` only for material execution changes.
- Enqueues buy plan, pre-open, close and weekly events with the design TTLs.
- Makes `a_share_strategy.review_watchlist_item(...)` and `build_watchlist_review_messages(...)` depend on `notification_outbox.sent_at` for the buy-plan event while preserving the existing bounded watchlist file as review-result compatibility storage.

- [x] **Step 1: Write failing run-id, replacement and review tests**

```python
def test_run_id_and_small_quote_change_do_not_create_a_new_plan(self):
    first = export_plan(run_id="r1", quote=Decimal("10.01"))
    second = export_plan(run_id="r2", quote=Decimal("10.011"))
    self.assertEqual(first.logical_signal_id, second.logical_signal_id)
    self.assertEqual(first.plan_version, second.plan_version)
    self.assertEqual(count_active_buy_plan_events(), 1)

def test_review_waits_for_successful_delivery(self):
    enqueue_buy_plan(state="pending")
    self.assertFalse(review_eligible(LOGICAL_ID))
    mark_sent(LOGICAL_ID)
    self.assertTrue(review_eligible(LOGICAL_ID))
```

Cover material quantity/target/entry tick/stop tick/expiry/version changes, old pending/leased cancellation, 15:00 plan TTL, 09:30 pre TTL, next-trading-day 09:15 close/weekly TTL, retry-after-success review and actual send time excluded from event hash.

- [x] **Step 2: Run RED**

Run: `python -m unittest tests.test_signal_watchlist tests.test_joinquant_notification tests.test_joinquant_exporter tests.test_alert_markdown -v`

Expected: current IDs/cooldown depend on run-specific message content and review depends on synchronous return.

- [x] **Step 3: Implement business plans and quiet scan producers**

Use canonical JSON for:

```python
logical_id = sha256_json({
    "account_scope_id": scope, "trade_date": trade_date,
    "strategy_id": strategy_id, "strategy_version": strategy_version,
    "code": code, "side": side, "setup_type": setup_type,
})[:20]
version = sha256_json({
    "code": code, "side": side, "target_qty": target_qty,
    "target_position": target_position, "entry_tick": entry_tick,
    "stop_tick": stop_tick, "frozen_valid_until": frozen_valid_until,
    "strategy_version": strategy_version, "parameters_version": parameters_version,
})[:16]
```

Persist the first expiry and current plan version. New material versions request cancellation of old active rows in the same transaction. Do not enqueue unchanged five-minute scans or empty plans. Render both business occurrence time and actual send time, but store the latter only as delivery metadata.

- [x] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_signal_watchlist tests.test_joinquant_notification tests.test_joinquant_exporter tests.test_alert_markdown -v`

Expected: stable event counts, correct TTL/cancellation and sent-based reviews.

Local GREEN evidence (2026-08-05): the stable-plan, notification, exporter and alert suite passed `114/114` after making the watchlist retention fixture date-relative. Coverage includes run-id-independent logical plans, material replacement/cancellation, calendar TTLs, sent-based review eligibility, quiet scans and explicit send-time rendering. This is local implementation evidence only; no commit, deployment, real notification observation or strategy validation occurred.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add a_share_strategy.py joinquant_exporter.py tests/test_signal_watchlist.py tests/test_joinquant_notification.py tests/test_joinquant_exporter.py tests/test_alert_markdown.py
git commit -m "feat: notify stable signal lifecycle events"
```

### Task 5: Implement CRITICAL trading-minute reminders and capacity ownership

**Files:**

- Modify: `notification_outbox.py`
- Modify: `notification_worker.py`
- Modify: `reconciliation.py`
- Modify: `trading_control.py`
- Modify: `joinquant_health.py`
- Modify: `tests/test_notification_outbox.py`
- Modify: `tests/test_notification_worker.py`
- Modify: `tests/test_reconciliation.py`
- Modify: `tests/test_trading_control.py`
- Modify: `tests/test_joinquant_health.py`

**Interfaces:**

- Produces `critical_trading_minutes(start, end, calendar, paused_intervals=()) -> int` and `next_critical_reminder_seq(minutes) -> int`.
- Produces capacity cleanup/control reconciliation owned only by `NOTIFICATION_CAPACITY`.

- [x] **Step 1: Write failing 180-minute and capacity matrix tests**

```python
def test_critical_reminder_counts_only_a_share_minutes(self):
    open_issue("09:31", severity="CRITICAL")
    advance_to("14:01")  # 119 morning + 61 afternoon trading minutes
    self.assertEqual(reminder_keys(), [incident_reminder_key(seq=1)])

def test_error_never_repeats_without_transition(self):
    open_issue("09:31", severity="ERROR")
    advance_trading_minutes(540)
    self.assertEqual(count_issue_events(), 1)
```

Also test 180/360 boundaries, lunch, overnight, weekend, configured holiday, CRITICAL-to-ERROR pause, re-escalation continuation, dead reminder followed by next sequence, recovery cancellation, new incident after recovery, normal/high/dead byte+row limits, 80% stop-buy, hard-cap gap and two five-minute low-water recovery checks.

- [x] **Step 2: Run RED**

Run: `python -m unittest tests.test_notification_outbox tests.test_notification_worker tests.test_reconciliation tests.test_trading_control tests.test_joinquant_health -v`

Expected: current ERROR reminder tests use 30 wall-clock minutes and there is no capacity owner.

- [x] **Step 3: Implement persistent incident time and owner-safe controls**

Update critical minutes only for newly elapsed A-share session minutes and persist the last counted boundary. `floor(minutes / 180)` is the reminder sequence; a failed/dead reminder does not reset time. An online worker creates each reached boundary, while restart after multiple missed boundaries creates only the highest due sequence and cancels lower unsent reminders so recovery cannot burst. Transitions increment `transition_seq`; CRITICAL downgrade pauses the counter and recovery closes it/cancels active rows.

Before claim/enqueue, run fixed cleanup order: cancel expired/replaced normal rows, clear body after 30 days, then compact terminal detail. At high-priority 80%, high-priority gap or write failure, use generation-bound CAS to stop buys and record whether this owner changed the control. Recover only after high active is below 20%, high dead/gaps are zero, the DB is writable and two checks at least five minutes apart agree. Never clear `kill_switch`, manual/reconciliation ownership or a changed buy-control generation.

- [x] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_notification_outbox tests.test_notification_worker tests.test_reconciliation tests.test_trading_control tests.test_joinquant_health -v`

Expected: transition-only ERROR, persistent 180-minute CRITICAL and owner-safe capacity behavior all pass.

Local GREEN evidence (2026-08-05): notification outbox/worker, reconciliation, control and health regressions passed in the focused local suites; the worker also verifies 180-minute A-share reminders, downgrade/re-escalation pause semantics, bounded dead detail and owner-safe two-cycle recovery. This is local implementation evidence only; no commit, deployment, real notification observation or strategy validation occurred.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add notification_outbox.py notification_worker.py reconciliation.py trading_control.py joinquant_health.py tests/test_notification_outbox.py tests/test_notification_worker.py tests/test_reconciliation.py tests/test_trading_control.py tests/test_joinquant_health.py
git commit -m "feat: quiet critical alerts and guard outbox capacity"
```

### Task 6: Retire active rule-shadow scoring and messages

**Files:**

- Modify: `a_share_strategy.py`
- Modify: `joinquant_exporter.py`
- Modify: `holdings_web.py`
- Modify: `strategy_compare_report.py`
- Modify: `ml_dataset.py`
- Delete: `shadow_score.py`
- Delete: `tests/test_shadow_score.py`
- Modify: `tests/test_alert_markdown.py`
- Modify: `tests/test_joinquant_notification.py`
- Modify: `tests/test_joinquant_exporter.py`
- Modify: `tests/test_holdings_web.py`
- Modify: `tests/test_strategy_compare_report.py`
- Modify: `tests/test_ml_dataset.py`

**Interfaces:**

- Keeps `final_score` as the only active deterministic rule score.
- Keeps historical `enhanced_score`, `shadow_rank` and `shadow_reason` parsing read-only where existing archives require it.
- Stops rule-shadow weekly WeCom; Batch C later supplies rule-versus-trained-model reports.

- [x] **Step 1: Replace positive shadow assertions with failing absence/compatibility tests**

```python
def test_live_alert_and_export_do_not_include_rule_shadow_fields(self):
    payload, markdown = build_live_outputs(ROW_WITH_LEGACY_SHADOW_FIELDS)
    self.assertNotIn("enhanced_score", payload["signals"][0])
    self.assertNotIn("影子", markdown)

def test_legacy_ml_sample_parser_still_reads_old_row(self):
    parsed = parse_legacy_sample({"features": {"final_score": 90, "enhanced_score": 95}})
    self.assertEqual(parsed["features"]["final_score"], 90)
```

- [x] **Step 2: Run RED**

Run: `python -m unittest tests.test_alert_markdown tests.test_joinquant_notification tests.test_joinquant_exporter tests.test_holdings_web tests.test_strategy_compare_report tests.test_ml_dataset -v`

Expected: active calls and display fields remain.

- [x] **Step 3: Remove active computation, display and scheduling**

Remove `apply_shadow_scores` imports/calls, shadow columns from live render/export, website fields and the Friday shadow message. Keep old JSON/DB field reads only in explicit legacy compatibility functions. Delete `shadow_score.py` and its tests after the active reference search is clean.

- [x] **Step 4: Run GREEN and reference audit**

```powershell
python -m unittest tests.test_alert_markdown tests.test_joinquant_notification tests.test_joinquant_exporter tests.test_holdings_web tests.test_strategy_compare_report tests.test_ml_dataset -v
rg -n "apply_shadow_scores|enhanced_score|shadow_rank|shadow_reason" --glob "*.py" .
```

Expected: tests pass; remaining Python hits are restricted to named legacy parsers/test fixtures and contain no live rendering, ranking or sending call.

Local GREEN evidence (2026-08-05): alert/exporter, holdings web, strategy comparison and ML compatibility suites passed (`114/114` plus `21/21`); `shadow_score.py` and its tests are deleted. Remaining historical shadow-field references are read-only compatibility/report paths, not live ranking, rendering or sending. This is local implementation evidence only; no commit, deployment, real notification observation or model validation occurred.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add a_share_strategy.py joinquant_exporter.py holdings_web.py strategy_compare_report.py ml_dataset.py tests
git rm shadow_score.py tests/test_shadow_score.py
git commit -m "refactor: retire rule shadow scoring"
```

### Task 7: Install the worker, verify conservation and update documents

**Files:**

- Modify: `run_ubuntu.sh`
- Create: `ledger_check.py`
- Modify: `tests/test_joinquant_linux_script.py`
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/codex_simulation_observation_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/superpowers/specs/2026-07-14-notification-review-idempotency-design.md`
- Modify: `docs/superpowers/specs/2026-07-15-trained-shadow-model-design.md`
- Modify: `docs/superpowers/specs/2026-07-26-runtime-evidence-integrity-repair-design.md`
- Modify: `docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md`
- Modify: `linux_deploy.md`

- [x] **Step 1: Write failing timer/legacy-route tests**

```python
def test_install_routes_retry_timer_to_sqlite_worker(self):
    script = Path("run_ubuntu.sh").read_text(encoding="utf-8")
    self.assertIn("notification_worker.py --once", script)
    self.assertNotIn("notify_retry.py --queue-file", script)
    self.assertNotIn("strategy-compare-weekly", installed_timer_commands(script))
```

- [x] **Step 2: Run RED**

Run: `python -m unittest tests.test_joinquant_linux_script -v`

Expected: old retry and shadow weekly paths are still installed.

- [x] **Step 3: Add worker operations and truthful documentation**

Route the five-minute retry unit to `notification_worker.py --once`, add `notify-status`, `notify-legacy-audit`, `notify-compact-dry-run`, `notify-compact-apply` and event-key-guarded `notify-resolve-write-failure` commands, and remove the active rule-shadow weekly timer. The install path must not print/change webhook or Token values. Add ledger/backup/control health counts for pending/leased/dead/gaps/tombstones, dead-detail bounds and unresolved write-failure markers.

Document status per sub-capability. After local completion: core enqueue/claim/send, retry, TTL, CRITICAL reminder and retirement are `implemented（本地功能分支，未提交） / not deployed / not observed / not validated`. Do not rewrite currently deployed behavior until actual server deployment is verified.

- [ ] **Step 4: Run focused, full and conservation tests**

```powershell
python -m py_compile notification_outbox.py notification_worker.py notifier.py notify_retry.py trading_store.py joinquant_sync.py joinquant_signal_server.py reconciliation.py trading_control.py joinquant_health.py trading_backup.py a_share_strategy.py joinquant_exporter.py holdings_web.py strategy_compare_report.py
python -m unittest tests.test_notification_outbox tests.test_notification_worker tests.test_notification_producers tests.test_notifier_retry tests.test_reconciliation tests.test_trading_control tests.test_joinquant_health tests.test_joinquant_notification tests.test_signal_watchlist tests.test_strategy_compare_report tests.test_joinquant_linux_script tests.test_trading_backup -v
python -m unittest discover -s tests -p "test_*.py" -v
git diff --check
git status --short --branch
```

Expected: all tests pass and fixture conservation satisfies `source unique events = sent + pending/leased + dead + cancelled + enqueue_gap`.

Local evidence (2026-08-05): Python compilation, focused notification/producer/backup/control/health suites and direct `ledger_check.py` execution pass. Windows full discovery is blocked only by the three Linux-script subprocess tests because this machine has no `bash.exe`; those tests must be rerun on Linux before this step is marked fully validated. No external system was accessed.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add run_ubuntu.sh tests/test_joinquant_linux_script.py docs/project_roadmap.md docs/project_handoff.md docs/live_trading_execution_plan.md docs/codex_simulation_observation_plan.md docs/data_storage_policy.md docs/superpowers/specs linux_deploy.md
git commit -m "docs: record notification outbox implementation"
```

## Deployment And Observation Boundary

A separate deployment authorization must cover a pre-migration online backup, schema 11 to 12 migration, Linux full tests, `ledger-check`, post-migration backup/isolated restore, timer installation and authorized service/timer restart while preserving every existing secret. Run the legacy audit once only after inspecting its dry-run counts; do not replay ambiguous history.

Core outbox becomes `observed` only after five valid trading days of source-to-state reconciliation. Retry, TTL cancellation and CRITICAL reminders each remain `not observed` until their own real event exists. Core validation requires 20 valid days of daily conservation, no high-priority dead/gap, retry within SLA and no concurrent duplicate claim/content conflict; none of those states can be inferred from unit tests or deployment alone.
