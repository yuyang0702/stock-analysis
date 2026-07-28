# BrokerAdapter and QMT Node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a platform-neutral broker contract, a signed Linux execution gateway, a deterministic fault simulator, and a Windows QMT thin node whose order path is disabled by default.

**Architecture:** Linux remains the only authority for strategy, sizing, risk, order state, reservations and the trading ledger. A Windows node actively polls Linux, performs a final broker-side snapshot, and submits only an exact, unexpired `ExecutionIntent` after Linux grants a one-use permit; normalized events flow back to Linux. XtQuant is an optional leaf dependency behind `QmtBrokerAdapter`, while all Linux and CI tests use the in-memory adapter and never require QMT.

**Tech Stack:** Python 3.11+, dataclasses, typing.Protocol, sqlite3, Flask, requests, HMAC-SHA256, unittest, optional XtQuant on Windows.

**Status:** `planned / not implemented / not deployed / not observed / not validated`.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md`, especially sections 5, 6, 8, 12.4, 13, 14.4 and 16.
- Execute after Batch A and Batch B. This plan starts from trading schema 12 and migrates it to schema 13; do not renumber independently.
- The frozen chain is `StrategyOrderCandidate -> PreTradeResult -> ExecutionIntent`; the Windows node may not create or edit any of those objects.
- Linux is the only authority for strategy, models, sizing, risk, controls, reservations, order state and reconciliation.
- `SUBMIT_UNKNOWN` is never automatically retried. A new attempt requires an explicit human control event, a fresh snapshot, a new pre-trade result and a new `client_order_id`.
- The node must not alter quantity, price, side, stop, target position or expiry, and must not submit cached work while Linux is unavailable.
- `QMT_ADAPTER_ENABLE=0` on Linux and `QMT_ORDER_ENABLE=0` on Windows are the defaults. Both account-scoped gates plus `RISK_MODE=enforce` are required for a real order.
- Frozen first-version timings are 10-second heartbeat, 30-second node lease, 30-second READY-order lease, 10-second one-use submit permit and 5 A-share trading minutes before an unresolved `SUBMIT_UNKNOWN` becomes CRITICAL and stops new buys. A later timing change requires a reviewed protocol/config version.
- Missing XtQuant must produce an explainable read-only capability failure and must not break Linux tests or current JoinQuant health.
- QMT account details, client paths, session parameters, VPN data and HMAC secrets stay in private environment/configuration and never enter Git, logs, notifications or reports.
- Node logs retain the current and previous 10 trading days of execution boundaries, rotate by day/size, keep ordinary logs 30 days and anomaly summaries at most 180 days.
- Git commit, push, dependency installation, server deployment, service restart and either order-enable gate require separate authorization at execution time.

---

## File Map

- Create `broker_contracts.py`: normalized orders, fills, capabilities, adapter protocol and canonical hashing; import the single `BrokerSnapshot`/`BrokerPosition`/`ExecutionIntent` definitions from Batch A `execution_contracts.py`.
- Create `broker_protocol.py`: signed request envelopes, HMAC verification, timestamp checks and stable protocol errors.
- Create `broker_gateway.py`: Linux Flask application for heartbeat, lease, preflight, permit, callback, snapshot and cancellation endpoints.
- Create `broker_simulator.py`: deterministic in-memory adapter and failure script for protocol/state-machine tests.
- Create `qmt_adapter.py`: optional XtQuant binding that maps native objects to normalized contracts without policy decisions.
- Create `qmt_node.py`: Windows polling loop, final preflight, one-use permit flow, callback replay and reconnect reconciliation.
- Create `qmt_node_state.py`: bounded local cursor and execution-boundary journal; not an order truth source.
- Create `run_qmt_node.ps1`: explicit Windows self-check/read-only/run entry points, with order mode still gated by private configuration.
- Modify `trading_store.py`: schema 13 node sessions, nonces, order leases, submit permits and idempotent callback APIs.
- Modify `execution_admission.py` and `trading_control.py`: consume a one-use human reissue authorization to create a fresh pre-trade result and new `client_order_id`; never reopen the old intent.
- Modify `config.py`: disabled-by-default Linux gateway and account-scoped adapter settings.
- Modify `run_ubuntu.sh`: gateway self-check/run/install helpers without changing current services when disabled.
- Test with `tests/test_broker_contracts.py`, `tests/test_broker_protocol.py`, `tests/test_broker_gateway.py`, `tests/test_broker_simulator.py`, `tests/test_qmt_adapter.py`, `tests/test_qmt_node.py`, `tests/test_qmt_node_state.py` and existing ledger/control tests.

### Task 1: Freeze normalized broker contracts

**Files:**

- Create: `broker_contracts.py`
- Create: `tests/test_broker_contracts.py`

**Interfaces:**

- Consumes and re-exports Batch A `BrokerPosition`, `BrokerSnapshot` and `ExecutionIntent` without redefining them.
- Produces `BrokerCapabilities`, `BrokerOrder`, `BrokerFill`, `BrokerSubmitResult` and `BrokerCancelResult` frozen dataclasses.
- Produces `BrokerAdapter` with `fetch_account()`, `fetch_positions()`, `fetch_orders(since)`, `fetch_trades(since)`, `place_order(intent)`, `cancel_order(order_id)` and `query_by_client_order_id(client_order_id)`.
- Produces `BrokerPreflightProvider` with `fetch_quote(code) -> QuoteSnapshot` and `fetch_instrument_rules(code) -> InstrumentRules`.
- Produces `canonical_payload(value) -> bytes` and `canonical_sha256(value) -> str`; account scope always comes from Batch A's persisted random UUID.

- [ ] **Step 1: Write failing normalization and secrecy tests**

```python
def test_snapshot_hash_is_stable_and_does_not_contain_raw_account(self):
    snapshot = BrokerSnapshot.from_values(
        account_scope_id="scope-uuid", trade_date="2026-07-28",
        broker_time="2026-07-28T10:00:00+08:00", total_equity=50_000,
        cash=20_000, available_cash=19_500, frozen_cash=500,
        positions=[], open_orders=[], fills=[], adapter_version="sim-1",
        node_version="node-1", session_id="session-1", capabilities_version="cap-1",
    )
    self.assertEqual(snapshot.snapshot_sha256, BrokerSnapshot.from_dict(snapshot.to_dict()).snapshot_sha256)
    self.assertNotIn("raw-account", canonical_payload(snapshot).decode("utf-8"))

