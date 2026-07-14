# Notification Review Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make JoinQuant execution reports originate only from newly persisted fills, review every successfully pushed buy signal at D+0/D+1/D+3/D+5/D+10, and add the server send time to every WeCom message.

**Architecture:** Extend the existing schema-6 snapshot transaction to return bounded in-memory `new_executions` records without adding storage. The signal server sends only those records, the shared notifier appends send time after dedupe identity is chosen, and the existing watchlist is reviewed by trading-day cohort against the full spot frame rather than the TopN candidate frame.

**Tech Stack:** Python 3, standard library, pandas, Flask test client, SQLite, unittest; no new dependency or database table.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-14-notification-review-idempotency-design.md`.
- Do not change strategy scores, buy/sell rules, position sizing, stops, JoinQuant matching, or trading controls.
- Do not add a database table, JSONL stream, per-run report, or third-party dependency.
- Use existing `fills` uniqueness as the primary execution-notification identity.
- Legacy snapshots without `trades` may notify only when cumulative filled quantity increases.
- Server send time is added after caller dedupe keys are computed and appears exactly once.
- Keep `signal_watchlist.json` atomic, retain at least 20 natural days, and cap it at 500 rows.
- A missing quote remains an explicit review result; it never removes the sample.
- Current Windows baseline: 257 of 260 tests pass. Three `test_joinquant_linux_script` cases cannot start because Git Bash is absent; treat this as an environment limitation and run those tests in a Linux-capable environment before deployment.
- Do not push, deploy, restart services, edit JoinQuant, or alter runtime data without separate user authorization.

---

## File Map

- `joinquant_sync.py`: produce `new_executions` from newly inserted fills or legacy cumulative-fill increases inside the existing snapshot transaction.
- `joinquant_signal_server.py`: render and send only `new_executions`; remove periodic filled-order scanning as a notification trigger.
- `notifier.py`: add one current server-time line at the shared WeCom transport boundary while keeping retry queue content time-neutral.
- `a_share_strategy.py`: calculate trading-day cohorts, retain bounded watchlist history, build complete chunked review messages, and consume full spot quotes.
- `config.py`: change the watchlist default retention from 10 to 20 natural days.
- `tests/test_joinquant_sync.py`: verify new fill and legacy progress event identity and replay idempotency.
- `tests/test_joinquant_signal_server.py`: verify repeated callbacks and partial fills do not produce repeated reports.
- `tests/test_notifier_retry.py`: verify one send-time line and retry-time behavior.
- `tests/test_signal_watchlist.py`: verify D+0/D+1/D+3/D+5/D+10 cohort coverage, missing quotes, chunking, and no candidate-pool bias.
- `tests/test_config_env.py`: verify the new bounded retention default.
- `docs/project_roadmap.md`, `docs/live_trading_execution_plan.md`, `docs/data_storage_policy.md`: record implemented behavior without claiming deployment or observation.

---

### Task 1: Return Newly Persisted Execution Events

**Files:**
- Modify: `joinquant_sync.py:156-276`
- Test: `tests/test_joinquant_sync.py`

**Interfaces:**
- Produces: `ingest_snapshot_payload(...)["new_executions"] -> list[dict[str, object]]`.
- Each event has `event_id`, `source`, `order_id`, `signal_id`, `stock_code`, `action`, `qty`, `cumulative_qty`, `price`, `status`, and `filled_at`.
- `source` is `fill` for an immutable trade and `legacy_order_progress` only for a snapshot without trade rows.

- [ ] **Step 1: Write failing tests for fill replay and partial-fill increments**

Add these assertions to `test_ingest_persists_snapshot_order_fill_and_daily_equity_idempotently` and add a second test:

```python
self.assertEqual([event["event_id"] for event in first["new_executions"]], ["fill:20"])
self.assertEqual(second["new_executions"], [])

