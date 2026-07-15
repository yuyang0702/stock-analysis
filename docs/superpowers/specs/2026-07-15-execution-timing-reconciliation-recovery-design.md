# Execution Timing, Reconciliation State, and Safe Recovery Design

> **Status (2026-07-15):** `implemented (pushed) / deployed (server; JoinQuant website update user-confirmed) / not observed / not validated`. Implementation commit `e2ce5b5` is on `origin/main`; Linux verification passes 324/324, the server ledger is schema 7, configuration hash is unchanged, and all three core services are active. The website update still requires a fresh trading-session snapshot before it is observed.
>
> This document records the user-approved design. It does not authorize Git operations, server deployment, service restart, JoinQuant website changes, or secret/configuration changes.

## 1. Purpose

The 2026-07-15 simulation run exposed a connected set of execution-contract defects:

- a daemon started shortly after midnight classified the run as `pre` and published a sell signal long before the executable session;
- the duplicate-stage sleep crossed the 09:30 boundary, delaying the first intraday refresh;
- JoinQuant judged every signal by the payload timestamp instead of the individual signal's last real validation time;
- an exit intent whose target had not yet been reached was immediately reconciled as `ERROR`, without distinguishing delivery, submission, market blocking, or fill latency;
- repeated snapshots could produce repeated reports without an explicit issue transition model;
- an updated JoinQuant template could run with an old in-memory `g` object missing newly introduced attributes;
- reconciliation-owned stop-buy state required manual recovery even after the transient discrepancy had demonstrably cleared.

The goal is to repair this execution chain without changing stock selection, entry scoring, position sizing, stop-loss, take-profit, or exit-price rules.

## 2. Status Language

All project documents and handoff reports must distinguish:

- `planned`: approved design or plan exists, but implementation is absent;
- `implemented`: code and tests exist in the stated workspace or commit;
- `deployed`: the exact code/version is installed in the stated runtime;
- `observed`: runtime evidence shows the behavior occurred;
- `validated`: enough repeated evidence exists to accept the behavior for its intended use.

Local tests never prove `deployed`, `observed`, or `validated`.

## 3. Non-goals and Safety Invariants

This change must not:

- alter buy/sell selection rules or price thresholds;
- automatically disable a `kill_switch`;
- automatically override a manual stop-buy, account risk gate, `RISK_OFF`, or other risk control;
- infer execution from a server-side signal publication;
- treat T+1, suspension, lunch, market close, or a price-limit block as infrastructure failure;
- edit or expose `stock-analysis.env`, `SYNC_TOKEN`, webhook URLs, SSH keys, or other secrets;
- add a message broker, database server, or duplicate unbounded event stream.

Legal sell execution remains allowed when only `buy_enabled=0`. Existing critical-ledger behavior remains fail-closed.

## 4. Selected Approach

Use a stateful schema-v7 execution contract:

1. make scheduler stages explicit and boundary-aligned;
2. give each signal separate creation, validation, and publication timestamps;
3. derive an exit's execution stage from platform order/fill evidence;
4. calculate escalation age in effective A-share trading minutes;
5. persist one current issue state per execution object for transition notifications;
6. self-heal JoinQuant runtime globals before every callback path;
7. permit automatic buy recovery only when the reconciliation subsystem owns the stop and strict evidence gates pass.

A minimal timing-only patch is rejected because it would leave false reconciliation errors and unsafe/manual recovery ambiguity. A separate event-bus service is rejected as unnecessary operational complexity for the current simulation scope.

## 5. Runtime Scheduling

### 5.1 Phases

`resolve_runtime_phase()` will use these local-time phases on configured A-share trading days:

| Phase | Time | Behavior |
|---|---|---|
| `closed` | 00:00:00–09:14:59 | no normal scan or signal publication |
| `pre` | 09:15:00–09:29:59 | one pre-market observation run |
| `intraday` | 09:30:00–11:30:00 and 13:00:00–15:00:00 | normal interval scans |
| `lunch` | after 11:30:00 and before 13:00:00 | no scan; wait for 13:00 |
| `after` | after 15:00:00 | one post-market run |