def test_adapter_contract_requires_exact_execution_intent(self):
    signature = inspect.signature(BrokerAdapter.place_order)
    self.assertEqual(list(signature.parameters), ["self", "intent"])
```

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_broker_contracts -v`

Expected: import fails because `broker_contracts.py` does not exist.

- [ ] **Step 3: Implement frozen records and protocol**

```python
class BrokerAdapter(Protocol):
    def fetch_account(self) -> Mapping[str, object]: ...
    def fetch_positions(self) -> list[BrokerPosition]: ...
    def fetch_orders(self, since: str | None) -> list[BrokerOrder]: ...
    def fetch_trades(self, since: str | None) -> list[BrokerFill]: ...
    def place_order(self, intent: ExecutionIntent) -> BrokerSubmitResult: ...
    def cancel_order(self, order_id: str) -> BrokerCancelResult: ...
    def query_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None: ...

class BrokerPreflightProvider(Protocol):
    def fetch_quote(self, code: str) -> QuoteSnapshot: ...
    def fetch_instrument_rules(self, code: str) -> InstrumentRules: ...
```

Reject non-finite money/price values, negative quantities, timezone-naive broker timestamps, unknown sides/statuses and mismatched account scopes. Normalize native status only at adapter boundaries. Hash canonical JSON with sorted keys, compact separators and `allow_nan=False`.

- [ ] **Step 4: Run GREEN and review**

Run: `python -m unittest tests.test_broker_contracts -v`