def test_new_execution_events_include_each_new_partial_fill_once(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = TradingStore(Path(tmp) / "trading.db")
        first = self._ledger_snapshot("2026-07-07 10:05:00")
        first["orders"][0]["filled"] = 50
        first["orders"][0]["amount"] = 100
        first["trades"][0]["trade_id"] = "fill-50-a"
        first["trades"][0]["amount"] = 50
        second = self._ledger_snapshot("2026-07-07 10:06:00")
        second["orders"][0]["filled"] = 100
        second["orders"][0]["amount"] = 100
        second["trades"] = [
            dict(first["trades"][0]),
            {
                "trade_id": "fill-50-b",
                "order_id": "10",
                "code": "600000",
                "action": "buy",
                "amount": 50,
                "price": 10.1,
                "commission": 1.0,
                "stamp_tax": 0.0,
                "other_fee": 0.0,
                "datetime": "2026-07-07 10:06:00",
            },
        ]

        first_result = joinquant_sync.ingest_snapshot_payload(first, store, "2026-07-07 10:05:02")
        second_result = joinquant_sync.ingest_snapshot_payload(second, store, "2026-07-07 10:06:02")
        replay_result = joinquant_sync.ingest_snapshot_payload(second, store, "2026-07-07 10:06:03")

        self.assertEqual([row["event_id"] for row in first_result["new_executions"]], ["fill:fill-50-a"])
        self.assertEqual([row["event_id"] for row in second_result["new_executions"]], ["fill:fill-50-b"])
        self.assertEqual(replay_result["new_executions"], [])
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_joinquant_sync.JoinQuantSyncTest.test_ingest_persists_snapshot_order_fill_and_daily_equity_idempotently tests.test_joinquant_sync.JoinQuantSyncTest.test_new_execution_events_include_each_new_partial_fill_once -v
```

Expected: FAIL because `new_executions` is absent.

- [ ] **Step 3: Collect inserted fills in the existing transaction**

In `persist_account_snapshot`, normalize orders before upsert, capture prior cumulative fill for the legacy path, and return only bounded event dictionaries:

```python
    trade_events = [event for event in snapshot.get("trades", []) if isinstance(event, dict)]
    has_trades = bool(trade_events)
    new_executions: list[dict[str, object]] = []
    orders_by_id: dict[str, dict[str, object]] = {}
    for event in snapshot.get("orders", []):
        if not isinstance(event, dict):
            continue
        order = normalize_order(event, trade_date=trade_date, strategy_version=strategy_version)
        previous = conn.execute(
            "SELECT filled_qty FROM orders WHERE client_order_id=?",
            (order["client_order_id"],),
        ).fetchone()
        previous_filled = int(previous[0]) if previous is not None else 0
        store.upsert_order(conn, order)
        if order.get("order_id"):
            orders_by_id[str(order["order_id"])] = order
        current_filled = int(order.get("filled_qty") or 0)
        if not has_trades and current_filled > previous_filled:
            new_executions.append({
                "event_id": f"legacy:{order['client_order_id']}:{current_filled}",
                "source": "legacy_order_progress",
                "order_id": order.get("order_id"),
                "signal_id": order.get("signal_id"),
                "stock_code": order.get("stock_code"),
                "action": order.get("action"),
                "qty": current_filled - previous_filled,
                "cumulative_qty": current_filled,
                "price": order.get("average_fill_price"),
                "status": order.get("status"),
                "filled_at": order.get("updated_at") or generated_at,
            })

    for event in trade_events:
        fill = normalize_fill(event, orders=orders_by_id)
        if not store.insert_fill(conn, fill):
            continue
        order = orders_by_id.get(str(fill.get("order_id") or ""), {})
        new_executions.append({
            "event_id": f"fill:{fill['fill_id']}",
            "source": "fill",
            "order_id": fill.get("order_id"),
            "signal_id": fill.get("signal_id"),
            "stock_code": fill.get("stock_code"),
            "action": fill.get("action"),
            "qty": fill.get("qty"),
            "cumulative_qty": order.get("filled_qty"),
            "price": fill.get("price"),
            "status": order.get("status") or "filled",
            "filled_at": fill.get("filled_at"),
        })
```

Replace the old `inserted_fills` return with:

```python
    return {
        "snapshot_id": sid,
        "retained_details": retain,
        "inserted_fills": sum(event["source"] == "fill" for event in new_executions),
        "new_executions": new_executions,
    }
```

- [ ] **Step 4: Add and pass the legacy cumulative-progress test**

```python
def test_legacy_order_progress_notifies_only_when_filled_quantity_increases(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = TradingStore(Path(tmp) / "trading.db")
        snapshot = self._ledger_snapshot()
        snapshot["trades"] = []
        snapshot["orders"][0]["amount"] = 100
        snapshot["orders"][0]["filled"] = 50

        first = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:02")
        replay = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:05:03")
        snapshot["generated_at"] = "2026-07-07 10:06:00"
        snapshot["orders"][0]["filled"] = 100
        increased = joinquant_sync.ingest_snapshot_payload(snapshot, store, "2026-07-07 10:06:02")

        self.assertEqual(first["new_executions"][0]["qty"], 50)
        self.assertEqual(replay["new_executions"], [])
        self.assertEqual(increased["new_executions"][0]["qty"], 50)
        self.assertEqual(increased["new_executions"][0]["cumulative_qty"], 100)
```

Run:

```powershell
python -m unittest tests.test_joinquant_sync -v
```

Expected: PASS.

- [ ] **Step 5: Commit the ledger event result**

```powershell
git add joinquant_sync.py tests/test_joinquant_sync.py
git commit -m "fix: expose newly persisted execution events"
```

---

### Task 2: Send Execution Reports Only for New Events

**Files:**
- Modify: `joinquant_signal_server.py:90-149,279-283`
- Test: `tests/test_joinquant_signal_server.py`

**Interfaces:**
- Consumes: `ledger_result["new_executions"]` from Task 1.
- Produces: `build_execution_markdown(payload, executions) -> str` and `_notify_execution(payload, executions) -> None`.
- Dedupe identity is the sorted execution `event_id` list, never Markdown content, account value, or snapshot time.

- [ ] **Step 1: Replace order-list tests with repeated-callback and partial-fill tests**

Add:

```python
def test_repeated_filled_snapshot_notifies_execution_once(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        signal_file = base / "signals.json"
        account_file = base / "account.json"
        signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
        app = joinquant_signal_server.create_app("secret", signal_file, account_file)
        client = app.test_client()
        payload = {
            "schema_version": 1,
            "trade_date": "2026-07-14",
            "generated_at": "2026-07-14 13:39:10",
            "positions": [],
            "orders": [{
                "order_id": "1783991771", "action": "sell", "code": "000021",
                "amount": 100, "filled": 100, "avg_price": 52.43,
                "status": "filled", "datetime": "2026-07-14 09:52:10",
            }],
            "trades": [{
                "trade_id": "trade-1783991771", "order_id": "1783991771",
                "action": "sell", "code": "000021", "amount": 100,
                "price": 52.43, "datetime": "2026-07-14 09:52:10",
            }],
        }
        with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)
            payload["generated_at"] = "2026-07-14 13:40:10"
            payload["total_value"] = 99482.757
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)

        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[1][0]["event_id"], "fill:trade-1783991771")
