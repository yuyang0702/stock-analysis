from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import config as app_config
from execution_contracts import BrokerSnapshot
from execution_state import classify_exit_execution
from order_ledger import fill_id, normalize_order
from trading_store import (
    TradingStore, canonical_json, order_allowed_quantity,
)


@dataclass
class ReconciliationDifference:
    category: str
    object_id: str
    reason_code: str
    local_value: str
    platform_value: str
    tolerance: float
    severity: str
    details: dict[str, Any]


@dataclass
class ReconciliationResult:
    reconciliation_id: str
    result: str
    severity: str
    differences: list[ReconciliationDifference]
    control_action: str
    snapshot_id: str | None
    transitions: list[dict[str, Any]] = field(default_factory=list)


_SEVERITY = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}
_STICKY_ISSUES = {"LEDGER_INTEGRITY_FAILURE", "IMMUTABLE_FILL_CONFLICT"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _qty(value: Any) -> int:
    return abs(int(_number(value)))


def _code(item: dict[str, Any]) -> str:
    return "".join(filter(str.isdigit, _text(item.get("code") or item.get("jq_code"))))[:6]


def _difference(
    category: str, object_id: str, reason: str, local: Any, platform: Any,
    tolerance: float, severity: str, **details: Any,
) -> ReconciliationDifference:
    return ReconciliationDifference(
        category, object_id, reason, _text(local), _text(platform), tolerance, severity, details,
    )


def reconcile_snapshot(
    store: TradingStore, conn: Any, payload: dict[str, Any], *, snapshot_id: str,
    broker_snapshot: BrokerSnapshot, mode: str, now: str,
) -> ReconciliationResult:
    broker_snapshot = BrokerSnapshot.from_dict(broker_snapshot.to_dict())
    stable_payload = dict(payload)
    stable_payload.pop("received_at", None)
    payload_snapshot_id = hashlib.sha256(
        canonical_json(stable_payload).encode("utf-8")
    ).hexdigest()[:32]
    if broker_snapshot.snapshot_id != f"joinquant-{payload_snapshot_id}":
        raise ValueError(
            "reconciliation broker snapshot is not bound to payload"
        )
    current = store.load_current_broker_snapshot(
        conn, broker_snapshot.account_scope_id,
    )
    if current is None or (
        current.snapshot_id != broker_snapshot.snapshot_id
        or current.snapshot_sha256 != broker_snapshot.snapshot_sha256
        or current.broker_time != broker_snapshot.broker_time
        or current.generated_at != broker_snapshot.generated_at
    ):
        raise ValueError(
            "reconciliation broker snapshot does not match current evidence"
        )
    raw_id = canonical_json({
        "account_scope_id": broker_snapshot.account_scope_id,
        "broker_snapshot_id": broker_snapshot.snapshot_id,
        "broker_snapshot_sha256": broker_snapshot.snapshot_sha256,
        "mode": mode,
    })
    reconciliation_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]
    existing = conn.execute(
        "SELECT * FROM reconciliation_runs WHERE reconciliation_id=?",
        (reconciliation_id,),
    ).fetchone()
    observation_now = str(existing["started_at"]) if existing is not None else now
    differences: list[ReconciliationDifference] = []
    account = conn.execute(
        "SELECT trade_date, cash, available_cash, total_value FROM account_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if account is None:
        differences.append(_difference(
            "ledger", snapshot_id, "LEDGER_INTEGRITY_FAILURE", "missing", "snapshot", 0,
            "CRITICAL",
        ))
    else:
        for column in ("cash", "available_cash", "total_value"):
            local = float(account[column])
            platform = _number(payload.get(column))
            if abs(local - platform) > 0.01:
                differences.append(_difference(
                    "account", column, "ACCOUNT_BALANCE_MISMATCH", local, platform, 0.01, "ERROR",
                ))

    platform_positions = {
        _code(item): item for item in payload.get("positions", [])
        if isinstance(item, dict) and _code(item)
    }
    local_positions = {
        str(row["stock_code"]): row for row in conn.execute(
            "SELECT * FROM position_snapshots WHERE snapshot_id=?", (snapshot_id,)
        )
    }
    if not local_positions and platform_positions:
        local_positions = {
            str(row["stock_code"]): row for row in conn.execute(
                """SELECT p.* FROM position_snapshots p
                   WHERE p.snapshot_id=(
                       SELECT snapshot_id FROM account_snapshots
                       WHERE retained_details=1
                       AND generated_at<=(SELECT generated_at FROM account_snapshots WHERE snapshot_id=?)
                       ORDER BY generated_at DESC LIMIT 1
                   )""",
                (snapshot_id,),
            )
        }
    for code in sorted(set(local_positions) | set(platform_positions)):
        local = local_positions.get(code)
        platform = platform_positions.get(code)
        local_qty = int(local["qty"]) if local is not None else 0
        platform_qty = _qty(platform.get("qty")) if platform else 0
        if local_qty != platform_qty:
            differences.append(_difference(
                "position", code, "POSITION_QTY_MISMATCH", local_qty, platform_qty, 0, "ERROR",
            ))
        local_sellable = int(local["closeable_qty"]) if local is not None else 0
        platform_sellable = _qty(platform.get("closeable_amount")) if platform else 0
        local_locked = int(local["locked_qty"]) if local is not None else 0
        platform_locked = _qty(platform.get("locked_amount")) if platform else 0
        if local_sellable != platform_sellable or local_locked != platform_locked:
            differences.append(_difference(
                "position", code, "POSITION_SELLABLE_MISMATCH",
                f"{local_sellable}/{local_locked}", f"{platform_sellable}/{platform_locked}",
                0, "WARNING",
            ))

    local_orders = {
        str(row["order_id"]): row for row in conn.execute("SELECT * FROM orders WHERE order_id IS NOT NULL")
    }
    execution_orders = [dict(row) for row in conn.execute("SELECT * FROM orders")]
    execution_orders.extend(dict(row) for row in conn.execute("SELECT * FROM order_events"))
    platform_orders = {
        _text(item.get("order_id")): item for item in payload.get("orders", [])
        if isinstance(item, dict) and _text(item.get("order_id"))
    }
    trade_date = _text(payload.get("trade_date"))
    strategy_version = _text(
        payload.get("strategy_template_version")
        or payload.get("template_version")
        or payload.get("strategy_version")
    )
    platform_fill_qty: dict[str, int] = {}
    for trade in payload.get("trades", []):
        if isinstance(trade, dict):
            order_id = _text(trade.get("order_id"))
            platform_fill_qty[order_id] = platform_fill_qty.get(order_id, 0) + _qty(
                trade.get("amount") or trade.get("qty")
            )
    for order_id in sorted(set(local_orders) | set(platform_orders)):
        local = local_orders.get(order_id)
        platform = platform_orders.get(order_id)
        if local is None:
            differences.append(_difference(
                "order", order_id, "ORDER_MISSING_LOCAL", "missing", "present", 0, "ERROR",
            ))
        elif platform is None and str(local["status"]) not in {
            "filled", "cancelled", "rejected", "failed", "skipped", "risk_rejected",
        }:
            differences.append(_difference(
                "order", order_id, "ORDER_MISSING_PLATFORM", "present", "missing", 0, "ERROR",
            ))
        if platform is not None:
            normalized = normalize_order(
                platform, trade_date=trade_date,
                strategy_version=strategy_version,
            )
            reported = int(normalized["filled_qty"])
            summed = platform_fill_qty.get(order_id, 0)
            if reported != summed:
                differences.append(_difference(
                    "order", order_id, "ORDER_FILL_QTY_MISMATCH", reported, summed, 0, "ERROR",
                ))
            if local is not None:
                comparisons = (
                    (
                        "ORDER_CODE_MISMATCH", str(local["stock_code"]),
                        str(normalized["stock_code"]),
                    ),
                    (
                        "ORDER_SIDE_MISMATCH", str(local["action"]).lower(),
                        str(normalized["action"]).lower(),
                    ),
                    (
                        "ORDER_QTY_MISMATCH",
                        order_allowed_quantity(
                            local["requested_qty"], local["target_qty"],
                        ),
                        order_allowed_quantity(
                            normalized["requested_qty"], normalized["target_qty"],
                        ),
                    ),
                    (
                        "ORDER_FILLED_QTY_MISMATCH", int(local["filled_qty"]),
                        reported,
                    ),
                    (
                        "ORDER_STATUS_MISMATCH", str(local["status"]).lower(),
                        str(normalized["status"]).lower(),
                    ),
                )
                for reason_code, local_value, platform_value in comparisons:
                    if local_value != platform_value:
                        differences.append(_difference(
                            "order", order_id, reason_code,
                            local_value, platform_value, 0, "ERROR",
                        ))

    local_fills = {
        str(row["fill_id"]): row for row in conn.execute(
            "SELECT * FROM fills WHERE substr(filled_at, 1, 10)=?",
            (str(account["trade_date"]),),
        )
    } if account is not None else {}
    platform_fill_ids: set[str] = set()
    for trade in payload.get("trades", []):
        if not isinstance(trade, dict):
            continue
        trade_id = fill_id(trade)
        platform_fill_ids.add(trade_id)
        local = local_fills.get(trade_id)
        if local is None:
            differences.append(_difference(
                "fill", trade_id, "FILL_MISSING_LOCAL", "missing", "present", 0, "ERROR",
            ))
        elif (
            int(local["qty"]) != _qty(trade.get("amount") or trade.get("qty"))
            or abs(float(local["price"]) - _number(trade.get("price"))) > 0.000001
            or str(local["order_id"] or "") != _text(trade.get("order_id"))
        ):
            differences.append(_difference(
                "fill", trade_id, "IMMUTABLE_FILL_CONFLICT",
                f"{local['order_id']}|{local['qty']}|{local['price']}",
                f"{trade.get('order_id')}|{_qty(trade.get('amount') or trade.get('qty'))}|{_number(trade.get('price'))}",
                0, "CRITICAL",
            ))
        signal_id = _text(trade.get("signal_id")) or (
            _text(local["signal_id"]) if local is not None else ""
        )
        if not signal_id:
            differences.append(_difference(
                "fill", trade_id, "MANUAL_TRADE", "no_signal", "platform_trade", 0, "WARNING",
            ))
    if mode == "full":
        for trade_id in sorted(set(local_fills) - platform_fill_ids):
            differences.append(_difference(
                "fill", trade_id, "FILL_MISSING_PLATFORM", "present", "missing", 0, "WARNING",
            ))

    quantities = {code: _qty(item.get("qty")) for code, item in platform_positions.items()}
    for intent in conn.execute("SELECT * FROM exit_intents WHERE status='active'"):
        code = str(intent["stock_code"])
        execution = classify_exit_execution(
            dict(intent), execution_orders, quantities.get(code, 0),
            observation_now, set(app_config.A_SHARE_HOLIDAYS_DEFAULT),
        )
        if not execution["complete"]:
            differences.append(_difference(
                "exit_intent", str(intent["signal_id"]), str(execution["state"]),
                intent["target_qty"], quantities.get(code, 0), 0,
                str(execution["severity"]), stock_code=code,
                stage_started_at=execution["stage_started_at"],
                age_minutes=execution["age_minutes"],
                filled_qty=execution.get("filled_qty", 0),
            ))

    severity = max((item.severity for item in differences), key=lambda item: _SEVERITY[item], default="INFO")
    result_text = "matched" if not differences else "mismatch"
    result = ReconciliationResult(
        reconciliation_id, result_text, severity, differences, "", snapshot_id,
    )
    difference_records = [
        {
            "category": item.category,
            "object_id": item.object_id,
            "reason_code": item.reason_code,
            "local_value": item.local_value,
            "platform_value": item.platform_value,
            "tolerance": item.tolerance,
            "severity": item.severity,
            "details": item.details,
        }
        for item in differences
    ]
    differences_json = canonical_json(sorted(
        difference_records, key=canonical_json,
    ))
    summary_json = canonical_json({
        "counts": {
            item.reason_code: sum(
                1 for candidate in differences
                if candidate.reason_code == item.reason_code
            )
            for item in differences
        },
        "differences_sha256": hashlib.sha256(
            differences_json.encode("utf-8")
        ).hexdigest(),
    })
    evidence = (
        mode, snapshot_id, broker_snapshot.account_scope_id,
        broker_snapshot.snapshot_id, broker_snapshot.snapshot_sha256,
        broker_snapshot.broker_time, broker_snapshot.generated_at,
        result_text, severity, len(differences), summary_json,
    )
    if existing is not None:
        columns = (
            "mode", "snapshot_id", "account_scope_id", "broker_snapshot_id",
            "broker_snapshot_sha256", "snapshot_broker_time",
            "snapshot_generated_at", "result", "severity",
            "difference_count", "summary_json",
        )
        if tuple(existing[column] for column in columns) != evidence:
            raise ValueError("immutable reconciliation evidence conflict")
        result.control_action = str(existing["control_action"] or "")
        return result
    conn.execute(
        """INSERT INTO reconciliation_runs(
           reconciliation_id, mode, snapshot_id, started_at, finished_at,
           result, severity, difference_count, control_action, summary_json,
           account_scope_id, broker_snapshot_id, broker_snapshot_sha256,
           snapshot_broker_time, snapshot_generated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reconciliation_id, mode, snapshot_id, observation_now,
            observation_now, result_text,
            severity, len(differences), "", summary_json,
            broker_snapshot.account_scope_id, broker_snapshot.snapshot_id,
            broker_snapshot.snapshot_sha256, broker_snapshot.broker_time,
            broker_snapshot.generated_at,
        ),
    )
    for item in differences:
        conn.execute(
            """INSERT INTO reconciliation_items(
               reconciliation_id, category, object_id, reason_code, local_value, platform_value,
               tolerance, severity, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reconciliation_id, item.category, item.object_id, item.reason_code,
                item.local_value, item.platform_value, item.tolerance, item.severity,
                canonical_json(item.details),
            ),
        )
    return result


def persist_issue_transitions(
    store: TradingStore, conn: Any, result: ReconciliationResult, now: str
) -> list[dict[str, Any]]:
    scope_row = conn.execute(
        """SELECT account_scope_id FROM reconciliation_runs
           WHERE reconciliation_id=?""",
        (result.reconciliation_id,),
    ).fetchone()
    account_scope_id = (
        str(scope_row["account_scope_id"] or "").strip()
        if scope_row is not None
        else ""
    )
    scope_prefix = f"scope:{account_scope_id}:" if account_scope_id else ""
    transitions: list[dict[str, Any]] = []
    active_keys: set[str] = set()
    by_key: dict[str, ReconciliationDifference] = {}
    for item in result.differences:
        key = f"{scope_prefix}{item.category}:{item.object_id}"
        current = by_key.get(key)
        if current is None or (
            _SEVERITY.get(item.severity, 0), item.reason_code
        ) > (
            _SEVERITY.get(current.severity, 0), current.reason_code
        ):
            by_key[key] = item
    for key in sorted(by_key):
        item = by_key[key]
        active_keys.add(key)
        previous = conn.execute(
            """SELECT state, recovered_at FROM execution_issue_state
               WHERE issue_key=?""", (key,)
        ).fetchone()
        if (
            previous is not None and previous["recovered_at"] is None
            and str(previous["state"]) in _STICKY_ISSUES
            and item.reason_code not in _STICKY_ISSUES
        ):
            continue
        changed = store.upsert_execution_issue(conn, {
            "issue_key": key, "object_type": item.category, "object_id": item.object_id,
            "state": item.reason_code, "severity": item.severity,
            "stage_started_at": str(item.details.get("stage_started_at") or now),
            "seen_at": now,
            "signal_id": item.object_id if item.category == "exit_intent" else "",
            "order_id": item.object_id if item.category == "order" else "",
            "reconciliation_id": result.reconciliation_id,
            "details": item.details,
        })
        if changed["transitioned"]:
            changed["transition"] = "OPENED" if not changed["previous_state"] else "CHANGED"
            transitions.append(changed)
        elif item.severity == "ERROR":
            notified = str(changed.get("last_notified_at") or "")
            reminder_base = notified or str(changed.get("last_transition_at") or "")
            if reminder_base and (
                datetime.fromisoformat(now) - datetime.fromisoformat(reminder_base)
            ).total_seconds() >= 1800:
                changed["transition"] = "REMINDER"
                transitions.append(changed)
    if scope_prefix:
        rows = conn.execute(
            """SELECT issue_key, state FROM execution_issue_state
               WHERE recovered_at IS NULL
                 AND substr(issue_key, 1, ?) = ?""",
            (len(scope_prefix), scope_prefix),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT issue_key, state FROM execution_issue_state
               WHERE recovered_at IS NULL
                 AND issue_key NOT LIKE 'scope:%'"""
        ).fetchall()
    for row in rows:
        key, state = str(row[0]), str(row[1])
        if key in active_keys or state in _STICKY_ISSUES:
            continue
        recovered = store.recover_execution_issue(conn, key, now)
        if recovered:
            recovered["transition"] = "RECOVERED"
            transitions.append(recovered)
    result.transitions = transitions
    return transitions


def build_reconciliation_markdown(
    result: ReconciliationResult, controls: dict[str, str]
) -> str:
    lines = [
        "#### JoinQuant 自动对账告警",
        f"> 对账编号：{result.reconciliation_id}",
        f"> 结果：{result.result} | 严重度：{result.severity} | 差异：{len(result.differences)}",
        f"> 控制：buy_enabled={controls.get('buy_enabled', '1')} | kill_switch={controls.get('kill_switch', '0')}",
    ]
    for item in result.differences[:8]:
        lines.append(f"- {item.reason_code} | {item.category}:{item.object_id}")
    for transition in result.transitions[:8]:
        if transition.get("state") == "RECOVERED":
            lines.append(f"- RECOVERED | {transition.get('issue_key')}")
    if len(result.differences) > 8:
        lines.append(f"> 另有 {len(result.differences) - 8} 项差异未展开")
    lines.extend((
        "```text",
        "bash run_ubuntu.sh trading-status",
        "bash run_ubuntu.sh reconcile",
        "bash run_ubuntu.sh unlock",
        "```",
    ))
    return "\n".join(lines)


def notify_reconciliation(
    result: ReconciliationResult, controls: dict[str, str], *, notifier: Any,
    store: TradingStore | None = None, now: str | None = None,
) -> bool:
    if not result.differences and not result.transitions:
        return False
    if result.transitions:
        visible = [
            item for item in result.transitions
            if item.get("state") == "RECOVERED"
            or item.get("severity") in {"WARNING", "ERROR", "CRITICAL"}
        ]
        if not visible:
            return False
        transition = visible[0]
        sent = bool(notifier.send_markdown(
            "JoinQuant 自动对账状态变化",
            build_reconciliation_markdown(result, controls),
            dedupe_key=(
                f"reconciliation-transition:{transition.get('issue_key')}:"
                f"{transition.get('state')}:{transition.get('severity')}:"
                f"{transition.get('transition')}"
            ),
        ))
        if sent and store is not None:
            notified_at = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with store.transaction() as conn:
                store.mark_execution_issues_notified(
                    conn, [str(item.get("issue_key") or "") for item in visible], notified_at
                )
        return sent
    primary = max(
        result.differences, key=lambda item: _SEVERITY.get(item.severity, 0)
    )
    control_state = f"{controls.get('buy_enabled', '1')}/{controls.get('kill_switch', '0')}"
    return bool(notifier.send_markdown(
        "JoinQuant 自动对账告警",
        build_reconciliation_markdown(result, controls),
        dedupe_key=f"reconciliation:{primary.reason_code}:{primary.object_id}:{control_state}",
    ))