Expected: all contract, hash, round-trip and invalid-value tests pass.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add broker_contracts.py tests/test_broker_contracts.py
git commit -m "feat: define normalized broker contracts"
```

### Task 2: Add schema 13 leases, replay protection and submit permits

**Files:**

- Modify: `trading_store.py`
- Modify: `execution_admission.py`
- Modify: `trading_control.py`
- Modify: `tests/test_trading_store.py`
- Modify: `tests/test_execution_admission.py`
- Modify: `tests/test_trading_control.py`
- Create: `tests/test_broker_execution_store.py`

**Interfaces:**

- Consumes schema-11 `execution_intents` and `capacity_reservations` from Batch A and schema-12 notification tables from Batch B.
- Produces `claim_ready_intent(conn, account_scope_id, node_id, now, lease_sec) -> dict | None`.
- Produces `record_final_preflight(conn, client_order_id, lease_token, snapshot_sha256, quote_sha256, rules_sha256, now) -> str` returning a preflight ID.
- Produces `grant_submit_permit(conn, client_order_id, lease_token, preflight_id, now, ttl_sec) -> dict`.
- Produces `consume_submit_permit(...)`, `record_broker_order_event(...)`, `record_broker_fill_event(...)`, `close_submit_unknown_as_not_submitted(...)` and bounded nonce/session APIs.
- Produces `authorize_order_reissue(store, prior_client_order_id, reason, operator, now) -> str` and consumes that one-use authorization only through a new `admit_candidate` call.

- [ ] **Step 1: Write failing migration, CAS and terminal-state tests**

```python
def test_two_nodes_cannot_claim_the_same_ready_intent(self):
    with store.transaction() as conn:
        first = store.claim_ready_intent(conn, "scope", "node-a", NOW, 30)
    with store.transaction() as conn:
        second = store.claim_ready_intent(conn, "scope", "node-b", NOW, 30)
    self.assertEqual(first["client_order_id"], "coid-1")
    self.assertIsNone(second)

def test_submit_unknown_cannot_be_released_or_retried(self):
    seed_order(status="SUBMIT_UNKNOWN", intent_expires_at="2026-07-28 10:01:00")
    self.assertFalse(store.expire_unsubmitted_intent("coid-1", "2026-07-28 10:30:00"))
    with self.assertRaisesRegex(InvalidOrderTransition, "MANUAL_REISSUE_REQUIRED"):
        store.grant_submit_permit_for_retry("coid-1")

def test_authoritative_not_submitted_can_be_human_reissued_as_a_new_order(self):
    close_with_authoritative_not_submitted("coid-1", reconciliation_id="recon-1")
    authorization_id = authorize_order_reissue(store, "coid-1", "manual retry after broker proof", "operator", NOW)
    result = admit_candidate(store, fresh_request(reissue_authorization_id=authorization_id), LATER)
    self.assertNotEqual(result.client_order_id, "coid-1")
    self.assertEqual(load_reissue(authorization_id)["consumed_by_client_order_id"], result.client_order_id)
```

Also test schema 12 to 13 migration, repeated initialization, one-use permit consumption, permit expiry, wrong lease token, event content conflicts, monotonic fills, nonce replay, nonce pruning, single active node per account and `NOT_SUBMITTED` requiring authoritative evidence plus full reconciliation ID.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_trading_store tests.test_broker_execution_store -v`

Expected: schema remains 12 and lease/permit APIs are absent.

- [ ] **Step 3: Add additive schema 13 storage**

```sql
CREATE TABLE broker_node_sessions(
  account_scope_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, session_id TEXT NOT NULL,
  node_version TEXT NOT NULL, capabilities_sha256 TEXT NOT NULL,
  lease_until TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL,
  reconciliation_complete INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE broker_request_nonces(
  account_scope_id TEXT NOT NULL, node_id TEXT NOT NULL, nonce TEXT NOT NULL,
  seen_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  PRIMARY KEY(account_scope_id, node_id, nonce)
);
CREATE TABLE broker_submit_permits(
  permit_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE,
  lease_token TEXT NOT NULL, preflight_id TEXT NOT NULL,
  issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT,
  result_boundary TEXT NOT NULL DEFAULT 'permit_received'
);
CREATE TABLE broker_reissue_authorizations(
  authorization_id TEXT PRIMARY KEY, prior_client_order_id TEXT NOT NULL UNIQUE,
  control_event_id TEXT NOT NULL UNIQUE, account_scope_id TEXT NOT NULL,
  reason TEXT NOT NULL, operator TEXT NOT NULL, created_at TEXT NOT NULL,
  consumed_at TEXT, consumed_by_client_order_id TEXT UNIQUE
);
```