```

Add this partial-fill test:

```python
def test_second_partial_fill_notifies_only_the_new_trade(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        signal_file = base / "signals.json"
        account_file = base / "account.json"
        signal_file.write_text(json.dumps({"schema_version": 1, "signals": []}), encoding="utf-8")
        app = joinquant_signal_server.create_app("secret", signal_file, account_file)
        client = app.test_client()
        first_trade = {
            "trade_id": "trade-1", "order_id": "order-1", "action": "buy",
            "code": "600000", "amount": 50, "price": 10.0,
            "datetime": "2026-07-14 10:00:00",
        }
        payload = {
            "schema_version": 1,
            "trade_date": "2026-07-14",
            "generated_at": "2026-07-14 10:00:10",
            "positions": [],
            "orders": [{
                "order_id": "order-1", "action": "buy", "code": "600000",
                "amount": 100, "filled": 50, "avg_price": 10.0,
                "status": "partial", "datetime": "2026-07-14 10:00:00",
            }],
            "trades": [first_trade],
        }
        with unittest.mock.patch("joinquant_signal_server._notify_execution") as notify:
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)
            payload["generated_at"] = "2026-07-14 10:01:10"
            payload["orders"][0]["filled"] = 100
            payload["orders"][0]["status"] = "filled"
            payload["trades"] = [
                first_trade,
                {
                    "trade_id": "trade-2", "order_id": "order-1", "action": "buy",
                    "code": "600000", "amount": 50, "price": 10.1,
                    "datetime": "2026-07-14 10:01:00",
                },
            ]
            self.assertEqual(client.post("/joinquant/account_snapshot?token=secret", json=payload).status_code, 200)

        self.assertEqual(notify.call_count, 2)
        self.assertEqual([row["event_id"] for row in notify.call_args_list[0].args[1]], ["fill:trade-1"])
        self.assertEqual([row["event_id"] for row in notify.call_args_list[1].args[1]], ["fill:trade-2"])
```

- [ ] **Step 2: Run the server tests and verify RED**

Run:

```powershell
python -m unittest tests.test_joinquant_signal_server -v
```

Expected: FAIL because the current callback scans all filled orders and `_notify_execution` has one argument.

- [ ] **Step 3: Render only new execution events with stable identity**

Change the public helpers to:

```python
def build_execution_markdown(payload: dict[str, Any], executions: list[dict[str, Any]]) -> str:
    positions = payload.get("positions", []) if isinstance(payload.get("positions"), list) else []
    lines = [
        "#### 【JoinQuant 模拟盘】执行回报",
        f"> 账户快照：{payload.get('generated_at') or payload.get('received_at') or '-'}",
        f"> 总资产：{_num(payload.get('total_value')):.2f} | 现金：{_num(payload.get('cash')):.2f} | 持仓：{len(positions)}",
        f"> 本次新增成交：{len(executions)}",
    ]
    for item in executions:
        action = "买入" if item.get("action") == "buy" else "卖出"
        lines.append(
            f"- {action} {item.get('stock_code') or '-'} | 本次 {int(_num(item.get('qty')))}股 "
            f"@ {_num(item.get('price')):.2f} | 累计 {int(_num(item.get('cumulative_qty')))}股 | "
            f"{item.get('status') or '-'}"
        )
        lines.append(f"  > 成交时间：{item.get('filled_at') or '-'} | 订单：{_short(item.get('order_id')) or '-'}")
    return "\n".join(lines)


def _notify_execution(payload: dict[str, Any], executions: list[dict[str, Any]]) -> None:
    if not app_config.WECOM_WEBHOOK_URL or not executions:
        return
    md = build_execution_markdown(payload, executions)
    identity = "|".join(sorted(str(item["event_id"]) for item in executions))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    notifier = WeComNotifier(
        webhook_url=app_config.WECOM_WEBHOOK_URL,
        state_file=app_config.CACHE_DIR / "wecom_notify_state.json",
        cooldown_sec=app_config.NOTIFY_COOLDOWN_SEC_DEFAULT,
        timeout_sec=app_config.WECOM_TIMEOUT_SEC,
    )
    notifier.send_markdown("JoinQuant 模拟盘执行回报", md, dedupe_key=f"joinquant-exec:{digest}")
```

Replace the callback trigger with:

```python
        new_executions = list(ledger_result.get("new_executions") or [])
        if new_executions:
            _notify_execution(payload, new_executions)
