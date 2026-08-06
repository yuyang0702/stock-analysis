"""Atomic admission from a strategy candidate to one exact READY order."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from functools import wraps
from typing import Mapping

from execution_contracts import (
    BrokerSnapshot,
    ExecutionIntent,
    InstrumentRules,
    PreTradeResult,
    QuoteSnapshot,
    StrategyOrderCandidate,
    canonical_json,
    client_order_id,
)
from exit_policy import PositionExitState, resolve_effective_stop
from notification_outbox import NotificationEvent, notification_event_key
from pre_trade_check import (
    AdoptedPositionCapacityEvidence,
    CapacityReservationEvidence,
    PositionCapacityEvidence,
    ReservationView,
    RiskPolicy,
    pre_trade_check,
)
from trading_store import TradingStore


ZERO = Decimal("0")
DECIMAL_CONTEXT = Context(prec=50)


def _enqueue_expired_order_notification(
    store: TradingStore,
    conn: object,
    intent: ExecutionIntent,
    occurred_at: str,
) -> None:
    scope = conn.execute(
        "SELECT adapter FROM account_scopes WHERE account_scope_id=?",
        (intent.account_scope_id,),
    ).fetchone()
    if scope is None:
        raise ValueError("execution intent account scope is not registered")
    adapter = str(scope["adapter"])
    event = NotificationEvent(
        event_key=notification_event_key(
            adapter,
            intent.account_scope_id,
            "order-terminal",
            client_order_id=intent.client_order_id,
            status="EXPIRED",
            reason_code="INTENT_EXPIRED",
        ),
        account_scope_id=intent.account_scope_id,
        adapter=adapter,
        event_type="order_terminal",
        object_type="order",
        object_id=intent.client_order_id,
        source_fact_id=intent.client_order_id,
        priority="high",
        payload_version=1,
        occurred_at=occurred_at,
        expires_at=None,
        title=f"{adapter.upper()} 订单终态",
        body=(
            f"> buy {intent.code} | EXPIRED | INTENT_EXPIRED\n"
            f"> 委托 {intent.order_qty}股 | 成交 0股\n"
            f"> 业务时间：{occurred_at}"
        ),
        payload={
            "client_order_id": intent.client_order_id,
            "stock_code": intent.code,
            "action": "buy",
            "status": "EXPIRED",
            "reason_code": "INTENT_EXPIRED",
            "requested_qty": intent.order_qty,
            "filled_qty": 0,
        },
        metadata={"renderer": "order-terminal-v1"},
    )
    store.enqueue_notification_or_gap(conn, event, occurred_at)


def _fixed_decimal_context(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext(DECIMAL_CONTEXT):
            return function(*args, **kwargs)

    return wrapped


@dataclass(frozen=True)
class AdmissionRequest:
    candidate: StrategyOrderCandidate
    quote: QuoteSnapshot | None
    instrument_rules: InstrumentRules | None
    risk_policy: RiskPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, StrategyOrderCandidate):
            raise ValueError("candidate must be StrategyOrderCandidate")
        if self.quote is not None and not isinstance(self.quote, QuoteSnapshot):
            raise ValueError("quote must be QuoteSnapshot or None")
        if self.instrument_rules is not None and not isinstance(
            self.instrument_rules, InstrumentRules
        ):
            raise ValueError(
                "instrument_rules must be InstrumentRules or None"
            )
        if not isinstance(self.risk_policy, RiskPolicy):
            raise ValueError("risk_policy must be RiskPolicy")


@dataclass(frozen=True)
class AdmissionResult:
    pre_trade_result: PreTradeResult
    execution_intent: ExecutionIntent | None = None
    reservation_id: str | None = None
    replayed: bool = False

    @property
    def allowed(self) -> bool:
        return self.pre_trade_result.allowed

    @property
    def client_order_id(self) -> str:
        return (
            self.execution_intent.client_order_id
            if self.execution_intent is not None
            else ""
        )


def _instant(value: object, name: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _normalized_timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat()


def _system_state(conn: object) -> dict[str, str]:
    keys = ("buy_enabled", "sell_enabled", "kill_switch", "market_regime")
    rows = conn.execute(
        "SELECT key, value FROM system_state WHERE key IN (?,?,?,?)", keys,
    ).fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _reservation_evidence(
    row: Mapping[str, object],
    intent: ExecutionIntent,
    status: str,
) -> CapacityReservationEvidence:
    normalized_status = {
        "partial": "partially_filled",
        "risk_rejected": "rejected",
        "failed": "rejected",
        "skipped": "rejected",
    }.get(str(status).strip().lower(), str(status).strip().lower())
    remaining = CapacityReservationEvidence(
        intent=intent,
        status=normalized_status,
        remaining_qty=int(row["remaining_target_qty"]),
    )
    original = CapacityReservationEvidence(
        intent=intent,
        status=normalized_status,
        remaining_qty=intent.order_qty,
    )
    candidate = intent.pre_trade_result.candidate
    if (
        str(row["account_scope_id"]) != intent.account_scope_id
        or str(row["client_order_id"]) != intent.client_order_id
        or str(row["stock_code"]) != intent.code
        or str(row["side"]).lower() != intent.side
        or int(row["target_qty"]) != intent.order_qty
        or str(row["industry"]) != candidate.industry
        or str(row["theme"]) != candidate.theme
        or int(row["uncategorized"]) not in {0, 1}
        or int(row["uncategorized"]) != int(candidate.uncategorized)
        or str(row["status"]) not in {"active", "released"}
    ):
        raise ValueError("capacity reservation does not match signed intent")
    stored = tuple(
        Decimal(str(row[name]))
        for name in (
            "cash_yuan", "position_value_yuan", "open_risk_yuan",
            "remaining_cash_yuan", "remaining_position_value_yuan",
            "remaining_open_risk_yuan",
        )
    )
    if any(not value.is_finite() or value < ZERO for value in stored) or stored != (
        original.cash_yuan,
        original.position_value_yuan,
        original.open_risk_yuan,
        remaining.cash_yuan,
        remaining.position_value_yuan,
        remaining.open_risk_yuan,
    ):
        raise ValueError("capacity reservation amounts conflict with signed intent")
    return remaining


def _active_reservations(
    conn: object,
    account_scope_id: str,
) -> tuple[CapacityReservationEvidence, ...]:
    rows = conn.execute(
        """SELECT r.*, i.status AS intent_status, i.intent_sha256,
                  i.payload_json AS intent_payload
           FROM capacity_reservations AS r
           JOIN execution_intents AS i
             ON i.account_scope_id=r.account_scope_id
            AND i.client_order_id=r.client_order_id
           WHERE r.account_scope_id=? AND r.status='active'
           ORDER BY r.reservation_id""",
        (account_scope_id,),
    ).fetchall()
    broker_status = {
        str(row["client_order_id"]): str(row["status"]).lower()
        for row in conn.execute(
            """SELECT client_order_id, status FROM broker_order_current
               WHERE account_scope_id=?""",
            (account_scope_id,),
        ).fetchall()
    }
    evidence: list[CapacityReservationEvidence] = []
    for row in rows:
        try:
            intent = ExecutionIntent.from_dict(
                json.loads(str(row["intent_payload"]))
            )
        except Exception as exc:
            raise ValueError("active reservation intent payload is invalid") from exc
        if (
            intent.intent_sha256 != str(row["intent_sha256"])
            or intent.account_scope_id != account_scope_id
            or intent.client_order_id != str(row["client_order_id"])
            or intent.code != str(row["stock_code"])
            or intent.side != str(row["side"])
            or intent.order_qty != int(row["target_qty"])
        ):
            raise ValueError("active reservation does not match signed intent")
        item = _reservation_evidence(
            row,
            intent,
            broker_status.get(
                intent.client_order_id, str(row["intent_status"]).lower()
            ),
        )
        evidence.append(item)
    return tuple(evidence)


def _effective_stop(cycle: Mapping[str, object], decision_batch_at: str) -> Decimal:
    state = PositionExitState(
        code=str(cycle["stock_code"]),
        mode=str(cycle["mode"]),
        initial_qty=int(cycle["initial_qty"]),
        current_qty=int(cycle["current_qty"]),
        entry_price=float(cycle["entry_price"]),
        initial_stop_price=float(cycle["initial_stop_price"]),
        highest_price=float(cycle["highest_price"]),
        atr14=float(cycle["atr14"]),
        take_profit_stage=int(cycle["take_profit_stage"]),
        holding_trade_days=0,
        manual_stop_price=float(cycle["manual_stop_price"] or 0),
        profit_protection_activated_at=str(
            cycle["profit_protection_activated_at"] or ""
        ),
        trailing_stop_active_from=str(cycle["trailing_stop_active_from"] or ""),
        decision_batch_at=decision_batch_at,
    )
    return Decimal(str(
        resolve_effective_stop(state, str(cycle["market_state"]))
        .effective_stop_price
    ))


def _position_evidence(
    store: TradingStore,
    conn: object,
    broker_snapshot: BrokerSnapshot,
    decision_batch_at: str,
) -> tuple[
    tuple[PositionCapacityEvidence | AdoptedPositionCapacityEvidence, ...],
    bool,
]:
    adopted = {
        evidence.position_cycle_id: evidence
        for payload in store.list_position_capacity_adoptions(
            conn, broker_snapshot.account_scope_id,
        )
        for evidence in (AdoptedPositionCapacityEvidence.from_dict(payload),)
    }
    result: list[
        PositionCapacityEvidence | AdoptedPositionCapacityEvidence
    ] = []
    complete = True
    for position in broker_snapshot.positions:
        if position.total_qty <= 0:
            continue
        cycles = conn.execute(
            """SELECT * FROM position_cycles
               WHERE stock_code=? AND status='active'""",
            (position.code,),
        ).fetchall()
        if len(cycles) != 1:
            complete = False
            continue
        cycle = cycles[0]
        if int(cycle["current_qty"]) != position.total_qty:
            complete = False
            continue
        signed_rows = conn.execute(
            """SELECT i.payload_json, i.intent_sha256, i.status,
                      o.status AS order_status, o.filled_qty
               FROM orders AS o
               JOIN execution_intents AS i
                 ON i.client_order_id=o.client_order_id
               WHERE i.account_scope_id=? AND o.signal_id=?""",
            (
                broker_snapshot.account_scope_id,
                cycle["entry_signal_id"],
            ),
        ).fetchall() if cycle["entry_signal_id"] else []
        if len(signed_rows) == 1:
            row = signed_rows[0]
            try:
                intent = ExecutionIntent.from_dict(
                    json.loads(str(row["payload_json"]))
                )
            except Exception as exc:
                raise ValueError("position entry intent payload is invalid") from exc
            intent_status = str(row["status"]).upper()
            order_status = str(row["order_status"]).lower()
            filled_qty = int(row["filled_qty"] or 0)
            if (
                intent.intent_sha256 != str(row["intent_sha256"])
                or intent.account_scope_id != broker_snapshot.account_scope_id
                or intent.adapter != broker_snapshot.adapter
                or intent.code != position.code
                or intent_status not in {"FILLED", "CANCELLED"}
                or order_status != intent_status.lower()
                or filled_qty <= 0
                or filled_qty > intent.order_qty
                or position.total_qty > filled_qty
            ):
                complete = False
                continue
            result.append(PositionCapacityEvidence(
                position_cycle_id=str(cycle["position_cycle_id"]),
                entry_intent=intent,
                effective_stop_price=_effective_stop(cycle, decision_batch_at),
            ))
            continue
        if len(signed_rows) > 1:
            complete = False
            continue
        legacy = adopted.get(str(cycle["position_cycle_id"]))
        if (
            legacy is None
            or legacy.code != position.code
            or position.total_qty > legacy.initial_qty
        ):
            complete = False
            continue
        result.append(legacy)
    return tuple(result), complete


def _date_bounds(trade_date: str) -> tuple[str, str]:
    start = datetime.strptime(trade_date, "%Y-%m-%d").date()
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _daily_order_ids(
    conn: object,
    trade_date: str,
    account_scope_id: str,
    adapter: str,
) -> set[str]:
    start, end = _date_bounds(trade_date)
    ids = {
        str(row["client_order_id"])
        for row in conn.execute(
            """SELECT client_order_id FROM capacity_reservations
               WHERE account_scope_id=? AND created_at>=? AND created_at<?""",
            (account_scope_id, start, end),
        ).fetchall()
    }
    if adapter == "joinquant":
        ids.update(
            str(row["client_order_id"])
            for row in conn.execute(
                """SELECT o.client_order_id FROM orders AS o
                   WHERE (
                       (o.first_submitted_at>=? AND o.first_submitted_at<?)
                       OR (o.first_submitted_at IS NULL
                           AND o.updated_at>=? AND o.updated_at<?)
                   ) AND NOT EXISTS(
                       SELECT 1 FROM capacity_reservations AS r
                       WHERE r.client_order_id=o.client_order_id
                   )""",
                (start, end, start, end),
            ).fetchall()
        )
    return ids


def _daily_new_positions(
    conn: object,
    trade_date: str,
    broker_snapshot: BrokerSnapshot,
    reservations: tuple[CapacityReservationEvidence, ...],
) -> int:
    start, end = _date_bounds(trade_date)
    codes = {
        str(row["stock_code"])
        for row in conn.execute(
            """SELECT r.stock_code FROM capacity_reservations AS r
               JOIN execution_intents AS i
                 ON i.account_scope_id=r.account_scope_id
                AND i.client_order_id=r.client_order_id
               WHERE r.account_scope_id=? AND r.side='buy'
                 AND r.created_at>=? AND r.created_at<? AND i.status='FILLED'""",
            (broker_snapshot.account_scope_id, start, end),
        ).fetchall()
    }
    if broker_snapshot.adapter == "joinquant":
        codes.update(
            str(row["stock_code"])
            for row in conn.execute(
                """SELECT stock_code FROM position_cycles
                   WHERE opened_at>=? AND opened_at<?""",
                (start, end),
            ).fetchall()
        )
    held = {
        item.code for item in broker_snapshot.positions if item.total_qty > 0
    }
    for item in reservations:
        if item.side == "buy" and item.status not in {
            "expired", "not_submitted", "rejected", "cancelled"
        } and item.code not in held:
            codes.add(item.code)
    return len(codes)


def _reservation_turnover(
    reservations: tuple[CapacityReservationEvidence, ...],
) -> Decimal:
    return sum((
        item._remaining_amount(
            item.intent.pre_trade_result.execution_fee.notional_yuan
        )
        for item in reservations
        if item.status in {
            "ready", "submitting", "submit_unknown", "submitted",
            "partially_filled", "pending_cancel",
        }
        and item.intent.pre_trade_result.execution_fee is not None
    ), ZERO)


def _reservation_view(
    store: TradingStore,
    conn: object,
    broker_snapshot: BrokerSnapshot,
    candidate: StrategyOrderCandidate,
    decision_batch_at: str,
) -> ReservationView:
    reservations = _active_reservations(
        conn, broker_snapshot.account_scope_id,
    )
    positions, position_complete = _position_evidence(
        store, conn, broker_snapshot, decision_batch_at,
    )
    exit_rows = conn.execute(
        """SELECT stock_code, signal_id, target_qty FROM exit_intents
           WHERE status='active'"""
    ).fetchall()
    exits = {
        str(row["stock_code"]): str(row["signal_id"])
        for row in exit_rows
    }
    trade_date = broker_snapshot.trade_date
    reserved_turnover = _reservation_turnover(reservations)
    logical_signal_ids = {
        item.intent.logical_signal_id for item in reservations
    }
    prior_attempts = conn.execute(
        """SELECT i.status AS intent_status, r.status AS reservation_status
           FROM strategy_order_candidates AS c
           JOIN pre_trade_results AS p
             ON p.account_scope_id=c.account_scope_id
            AND p.candidate_id=c.candidate_id
           JOIN execution_intents AS i
             ON i.account_scope_id=p.account_scope_id
            AND i.pre_trade_result_id=p.pre_trade_result_id
           JOIN capacity_reservations AS r
             ON r.account_scope_id=i.account_scope_id
            AND r.client_order_id=i.client_order_id
           WHERE c.account_scope_id=? AND c.logical_signal_id=?
           ORDER BY i.client_order_id""",
        (broker_snapshot.account_scope_id, candidate.logical_signal_id),
    ).fetchall()
    safe_sell_retry = candidate.side == "sell" and prior_attempts and all(
        str(row["intent_status"]).upper() in {
            "EXPIRED", "CANCELLED", "REJECTED",
        }
        and str(row["reservation_status"]).lower() == "released"
        for row in prior_attempts
    )
    if prior_attempts and not safe_sell_retry:
        logical_signal_ids.add(candidate.logical_signal_id)
    daily_complete = (
        broker_snapshot.daily_risk_evidence_status == "reported"
        and broker_snapshot.total_equity > ZERO
    )
    return ReservationView(
        account_scope_id=broker_snapshot.account_scope_id,
        broker_snapshot_id=broker_snapshot.snapshot_id,
        broker_snapshot_sha256=broker_snapshot.snapshot_sha256,
        positions=positions,
        active_reservations=reservations,
        active_logical_signal_ids=frozenset(logical_signal_ids),
        position_exit_owner_ids=exits,
        position_exit_target_qtys={
            str(row["stock_code"]): int(row["target_qty"])
            for row in exit_rows
        },
        capacity_evidence_complete=position_complete,
        daily_evidence_complete=daily_complete,
        daily_trade_date=trade_date if daily_complete else "",
        daily_source=(
            f"broker:{broker_snapshot.snapshot_id}+ledger:schema11"
            if daily_complete else ""
        ),
        daily_new_positions=_daily_new_positions(
            conn, trade_date, broker_snapshot, reservations,
        ),
        daily_orders=len(_daily_order_ids(
            conn,
            trade_date,
            broker_snapshot.account_scope_id,
            broker_snapshot.adapter,
        )),
        daily_turnover_fraction=(
            broker_snapshot.daily_turnover_fraction
            + reserved_turnover / broker_snapshot.total_equity
            if daily_complete else ZERO
        ),
        consecutive_losses=broker_snapshot.consecutive_losses,
    )


def _intent_from_result(
    result: PreTradeResult,
    broker_snapshot: BrokerSnapshot,
) -> ExecutionIntent:
    candidate = result.candidate
    current = next((
        item.total_qty for item in broker_snapshot.positions
        if item.code == candidate.code
    ), 0)
    exact_order = {
        "code": candidate.code,
        "side": candidate.side,
        "order_qty": result.approved_qty,
        "expected_current_qty": current,
        "target_position_qty": result.target_position_qty,
        "limit_price": result.approved_limit_price,
        "price_cap": result.approved_price_cap,
        "stop_price": candidate.stop_price,
        "expires_at": result.valid_until,
    }
    order_id = client_order_id(
        candidate.account_scope_id,
        broker_snapshot.adapter,
        candidate.logical_signal_id,
        result.pre_trade_result_id,
        exact_order,
        result.submission_attempt_id,
    )
    return ExecutionIntent(
        client_order_id=order_id,
        pre_trade_result_id=result.pre_trade_result_id,
        pre_trade_result=result,
        pre_trade_result_sha256=result.result_sha256,
        submission_attempt_id=result.submission_attempt_id,
        account_scope_id=candidate.account_scope_id,
        adapter=broker_snapshot.adapter,
        logical_signal_id=candidate.logical_signal_id,
        source_signal_id=candidate.source_signal_id,
        strategy_id=candidate.strategy_id,
        strategy_version=candidate.strategy_version,
        parameter_version=candidate.parameter_version,
        model_version=candidate.model_version,
        fee_schedule_version=result.fee_schedule_version,
        fee_schedule_sha256=result.fee_schedule_sha256,
        fee_evidence_status=result.fee_evidence_status,
        rule_evidence_status=result.rule_evidence_status,
        code=candidate.code,
        side=candidate.side,
        order_qty=result.approved_qty,
        expected_current_qty=current,
        target_position_qty=result.target_position_qty,
        limit_price=result.approved_limit_price,
        price_cap=result.approved_price_cap,
        stop_price=candidate.stop_price,
        signal_time=candidate.signal_time,
        expires_at=result.valid_until,
        broker_snapshot_id=result.broker_snapshot_id,
        broker_snapshot_sha256=result.broker_snapshot_sha256,
        quote_snapshot_id=result.quote_snapshot_id,
        quote_snapshot_sha256=result.quote_snapshot_sha256,
        instrument_rules_sha256=result.instrument_rules_sha256,
    )


def _ready_order(intent: ExecutionIntent, now: str) -> dict[str, object]:
    payload = {
        "source": "execution_admission",
        "client_order_id": intent.client_order_id,
        "intent_sha256": intent.intent_sha256,
        "pre_trade_result_id": intent.pre_trade_result_id,
        "requested_qty": intent.order_qty,
        "target_qty": intent.target_position_qty,
    }
    return {
        "client_order_id": intent.client_order_id,
        "signal_id": intent.source_signal_id,
        "order_id": None,
        "stock_code": intent.code,
        "action": intent.side,
        "target_qty": intent.target_position_qty,
        "requested_qty": intent.order_qty,
        "filled_qty": 0,
        "average_fill_price": 0,
        "status": "ready",
        "submit_count": 0,
        "reason": "admitted",
        "first_submitted_at": None,
        "updated_at": now,
        "completed_at": None,
        "raw_json": canonical_json(payload),
    }


def _validate_order_matches_intent(row: object, intent: ExecutionIntent) -> None:
    if row is None:
        raise ValueError("execution intent is missing its order row")
    if (
        str(row["client_order_id"]) != intent.client_order_id
        or str(row["stock_code"]) != intent.code
        or str(row["action"]).lower() != intent.side
        or int(row["requested_qty"] or 0) != intent.order_qty
        or int(row["target_qty"] or 0) != intent.target_position_qty
        or int(row["filled_qty"] or 0) > intent.order_qty
    ):
        raise ValueError("order row conflicts with execution intent")
    if str(row["status"]).lower() == "ready":
        try:
            payload = json.loads(str(row["raw_json"]))
        except Exception as exc:
            raise ValueError("READY order payload is invalid") from exc
        if payload.get("intent_sha256") != intent.intent_sha256:
            raise ValueError("READY order payload conflicts with execution intent")


def _has_execution_evidence(
    conn: object,
    intent: ExecutionIntent,
) -> bool:
    if conn.execute(
        """SELECT 1 FROM broker_order_current
           WHERE account_scope_id=? AND client_order_id=?""",
        (intent.account_scope_id, intent.client_order_id),
    ).fetchone() is not None:
        return True
    if conn.execute(
        "SELECT 1 FROM fills WHERE client_order_id=?",
        (intent.client_order_id,),
    ).fetchone() is not None:
        return True
    return conn.execute(
        """SELECT 1 FROM order_events
           WHERE (
               CASE WHEN json_valid(raw_json)
                    THEN json_extract(raw_json, '$.client_order_id') END
           )=? OR (signal_id=? AND stock_code=? AND lower(action)=?)
           LIMIT 1""",
        (
            intent.client_order_id, intent.source_signal_id,
            intent.code, intent.side,
        ),
    ).fetchone() is not None


def _existing_pre_trade_result(
    conn: object,
    candidate: StrategyOrderCandidate,
) -> PreTradeResult | None:
    rows = conn.execute(
        """SELECT allowed, result_sha256, payload_json
           FROM pre_trade_results
           WHERE account_scope_id=? AND candidate_id=?
           ORDER BY pre_trade_result_id""",
        (candidate.account_scope_id, candidate.candidate_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("candidate resolves to multiple pre-trade results")
    try:
        result = PreTradeResult.from_dict(json.loads(str(rows[0]["payload_json"])))
    except Exception as exc:
        raise ValueError("persisted pre-trade result payload is invalid") from exc
    stored_allowed = int(rows[0]["allowed"])
    if (
        stored_allowed not in {0, 1}
        or result.result_sha256 != str(rows[0]["result_sha256"])
        or result.allowed != (stored_allowed == 1)
        or result.candidate_id != candidate.candidate_id
        or result.candidate.account_scope_id != candidate.account_scope_id
        or result.candidate.payload_sha256 != candidate.payload_sha256
    ):
        raise ValueError("persisted pre-trade result conflicts with candidate")
    return result


def _existing_allowed_admission(
    conn: object,
    candidate: StrategyOrderCandidate,
    now: str,
) -> AdmissionResult | None:
    rows = conn.execute(
        """SELECT i.payload_json, i.intent_sha256
           FROM execution_intents AS i
           JOIN pre_trade_results AS p
             ON p.account_scope_id=i.account_scope_id
            AND p.pre_trade_result_id=i.pre_trade_result_id
           WHERE p.account_scope_id=? AND p.candidate_id=?""",
        (candidate.account_scope_id, candidate.candidate_id),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("candidate resolves to multiple execution intents")
    try:
        intent = ExecutionIntent.from_dict(json.loads(str(rows[0]["payload_json"])))
    except Exception as exc:
        raise ValueError("persisted execution intent payload is invalid") from exc
    if intent.intent_sha256 != str(rows[0]["intent_sha256"]):
        raise ValueError("persisted execution intent hash conflicts with payload")
    reservation = conn.execute(
        """SELECT r.*, i.status AS intent_status
           FROM capacity_reservations AS r
           JOIN execution_intents AS i
             ON i.account_scope_id=r.account_scope_id
            AND i.client_order_id=r.client_order_id
           WHERE r.account_scope_id=? AND r.client_order_id=?""",
        (candidate.account_scope_id, intent.client_order_id),
    ).fetchone()
    if reservation is None:
        raise ValueError("execution intent is missing its capacity reservation")
    _reservation_evidence(
        reservation, intent, str(reservation["intent_status"]).lower(),
    )
    order = conn.execute(
        "SELECT * FROM orders WHERE client_order_id=?",
        (intent.client_order_id,),
    ).fetchone()
    _validate_order_matches_intent(order, intent)
    if (
        str(reservation["intent_status"]).upper() != "READY"
        or str(reservation["status"]).lower() != "active"
        or str(order["status"]).lower() != "ready"
        or order["order_id"] is not None
        or int(order["submit_count"] or 0) != 0
        or order["first_submitted_at"] is not None
        or int(order["filled_qty"] or 0) != 0
        or _has_execution_evidence(conn, intent)
        or _instant(now, "now") >= _instant(intent.expires_at, "intent.expires_at")
    ):
        raise ValueError("execution intent is no longer replayable")
    return AdmissionResult(
        pre_trade_result=intent.pre_trade_result,
        execution_intent=intent,
        reservation_id=str(reservation["reservation_id"]),
        replayed=True,
    )


@_fixed_decimal_context
def admit_candidate(
    store: TradingStore,
    request: AdmissionRequest,
    now: str,
) -> AdmissionResult:
    if not isinstance(store, TradingStore):
        raise ValueError("store must be TradingStore")
    if not isinstance(request, AdmissionRequest):
        raise ValueError("request must be AdmissionRequest")
    normalized_now = _normalized_timestamp(now, "now")
    if _instant(request.risk_policy.checked_at, "risk_policy.checked_at") != _instant(
        normalized_now, "now"
    ):
        raise ValueError("now must match risk_policy.checked_at")
    store.initialize()
    candidate = request.candidate
    with store.transaction() as conn:
        scope = conn.execute(
            """SELECT adapter FROM account_scopes
               WHERE account_scope_id=?""",
            (candidate.account_scope_id,),
        ).fetchone()
        if scope is None:
            raise ValueError("candidate account scope is not registered")
        if str(scope["adapter"]) != request.risk_policy.adapter:
            raise ValueError("risk policy adapter does not match account scope")
        store.insert_strategy_order_candidate(conn, candidate)
        existing_result = _existing_pre_trade_result(conn, candidate)
        if existing_result is not None:
            if not existing_result.allowed:
                return AdmissionResult(
                    pre_trade_result=existing_result,
                    replayed=True,
                )
            existing = _existing_allowed_admission(
                conn, candidate, normalized_now,
            )
            if existing is None:
                raise ValueError(
                    "allowed pre-trade result is missing its execution intent"
                )
            return existing
        broker_snapshot = store.load_current_broker_snapshot(
            conn, candidate.account_scope_id,
        )
        if broker_snapshot is None:
            result = pre_trade_check(
                candidate,
                None,
                request.quote,
                request.instrument_rules,
                _system_state(conn),
                request.risk_policy,
                ReservationView(account_scope_id=candidate.account_scope_id),
            )
            store.insert_pre_trade_result(conn, result)
            return AdmissionResult(pre_trade_result=result)
        reservations = _reservation_view(
            store, conn, broker_snapshot, candidate, normalized_now,
        )
        result = pre_trade_check(
            candidate,
            broker_snapshot,
            request.quote,
            request.instrument_rules,
            _system_state(conn),
            request.risk_policy,
            reservations,
        )
        store.insert_pre_trade_result(conn, result)
        if not result.allowed:
            return AdmissionResult(pre_trade_result=result)
        intent = _intent_from_result(result, broker_snapshot)
        store.insert_execution_intent(conn, intent)
        reservation_id = f"reservation:{intent.client_order_id}"
        fee = result.execution_fee
        store.reserve_capacity(
            conn,
            account_scope_id=candidate.account_scope_id,
            reservation_id=reservation_id,
            client_order_id=intent.client_order_id,
            stock_code=intent.code,
            side=intent.side,
            target_qty=intent.order_qty,
            cash_yuan=(
                fee.notional_yuan + fee.total_yuan
                if intent.side == "buy" and fee is not None else ZERO
            ),
            position_value_yuan=(
                fee.notional_yuan
                if intent.side == "buy" and fee is not None else ZERO
            ),
            open_risk_yuan=(
                result.per_trade_risk_yuan if intent.side == "buy" else ZERO
            ),
            industry=candidate.industry,
            theme=candidate.theme,
            uncategorized=candidate.uncategorized,
            created_at=normalized_now,
        )
        order = _ready_order(intent, normalized_now)
        store.upsert_order(conn, order)
        persisted = conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?",
            (intent.client_order_id,),
        ).fetchone()
        _validate_order_matches_intent(persisted, intent)
        return AdmissionResult(
            pre_trade_result=result,
            execution_intent=intent,
            reservation_id=reservation_id,
        )


_INTENT_TRANSITIONS = {
    "READY": ("SUBMITTING", "EXPIRED", "REJECTED"),
    "SUBMITTING": (
        "SUBMITTED", "REJECTED", "NOT_SUBMITTED", "SUBMIT_UNKNOWN",
    ),
    "SUBMITTED": (
        "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED",
    ),
    "PARTIALLY_FILLED": ("FILLED", "CANCELLED"),
    "SUBMIT_UNKNOWN": (
        "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED",
        "REJECTED", "NOT_SUBMITTED",
    ),
    "EXPIRED": (),
    "NOT_SUBMITTED": (),
    "REJECTED": (),
    "CANCELLED": (),
    "FILLED": (),
}


def _intent_target(order: Mapping[str, object]) -> str | None:
    status = str(order["status"] or "").strip().lower()
    filled = int(order["filled_qty"] or 0)
    if status == "pending_cancel":
        return "PARTIALLY_FILLED" if filled > 0 else "SUBMITTED"
    return {
        "ready": "READY",
        "submitting": "SUBMITTING",
        "submit_unknown": "SUBMIT_UNKNOWN",
        "new": "SUBMITTED",
        "open": "SUBMITTED",
        "held": "SUBMITTED",
        "submitted": "SUBMITTED",
        "partial": "PARTIALLY_FILLED",
        "partially_filled": "PARTIALLY_FILLED",
        "filled": "FILLED",
        "cancelled": "CANCELLED",
        "rejected": "REJECTED",
        "risk_rejected": "REJECTED",
        "failed": "REJECTED",
        "skipped": "REJECTED",
        "not_submitted": "NOT_SUBMITTED",
        "expired": "EXPIRED",
    }.get(status)


def _transition_path(current: str, target: str) -> tuple[str, ...]:
    if current == target:
        return ()
    pending: list[tuple[str, tuple[str, ...]]] = [(current, ())]
    visited = {current}
    while pending:
        state, path = pending.pop(0)
        for next_state in _INTENT_TRANSITIONS.get(state, ()):
            next_path = (*path, next_state)
            if next_state == target:
                return next_path
            if next_state not in visited:
                visited.add(next_state)
                pending.append((next_state, next_path))
    raise ValueError(
        f"execution intent cannot transition from {current} to {target}"
    )


@_fixed_decimal_context
def synchronize_execution_lifecycle(
    store: TradingStore,
    conn: object,
    account_scope_id: str,
    *,
    now: str,
    reconciliation_id: str | None = None,
) -> dict[str, int]:
    """Advance persisted intents from authoritative order/snapshot facts."""
    if not isinstance(store, TradingStore):
        raise ValueError("store must be TradingStore")
    normalized_now = _normalized_timestamp(now, "now")
    if not getattr(conn, "in_transaction", False):
        raise ValueError("execution lifecycle synchronization requires a transaction")
    account_scope_id = str(account_scope_id or "").strip()
    if not account_scope_id:
        raise ValueError("account_scope_id is required")
    rows = conn.execute(
        """SELECT i.client_order_id, i.status AS intent_status,
                  i.intent_sha256, i.payload_json,
                   o.order_id, o.stock_code, o.action, o.target_qty,
                   o.requested_qty, o.filled_qty, o.status, o.submit_count,
                   o.first_submitted_at,
                  o.updated_at, o.raw_json,
                  b.stock_code AS broker_stock_code,
                  b.side AS broker_side,
                  b.target_qty AS broker_target_qty,
                  b.filled_qty AS broker_filled_qty,
                  b.status AS broker_status,
                  b.updated_at AS broker_updated_at
           FROM execution_intents AS i
           JOIN orders AS o ON o.client_order_id=i.client_order_id
           JOIN capacity_reservations AS r
             ON r.account_scope_id=i.account_scope_id
            AND r.client_order_id=i.client_order_id
           LEFT JOIN broker_order_current AS b
             ON b.account_scope_id=i.account_scope_id
            AND b.client_order_id=i.client_order_id
           WHERE i.account_scope_id=? AND r.status='active'
           ORDER BY i.client_order_id""",
        (account_scope_id,),
    ).fetchall()
    advanced = adjusted = released = 0
    for row in rows:
        try:
            intent = ExecutionIntent.from_dict(
                json.loads(str(row["payload_json"]))
            )
        except Exception as exc:
            raise ValueError("execution lifecycle intent payload is invalid") from exc
        if (
            intent.intent_sha256 != str(row["intent_sha256"])
            or intent.account_scope_id != account_scope_id
        ):
            raise ValueError("execution lifecycle intent hash mismatch")
        _validate_order_matches_intent(row, intent)
        lifecycle_order: Mapping[str, object] = row
        if row["broker_status"] is not None:
            if (
                str(row["broker_stock_code"]) != intent.code
                or str(row["broker_side"]).lower() != intent.side
                or int(row["broker_target_qty"]) != intent.order_qty
                or int(row["broker_filled_qty"]) != int(row["filled_qty"] or 0)
            ):
                raise ValueError(
                    "current broker order conflicts with execution intent"
                )
            lifecycle_order = {
                "status": row["broker_status"],
                "filled_qty": row["broker_filled_qty"],
            }
        target = _intent_target(lifecycle_order)
        if target is None:
            raise ValueError(
                f"unsupported execution order status: {lifecycle_order['status']}"
            )
        current = str(row["intent_status"]).upper()
        if (
            target == "NOT_SUBMITTED"
            and current == "READY"
            and int(row["submit_count"] or 0) == 0
            and not row["order_id"]
            and not row["first_submitted_at"]
        ):
            target = "REJECTED"
        if (
            target == "NOT_SUBMITTED"
            and current in {"READY", "SUBMITTING", "SUBMIT_UNKNOWN"}
            and not reconciliation_id
        ):
            target = "SUBMIT_UNKNOWN"
        event_at = _normalized_timestamp(
            row["broker_updated_at"] or row["updated_at"],
            "order.updated_at",
        )
        path = _transition_path(current, target)
        for next_state in path:
            transitioned_at = event_at
            if current == "READY" and next_state == "SUBMITTING":
                if not row["first_submitted_at"]:
                    raise ValueError(
                        "submitted execution order lacks first_submitted_at"
                    )
                transitioned_at = _normalized_timestamp(
                    row["first_submitted_at"], "order.first_submitted_at",
                )
                if _instant(transitioned_at, "order.first_submitted_at") > _instant(
                    event_at, "order.updated_at"
                ):
                    raise ValueError(
                        "order first_submitted_at follows its update"
                    )
            if not store.compare_and_set_execution_intent_status(
                conn,
                account_scope_id,
                intent.client_order_id,
                expected_status=current,
                new_status=next_state,
                transitioned_at=transitioned_at,
            ):
                raise ValueError("execution intent transition compare-and-set failed")
            current = next_state
        if path:
            advanced += 1

        reservation = conn.execute(
            """SELECT reservation_id, status, remaining_target_qty
               FROM capacity_reservations
               WHERE account_scope_id=? AND client_order_id=?""",
            (account_scope_id, intent.client_order_id),
        ).fetchone()
        if reservation is None:
            raise ValueError("execution intent is missing its capacity reservation")
        filled_qty = int(row["filled_qty"] or 0)
        if (
            current == "PARTIALLY_FILLED"
            and 0 < filled_qty < intent.order_qty
            and str(reservation["status"]) == "active"
            and int(reservation["remaining_target_qty"])
            != intent.order_qty - filled_qty
        ):
            if not store.adjust_capacity_reservation(
                conn,
                account_scope_id,
                str(reservation["reservation_id"]),
                cumulative_filled_qty=filled_qty,
            ):
                raise ValueError("capacity reservation partial adjustment failed")
            adjusted += 1

        if current == "EXPIRED" and str(reservation["status"]) == "active":
            if not store.release_capacity_reservation(
                conn,
                account_scope_id,
                str(reservation["reservation_id"]),
                released_at=normalized_now,
                reason="never-submitted READY intent expired",
            ):
                raise ValueError("expired capacity reservation release failed")
            released += 1
        elif (
            reconciliation_id
            and current in {
                "NOT_SUBMITTED", "REJECTED", "CANCELLED", "FILLED",
            }
            and str(reservation["status"]) == "active"
        ):
            if not store.release_capacity_reservation(
                conn,
                account_scope_id,
                str(reservation["reservation_id"]),
                released_at=normalized_now,
                reason=f"{current} confirmed by matched full reconciliation",
                reconciliation_id=reconciliation_id,
            ):
                raise ValueError("terminal capacity reservation release failed")
            released += 1
    return {"advanced": advanced, "adjusted": adjusted, "released": released}


@_fixed_decimal_context
def expire_ready_intents_in_transaction(
    store: TradingStore,
    conn: object,
    now: str,
) -> int:
    if not isinstance(store, TradingStore):
        raise ValueError("store must be TradingStore")
    normalized_now = _normalized_timestamp(now, "now")
    if not getattr(conn, "in_transaction", False):
        raise ValueError("READY expiry requires a transaction")
    released = 0
    rows = conn.execute(
        """SELECT i.account_scope_id, i.client_order_id, i.expires_at,
                  i.payload_json, i.intent_sha256, r.reservation_id
           FROM execution_intents AS i
           JOIN capacity_reservations AS r
             ON r.account_scope_id=i.account_scope_id
            AND r.client_order_id=i.client_order_id
           WHERE i.status='READY' AND r.status='active'
           ORDER BY i.account_scope_id, i.client_order_id"""
    ).fetchall()
    now_instant = _instant(normalized_now, "now")
    for row in rows:
        if _instant(row["expires_at"], "expires_at") > now_instant:
            continue
        try:
            intent = ExecutionIntent.from_dict(
                json.loads(str(row["payload_json"]))
            )
        except Exception as exc:
            raise ValueError("READY expiry intent payload is invalid") from exc
        if (
            intent.intent_sha256 != str(row["intent_sha256"])
            or intent.account_scope_id != str(row["account_scope_id"])
            or intent.client_order_id != str(row["client_order_id"])
            or _has_execution_evidence(conn, intent)
        ):
            raise ValueError("expired READY intent has execution evidence")
        order = conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?",
            (row["client_order_id"],),
        ).fetchone()
        if (
            order is None
            or str(order["status"]).lower() != "ready"
            or order["order_id"] is not None
            or int(order["submit_count"] or 0) != 0
            or order["first_submitted_at"] is not None
            or int(order["filled_qty"] or 0) != 0
        ):
            raise ValueError("expired READY intent has submission evidence")
        changed = conn.execute(
            """UPDATE orders
               SET status='expired', reason='intent expired',
                   updated_at=?, completed_at=?
               WHERE client_order_id=? AND status='ready'
                 AND order_id IS NULL AND submit_count=0
                 AND first_submitted_at IS NULL AND filled_qty=0""",
            (normalized_now, normalized_now, row["client_order_id"]),
        )
        if changed.rowcount != 1:
            raise ValueError("READY order expiry compare-and-set failed")
        if not store.compare_and_set_execution_intent_status(
            conn,
            str(row["account_scope_id"]),
            str(row["client_order_id"]),
            expected_status="READY",
            new_status="EXPIRED",
            transitioned_at=normalized_now,
        ):
            raise ValueError("READY intent expiry compare-and-set failed")
        if not store.release_capacity_reservation(
            conn,
            str(row["account_scope_id"]),
            str(row["reservation_id"]),
            released_at=normalized_now,
            reason="never-submitted READY intent expired",
        ):
            raise ValueError("expired capacity reservation release failed")
        _enqueue_expired_order_notification(
            store, conn, intent, normalized_now,
        )
        released += 1
    return released


def expire_ready_intents(store: TradingStore, now: str) -> int:
    if not isinstance(store, TradingStore):
        raise ValueError("store must be TradingStore")
    store.initialize()
    with store.transaction() as conn:
        return expire_ready_intents_in_transaction(store, conn, now)
