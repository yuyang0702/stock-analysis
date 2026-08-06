"""Strict, cost-aware labels for the five-head ML pipeline.

The module deliberately consumes only point-in-time candidate cohorts and their
independent historical price paths.  It never reaches into the live quote cache
or a network provider.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from execution_contracts import FeeBreakdown, FeeSchedule
from ml_contracts import (
    CandidateSample,
    DownsideLabel,
    HorizonLabel,
    LabelRecord,
    canonical_hash,
)


ZERO = Decimal("0")


@dataclass(frozen=True)
class LabelPolicy:
    version: str = "ml-label-v2"
    reference_notional_yuan: float = 10_000.0
    lot_size: int = 100
    reference_quantity: int | None = None
    fill_window_bars: int = 1
    max_fill_delay_sec: int = 15 * 60
    planned_price_tolerance: float = 0.0
    horizons: tuple[int, ...] = (3, 5, 10)

    def __post_init__(self) -> None:
        if not str(self.version).strip():
            raise ValueError("label policy version is required")
        if (
            type(self.reference_notional_yuan) not in {int, float}
            or self.reference_notional_yuan <= 0
        ):
            raise ValueError("reference_notional_yuan must be positive")
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise ValueError("lot_size must be a positive integer")
        if self.reference_quantity is not None and (
            type(self.reference_quantity) is not int or self.reference_quantity <= 0
        ):
            raise ValueError("reference_quantity must be a positive integer or None")
        if type(self.fill_window_bars) is not int or self.fill_window_bars <= 0:
            raise ValueError("fill_window_bars must be a positive integer")
        if type(self.max_fill_delay_sec) is not int or self.max_fill_delay_sec <= 0:
            raise ValueError("max_fill_delay_sec must be a positive integer")
        if self.planned_price_tolerance < 0:
            raise ValueError("planned_price_tolerance must not be negative")
        normalized = tuple(sorted({int(value) for value in self.horizons}))
        if normalized != (3, 5, 10):
            raise ValueError("the first label policy requires horizons 3, 5, and 10")
        object.__setattr__(self, "version", str(self.version).strip())
        object.__setattr__(self, "horizons", normalized)

    @property
    def contract_sha256(self) -> str:
        return canonical_hash(
            {
                "version": self.version,
                "reference_notional_yuan": self.reference_notional_yuan,
                "lot_size": self.lot_size,
                "reference_quantity": self.reference_quantity,
                "fill_window_bars": self.fill_window_bars,
                "max_fill_delay_sec": self.max_fill_delay_sec,
                "planned_price_tolerance": self.planned_price_tolerance,
                "horizons": self.horizons,
            }
        )


@dataclass(frozen=True)
class PathOutcome:
    downside_loss: float
    exit_blocked: int


@dataclass(frozen=True)
class LabelOutcome:
    sample_id: str
    label_version: str
    label_source: str
    cost_version: str
    cost_sha256: str
    policy_version: str
    policy_sha256: str
    candidate_source: str
    dataset_id: str
    trade_date: str
    decision_at: str
    code: str
    candidate_content_sha256: str
    candidate_origin: str
    fill_label: int | None
    fill_status: str
    fill_reason: str
    fill_evidence_sha256: str
    fill_delay_sec: float | None
    fill_price: float | None
    fill_at: str | None
    fill_matured_at: str | None
    reference_qty: int
    reference_notional_yuan: float
    reference_trade_notional_yuan: float | None
    ret_3d_gross: float | None = None
    ret_3d_net: float | None = None
    ret_5d_gross: float | None = None
    ret_5d_net: float | None = None
    ret_10d_gross: float | None = None
    ret_10d_net: float | None = None
    mfe_10d: float | None = None
    mae_10d: float | None = None
    downside_loss: float | None = None
    hit_stop: int | None = None
    hit_take: int | None = None
    exit_blocked: int = 0
    paused_path: int = 0
    buy_cost: float | None = None
    sell_cost: float | None = None
    slippage_cost: float | None = None
    commission_cost: float | None = None
    stamp_tax_cost: float | None = None
    transfer_fee_cost: float | None = None
    other_fee_cost: float | None = None
    net_cost: float | None = None
    quality_status: str = "pending"
    quality_reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ()
    market_data_sha256: str = ""
    matured_3d_at: str | None = None
    matured_5d_at: str | None = None
    matured_10d_at: str | None = None
    downside_matured_at: str | None = None
    matured_at: str | None = None
    horizons: tuple[HorizonLabel, ...] = ()
    downside: DownsideLabel | None = None

    def to_record(self) -> LabelRecord:
        return LabelRecord(**self.__dict__)


@dataclass(frozen=True)
class LabelBuildResult:
    dataset_id: str
    candidate_count: int
    filled_count: int
    no_fill_count: int
    pending_count: int
    changed_count: int
    outcomes: tuple[LabelOutcome, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class _CostComponents:
    buy_cost: float
    sell_cost: float
    slippage_cost: float
    commission_cost: float
    stamp_tax_cost: float
    transfer_fee_cost: float
    other_fee_cost: float
    net_cost: float
    buy: FeeBreakdown
    sell: FeeBreakdown


def label_path(
    *,
    entry_ref: Decimal | float | str,
    lows: Sequence[Decimal | float | str],
    fee_schedule: FeeSchedule | None = None,
    qty: int = 100,
    limit_down_blocked: bool = False,
) -> PathOutcome:
    """Return positive downside loss for a sequence of official low marks."""
    entry = _positive_decimal(entry_ref, "entry_ref")
    if type(qty) is not int or qty <= 0:
        raise ValueError("qty must be a positive integer")
    worst = ZERO
    for value in lows:
        low = _positive_decimal(value, "low")
        gross = low / entry - Decimal("1")
        cost_rate = ZERO
        if fee_schedule is not None:
            cost = fee_schedule.estimate_round_trip(entry, low, qty)
            cost_rate = cost.total_yuan / (entry * qty)
        net_mark = gross - cost_rate
        worst = min(worst, net_mark)
    return PathOutcome(
        downside_loss=float(max(ZERO, -worst)),
        exit_blocked=int(bool(limit_down_blocked)),
    )


def label_sample(
    sample: CandidateSample,
    price_path: Iterable[Mapping[str, object]],
    *,
    as_of: str,
    fee_schedule: FeeSchedule,
    policy: LabelPolicy | None = None,
    trading_dates: Sequence[str] | None = None,
) -> LabelOutcome:
    """Build one independently maturing label from a strict price path."""
    if not isinstance(sample, CandidateSample):
        raise TypeError("sample must be CandidateSample")
    if not isinstance(fee_schedule, FeeSchedule):
        raise TypeError("fee_schedule must be FeeSchedule")
    policy = policy or LabelPolicy()
    as_of_time = _aware_time(as_of, "as_of")
    decision_time = _aware_time(sample.decision_at, "decision_at")
    if as_of_time < decision_time:
        raise ValueError("as_of must not be before decision_at")

    rows, future_evidence = _normalize_prices(price_path, as_of_time=as_of_time)
    fill_deadline = decision_time + timedelta(seconds=policy.max_fill_delay_sec)
    market_hash = canonical_hash(rows)
    label_source = (
        "strict_counterfactual_v2"
        if sample.source.startswith("strict")
        else f"{sample.source}_v2"
    )
    base = LabelOutcome(
        sample_id=sample.sample_id,
        label_version=policy.version,
        label_source=label_source,
        cost_version=fee_schedule.version,
        cost_sha256=fee_schedule.contract_sha256,
        policy_version=policy.version,
        policy_sha256=policy.contract_sha256,
        candidate_source=sample.source,
        dataset_id=sample.dataset_id,
        trade_date=sample.trade_date,
        decision_at=sample.decision_at,
        code=sample.code,
        candidate_content_sha256=canonical_hash(sample),
        candidate_origin="history" if sample.source.startswith("strict") else "ml",
        fill_label=None,
        fill_status="pending",
        fill_reason="",
        fill_evidence_sha256="",
        fill_delay_sec=None,
        fill_price=None,
        fill_at=None,
        fill_matured_at=None,
        reference_qty=policy.reference_quantity or policy.lot_size,
        reference_notional_yuan=float(policy.reference_notional_yuan),
        reference_trade_notional_yuan=None,
        quality_status="pending",
        market_data_sha256=market_hash,
    )
    if not rows:
        if future_evidence or as_of_time < fill_deadline:
            return base
        return replace(
            base,
            fill_status="failed",
            fill_reason="NO_OFFICIAL_PATH",
            fill_matured_at=fill_deadline.isoformat(),
            quality_status="failed",
            quality_reasons=("NO_OFFICIAL_PATH",),
            failure_reasons=("NO_OFFICIAL_PATH",),
        )

    adjustment_versions = {str(row["adjustment_version"]) for row in rows}
    if "" in adjustment_versions or len(adjustment_versions) != 1:
        return replace(
            base,
            fill_status="failed",
            fill_reason="ADJUSTMENT_VERSION_MISMATCH",
            fill_matured_at=min(str(row["available_at"]) for row in rows),
            quality_status="failed",
            quality_reasons=("ADJUSTMENT_VERSION_MISMATCH",),
            failure_reasons=("ADJUSTMENT_VERSION_MISMATCH",),
        )

    planned_price = _feature_number(sample, "entry_price")
    if planned_price is None or planned_price <= 0:
        return replace(
            base,
            fill_status="failed",
            fill_reason="PLANNED_PRICE_MISSING",
            fill_matured_at=decision_time.isoformat(),
            quality_status="failed",
            quality_reasons=("PLANNED_PRICE_MISSING",),
            failure_reasons=("PLANNED_PRICE_MISSING",),
        )
    (
        fill_row,
        fill_price,
        no_fill_reasons,
        fill_failures,
        fill_evidence,
        fill_matured_at,
    ) = _find_fill(
        rows,
        decision_time=decision_time,
        as_of_time=as_of_time,
        planned_price=planned_price,
        policy=policy,
    )
    if fill_row is None:
        if fill_matured_at is None:
            return replace(base, fill_evidence_sha256=fill_evidence)
        if fill_failures:
            failures = tuple(sorted(fill_failures))
            return replace(
                base,
                fill_status="failed",
                fill_reason=failures[0],
                fill_evidence_sha256=fill_evidence,
                fill_matured_at=fill_matured_at,
                quality_status="failed",
                quality_reasons=failures,
                failure_reasons=failures,
            )
        if not no_fill_reasons:
            failures = ("MISSING_FILL_WINDOW",)
            return replace(
                base,
                fill_status="failed",
                fill_reason=failures[0],
                fill_evidence_sha256=fill_evidence,
                fill_matured_at=fill_matured_at,
                quality_status="failed",
                quality_reasons=failures,
                failure_reasons=failures,
            )
        reasons = tuple(sorted(no_fill_reasons))
        return replace(
            base,
            fill_label=0,
            fill_status="not_filled",
            fill_reason=reasons[0],
            fill_evidence_sha256=fill_evidence,
            fill_matured_at=fill_matured_at,
            quality_status="complete",
            quality_reasons=reasons,
            flags=reasons,
            matured_at=fill_matured_at,
        )

    fill_at = str(fill_row["bar_at"])
    fill_time = _aware_time(fill_at, "fill_at")
    entry = _positive_decimal(fill_price, "fill_price")
    qty = _reference_quantity(entry, policy)
    calendar = _normalize_trading_dates(
        trading_dates,
        rows=rows,
        start_date=fill_time.date().isoformat(),
        end_date=as_of_time.date().isoformat(),
    )
    days, path_flags, path_failures, paused_path, exit_blocked = _daily_marks(
        rows, fill_time=fill_time, trading_dates=calendar
    )
    day_by_date = {str(day["trade_date"]): day for day in days}
    future_dates = [date for date in calendar if date > fill_time.date().isoformat()]

    horizon_values: dict[int, dict[str, object]] = {}
    horizon_labels: list[HorizonLabel] = []
    for horizon in policy.horizons:
        if len(future_dates) < horizon:
            continue
        target_date = future_dates[horizon - 1]
        mark = day_by_date.get(target_date)
        if mark is None:
            path_failures.add(f"D{horizon}_MISSING_TRADING_DAY")
            continue
        exit_price = mark.get("close")
        if exit_price is None:
            path_failures.add(f"D{horizon}_NO_OFFICIAL_CLOSE")
            continue
        exit_decimal = _positive_decimal(exit_price, f"ret_{horizon}d_exit")
        costs = _cost_components(fee_schedule, entry, exit_decimal, qty)
        gross = float(exit_decimal / entry - Decimal("1"))
        net = gross - costs.net_cost / float(entry * qty)
        horizon_values[horizon] = {
            "gross": gross,
            "net": net,
            "matured_at": str(mark["available_at"]),
            "costs": costs,
        }
        horizon_labels.append(
            HorizonLabel(
                horizon_days=horizon,
                gross_return=gross,
                net_return=net,
                exit_price=float(exit_decimal),
                buy_commission_yuan=float(costs.buy.commission_yuan),
                buy_transfer_fee_yuan=float(costs.buy.transfer_fee_yuan),
                buy_other_fee_yuan=float(costs.buy.other_fee_yuan),
                buy_slippage_yuan=float(costs.buy.slippage_yuan),
                sell_commission_yuan=float(costs.sell.commission_yuan),
                sell_stamp_tax_yuan=float(costs.sell.stamp_tax_yuan),
                sell_transfer_fee_yuan=float(costs.sell.transfer_fee_yuan),
                sell_other_fee_yuan=float(costs.sell.other_fee_yuan),
                sell_slippage_yuan=float(costs.sell.slippage_yuan),
                total_cost_yuan=costs.net_cost,
                cost_rate=costs.net_cost / float(entry * qty),
                matured_at=str(mark["available_at"]),
                market_data_sha256=canonical_hash(
                    {
                        "dataset_id": sample.dataset_id,
                        "sample_id": sample.sample_id,
                        "horizon_days": horizon,
                        "rows": [
                            row
                            for row in rows
                            if str(row["bar_at"])[:10] <= target_date
                        ],
                    }
                ),
            )
        )

    downside_record: DownsideLabel | None = None
    if len(future_dates) >= 10:
        downside_record = _build_downside(
            sample=sample,
            rows=rows,
            trading_dates=calendar,
            fill_time=fill_time,
            d10_date=future_dates[9],
            entry=entry,
            qty=qty,
            fee_schedule=fee_schedule,
        )
        if downside_record.status == "failed":
            path_failures.add(downside_record.failure_reason)
        elif downside_record.status == "complete":
            paused_path = max(paused_path, downside_record.paused_path)
            exit_blocked = max(exit_blocked, downside_record.exit_blocked)
            if downside_record.paused_path:
                path_flags.add("PAUSED_PATH")
            if downside_record.exit_blocked:
                path_flags.add("EXIT_LIMIT_DOWN_BLOCKED")

    # The singular cost decomposition is bound to the D+5 primary label.  It
    # remains null during D+3-only partial maturity instead of changing meaning
    # when later horizons mature.
    primary = horizon_values.get(5)
    costs = primary["costs"] if primary is not None else None
    matured_times = [fill_matured_at] if fill_matured_at is not None else []
    matured_times.extend(
        str(value["matured_at"]) for value in horizon_values.values()
    )
    if downside_record is not None and downside_record.matured_at is not None:
        matured_times.append(downside_record.matured_at)
    quality_status = (
        "complete"
        if 10 in horizon_values
        and downside_record is not None
        and downside_record.status == "complete"
        else "partial"
        if horizon_values
        else "failed"
        if path_failures
        else "pending"
    )
    flags = tuple(sorted(path_flags))
    failures = tuple(sorted(path_failures))
    reasons = tuple(sorted(set(flags) | set(failures)))
    return replace(
        base,
        fill_label=1,
        fill_status="filled",
        fill_evidence_sha256=fill_evidence,
        fill_delay_sec=(fill_time - decision_time).total_seconds(),
        fill_price=float(entry),
        fill_at=fill_time.isoformat(),
        fill_matured_at=fill_matured_at,
        reference_qty=qty,
        reference_trade_notional_yuan=float(entry * qty),
        ret_3d_gross=_horizon_value(horizon_values, 3, "gross"),
        ret_3d_net=_horizon_value(horizon_values, 3, "net"),
        ret_5d_gross=_horizon_value(horizon_values, 5, "gross"),
        ret_5d_net=_horizon_value(horizon_values, 5, "net"),
        ret_10d_gross=_horizon_value(horizon_values, 10, "gross"),
        ret_10d_net=_horizon_value(horizon_values, 10, "net"),
        mfe_10d=None if downside_record is None else downside_record.mfe_10d_net,
        mae_10d=None if downside_record is None else downside_record.mae_10d_net,
        downside_loss=None if downside_record is None else downside_record.downside_loss,
        hit_stop=None if downside_record is None else downside_record.hit_stop,
        hit_take=None if downside_record is None else downside_record.hit_take,
        exit_blocked=exit_blocked,
        paused_path=paused_path,
        buy_cost=None if costs is None else costs.buy_cost,
        sell_cost=None if costs is None else costs.sell_cost,
        slippage_cost=None if costs is None else costs.slippage_cost,
        commission_cost=None if costs is None else costs.commission_cost,
        stamp_tax_cost=None if costs is None else costs.stamp_tax_cost,
        transfer_fee_cost=None if costs is None else costs.transfer_fee_cost,
        other_fee_cost=None if costs is None else costs.other_fee_cost,
        net_cost=None if costs is None else costs.net_cost,
        quality_status=quality_status,
        quality_reasons=reasons,
        flags=flags,
        failure_reasons=failures,
        matured_3d_at=_horizon_value(horizon_values, 3, "matured_at"),
        matured_5d_at=_horizon_value(horizon_values, 5, "matured_at"),
        matured_10d_at=_horizon_value(horizon_values, 10, "matured_at"),
        downside_matured_at=(
            None if downside_record is None else downside_record.matured_at
        ),
        matured_at=max(matured_times) if matured_times else None,
        horizons=tuple(horizon_labels),
        downside=downside_record,
    )


def build_labels(
    store: object,
    ml_store: object,
    dataset_id: str,
    as_of: str,
    fee_schedule: FeeSchedule,
    policy: LabelPolicy | None = None,
    *,
    decision_start: str | None = None,
    decision_end: str | None = None,
    cursor: str | None = None,
    limit: int = 1000,
) -> LabelBuildResult:
    """Label a bounded page of strict candidates and persist partial maturity."""
    if type(limit) is not int or limit <= 0 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    policy = policy or LabelPolicy()
    samples = _dataset_candidates(
        store,
        str(dataset_id),
        decision_start=decision_start,
        decision_end=decision_end,
        cursor=cursor,
        limit=limit,
    )
    calendar = _dataset_trading_dates(
        store,
        str(dataset_id),
        min((sample.trade_date for sample in samples), default=str(as_of)[:10]),
        str(as_of)[:10],
    )
    outcomes = []
    for sample in samples:
        path_reader = store.candidate_price_path
        try:
            path = path_reader(
                str(dataset_id),
                sample.code,
                sample.decision_at,
                as_of,
                available_by=as_of,
            )
        except TypeError:
            path = path_reader(
                str(dataset_id), sample.code, sample.decision_at, as_of
            )
        outcomes.append(
            label_sample(
                sample,
                path,
                as_of=as_of,
                fee_schedule=fee_schedule,
                policy=policy,
                trading_dates=calendar,
            )
        )
    changed = int(ml_store.upsert_labels([item.to_record() for item in outcomes]))
    next_cursor = None
    if len(samples) == limit:
        last = samples[-1]
        next_cursor = f"{last.decision_at}|{last.sample_id}"
    return LabelBuildResult(
        dataset_id=str(dataset_id),
        candidate_count=len(outcomes),
        filled_count=sum(item.fill_label == 1 for item in outcomes),
        no_fill_count=sum(item.fill_label == 0 for item in outcomes),
        pending_count=sum(item.fill_label is None for item in outcomes),
        changed_count=changed,
        outcomes=tuple(outcomes),
        next_cursor=next_cursor,
    )


def _dataset_candidates(
    store: object,
    dataset_id: str,
    *,
    decision_start: str | None,
    decision_end: str | None,
    cursor: str | None,
    limit: int,
) -> list[CandidateSample]:
    candidates = getattr(store, "candidates", None)
    if candidates is not None:
        ordered = sorted(
            [
                item
                for item in candidates
                if item.dataset_id == dataset_id
                and (decision_start is None or item.decision_at >= decision_start)
                and (decision_end is None or item.decision_at <= decision_end)
            ],
            key=lambda item: (item.decision_at, item.code),
        )
        if cursor:
            cursor_at, cursor_id = _parse_cursor(cursor)
            ordered = [
                item
                for item in ordered
                if (item.decision_at, item.sample_id) > (cursor_at, cursor_id)
            ]
        return ordered[:limit]
    connect = getattr(store, "connect", None)
    cohort = getattr(store, "candidate_cohort", None)
    if not callable(connect) or not callable(cohort):
        raise TypeError("historical store must expose connect and candidate_cohort")
    clauses = ["dataset_id=?"]
    parameters: list[object] = [dataset_id]
    if decision_start is not None:
        clauses.append("decision_at>=?")
        parameters.append(_aware_time(decision_start, "decision_start").isoformat())
    if decision_end is not None:
        clauses.append("decision_at<=?")
        parameters.append(_aware_time(decision_end, "decision_end").isoformat())
    if cursor:
        cursor_at, cursor_id = _parse_cursor(cursor)
        clauses.append("(decision_at>? OR (decision_at=? AND sample_id>?))")
        parameters.extend((cursor_at, cursor_at, cursor_id))
    parameters.append(limit)
    with connect() as connection:
        rows = connection.execute(
            "SELECT decision_at,sample_id FROM decision_candidates WHERE "
            + " AND ".join(clauses)
            + " ORDER BY decision_at,sample_id LIMIT ?",
            tuple(parameters),
        ).fetchall()
    wanted = {(str(row[0]), str(row[1])) for row in rows}
    result: list[CandidateSample] = []
    for decision_at in sorted({item[0] for item in wanted}):
        result.extend(
            sample
            for sample in cohort(dataset_id, decision_at)
            if (sample.decision_at, sample.sample_id) in wanted
        )
    return sorted(result, key=lambda item: (item.decision_at, item.sample_id))


def _dataset_trading_dates(
    store: object, dataset_id: str, start: str, end: str
) -> list[str]:
    connect = getattr(store, "connect", None)
    if callable(connect):
        with connect() as connection:
            rows = connection.execute(
                """SELECT trade_date FROM (
                     SELECT trade_date AS trade_date FROM daily_bars
                     WHERE dataset_id=? AND trade_date BETWEEN ? AND ?
                     UNION
                     SELECT substr(bar_at,1,10) AS trade_date FROM candidate_prices
                     WHERE dataset_id=? AND substr(bar_at,1,10) BETWEEN ? AND ?
                   ) ORDER BY trade_date""",
                (dataset_id, start, end, dataset_id, start, end),
            ).fetchall()
        return [str(row[0]) for row in rows]
    trade_dates = getattr(store, "trade_dates", None)
    if callable(trade_dates):
        return [str(value) for value in trade_dates(dataset_id, start, end)]
    candidates = getattr(store, "price_rows", ())
    return sorted(
        {
            str(row["bar_at"])[:10]
            for row in candidates
            if str(row.get("dataset_id")) == dataset_id
            and start <= str(row["bar_at"])[:10] <= end
        }
    )


def _parse_cursor(cursor: str) -> tuple[str, str]:
    try:
        decision_at, sample_id = str(cursor).rsplit("|", 1)
    except ValueError as exc:
        raise ValueError("invalid label cursor") from exc
    return _aware_time(decision_at, "cursor").isoformat(), sample_id


def _normalize_prices(
    price_path: Iterable[Mapping[str, object]], *, as_of_time: datetime
) -> tuple[list[dict[str, object]], bool]:
    result = []
    future_evidence = False
    for source in price_path:
        row = dict(source)
        bar_time = _aware_time(str(row.get("bar_at") or ""), "bar_at")
        available_time = _aware_time(
            str(row.get("available_at") or ""), "available_at"
        )
        if bar_time > as_of_time or available_time > as_of_time:
            future_evidence = True
            continue
        paused = int(bool(row.get("paused")))
        normalized = {
            "dataset_id": str(row.get("dataset_id") or ""),
            "code": "".join(filter(str.isdigit, str(row.get("code") or ""))).zfill(6),
            "bar_at": bar_time.isoformat(),
            "available_at": available_time.isoformat(),
            "open": _optional_positive(row.get("open")),
            "high": _optional_positive(row.get("high")),
            "low": _optional_positive(row.get("low")),
            "close": _optional_positive(row.get("close")),
            "volume": _optional_nonnegative(row.get("volume")),
            "amount": _optional_nonnegative(row.get("amount")),
            "paused": paused,
            "limit_up": _optional_positive(row.get("limit_up")),
            "limit_down": _optional_positive(row.get("limit_down")),
            "adjustment_version": str(row.get("adjustment_version") or "").strip(),
        }
        result.append(normalized)
    result.sort(key=lambda item: (str(item["bar_at"]), str(item["code"])))
    return result, future_evidence


def _find_fill(
    rows: Sequence[Mapping[str, object]],
    *,
    decision_time: datetime,
    as_of_time: datetime,
    planned_price: float,
    policy: LabelPolicy,
) -> tuple[
    Mapping[str, object] | None,
    float | None,
    set[str],
    set[str],
    str,
    str | None,
]:
    no_fill_reasons: set[str] = set()
    failures: set[str] = set()
    eligible = [
        row
        for row in rows
        if _aware_time(str(row["bar_at"]), "bar_at") > decision_time
        and (_aware_time(str(row["bar_at"]), "bar_at") - decision_time).total_seconds()
        <= policy.max_fill_delay_sec
    ][: policy.fill_window_bars]
    evidence_sha256 = canonical_hash(
        {
            "decision_at": decision_time.isoformat(),
            "planned_price": planned_price,
            "policy_sha256": policy.contract_sha256,
            "rows": eligible,
        }
    )
    fill_deadline = decision_time + timedelta(seconds=policy.max_fill_delay_sec)
    for row in eligible:
        if int(row["paused"]):
            no_fill_reasons.add("SUSPENDED_FILL_WINDOW")
            continue
        open_price = row.get("open")
        high = row.get("high")
        low = row.get("low")
        if open_price is None or high is None or low is None:
            failures.add("MISSING_FILL_PRICE")
            continue
        if row.get("volume") is None or row.get("amount") is None:
            failures.add("MISSING_FILL_LIQUIDITY")
            continue
        if float(row["volume"]) <= 0 or float(row["amount"]) <= 0:
            no_fill_reasons.add("NO_FILL_LIQUIDITY")
            continue
        limit_up = row.get("limit_up")
        if (
            limit_up is not None
            and float(open_price) >= float(limit_up) - 1e-12
            and float(low) >= float(limit_up) - 1e-12
        ):
            no_fill_reasons.add("OPENING_LIMIT_UP")
            continue
        cap = planned_price * (1.0 + policy.planned_price_tolerance)
        if float(open_price) <= cap:
            return (
                row,
                float(open_price),
                no_fill_reasons,
                failures,
                evidence_sha256,
                str(row["available_at"]),
            )
        if float(low) <= cap:
            return (
                row,
                cap,
                no_fill_reasons,
                failures,
                evidence_sha256,
                str(row["available_at"]),
            )
        no_fill_reasons.add("PLANNED_PRICE_NOT_REACHED")
    matured_at = None
    if len(eligible) >= policy.fill_window_bars:
        matured_at = str(eligible[-1]["available_at"])
    elif as_of_time >= fill_deadline:
        matured_at = fill_deadline.isoformat()
    return None, None, no_fill_reasons, failures, evidence_sha256, matured_at


def _daily_marks(
    rows: Sequence[Mapping[str, object]],
    *,
    fill_time: datetime,
    trading_dates: Sequence[str],
) -> tuple[list[dict[str, object]], set[str], set[str], int, int]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        row_time = _aware_time(str(row["bar_at"]), "bar_at")
        if row_time < fill_time:
            continue
        grouped.setdefault(row_time.date().isoformat(), []).append(row)
    result: list[dict[str, object]] = []
    flags: set[str] = set()
    failures: set[str] = set()
    last_close: float | None = None
    paused_path = 0
    exit_blocked = 0
    for trade_date in trading_dates:
        if trade_date < fill_time.date().isoformat():
            continue
        day_rows = sorted(grouped.get(trade_date, ()), key=lambda row: str(row["bar_at"]))
        if not day_rows:
            failures.add(f"MISSING_PRICE_DAY:{trade_date}")
            result.append(
                {
                    "trade_date": trade_date,
                    "close": None,
                    "high": None,
                    "low": None,
                    "available_at": None,
                }
            )
            continue
        official = [row for row in day_rows if not int(row["paused"])]
        if any(int(row["paused"]) for row in day_rows):
            paused_path = 1
            flags.add("PAUSED_PATH")
        closes = [float(row["close"]) for row in official if row.get("close") is not None]
        highs = [float(row["high"]) for row in official if row.get("high") is not None]
        lows = [float(row["low"]) for row in official if row.get("low") is not None]
        if closes:
            last_close = closes[-1]
        elif all(int(row["paused"]) for row in day_rows) and last_close is not None:
            closes = [last_close]
            highs = [last_close]
            lows = [last_close]
        elif all(int(row["paused"]) for row in day_rows):
            failures.add(f"PAUSED_WITHOUT_REFERENCE:{trade_date}")
        for row in official:
            limit_down = row.get("limit_down")
            if (
                limit_down is not None
                and row.get("low") is not None
                and row.get("high") is not None
                and float(row["low"]) <= float(limit_down) + 1e-12
                and float(row["high"]) <= float(limit_down) + 1e-12
            ):
                exit_blocked = 1
                flags.add("EXIT_LIMIT_DOWN_BLOCKED")
        result.append(
            {
                "trade_date": trade_date,
                "close": closes[-1] if closes else None,
                "high": max(highs) if highs else last_close,
                "low": min(lows) if lows else last_close,
                "available_at": str(day_rows[-1]["available_at"]),
            }
        )
    return result, flags, failures, paused_path, exit_blocked


def _normalize_trading_dates(
    trading_dates: Sequence[str] | None,
    *,
    rows: Sequence[Mapping[str, object]],
    start_date: str,
    end_date: str,
) -> list[str]:
    if trading_dates is None:
        values = {str(row["bar_at"])[:10] for row in rows}
    else:
        values = {str(value)[:10] for value in trading_dates}
    return sorted(value for value in values if start_date <= value <= end_date)


def _build_downside(
    *,
    sample: CandidateSample,
    rows: Sequence[Mapping[str, object]],
    trading_dates: Sequence[str],
    fill_time: datetime,
    d10_date: str,
    entry: Decimal,
    qty: int,
    fee_schedule: FeeSchedule,
) -> DownsideLabel:
    relevant_dates = [
        value
        for value in trading_dates
        if fill_time.date().isoformat() <= value <= d10_date
    ]
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        bar_time = _aware_time(str(row["bar_at"]), "bar_at")
        if bar_time < fill_time or bar_time.date().isoformat() > d10_date:
            continue
        grouped.setdefault(bar_time.date().isoformat(), []).append(row)

    failures: set[str] = set()
    paused_path = 0
    exit_blocked = 0
    last_close: float | None = float(entry)
    lows: list[float] = []
    highs: list[float] = []
    evidence_rows: list[Mapping[str, object]] = []
    matured_at: str | None = None
    for trade_date in relevant_dates:
        day_rows = sorted(grouped.get(trade_date, ()), key=lambda row: str(row["bar_at"]))
        if not day_rows:
            failures.add(f"DOWNSIDE_MISSING_PRICE_DAY:{trade_date}")
            continue
        official = [row for row in day_rows if not int(row["paused"])]
        if any(int(row["paused"]) for row in day_rows):
            paused_path = 1
        if not official:
            if last_close is None:
                failures.add(f"DOWNSIDE_PAUSED_WITHOUT_REFERENCE:{trade_date}")
                continue
            lows.append(last_close)
            highs.append(last_close)
            evidence_rows.extend(day_rows)
            matured_at = str(day_rows[-1]["available_at"])
            continue
        for row in official:
            bar_time = _aware_time(str(row["bar_at"]), "bar_at")
            low = row.get("low")
            high = row.get("high")
            close = row.get("close")
            if low is None or high is None or close is None:
                failures.add(f"DOWNSIDE_INCOMPLETE_BAR:{row['bar_at']}")
                continue
            lows.append(float(low))
            # The fill bar's high can precede the fill, so it is not counted as
            # favorable post-entry evidence.  Its low remains a conservative
            # downside mark.
            if bar_time > fill_time:
                highs.append(float(high))
            last_close = float(close)
            limit_down = row.get("limit_down")
            if (
                limit_down is not None
                and float(low) <= float(limit_down) + 1e-12
                and float(high) <= float(limit_down) + 1e-12
            ):
                exit_blocked = 1
            evidence_rows.append(row)
            matured_at = str(row["available_at"])

    evidence_sha256 = canonical_hash(
        {
            "sample_id": sample.sample_id,
            "fill_at": fill_time.isoformat(),
            "d10_date": d10_date,
            "rows": evidence_rows,
            "fee_schedule_sha256": fee_schedule.contract_sha256,
            "qty": qty,
        }
    )
    if failures or not lows or matured_at is None:
        return DownsideLabel(
            status="failed",
            mfe_10d_net=None,
            mae_10d_net=None,
            downside_loss=None,
            paused_path=paused_path,
            exit_blocked=exit_blocked,
            hit_stop=None,
            hit_take=None,
            failure_reason=sorted(failures or {"DOWNSIDE_NO_OFFICIAL_PATH"})[0],
            matured_at=None,
            evidence_sha256=evidence_sha256,
        )

    buy = fee_schedule.estimate("buy", entry, qty)
    reference_notional = entry * qty

    def net_mark(price: float) -> float:
        mark = _positive_decimal(price, "path_mark")
        sell = fee_schedule.estimate("sell", mark, qty)
        return float(
            mark / entry
            - Decimal("1")
            - (buy.total_yuan + sell.total_yuan) / reference_notional
        )

    low_returns = [net_mark(value) for value in lows]
    high_returns = [net_mark(value) for value in highs]
    mae = min(0.0, min(low_returns))
    mfe = max(0.0, max(high_returns, default=0.0))
    stop_price = _feature_number(sample, "stop_loss")
    take_price = _feature_number(sample, "take_profit")
    return DownsideLabel(
        status="complete",
        mfe_10d_net=mfe,
        mae_10d_net=mae,
        downside_loss=-mae,
        paused_path=paused_path,
        exit_blocked=exit_blocked,
        hit_stop=int(
            stop_price is not None and any(value <= stop_price for value in lows)
        ),
        hit_take=int(
            take_price is not None and any(value >= take_price for value in highs)
        ),
        failure_reason="",
        matured_at=matured_at,
        evidence_sha256=evidence_sha256,
    )


def _cost_components(
    schedule: FeeSchedule, entry: Decimal, exit_price: Decimal, qty: int
) -> _CostComponents:
    buy = schedule.estimate("buy", entry, qty)
    sell = schedule.estimate("sell", exit_price, qty)
    buy_without_slippage = _fee_without_slippage(buy)
    sell_without_slippage = _fee_without_slippage(sell)
    slippage = buy.slippage_yuan + sell.slippage_yuan
    commission = buy.commission_yuan + sell.commission_yuan
    stamp = buy.stamp_tax_yuan + sell.stamp_tax_yuan
    transfer = buy.transfer_fee_yuan + sell.transfer_fee_yuan
    other = buy.other_fee_yuan + sell.other_fee_yuan
    total = buy_without_slippage + sell_without_slippage + slippage
    return _CostComponents(
        buy_cost=float(buy_without_slippage),
        sell_cost=float(sell_without_slippage),
        slippage_cost=float(slippage),
        commission_cost=float(commission),
        stamp_tax_cost=float(stamp),
        transfer_fee_cost=float(transfer),
        other_fee_cost=float(other),
        net_cost=float(total),
        buy=buy,
        sell=sell,
    )


def _reference_quantity(entry: Decimal, policy: LabelPolicy) -> int:
    if policy.reference_quantity is not None:
        return policy.reference_quantity
    lot_notional = entry * policy.lot_size
    lots = int(Decimal(str(policy.reference_notional_yuan)) // lot_notional)
    return max(1, lots) * policy.lot_size


def _fee_without_slippage(value: FeeBreakdown) -> Decimal:
    return (
        value.commission_yuan
        + value.stamp_tax_yuan
        + value.transfer_fee_yuan
        + value.other_fee_yuan
    )


def _horizon_value(
    values: Mapping[int, Mapping[str, object]], horizon: int, field: str
) -> object | None:
    item = values.get(horizon)
    return None if item is None else item[field]


def _feature_number(sample: CandidateSample, name: str) -> float | None:
    feature = sample.features.get(name)
    if feature is None or feature.value in (None, ""):
        return None
    try:
        return float(feature.value)
    except (TypeError, ValueError):
        return None


def _aware_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"TIMEZONE_AWARE_TIMESTAMP_REQUIRED: {field}")
    return parsed


def _positive_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite() or result <= ZERO:
        raise ValueError(f"{field} must be positive and finite")
    return result


def _optional_positive(value: object) -> float | None:
    if value in (None, ""):
        return None
    result = float(value)
    if result <= 0 or result != result or result in {float("inf"), float("-inf")}:
        raise ValueError("price fields must be positive and finite")
    return result


def _optional_nonnegative(value: object) -> float | None:
    if value in (None, ""):
        return None
    result = float(value)
    if result < 0 or result != result or result in {float("inf"), float("-inf")}:
        raise ValueError("volume fields must be nonnegative and finite")
    return result