```

Delete `_filled_trade_orders`; it is no longer an event detector.

- [ ] **Step 4: Run execution server and integration tests**

Run:

```powershell
python -m unittest tests.test_joinquant_signal_server tests.test_joinquant_sync tests.test_execution_ledger_integration -v
```

Expected: PASS, including one report for repeated snapshots and one report per new partial fill.

- [ ] **Step 5: Commit execution notification idempotency**

```powershell
git add joinquant_signal_server.py tests/test_joinquant_signal_server.py
git commit -m "fix: notify only newly persisted fills"
```

---

### Task 3: Add Server Send Time at the Shared Notifier Boundary

**Files:**
- Modify: `notifier.py:1-114`
- Test: `tests/test_notifier_retry.py`

**Interfaces:**
- Produces: `_server_time_text() -> str` and `_render_timed_content(content: str) -> str`.
- The retry queue continues to store raw business `content`, not rendered dynamic time.

- [ ] **Step 1: Write failing transport and retry tests**

Add:

```python
def test_send_adds_exactly_one_current_server_time_without_changing_queue_content(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        queue_file = base / "queue.jsonl"
        notifier = WeComNotifier(
            "https://example.invalid/webhook",
            base / "state.json",
            retry_queue_file=queue_file,
            cooldown_sec=0,
        )
        with patch("notifier._server_time_text", return_value="2026-07-14 18:30:00 Asia/Shanghai"):
            with patch("notifier.requests.post", return_value=FakeResponse({"errcode": 0})) as post:
                self.assertTrue(notifier.send_markdown("Title", "Body", dedupe_key="time-1"))
        content = post.call_args.kwargs["json"]["markdown"]["content"]
        self.assertEqual(content.count("服务器时间："), 1)
        self.assertIn("服务器时间：2026-07-14 18:30:00 Asia/Shanghai", content)

        with patch("notifier._server_time_text", return_value="2026-07-14 18:31:00 Asia/Shanghai"):
            with patch("notifier.requests.post", side_effect=requests.RequestException("offline")):
                self.assertFalse(notifier.send_markdown("Title", "Body", dedupe_key="time-2"))
        queued = json.loads(queue_file.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(queued["content"], "Body")
```

Extend the existing retry test with a captured retry request:

```python
            with patch("notifier._server_time_text", return_value="2026-07-14 18:32:00 Asia/Shanghai"):
                with patch("notifier.requests.post", return_value=FakeResponse({"errcode": 0})) as post:
                    sent = retry_failed_notifications(
                        "https://example.invalid/webhook",
                        queue_file,
                        state_file,
                        cooldown_sec=0,
                    )

            retry_content = post.call_args.kwargs["json"]["markdown"]["content"]
            self.assertEqual(retry_content.count("服务器时间："), 1)
            self.assertIn("服务器时间：2026-07-14 18:32:00 Asia/Shanghai", retry_content)
```

- [ ] **Step 2: Run the notifier tests and verify RED**

Run:

```powershell
python -m unittest tests.test_notifier_retry -v
```

Expected: FAIL because `_server_time_text` does not exist and payload content has no server time.

- [ ] **Step 3: Add the shared time renderer**

```python
def _server_time_text() -> str:
    now = datetime.now().astimezone()
    zone = getattr(now.tzinfo, "key", None) or now.tzname() or "local"
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {zone}"


def _render_timed_content(content: str) -> str:
    return f"> 服务器时间：{_server_time_text()}\n{content}"
```

Use it only for the outbound payload:

```python
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### {title}\n{_render_timed_content(content)}",
            },
        }
```

Keep both `_queue_failed(...)` calls passing the original `content` variable.

- [ ] **Step 4: Run all notification tests**

Run:

```powershell
python -m unittest tests.test_notifier_retry tests.test_joinquant_signal_server tests.test_reconciliation tests.test_trading_backup -v
```

Expected: PASS; queued content has no dynamic time and every outbound attempt has one time line.

- [ ] **Step 5: Commit shared timestamp behavior**

```powershell
git add notifier.py tests/test_notifier_retry.py
git commit -m "feat: timestamp all WeCom messages"
```

---

### Task 4: Define Bounded Trading-Day Review Cohorts

**Files:**
- Modify: `a_share_strategy.py:434-615,2721-2726`
- Modify: `config.py:294`
- Test: `tests/test_signal_watchlist.py`
- Test: `tests/test_config_env.py`

**Interfaces:**
- Produces: `trading_day_age(pushed_at: datetime, now: datetime) -> int`.
- Produces: `due_review_offset(item: dict[str, Any], now: datetime) -> int | None` for offsets `(0, 1, 3, 5, 10)`.
- `prune_signal_watchlist_items` retains 20 natural days and at most 500 rows.

- [ ] **Step 1: Write failing cohort and retention tests**

```python
def test_review_offsets_use_a_share_trading_days(self) -> None:
    friday = datetime(2026, 7, 10, 10, 0)
    monday = datetime(2026, 7, 13, 15, 30)
    self.assertEqual(strat.trading_day_age(friday, monday), 1)
    self.assertEqual(
        strat.due_review_offset({"kind": "买点", "pushed_at": "2026-07-10 10:00:00"}, monday),
        1,
    )
    self.assertIsNone(
        strat.due_review_offset({"kind": "风险", "pushed_at": "2026-07-10 10:00:00"}, monday)
    )
    now = datetime(2026, 7, 14, 15, 30)
    expected = {
        "2026-07-14 10:00:00": 0,
        "2026-07-13 10:00:00": 1,
        "2026-07-09 10:00:00": 3,
        "2026-07-07 10:00:00": 5,
        "2026-06-30 10:00:00": 10,
    }
    for pushed_at, offset in expected.items():
        with self.subTest(offset=offset):
            self.assertEqual(
                strat.due_review_offset({"kind": "买点", "pushed_at": pushed_at}, now),
                offset,
            )