Add order/intent lease, adapter scope, normalized intent hash and preflight references idempotently. Every claim and transition uses `UPDATE ... WHERE status=? AND lease_token=?` and checks `rowcount == 1`. Never turn elapsed time alone into `NOT_SUBMITTED`; terminal closure requires broker evidence and a completed full reconciliation. The manual command may create a reissue authorization only after that closure. Admission consumes it in the same transaction as a new candidate/result/intent and derives the new `client_order_id` from the authorization's control-event ID; it never changes or reopens the old row.

- [ ] **Step 4: Run GREEN and ledger regressions**

Run: `python -m unittest tests.test_trading_store tests.test_broker_execution_store tests.test_execution_admission tests.test_order_ledger tests.test_execution_ledger_integration tests.test_trading_control -v`

Expected: schema 13 is healthy; old order/fill behavior remains monotonic and `SUBMIT_UNKNOWN` keeps buy recovery blocked.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add trading_store.py execution_admission.py trading_control.py tests/test_trading_store.py tests/test_broker_execution_store.py tests/test_execution_admission.py tests/test_trading_control.py
git commit -m "feat: add broker execution leases"
```

### Task 3: Implement the signed Linux gateway

**Files:**

- Create: `broker_protocol.py`
- Create: `broker_gateway.py`
- Create: `tests/test_broker_protocol.py`
- Create: `tests/test_broker_gateway.py`
- Modify: `config.py`
- Modify: `tests/test_config_env.py`

**Interfaces:**

- Produces `sign_request(method, path, body, timestamp, nonce, secret) -> dict[str, str]`.
- Produces `verify_request(request, expected_node_id, expected_scope, secret, now, nonce_store) -> VerifiedEnvelope`.
- Produces `create_broker_gateway(store, settings, clock) -> flask.Flask`.

- [ ] **Step 1: Write failing signature, scope and disabled-gate tests**

```python
def test_replayed_nonce_is_rejected(self):
    headers = sign_request("POST", "/broker/v1/heartbeat", b"{}", NOW, "nonce-1", SECRET)
    self.assertEqual(client.post("/broker/v1/heartbeat", data=b"{}", headers=headers).status_code, 200)
    response = client.post("/broker/v1/heartbeat", data=b"{}", headers=headers)
    self.assertEqual(response.status_code, 409)
    self.assertEqual(response.json["code"], "NONCE_REPLAY")

def test_qmt_buy_lease_requires_linux_enable_and_enforce(self):
    settings = replace(DEFAULTS, qmt_adapter_enable=False, risk_mode="observe")
    response = signed_post(client, "/broker/v1/lease", {"side": "buy"}, settings)
    self.assertEqual(response.status_code, 423)
    self.assertEqual(response.json["code"], "QMT_ORDER_GATE_CLOSED")
```

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_broker_protocol tests.test_broker_gateway tests.test_config_env -v`

Expected: protocol and application imports fail.

- [ ] **Step 3: Implement canonical signing and endpoints**

The signed bytes are exactly:

```python
canonical = b"\n".join([
    method.upper().encode("ascii"), path.encode("ascii"), timestamp.encode("ascii"),
    nonce.encode("ascii"), hashlib.sha256(body).hexdigest().encode("ascii"),
])
signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
```

Expose versioned POST endpoints for `health/capabilities`, `heartbeat`, `snapshot/full`, `lease`, `preflight`, `permit`, `submit-result`, `order-events`, `fill-events`, `query-result` and `cancel-request/result`. Verify body hash, protocol version, node/account scope, 30-second clock skew and nonce before parsing business content. Use stable error codes and never include secret/header values in responses or logs.

Add Linux settings with disabled defaults: `BROKER_GATEWAY_ENABLE=0`, `QMT_ADAPTER_ENABLE=0`, `BROKER_GATEWAY_HOST=127.0.0.1`, `BROKER_GATEWAY_PORT=8010`, account-scope/node IDs and secret loaded only from environment. Reject startup when QMT is enabled with `RISK_MODE != enforce`, missing scope/node/secret, or a non-private bind not explicitly approved.