Non-trading days remain subject to the existing weekend and `A_SHARE_HOLIDAYS` rules. Ordinary trading notifications remain silent unless the existing explicit non-trading-day override is enabled.

### 5.2 Boundary-aligned waiting

The daemon will calculate the next phase boundary instead of applying a fixed 900-second sleep to duplicate stages.

- a completed `pre` run waits until 09:30;
- lunch waits until 13:00;
- a completed `after` run waits until the next eligible 09:15;
- `closed` waits until the next eligible 09:15;
- intraday waits for the configured interval but never later than 11:30 or 15:00;
- jitter may shorten or slightly vary a normal intraday interval, but may not move a wake-up past a phase boundary.

Restarting the service at midnight therefore cannot generate a pre-market trade signal and cannot make the daemon sleep across the open.

## 6. Signal Time Contract

Every exported signal will expose:

- `created_at`: immutable first creation time of the signal or stable exit intent;
- `validated_at`: latest time the server re-evaluated the signal against current strategy and position facts;
- `published_at`: time this payload instance was published.

The payload-level `generated_at` remains for schema-v1 compatibility and has the same meaning as `published_at`. It is not proof that an individual signal was revalidated.

JoinQuant freshness order:

1. `signal.validated_at`;
2. `signal.created_at` for a new-format signal missing validation due to a partial rollout;
3. payload `generated_at` only for legacy signals.

A stable exit signal ID may be republished only if the server has revalidated the active intent during the current run. Republishing the outer JSON alone must not renew freshness.

The ledger keeps immutable signal identity fields separate from mutable lifecycle timestamps. Advancing `validated_at` or `published_at` is allowed; changing immutable code, action, target, or creation facts under the same ID remains a `SignalConflictError`.

## 7. JoinQuant Runtime Compatibility

Add `_ensure_runtime_state(context)` and call it from `initialize`, `handle_data`, signal fetch/execution, order recording, and snapshot publication paths.

It initializes missing attributes without erasing valid current state:

- `signals`;
- `executed_signal_ids`;
- `order_events`;
- `order_signal_ids`;
- signal payload timestamps;
- daily account metric fields.

This protects a long-running simulation after a template update where the platform has not reconstructed `g`. The template version must be bumped. Future website deployment must preserve the existing `SIGNAL_URL`, `SNAPSHOT_URL`, `SYNC_TOKEN`, `DRY_RUN`, and runtime settings; that deployment is outside this implementation authorization.

## 8. Exit Execution State Machine

An active exit intent will be classified from ledger and platform evidence:

```text
TRIGGERED
  -> PUBLISHED
  -> PLATFORM_RECEIVED
  -> ORDER_SUBMITTED
  -> PARTIAL_FILL
  -> TARGET_REACHED
```

Failure or blocking states are orthogonal:

- `SIGNAL_DELIVERY_PENDING`: no platform response for the current validated publication;
- `SIGNAL_STALE`: platform explicitly skipped the signal as stale;
- `ORDER_SUBMIT_PENDING`: platform responded but no effective order exists;
- `FILL_PENDING`: a live order exists and target quantity is not reached;
- `PARTIAL_FILL_PENDING`: some quantity filled but target is not reached;
- `MARKET_BLOCKED_T1`;
- `MARKET_BLOCKED_SUSPENDED`;
- `MARKET_BLOCKED_LIMIT_DOWN`;
- `SUBMIT_UNKNOWN`;
- `EXIT_TARGET_REACHED`.

`EXIT_INTENT_MISMATCH` remains available for structural contradictions, but is no longer emitted merely because an otherwise valid exit is still inside its execution window.

Numeric zero must be rendered as `"0"`, never as an empty string. This applies especially to a full-exit `target_qty=0`.

## 9. Effective Trading Minutes and Escalation

Age is accumulated only inside continuous-auction windows:

- 09:30–11:30;
- 13:00–15:00;
- configured trading days only.