def test_review_offsets_honor_configured_a_share_holiday(self) -> None:
    with patch.object(strat.app_config, "A_SHARE_HOLIDAYS_DEFAULT", {"2026-07-13"}):
        self.assertEqual(
            strat.trading_day_age(
                datetime(2026, 7, 10, 10, 0),
                datetime(2026, 7, 14, 15, 30),
            ),
            1,
        )

def test_watchlist_retention_is_twenty_days_and_capped_at_five_hundred(self) -> None:
    now = datetime(2026, 7, 31, 15, 30)
    items = [
        {
            "code": f"{index:06d}",
            "pushed_at": (datetime(2026, 7, 15, 10, 0) + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for index in range(520)
    ]
    kept = strat.prune_signal_watchlist_items(list(reversed(items)), now=now)
    self.assertEqual(len(kept), 500)
    self.assertEqual(kept[0]["code"], "000020")
```

Add `timedelta` to the datetime import in `tests/test_signal_watchlist.py` for the retention test.

In `tests/test_config_env.py`, assert `SIGNAL_WATCHLIST_DAYS_DEFAULT == 20` with the environment variable absent.

- [ ] **Step 2: Run cohort tests and verify RED**

Run:

```powershell
python -m unittest tests.test_signal_watchlist tests.test_config_env -v
```

Expected: FAIL because cohort helpers do not exist and defaults are 10 days/80 rows.

- [ ] **Step 3: Implement trading-day offsets and bounded retention**

Add an alias that does not conflict with the existing standard-library `time` module:

```python
from datetime import date, datetime, time as datetime_time, timedelta
```

Then implement:

```python
REVIEW_TRADING_DAY_OFFSETS = (0, 1, 3, 5, 10)
SIGNAL_WATCHLIST_MAX_ITEMS = 500


def trading_day_age(pushed_at: datetime, now: datetime) -> int:
    if pushed_at.date() >= now.date():
        return 0
    cursor = pushed_at.date()
    count = 0
    while cursor < now.date():
        cursor += timedelta(days=1)
        if is_a_share_trading_day(datetime.combine(cursor, datetime_time.min)):
            count += 1
    return count


def due_review_offset(item: dict[str, Any], now: datetime) -> int | None:
    if safe_text(item.get("kind")) != "买点":
        return None
    pushed_at = _parse_watchlist_time(item.get("pushed_at"))
    if pushed_at is None:
        return None
    age = trading_day_age(pushed_at, now)
    return age if age in REVIEW_TRADING_DAY_OFFSETS else None
```

Change `config.py` to:

```python
SIGNAL_WATCHLIST_DAYS_DEFAULT = _env_int("SIGNAL_WATCHLIST_DAYS", 20)
```

Sort by parsed push time before applying the hard bound so shuffled input still evicts the oldest signals:

```python
    kept.sort(key=lambda item: _parse_watchlist_time(item.get("pushed_at")) or datetime.min)
    return kept[-SIGNAL_WATCHLIST_MAX_ITEMS:]
```

- [ ] **Step 4: Run cohort and existing watchlist tests**

Run:

```powershell
python -m unittest tests.test_signal_watchlist tests.test_config_env -v
```

Expected: PASS.

- [ ] **Step 5: Commit cohort primitives**

```powershell
git add a_share_strategy.py config.py tests/test_signal_watchlist.py tests/test_config_env.py
git commit -m "feat: define bounded trading-day review cohorts"
```

---

### Task 5: Build Complete Chunked D+N Review Messages

**Files:**
- Modify: `a_share_strategy.py:615-758,3446-3453,3479-3567`
- Test: `tests/test_signal_watchlist.py`

**Interfaces:**
- Produces: `build_watchlist_review_messages(quotes, path, chunk_size=6, now=None) -> list[tuple[str, str]]`.
- Each tuple is `(dedupe_suffix, markdown)`.
- Consumes the full `spot` DataFrame, not `result` or TopN candidates.

- [ ] **Step 1: Write failing complete-cohort tests**

Create seven prior-trading-day buy records, one same-day buy, and one prior-day risk record. Provide full quotes for six prior buys and omit one quote:

```python
def test_review_messages_cover_complete_cohorts_and_chunk_without_candidate_bias(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "signal_watchlist.json"
        prior_buys = [
            {
                "code": f"60000{index}",
                "name": f"昨日买点{index}",
                "kind": "买点",
                "mode": "intraday",
                "pushed_at": f"2026-07-13 10:0{index}:00",
                "entry_price": 10.0,
                "stop_loss": 9.5,
                "take_profit": 11.0,
                "signal_id": f"60000{index}:mid:2026-07-13",
                "active": True,
            }
            for index in range(7)
        ]
        same_day = {
            "code": "000001", "name": "今日买点", "kind": "买点", "mode": "intraday",
            "pushed_at": "2026-07-14 10:00:00", "entry_price": 10.0,
            "stop_loss": 9.5, "take_profit": 11.0,
            "signal_id": "000001:mid:2026-07-14", "active": True,
        }
        risk = {
            "code": "300001", "name": "风险样本", "kind": "风险", "mode": "after",
            "pushed_at": "2026-07-13 15:00:00", "entry_price": 10.0,
            "signal_id": "300001:mid:2026-07-13", "active": True,
        }
        strat.save_signal_watchlist(path, {"items": prior_buys + [same_day, risk]})
        quotes = pd.DataFrame([
            {
                "code": f"60000{index}", "name": f"昨日买点{index}", "price": 10.5,
                "high": 10.8, "low": 9.9, "pct_chg": 2.0,
                "signal_action": "continue", "signal_state": "fresh",
            }
            for index in range(6)
        ] + [{
            "code": "000001", "name": "今日买点", "price": 10.2,
            "high": 10.3, "low": 9.9, "pct_chg": 1.0,
            "signal_action": "continue", "signal_state": "fresh",
        }])

        messages = strat.build_watchlist_review_messages(
            quotes,
            path,
            chunk_size=3,
            now=datetime(2026, 7, 14, 15, 30),
        )
        combined = "\n".join(markdown for _, markdown in messages)
        self.assertEqual(len(messages), 4)
        self.assertIn("D+1", combined)
        self.assertIn("D+0", combined)
        for code in ("600000", "600001", "600002", "600003", "600004", "600005", "600006"):
            self.assertIn(code, combined)
        self.assertIn("600006", combined)
        self.assertIn("行情缺失", combined)
        self.assertNotIn("风险样本", combined)
        d0_markdown = "\n".join(markdown for suffix, markdown in messages if ":d0:" in suffix)
        self.assertIn("待后续复盘", d0_markdown)
        self.assertNotIn("触及止盈", d0_markdown)
```

Add the all-missing regression:

```python
def test_review_keeps_prior_buy_when_full_quote_frame_has_no_matching_code(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "signal_watchlist.json"
        strat.save_signal_watchlist(path, {"items": [{
            "code": "600000", "name": "昨日买点", "kind": "买点",
            "pushed_at": "2026-07-13 10:00:00", "entry_price": 10.0,
            "stop_loss": 9.5, "take_profit": 11.0,
            "signal_id": "600000:mid:2026-07-13", "active": True,
        }]})

        messages = strat.build_watchlist_review_messages(
            pd.DataFrame(columns=["code", "price", "high", "low"]),
            path,
            now=datetime(2026, 7, 14, 15, 30),
        )

        self.assertEqual(len(messages), 1)
        self.assertIn("600000", messages[0][1])
        self.assertIn("行情缺失", messages[0][1])
```

Add a present-row-without-price regression so an incomplete quote cannot reach numeric formatting:

```python
def test_review_treats_present_row_without_price_as_missing_quote(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "signal_watchlist.json"
        strat.save_signal_watchlist(path, {"items": [{
            "code": "600000", "name": "昨日买点", "kind": "买点",
            "pushed_at": "2026-07-13 10:00:00", "entry_price": 10.0,
            "signal_id": "600000:mid:2026-07-13", "active": True,
        }]})

        messages = strat.build_watchlist_review_messages(
            pd.DataFrame([{"code": "600000", "name": "昨日买点", "price": None}]),
            path,
            now=datetime(2026, 7, 14, 15, 30),
        )

        self.assertEqual(len(messages), 1)
        self.assertIn("行情缺失", messages[0][1])
```

The expected four messages are three D+1 chunks for seven rows plus one D+0 chunk. The same-day and risk assertions prove that cohorts stay separate.

Replace the old `test_after_review_message_tracks_previous_pushed_stock` with a D+1 buy-signal case (`kind="买点"`, pushed on 2026-07-13 and reviewed on 2026-07-14). Keep assertions for maximum gain/drawdown and strategy-quality summary so the new complete-cohort implementation does not discard existing review metrics; remove the obsolete natural-day `D+2` and single-Markdown API assertions.

- [ ] **Step 2: Run review tests and verify RED**

Run:

```powershell
python -m unittest tests.test_signal_watchlist -v
```

Expected: FAIL because only one truncated Markdown string is returned and unmatched stocks are skipped.

- [ ] **Step 3: Replace candidate-intersection review with cohort messages**

Select rows by `due_review_offset` before quote lookup. Use this exact outer structure:

```python
def build_watchlist_review_messages(
    quotes: pd.DataFrame,
    path: Path = SIGNAL_WATCHLIST_FILE,
    chunk_size: int = 6,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    now = now or datetime.now()
    items = prune_signal_watchlist_items(load_signal_watchlist(path)["items"], now=now)
    rows_by_code = {
        clean_code(row.get("code")): row
        for _, row in quotes.iterrows()
        if clean_code(row.get("code"))
    }
    due: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        offset = due_review_offset(item, now)
        if offset is not None:
            due.setdefault(offset, []).append(item)

    updated_by_identity: dict[tuple[str, str], dict[str, Any]] = {
        (clean_code(item.get("code")), safe_text(item.get("signal_id"))): item
        for item in items
    }
    messages: list[tuple[str, str]] = []
    safe_chunk_size = max(1, chunk_size)
    for offset in REVIEW_TRADING_DAY_OFFSETS:
        cohort = due.get(offset, [])
        if not cohort:
            continue
        reviewed = [
            review_watchlist_item(item, rows_by_code.get(clean_code(item.get("code"))), now, offset)
            for item in cohort
        ]
        for item, lines in reviewed:
            updated_by_identity[(clean_code(item.get("code")), safe_text(item.get("signal_id")))] = item
        chunks = [reviewed[index:index + safe_chunk_size] for index in range(0, len(reviewed), safe_chunk_size)]
        for index, chunk in enumerate(chunks, start=1):
            header = [
                f"#### 推送跟踪复盘 D+{offset} 第 {index}/{len(chunks)} 组",
                f"> 批次样本：{len(cohort)} | 本组：{len(chunk)}",
            ]
            if offset > 0:
                header.extend(build_review_cohort_summary(reviewed))
            body = [line for _, lines in chunk for line in lines]
            messages.append((f"{now.date().isoformat()}:d{offset}:{index}:{len(chunks)}", "\n".join(header + body)))

    save_signal_watchlist(path, {"items": prune_signal_watchlist_items(list(updated_by_identity.values()), now=now)})
    return messages
```

Implement the extracted helper with the current formulas and an explicit missing-quote branch:

```python
def review_watchlist_item(
    item: dict[str, Any],
    row: pd.Series | None,
    now: datetime,
    offset: int,
) -> tuple[dict[str, Any], list[str]]:
    code = clean_code(item.get("code"))
    name = safe_text((row.get("name") if row is not None else "") or item.get("name"))
    history = [entry for entry in item.get("review_history", []) if isinstance(entry, dict)]
    history = [entry for entry in history if safe_text(entry.get("date")) != now.date().isoformat()]
    if offset == 0:
        history.append({
            "date": now.date().isoformat(), "day": "D+0",
            "close": None, "high": None, "low": None,
            "return_pct": None, "result": "listed",
        })
        reviewed = {
            **item,
            "last_reviewed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "review_day": "D+0",
            "review_result": "listed",
            "review_history": history[-12:],
        }
        return reviewed, [
            f"- {code} {name} | D+0 | 入{float(item.get('entry_price') or 0):.2f} "
            f"止损{float(item.get('stop_loss') or 0):.2f} "
            f"止盈{float(item.get('take_profit') or 0):.2f} | 待后续复盘"
        ]

    price = _series_float(row, "price") if row is not None else None
    if row is None or price is None:
        history.append({
            "date": now.date().isoformat(), "day": f"D+{offset}",
            "close": None, "high": None, "low": None,
            "return_pct": None, "result": "missing_quote",
        })
        reviewed = {
            **item,
            "last_reviewed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "review_day": f"D+{offset}",
            "review_result": "missing_quote",
            "review_history": history[-12:],
        }
        return reviewed, [f"- {code} {name} | D+{offset} | 行情缺失"]

    high = _series_float(row, "high") or price
    low = _series_float(row, "low") or price
    pct_chg = _float_value(row.get("pct_chg"))
    entry = float(item.get("entry_price") or 0)
    stop = float(item.get("stop_loss") or 0)
    take = float(item.get("take_profit") or 0)
    entered = bool(entry and high and high >= entry)
    active = True
    status = "继续观察"
    if entered and high and take and high >= take:
        status = "触及止盈"
        active = False
    elif entered and low and stop and low <= stop:
        status = "触及止损"
        active = False
    elif safe_text(row.get("signal_action")) in {"time_stop", "stop_loss", "take_profit"}:
        status = safe_text(row.get("signal_state")) or safe_text(row.get("signal_action"))
        active = False
    elif not entered:
        status = "未入场"
    review_return = (price - entry) / entry * 100.0 if price and entry and entered else None
    review_result = (
        "take_profit" if status == "触及止盈" else
        "stop_loss" if status == "触及止损" else
        "entered" if entered else "not_entered"
    )
    history.append({
        "date": now.date().isoformat(), "day": f"D+{offset}",
        "close": round(price, 2) if price else None,
        "high": round(high, 2) if high else None,
        "low": round(low, 2) if low else None,
        "return_pct": round(review_return, 2) if review_return is not None else None,
        "result": review_result,
    })
    reviewed = {
        **item,
        "last_reviewed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "active": active,
        "review_day": f"D+{offset}",
        "review_result": review_result,
        "review_return_pct": round(review_return, 2) if review_return is not None else None,
        "review_high": high,
        "review_low": low,
        "review_close": price,
        "review_history": history[-12:],
    }
    return reviewed, [
        f"- {code} {name} | D+{offset} | 入{entry:.2f} 高{high:.2f} 低{low:.2f} 收{price:.2f} "
        f"{pct_chg:+.2f}% | {status}"
    ]
```

Extract the existing batch metrics into `build_review_cohort_summary(reviewed) -> list[str]`. It must calculate the entered/take-profit/stop-loss counts, average maximum gain, average maximum drawdown, and the current mode/theme/market strategy-quality groups from the complete cohort (not from one chunk). Return no performance line for D+0. Repeat the complete-cohort summary in each D+N chunk so every standalone WeCom fragment has the same denominator.

- [ ] **Step 4: Wire the full spot frame and send every chunk**

Add a `review_quotes` parameter:

```python
def dispatch_notifications(
    cfg: Config,
    notifier: WeComNotifier,
    result: pd.DataFrame,
    market_info: dict[str, Any],
    market_news_state: str,
    watch_result: pd.DataFrame | None = None,
    review_quotes: pd.DataFrame | None = None,
    ai_overview: str = "",
) -> None:
```

At the scan call site pass the already loaded full market frame:

```python
            watch_result=watch_result,
            review_quotes=spot,
            ai_overview=ai_overview,
```

Replace the old single `review_md` send with:

```python
    if cfg.mode == "after" and review_quotes is not None:
        for suffix, review_md in build_watchlist_review_messages(
            review_quotes,
            SIGNAL_WATCHLIST_FILE,
            chunk_size=min(max(cfg.notify_top, 1), 6),
        ):
            notifier.send_markdown(
                notification_title(cfg.mode, "推送跟踪复盘"),
                review_md,
                dedupe_key=f"watch-review:{suffix}",
            )
```

- [ ] **Step 5: Run review, notification, and strategy tests**

Run:

```powershell
python -m unittest tests.test_signal_watchlist tests.test_alert_markdown tests.test_intraday_buy_state tests.test_theme_and_anchor -v
```

Expected: PASS; seven D+1 samples are present across three chunks, same-day buys are D+0, risk records are excluded, and missing quotes remain visible.

- [ ] **Step 6: Commit complete review behavior**

```powershell
git add a_share_strategy.py tests/test_signal_watchlist.py
git commit -m "feat: review complete buy-signal cohorts"
```

---

### Task 6: Documentation, Regression Verification, and Handoff

**Files:**
- Modify: `docs/project_roadmap.md`
- Modify: `docs/live_trading_execution_plan.md`
- Modify: `docs/data_storage_policy.md`
- Verify: all files changed in Tasks 1-5

**Interfaces:**
- Produces no new runtime API.
- Records `implemented / not deployed / not observed / not validated` accurately.

- [ ] **Step 1: Update active documentation**

Update the roadmap row for the专项 spec from `planned` to `implemented（仅本地）` only after all focused tests pass. Add these facts to the current notification/review sections:

```text
- 执行回报只由 SQLite 首次入账的新 fill 或 legacy 累计成交增量触发；周期快照不再重复推送历史成交。
- 盘后按 D+0/D+1/D+3/D+5/D+10 交易日批次完整复盘成功推送的买点，掉出候选池和行情缺失均不丢样本。
- 所有企业微信消息由统一通知出口增加服务器发送时间；动态时间不参与业务去重键。
- signal_watchlist.json 原子覆盖、热保留20个自然日、最多500条，目标低于1 MB。
- 当前仅为 implemented / not deployed / not observed / not validated。
```

- [ ] **Step 2: Run focused regression suites**

Run:

```powershell
python -m unittest tests.test_joinquant_sync tests.test_joinquant_signal_server tests.test_execution_ledger_integration tests.test_notifier_retry tests.test_signal_watchlist tests.test_config_env tests.test_reconciliation tests.test_trading_backup -v
```

Expected: PASS with zero failures and zero errors.

- [ ] **Step 3: Run syntax checks**

Run:

```powershell
python -m py_compile joinquant_sync.py joinquant_signal_server.py notifier.py a_share_strategy.py config.py
```

Expected: exit code 0 and no output.

- [ ] **Step 4: Run the full Windows-capable suite**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected in an environment with Bash: all 260 or more tests PASS. In the current Windows environment without Git Bash, only the three known `test_joinquant_linux_script` process-start errors may remain; no other failure or error is acceptable.

- [ ] **Step 5: Verify Git scope and document state**

Run:

```powershell
git diff --check
git status --short
git log --oneline --decorate -8
```

Expected: only the files listed in this plan are modified; no runtime data, secret, cache, output, database, or user worktree file is present.

- [ ] **Step 6: Commit documentation and final implementation state**

```powershell
git add docs/project_roadmap.md docs/live_trading_execution_plan.md docs/data_storage_policy.md
git commit -m "docs: record idempotent notifications and full reviews"
```

- [ ] **Step 7: Stop before external changes**

Report the branch, commit list, focused/full test evidence, and the known Bash environment limitation. Do not push, deploy, restart services, update JoinQuant, or mutate server runtime state. Await separate user authorization for each external action.

---

## Deployment and Observation Checklist (Separate Authorization Required)

After the implementation branch is reviewed and explicitly authorized for push/deployment:

1. Push and fast-forward the server to the approved commit without overwriting `stock-analysis.env` or `cache/`.
2. Run the full Linux suite and `bash run_ubuntu.sh ledger-check` before restarting services.
3. Restart only the affected strategy, JoinQuant signal API, and notification retry services through the existing deployment path.
4. Do not change the JoinQuant website template unless the implementation changed its contract; this design should not require a template edit.
5. On the first trading day, verify one real fill generates one report, repeated minute snapshots generate zero additional reports, D+1 count equals the previous trading day's successful buy alerts, and every WeCom message shows one server time.
6. Keep status `deployed` after installation, `observed` after real evidence, and `validated` only after three valid trading days meet the spec.