- [ ] **Step 4: Run GREEN and Flask regressions**

Run: `python -m unittest tests.test_broker_protocol tests.test_broker_gateway tests.test_config_env tests.test_joinquant_signal_server -v`

Expected: signature, replay, lease, final-preflight, permit, callback and fail-closed tests pass; JoinQuant routes are unchanged.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add broker_protocol.py broker_gateway.py config.py tests/test_broker_protocol.py tests/test_broker_gateway.py tests/test_config_env.py
git commit -m "feat: add signed broker gateway"
```

### Task 4: Build the deterministic broker fault simulator

**Files:**

- Create: `broker_simulator.py`
- Create: `tests/test_broker_simulator.py`
- Modify: `tests/test_broker_gateway.py`

**Interfaces:**

- Consumes `ExecutionIntent` dictionaries and `BrokerAdapter` contracts.
- Produces `SimulatedBroker(script: list[SimulatedStep], clock)` and `SimulatedStep(operation, outcome, payload)`.
- Supports `accepted`, `rejected`, `timeout_before_call`, `timeout_after_accept`, `partial_fill`, `duplicate_callback`, `out_of_order_callback`, `disconnect` and `restart`.

- [ ] **Step 1: Write failing unknown-submit and disorder tests**

```python
def test_timeout_after_accept_becomes_unknown_then_query_recovers(self):
    broker = SimulatedBroker([SimulatedStep("place", "timeout_after_accept", {"order_id": "b-1"})])
    with self.assertRaises(SubmitOutcomeUnknown):
        broker.place_order(INTENT)
    self.assertEqual(broker.query_by_client_order_id("coid-1").order_id, "b-1")
    self.assertEqual(broker.place_call_count, 1)

def test_duplicate_and_out_of_order_fills_do_not_regress_quantity(self):
    replay_events([fill(200), fill(100), fill(200)])
    self.assertEqual(load_order("coid-1")["filled_qty"], 200)
    self.assertEqual(count_fills("broker-fill-200"), 1)
```

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_broker_simulator tests.test_broker_gateway -v`

Expected: simulator imports fail.

- [ ] **Step 3: Implement scripted outcomes without production policy**

The simulator records `place_called` before injecting an after-call timeout, preserves accepted orders across simulated restart, and exposes only broker facts. It never decides whether to retry. Add gateway integration cases proving that unknown status invokes query/reconciliation, never a second `place_order`, and escalates to CRITICAL plus stop-buy when still unknown past the frozen threshold.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_broker_simulator tests.test_broker_gateway tests.test_execution_state tests.test_trading_control -v`

Expected: every failure boundary is deterministic and all state transitions are forward-only.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add broker_simulator.py tests/test_broker_simulator.py tests/test_broker_gateway.py
git commit -m "test: add broker failure simulator"
```

### Task 5: Add the optional XtQuant adapter

**Files:**

- Create: `qmt_adapter.py`
- Create: `tests/test_qmt_adapter.py`

**Interfaces:**

- Produces `QmtBrokerAdapter(QmtPrivateSettings, xt_module=None)` implementing `BrokerAdapter`.
- Produces `QmtCapabilityError(code, detail)` with redacted, stable error codes.
- Consumes exact `ExecutionIntent.order_qty` and price constraints without adjustment.

- [ ] **Step 1: Write failing fake-XtQuant mapping tests**

```python
def test_place_order_uses_exact_qty_and_never_rounds(self):
    adapter = QmtBrokerAdapter(PRIVATE_SETTINGS, xt_module=fake_xt)
    adapter.place_order({**INTENT, "order_qty": 300, "limit_price": 10.12})
    self.assertEqual(fake_xt.last_call["order_qty"], 300)
    self.assertEqual(fake_xt.last_call["price"], 10.12)

def test_missing_xtquant_is_explainable_and_not_import_fatal(self):
    adapter = QmtBrokerAdapter(PRIVATE_SETTINGS, xt_module=None)
    with self.assertRaisesRegex(QmtCapabilityError, "XTQUANT_UNAVAILABLE"):
        adapter.fetch_account()
```