Lunch, overnight periods, weekends, and configured holidays add zero execution age.

Thresholds are counted from the relevant stage start: current validated publication for delivery, first valid submission for fills, and most recent material partial fill for partial-fill progress.

| Exit family | INFO | WARNING | ERROR |
|---|---:|---:|---:|
| hard stop | 1 minute | 2 minutes | 3 minutes |
| breakeven/trailing stop | 2 minutes | 3 minutes | 5 minutes |
| time stop | 3 minutes | 5 minutes | 10 minutes |
| take-profit/partial exit | 5 minutes | 10 minutes | 15 minutes |

Before the INFO threshold, the object remains tracked without an alert. INFO and WARNING do not change controls. ERROR may stop new buys under the ownership rules below. Existing immutable-fill or ledger-integrity conflicts remain CRITICAL.

T+1, suspension, and limit-down states are market blocks rather than delivery or ledger errors. They remain visible and transition-notified, but do not become infrastructure ERROR merely because time passes. A separate risk warning may remain active until the market block clears.

## 10. Persisted Issue State and Notifications

Schema v7 adds a bounded current-state table, tentatively `execution_issue_state`, keyed by stable issue/object identity. It stores:

- object type and object ID;
- current state and severity;
- first-seen, stage-start, last-seen, last-transition, and last-notified times;
- related signal/order/reconciliation IDs;
- recovery time and a bounded details JSON.

It does not store a duplicate row for every minute. Orders, fills, reconciliation runs/items, and control events remain the durable historical evidence.

Immediate WeCom notifications occur only on meaningful transitions:

```text
PENDING -> WARNING -> ERROR -> RECOVERED
```

An unchanged ERROR may be reminded at most once every 30 minutes. Identical snapshot replay and unchanged state produce no message. Recovery produces exactly one `RECOVERED` notification. Notification failure continues to use the existing retry mechanism and must not roll back ledger state.

## 11. Reconciliation-owned Automatic Buy Recovery

### 11.1 Ownership marker

Only when `apply_reconciliation_control()` changes `buy_enabled` from `1` to `0` because of an `ERROR` reconciliation does it record an auto-recovery ownership marker containing:

- originating reconciliation and control-event IDs;
- expected disabled value and `system_state.updated_at` generation;
- stop time;
- ownership type `reconciliation`.

`CRITICAL` never creates or retains an auto-recovery ownership marker. It sets or preserves `buy_enabled=0` and may enable `kill_switch`; neither state is automatically cleared after a critical event. Recovery requires explicit human review and separate manual control actions.

After the required review and clean reconciliations, an explicit manual `resume-buy` with a non-empty reason may acknowledge the sticky ledger-integrity or immutable-fill issue as recovered in the bounded issue table. This acknowledgment never disables `kill_switch`; the operator must clear that control separately and its audit event remains durable.

### 11.2 Manual precedence

Any manual buy-control or kill-switch action cancels the ownership marker, including `stop-buy` when the value is already `0`. This no-op value assertion must still create an auditable `hold_buy_disabled` or `cancel_auto_resume` control event.

Manual kill-switch actions, manual stop-buy, account risk controls, and `RISK_OFF` always take precedence. Enabling `buy_enabled` does not bypass exporter or JoinQuant risk gates.

### 11.3 Eligibility gates

Automatic recovery requires all conditions in one fresh evaluation:

1. ownership marker exists and belongs to reconciliation;
2. `buy_enabled=0` and its current generation matches the marker;
3. `kill_switch=0`;
4. two consecutive matched reconciliations after the stop use distinct fresh snapshot IDs;
5. the latest snapshot is within the configured freshness window;
6. the reported JoinQuant template version satisfies the required version;
7. no unresolved ERROR/CRITICAL execution issue exists;
8. no `submit_unknown` order exists;
9. no ledger-integrity or immutable-fill conflict is unresolved;
10. no manual control event superseded the marker.

The two reconciliations may be incremental callback reconciliations or explicit full reconciliations, but they must be consecutive, post-stop, and based on distinct platform snapshots. A replay of the same snapshot never counts twice.

