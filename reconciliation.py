from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from order_ledger import fill_id
from trading_store import TradingStore, canonical_json


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


_SEVERITY = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}


def _text(value: Any) -> str:
    return str(value or "").strip()


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
    mode: str, now: str,
) -> ReconciliationResult:
    del store
    differences: list[ReconciliationDifference] = []
    account = conn.execute(
        "SELECT cash, available_cash, total_value FROM account_snapshots WHERE snapshot_id=?",
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
    platform_orders = {
        _text(item.get("order_id")): item for item in payload.get("orders", [])
        if isinstance(item, dict) and _text(item.get("order_id"))
    }
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
        elif platform is None and str(local["status"]) not in {"filled", "cancelled", "rejected", "risk_rejected"}:
            differences.append(_difference(
                "order", order_id, "ORDER_MISSING_PLATFORM", "present", "missing", 0, "ERROR",
            ))
        if platform is not None:
            reported = _qty(platform.get("filled") or platform.get("filled_qty"))
            summed = platform_fill_qty.get(order_id, 0)
            if reported != summed:
                differences.append(_difference(
                    "order", order_id, "ORDER_FILL_QTY_MISMATCH", reported, summed, 0, "ERROR",
                ))

    local_fills = {str(row["fill_id"]): row for row in conn.execute("SELECT * FROM fills")}
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
        if quantities.get(code, 0) > int(intent["target_qty"]):
            differences.append(_difference(
                "exit_intent", str(intent["signal_id"]), "EXIT_INTENT_MISMATCH",
                intent["target_qty"], quantities.get(code, 0), 0, "ERROR", stock_code=code,
            ))

    severity = max((item.severity for item in differences), key=lambda item: _SEVERITY[item], default="INFO")
    result_text = "matched" if not differences else "mismatch"
    raw_id = canonical_json({"snapshot_id": snapshot_id, "mode": mode})
    reconciliation_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]
    result = ReconciliationResult(
        reconciliation_id, result_text, severity, differences, "", snapshot_id,
    )
    conn.execute(
        """INSERT OR REPLACE INTO reconciliation_runs(
           reconciliation_id, mode, snapshot_id, started_at, finished_at, result, severity,
           difference_count, control_action, summary_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reconciliation_id, mode, snapshot_id, now, now, result_text, severity, len(differences), "",
            canonical_json({"counts": {item.reason_code: sum(
                1 for candidate in differences if candidate.reason_code == item.reason_code
            ) for item in differences}}),
        ),
    )
    conn.execute("DELETE FROM reconciliation_items WHERE reconciliation_id=?", (reconciliation_id,))
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
    result: ReconciliationResult, controls: dict[str, str], *, notifier: Any
) -> bool:
    if not result.differences:
        return False
    primary = max(
        result.differences, key=lambda item: _SEVERITY.get(item.severity, 0)
    )
    control_state = f"{controls.get('buy_enabled', '1')}/{controls.get('kill_switch', '0')}"
    return bool(notifier.send_markdown(
        "JoinQuant 自动对账告警",
        build_reconciliation_markdown(result, controls),
        dedupe_key=f"reconciliation:{primary.reason_code}:{primary.object_id}:{control_state}",
    ))
