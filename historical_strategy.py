"""Pure point-in-time candidate generation for historical backtests."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

import pandas as pd

from candidate_core import score_candidate_frame
from exit_policy import board_type, initial_stop_price, market_regime, risk_position_pct
from historical_data import HistoricalDataValidationError, HistoricalStore, STRICT_FEATURES
from ml_contracts import CandidateSample


@dataclass(frozen=True)
class Candidate:
    code: str
    score: float
    position_pct: float
    entry_price: float
    stop_loss: float
    take_profit: float
    atr14: float
    mode: str
    market_regime: str
    industry: str
    theme: str
    evidence: dict[str, Any]


def fetch_live_quotes(*_args: object, **_kwargs: object) -> None:
    """Explicit guard retained so tests prove strict replay never reaches live data."""
    raise HistoricalDataValidationError("HISTORICAL_LIVE_DATA_FORBIDDEN")


def generate_candidates_at(
    store: HistoricalStore,
    dataset_id: str,
    decision_at: str,
    strategy_config: Mapping[str, object],
) -> list[CandidateSample]:
    """Replay one immutable imported five-minute cohort without recomputation."""
    if not isinstance(strategy_config, Mapping):
        raise HistoricalDataValidationError("STRICT_STRATEGY_CONFIG_REQUIRED")
    version_fields = (
        "strategy_version",
        "parameter_version",
        "feature_schema_version",
        "market_data_version",
        "code_hash",
        "generator_hash",
    )
    missing = [
        field
        for field in version_fields
        if not str(strategy_config.get(field) or "").strip()
    ]
    if missing:
        raise HistoricalDataValidationError(
            "STRICT_STRATEGY_CONFIG_INCOMPLETE: " + ",".join(missing)
        )
    samples = store.candidate_cohort(dataset_id, decision_at)
    if not samples:
        raise HistoricalDataValidationError("STRICT_COHORT_NOT_FOUND")
    for field in version_fields:
        expected = str(strategy_config[field])
        if any(str(getattr(sample, field)) != expected for sample in samples):
            raise HistoricalDataValidationError(f"STRICT_COHORT_VERSION_MISMATCH: {field}")
    if any(sample.dataset_id != str(dataset_id) for sample in samples):
        raise HistoricalDataValidationError("STRICT_COHORT_DATASET_MISMATCH")
    if any(sample.source != "strict_history" for sample in samples):
        raise HistoricalDataValidationError("STRICT_HISTORY_SOURCE_REQUIRED")
    return samples


def generate_daily_candidates(
    store: HistoricalStore,
    dataset_id: str,
    trade_date: str,
    *,
    mode: str,
    parameter_version: str,
    min_score: float = 75,
    cooldown_codes: set[str] | None = None,
    caution_min_score: float = 85,
    require_trend_confirmation: bool = False,
    require_breakout_confirmation: bool = False,
    max_chase_atr: float = 0.0,
    max_entry_score: float = 100.0,
    alpha_profile: str = "legacy",
    benchmark_closes: Mapping[str, float] | None = None,
) -> list[Candidate]:
    rows = [row for row in store.daily_slice(dataset_id, trade_date) if _eligible(row)]
    if mode == "strict":
        candidates = _strict_candidates(
            store, dataset_id, trade_date, rows, parameter_version,
            cooldown_codes=cooldown_codes,
            caution_min_score=caution_min_score,
        )
    elif mode == "price_core":
        candidates = _price_core_candidates(
            store, dataset_id, trade_date, rows, parameter_version,
            cooldown_codes=cooldown_codes,
            require_trend_confirmation=require_trend_confirmation,
            require_breakout_confirmation=require_breakout_confirmation,
            max_chase_atr=max_chase_atr,
            alpha_profile=alpha_profile,
            benchmark_closes=benchmark_closes,
        )
    else:
        raise HistoricalDataValidationError(f"unknown strategy mode: {mode}")
    return sorted(
        (
            candidate for candidate in candidates
            if candidate.score >= (
                max(float(min_score), float(caution_min_score))
                if candidate.market_regime == "CAUTION"
                else float(min_score)
            )
            and candidate.score <= float(max_entry_score)
        ),
        key=lambda candidate: (-candidate.score, candidate.code),
    )


def _eligible(row: dict) -> bool:
    return bool(
        row["listed"]
        and not row["st"]
        and not row["suspended"]
        and float(row["close"]) > 0
        and float(row["close"]) < float(row["limit_up"])
    )


def _strict_candidates(
    store: HistoricalStore,
    dataset_id: str,
    trade_date: str,
    rows: list[dict],
    parameter_version: str,
    *,
    cooldown_codes: set[str] | None = None,
    caution_min_score: float = 85,
) -> list[Candidate]:
    all_features = store.features_for_date(dataset_id, trade_date)
    prepared = [
        (row, all_features.get(str(row["code"]), {}))
        for row in rows
        if str(row["code"]) not in (cooldown_codes or set())
        if STRICT_FEATURES.issubset(all_features.get(str(row["code"]), {}))
    ]
    if not prepared:
        return []
    scored = score_candidate_frame(
        pd.DataFrame(
            [
                {
                    "score": features["score"],
                    "news_score": features["news_score"],
                    "pct_chg": features["pct_chg"],
                    "turnover": features["turnover"],
                }
                for _, features in prepared
            ]
        )
    )
    pct_ranks = pd.to_numeric(scored["pct_chg"], errors="coerce").rank(pct=True).fillna(0)
    turnover_ranks = pd.to_numeric(scored["turnover"], errors="coerce").rank(pct=True).fillna(0)
    result = []
    for index, (row, features) in enumerate(prepared):
        pct_rank = float(pct_ranks.iloc[index])
        turnover_rank = float(turnover_ranks.iloc[index])
        score = float(scored["final_score"].iloc[index])
        entry = _float(features["entry_price"]) or float(row["close"])
        atr = _float(features["atr14"])
        state = market_regime(features["market_regime"])
        board = board_type(str(row["code"]), entry, atr)
        stop = _float(features["stop_loss"]) or initial_stop_price(
            entry, _float(features["support_level"]), atr, board
        )
        position = risk_position_pct(
            entry, stop, board, _float(features["position_pct"]), state
        )
        expected_gross = _float(features.get("expected_gross_return_bps"))
        expected_net = _float(features.get("expected_net_return_bps"))
        evidence = {
            "proxy_only": False,
            "parameter_version": parameter_version,
            "pct_rank": pct_rank,
            "turnover_rank": turnover_rank,
        }
        if expected_gross is not None and math.isfinite(expected_gross) and expected_gross >= 0:
            evidence["expected_gross_return_bps"] = expected_gross
        if expected_net is not None and math.isfinite(expected_net):
            evidence["expected_net_return_bps"] = expected_net
        result.append(
            Candidate(
                code=str(row["code"]),
                score=round(score, 4),
                position_pct=position,
                entry_price=entry,
                stop_loss=stop,
                take_profit=_float(features["take_profit"]),
                atr14=atr,
                mode=str(features["strategy_mode"] or "short"),
                market_regime=state,
                industry=str(features["industry"] or "unknown"),
                theme=str(features["theme"] or "unknown"),
                evidence=evidence,
            )
        )
    return result


def _price_core_candidates(
    store: HistoricalStore,
    dataset_id: str,
    trade_date: str,
    rows: list[dict],
    parameter_version: str,
    *,
    cooldown_codes: set[str] | None = None,
    require_trend_confirmation: bool = False,
    require_breakout_confirmation: bool = False,
    max_chase_atr: float = 0.0,
    alpha_profile: str = "legacy",
    benchmark_closes: Mapping[str, float] | None = None,
) -> list[Candidate]:
    prepared = []
    for row in rows:
        if str(row["code"]) in (cooldown_codes or set()):
            continue
        history = store.history_until(dataset_id, str(row["code"]), trade_date, 40)
        if len(history) < 21:
            continue
        closes = [float(item["close"]) for item in history]
        amounts = [float(item["amount"]) for item in history]
        returns = [
            (float(item["close"]) / float(item["prev_close"]) - 1) * 100
            for item in history
            if float(item["prev_close"]) > 0
        ]
        atr = _atr14(history)
        prepared.append((row, history, closes, amounts, returns, atr))
    if alpha_profile not in {"legacy", "relative_v1", "relative_v2"}:
        raise HistoricalDataValidationError(f"UNKNOWN_ALPHA_PROFILE:{alpha_profile}")
    benchmark_return = None
    if alpha_profile == "relative_v2":
        sessions = store.trade_dates(dataset_id, "1900-01-01", trade_date)[-21:]
        if len(sessions) < 21 or any(day not in (benchmark_closes or {}) for day in sessions):
            # The first 20 sessions are a normal warm-up period.  A missing
            # benchmark after warm-up is a fail-closed data quality issue, but
            # should not abort an otherwise reproducible backtest run.
            return []
        values = [float(benchmark_closes[day]) for day in sessions]
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise HistoricalDataValidationError("BENCHMARK_HISTORY_INVALID")
        benchmark_return = values[-1] / values[0] - 1
    pct_values = [item[4][-1] for item in prepared]
    relative_values = [_return_over(item[2], 20) for item in prepared]
    amount_values = [item[3][-1] for item in prepared]
    market_return_5 = _cross_sectional_return(prepared, 5)
    market_return_20 = _cross_sectional_return(prepared, 20)
    breadth = _trend_breadth(prepared)
    volatility = _median_volatility(prepared)
    liquidity_ratio = _median_liquidity_ratio(prepared)
    market_state = _price_core_market_regime(
        market_return_5,
        market_return_20,
        breadth=breadth,
        volatility=volatility,
        liquidity_ratio=liquidity_ratio,
    )
    # The price-core dataset has no point-in-time index membership or status
    # history.  In that proxy mode, a weak broad tape is a reason to stay flat
    # rather than manufacture long entries from relative strength.  The signal
    # research pass showed that CAUTION entries stayed negative after costs,
    # so only NORMAL conditions can open new long positions.
    if market_state == "RISK_OFF" or (
        market_state == "CAUTION" and alpha_profile in {"legacy", "relative_v2"}
    ):
        return []
    if alpha_profile == "relative_v2" and benchmark_return < 0:
        return []
    industry_features = store.features_for_date(dataset_id, trade_date)
    result = []
    for row, history, closes, amounts, returns, atr in prepared:
        close = closes[-1]
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        trend = close > ma5 > ma10 > ma20
        breakout = close >= max(closes[-21:-1])
        chase_atr = (close - ma20) / atr if atr > 0 else 0.0
        if require_trend_confirmation and not trend:
            continue
        if require_breakout_confirmation and not breakout:
            continue
        if max_chase_atr > 0 and chase_atr > max_chase_atr:
            continue
        pct_rank = _percentile(returns[-1], pct_values)
        relative_strength_20 = _return_over(closes, 20)
        relative_rank = _percentile(relative_strength_20, relative_values)
        amount_rank = _percentile(amounts[-1], amount_values)
        volatility_values = [_realized_volatility(item[4][-20:]) for item in prepared]
        volatility_rank = _percentile(_realized_volatility(returns[-20:]), volatility_values)
        liquidity_ratio_stock = _liquidity_ratio(amounts)
        liquidity_values = [_liquidity_ratio(item[3]) for item in prepared]
        liquidity_rank = _percentile(liquidity_ratio_stock, liquidity_values)
        if alpha_profile == "relative_v2":
            # Experimental ablation: volume/volatility stay in execution risk,
            # not alpha. Independent benchmark strength must also be positive.
            excess_strength = relative_strength_20 - benchmark_return
            if excess_strength <= 0:
                continue
            score = min(100.0, 60 + 12 * int(trend) + 8 * int(breakout) + 20 * relative_rank)
        elif alpha_profile == "relative_v1":
            # Cross-sectional strength is more useful than an absolute score
            # when the A-share market rotates between styles.  Volatility and
            # liquidity are included as execution-aware quality terms.
            score = (
                55
                + 12 * int(trend)
                + 10 * int(breakout)
                + 12 * relative_rank
                + 6 * amount_rank
                + 5 * liquidity_rank
                + 5 * (1 - volatility_rank)
            )
        else:
            score = 70 + 10 * int(trend) + 8 * int(breakout) + 5 * pct_rank + 2 * amount_rank
        board = board_type(str(row["code"]), close, atr)
        stop = initial_stop_price(close, min(closes[-10:]), atr, board)
        position = risk_position_pct(close, stop, board, 10, market_state)
        risk = max(close - stop, 0)
        result.append(
            Candidate(
                code=str(row["code"]),
                score=round(score, 4),
                position_pct=position,
                entry_price=close,
                stop_loss=stop,
                take_profit=round(close + 2 * risk, 2),
                atr14=atr,
                mode="short" if breakout or returns[-1] >= 5 else "mid",
                market_regime=market_state,
                industry=str(industry_features.get(str(row["code"]), {}).get("industry") or "unknown"),
                theme="unknown",
                evidence={
                    "proxy_only": True,
                    "parameter_version": parameter_version,
                    "trend": trend,
                    "breakout": breakout,
                    "chase_atr": round(chase_atr, 4),
                    "pct_rank": pct_rank,
                    "amount_rank": amount_rank,
                    "alpha_profile": alpha_profile,
                    "relative_strength_20": round(relative_strength_20, 6),
                    "benchmark_return_20": benchmark_return,
                    "excess_strength_20": (relative_strength_20 - benchmark_return) if benchmark_return is not None else None,
                    "relative_rank": round(relative_rank, 6),
                    "volatility_20": round(_realized_volatility(returns[-20:]), 6),
                    "liquidity_ratio": round(liquidity_ratio_stock, 6),
                    "market_breadth": round(breadth, 6),
                    "market_volatility": round(volatility, 6),
                    "market_liquidity_ratio": round(liquidity_ratio, 6),
                    "market_return_5": round(market_return_5, 4),
                    "market_return_20": round(market_return_20, 4),
                    "recent_returns": {
                        str(item["trade_date"]): round(
                            float(item["close"]) / float(item["prev_close"]) - 1, 6
                        )
                        for item in history[-20:]
                        if float(item["prev_close"]) > 0
                    },
                },
            )
        )
    return result


def _cross_sectional_return(prepared: list[tuple], lookback: int) -> float:
    """Approximate broad market return from the available price-core universe."""
    values = []
    for item in prepared:
        closes = item[2]
        if len(closes) > lookback and closes[-lookback - 1] > 0:
            values.append(closes[-1] / closes[-lookback - 1] - 1)
    return sum(values) / len(values) if values else 0.0


def _return_over(closes: list[float], lookback: int) -> float:
    if len(closes) <= lookback or closes[-lookback - 1] <= 0:
        return 0.0
    return closes[-1] / closes[-lookback - 1] - 1


def _realized_volatility(returns: list[float]) -> float:
    values = [float(value) for value in returns if math.isfinite(float(value))]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5


def _liquidity_ratio(amounts: list[float]) -> float:
    if len(amounts) < 2:
        return 1.0
    baseline = sorted(float(value) for value in amounts[:-1] if float(value) > 0)
    if not baseline:
        return 0.0
    median = baseline[len(baseline) // 2]
    return float(amounts[-1]) / median if median > 0 else 0.0


def _trend_breadth(prepared: list[tuple]) -> float:
    eligible = 0
    positive = 0
    for item in prepared:
        closes = item[2]
        if len(closes) < 20:
            continue
        eligible += 1
        if closes[-1] > sum(closes[-20:]) / 20:
            positive += 1
    return positive / eligible if eligible else 0.0


def _median_volatility(prepared: list[tuple]) -> float:
    values = sorted(_realized_volatility(item[4][-20:]) for item in prepared)
    return values[len(values) // 2] if values else 0.0


def _median_liquidity_ratio(prepared: list[tuple]) -> float:
    values = sorted(_liquidity_ratio(item[3]) for item in prepared)
    return values[len(values) // 2] if values else 0.0


def price_core_market_state(
    store: HistoricalStore, dataset_id: str, trade_date: str
) -> str:
    """Return the same point-in-time tape state used by price-core entries.

    Keeping this calculation in the strategy module lets the backtest and a
    future broker adapter apply the same market-risk exit without duplicating
    regime logic in an execution layer.
    """
    rows = [row for row in store.daily_slice(dataset_id, trade_date) if _eligible(row)]
    prepared = []
    for row in rows:
        history = store.history_until(dataset_id, str(row["code"]), trade_date, 40)
        if len(history) < 21:
            continue
        closes = [float(item["close"]) for item in history]
        amounts = [float(item["amount"]) for item in history]
        returns = [
            (float(item["close"]) / float(item["prev_close"]) - 1) * 100
            for item in history
            if float(item["prev_close"]) > 0
        ]
        prepared.append((row, history, closes, amounts, returns, _atr14(history)))
    return _price_core_market_regime(
        _cross_sectional_return(prepared, 5),
        _cross_sectional_return(prepared, 20),
        breadth=_trend_breadth(prepared),
        volatility=_median_volatility(prepared),
        liquidity_ratio=_median_liquidity_ratio(prepared),
    )


def _price_core_market_regime(
    return_5: float,
    return_20: float,
    *,
    breadth: float | None = None,
    volatility: float | None = None,
    liquidity_ratio: float | None = None,
) -> str:
    """Map broad tape momentum to the shared exit-policy risk states."""
    if (
        (return_20 < -0.03 and return_5 < 0)
        or (breadth is not None and breadth < 0.30)
        or (volatility is not None and volatility > 0.05 and return_20 < 0)
    ):
        return "RISK_OFF"
    if (
        return_20 < 0
        or return_5 < 0
        or (breadth is not None and breadth < 0.50)
        or (liquidity_ratio is not None and liquidity_ratio < 0.70)
    ):
        return "CAUTION"
    return "NORMAL"


def _atr14(history: list[dict]) -> float:
    ranges = []
    for row in history[-14:]:
        high = float(row["high"])
        low = float(row["low"])
        previous = float(row["prev_close"])
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    return round(sum(ranges) / len(ranges), 4) if ranges else 0.0


def _percentile(value: float, values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(item <= value for item in values) / len(values)


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