Also test stock-code/exchange mapping, order/fill status mapping, broker remark containing `client_order_id`, cancel/query, no raw account in normalized output, price/quantity mismatch refusal and unknown native status refusal.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_qmt_adapter -v`

Expected: `qmt_adapter` import fails.

- [ ] **Step 3: Implement a lazy optional binding**

Import XtQuant only inside adapter construction. `place_order` must compare the intent against the already-fetched final snapshot and either submit the exact order once or return a stable preflight rejection; it must never resize, reprice or retry. Map native callbacks immediately to `BrokerOrder`/`BrokerFill`, preserve native IDs only in bounded redacted evidence and keep account/client paths out of exception text.

- [ ] **Step 4: Run GREEN without XtQuant installed**

Run: `python -m unittest tests.test_qmt_adapter tests.test_broker_contracts -v`

Expected: all tests pass with the fake module; the production import path reports `XTQUANT_UNAVAILABLE` without crashing module import.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add qmt_adapter.py tests/test_qmt_adapter.py
git commit -m "feat: add optional qmt broker adapter"
```

### Task 6: Implement the default-disabled Windows polling node

**Files:**

- Create: `qmt_node_state.py`
- Create: `qmt_node.py`
- Create: `run_qmt_node.ps1`
- Create: `tests/test_qmt_node_state.py`
- Create: `tests/test_qmt_node.py`

**Interfaces:**

- Produces `QmtNode(adapter, gateway_client, state_store, settings, clock).run_once() -> NodeCycleResult`.
- Produces `NodeStateStore(path, keep_trading_days=10, log_days=30, anomaly_days=180)` with atomic cursor/journal operations.
- Produces CLI commands `self-check`, `sync-once`, `run-readonly` and `run`; `run` still refuses order submission unless the Windows gate is true.

- [ ] **Step 1: Write failing reconnect and double-gate tests**

```python
def test_restart_reconciles_before_leasing_new_buy(self):
    node = make_node(local_state={"session_id": "old", "reconciled": False})
    node.run_once()
    self.assertEqual(gateway.calls[:4], ["heartbeat", "snapshot/full", "order-events", "fill-events"])
    self.assertNotIn("lease", gateway.calls)

def test_windows_gate_closed_never_calls_place_order(self):
    node = make_node(qmt_order_enable=False, lease=READY_INTENT)
    result = node.run_once()
    self.assertEqual(result.code, "WINDOWS_ORDER_GATE_CLOSED")
    self.assertEqual(adapter.place_call_count, 0)
```

Also test expired intent, account-scope mismatch, final snapshot mismatch, permit expiry, crash at `permit_received`, crash at `place_called`, response loss, Linux unavailable, sell revalidation, cursor replay and bounded journal pruning.

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_qmt_node_state tests.test_qmt_node -v`

Expected: node modules are absent.

- [ ] **Step 3: Implement the frozen cycle**

```text
heartbeat and capability check
-> on new/restarted session post full account, position, order and fill snapshot
-> replay unacknowledged normalized callbacks
-> if reconciliation incomplete, stop cycle
-> lease one exact READY intent
-> fetch final account/positions/orders/fills/quote/rules
-> post final preflight hashes
-> receive and persist one-use permit boundary
-> if both gates are open and intent is unexpired, persist place_called then call adapter once
-> persist result_received and post normalized result/events
```

If the process restarts after `place_called` without a result, query by `client_order_id` and post facts; never call `place_order` again. If Linux is unreachable, only collect/replay broker facts and do not execute cached intents. `run_qmt_node.ps1 self-check` prints only capability names, versions, gate states and redacted scope IDs.

- [ ] **Step 4: Run GREEN**

Run: `python -m unittest tests.test_qmt_node_state tests.test_qmt_node tests.test_broker_simulator -v`

Expected: all crash-boundary, reconnect, gate and retention tests pass.

- [ ] **Step 5: Commit only after separate authorization**

```bash
git add qmt_node.py qmt_node_state.py run_qmt_node.ps1 tests/test_qmt_node.py tests/test_qmt_node_state.py
git commit -m "feat: add default-disabled qmt node"
```

### Task 7: Integrate operations, backup and verification

**Files:**

- Modify: `run_ubuntu.sh`
- Modify: `tests/test_joinquant_linux_script.py`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/data_storage_policy.md`
- Modify: `docs/project_roadmap.md`
- Modify: `docs/project_handoff.md`
- Modify: `docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md`