### 11.4 Transaction and notification

If the gates pass, the same SQLite transaction:

- compares the expected control generation again;
- sets `buy_enabled=1`;
- records `auto_resume_buy` with operator `system` and the qualifying reconciliation;
- clears the ownership marker.

After commit, send one recovery notification with the stop event, two qualifying reconciliation IDs, and current control state. A concurrent manual change causes the compare-and-set to fail safely.

## 12. Schema v7 Migration

The idempotent migration will:

- add lifecycle timestamp columns needed by signals and exit intents;
- create `execution_issue_state` and required indexes;
- preserve every schema-v6 row;
- initialize new fields conservatively from existing creation/generation times without inventing delivery, order, or fill facts;
- update backup core-table reporting and restore checks.

No historical issue state or validation event will be fabricated. Existing schema-v6 backups remain restorable through the normal migration path.

High-frequency account summaries and retained position checkpoints keep the existing 366-day hot-retention policy. Orders, fills, control events, mismatch evidence, and material transition evidence remain long-term. Current issue rows are one-per-object and may be compacted only after recovery evidence is durable.

## 13. Failure Handling

- Scheduler calculation failure logs the error and uses a short bounded retry without scanning outside an allowed phase.
- Signal lifecycle persistence failure blocks new buy publication; legal sells follow the existing fail-safe contract only if their immutable ledger evidence is already durable.
- Snapshot-ledger failure returns 503, keeps the previous compatible JSON, stops buys, and raises the existing critical ledger alert.
- Auto-recovery evaluation failure leaves buying disabled.
- Notification failure never changes execution or control outcomes and enters the existing retry queue.

## 14. Verification Strategy

Implementation must be test-driven and cover at least:

### Scheduler

- 00:00, 09:14:59, 09:15, 09:29:59, 09:30;
- 11:30, lunch, 13:00, 15:00 and post-market;
- weekend/configured holiday behavior;
- duplicate pre/after stages cannot sleep across the next boundary.

### Signal lifecycle and JoinQuant

- immutable `created_at` and advancing `validated_at`/`published_at`;
- legacy payload fallback;
- per-signal freshness taking precedence over payload time;
- stale response classification;
- missing runtime globals self-heal without erasing existing state;
- template version alignment.

### Reconciliation

- target quantity zero rendering;
- delivery, submission, partial fill, target reached, T+1, suspension, and limit-down states;
- effective trading-minute calculations across lunch and overnight;
- all four escalation families;
- existing ledger CRITICAL rules remain intact.

### Notifications

- first transition only;
- unchanged replay silence;
- 30-minute ERROR reminder boundary;
- exactly one recovery notification;
- retry behavior and secret exclusion.

### Controls

- reconciliation establishes ownership only on a real `1 -> 0` transition;
- two distinct post-stop matched snapshots are required;
- same-snapshot replay is rejected;
- manual stop while already disabled cancels auto-recovery;
- kill switch, stale snapshot, old template, unresolved issue, and `submit_unknown` block recovery;
- compare-and-set race leaves buying disabled;
- successful recovery creates one control event and one notification.

### Migration and integration

- schema 6 to 7 preserves all rows;
- backup manifests include the new table;
- end-to-end stale exit, refreshed publication, submission, fill, reconciliation recovery, and guarded buy resume;
- complete platform-independent suite and Linux-specific script tests.

## 15. Documentation and Rollout Status

The historical local implementation checkpoint was:

`implemented (local workspace) / not deployed / not observed / not validated`

The later authorized Git and server operations completed for `e2ce5b5`; the user separately confirmed the JoinQuant website update. Observation still requires fresh runtime logs, database facts, and platform evidence from a trading session; validation requires repeated representative trading sessions.

The later deployment sequence, if separately authorized, must preserve server configuration and secrets, back up SQLite, migrate and check the ledger, update the JoinQuant template while preserving its URL/token/runtime configuration, restart only named services, and verify exact versions and post-deployment logs.