**Interfaces:**

- Produces Linux commands `broker-gateway-check`, `broker-gateway-run`, `broker-node-status` and `broker-reconcile`.
- Keeps `install` from enabling a QMT gateway/service unless `BROKER_GATEWAY_ENABLE=1` is explicitly present in private configuration.

- [ ] **Step 1: Write failing shell-surface and secret-redaction tests**

```python
def test_install_does_not_enable_qmt_gateway_by_default(self):
    script = Path("run_ubuntu.sh").read_text(encoding="utf-8")
    self.assertIn('BROKER_GATEWAY_ENABLE:-0', script)
    self.assertNotIn("cat stock-analysis.env", script)

def test_qmt_is_not_applicable_to_joinquant_health_when_disabled(self):
    result = broker_health(qmt_adapter_enable=False, node=None)
    self.assertEqual(result["qmt_status"], "not_applicable")
    self.assertEqual(result["system_status"], "healthy")
```

- [ ] **Step 2: Run RED**

Run: `python -m unittest tests.test_joinquant_linux_script tests.test_broker_gateway -v`

Expected: new commands/status are absent.

- [ ] **Step 3: Add guarded operations and truthful documentation**

Add self-check/run helpers, an optional dedicated service definition and redacted status output. Extend trading backup table-count manifests and isolated restore checks to schema 13 tables. Document the Linux/Windows boundary, VPN prerequisite, private configuration keys by name only, recovery order and the explicit prohibition on enabling real orders as part of ordinary deployment.

Update status only to `implemented / not deployed / not observed / not validated` after all local tests pass. A fake adapter does not make QMT read-only or order execution observed.

- [ ] **Step 4: Run focused and full verification**

```powershell
python -m py_compile broker_contracts.py broker_protocol.py broker_gateway.py broker_simulator.py qmt_adapter.py qmt_node.py qmt_node_state.py trading_store.py config.py
python -m unittest tests.test_broker_contracts tests.test_broker_protocol tests.test_broker_execution_store tests.test_broker_gateway tests.test_broker_simulator tests.test_qmt_adapter tests.test_qmt_node_state tests.test_qmt_node tests.test_trading_store tests.test_execution_ledger_integration tests.test_trading_control tests.test_joinquant_linux_script -v
python -m unittest discover -s tests -v
git diff --check
git status --short --branch
```

Expected: all tests pass, schema 13 initializes and restores idempotently, current JoinQuant output is unchanged, and no test needs XtQuant or a real broker account.

- [ ] **Step 5: Perform the security and state-machine review**

Search committed output for private account values, client paths, HMAC material, tokens and webhook fragments. Replay every simulator boundary and verify no path issues a second place call after `permit_received/place_called` ambiguity, no expired intent executes, callbacks are monotonic, and legal sells are not blocked by the buy gate.

- [ ] **Step 6: Commit only after separate authorization**

```bash
git add run_ubuntu.sh tests/test_joinquant_linux_script.py docs/live_trading_execution_plan.md docs/data_storage_policy.md docs/project_roadmap.md docs/project_handoff.md docs/superpowers/specs/2026-07-28-small-capital-live-readiness-integration-design.md
git commit -m "docs: record broker adapter implementation"
```

## Deployment And Observation Boundary

Code completion permits only an `implemented` claim. A separately authorized Linux deployment must preserve and compare the private environment hash, create and verify a trading SQLite backup, run the Linux full suite and `ledger-check`, perform an isolated restore, and restart only explicitly authorized services. A Windows read-only deployment may then prove account/position/order/fill normalization and reconnect reconciliation.

Real QMT order submission remains blocked until the user separately authorizes both account-scoped gates, supplies verified broker fee/rule facts, confirms a private network, completes failure drills, and approves a small-capital grey rollout. None of those external facts can be inferred from Git or simulator tests.
