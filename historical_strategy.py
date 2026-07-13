"""Pure point-in-time candidate generation for historical backtests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from exit_policy import board_type, initial_stop_price, market_regime, risk_position_pct
from historical_data import HistoricalDataValidationError, HistoricalStore, STRICT_FEATURES


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


def generate_daily_candidates(
    store: HistoricalStore,
    dataset_id: str,
    trade_date: str,
    *,
    mode: str,
    parameter_version: str,
    min_score: float = 75,
) -> list[Candidate]:
    rows = [row for row in store.daily_slice(dataset_id, trade_date) if _eligible(row)]
    if mode == "strict":
        candidates = _strict_candidates(store, dataset_id, trade_date, rows, parameter_version)
    elif mode == "price_core":
        candidates = _price_core_candidates(store, dataset_id, trade_date, rows, parameter_version)
    else:
        raise HistoricalDataValidationError(f"unknown strategy mode: {mode}")
    return sorted(
        (candidate for candidate in candidates if candidate.score >= min_score),
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
) -> list[Candidate]:
    all_features = store.features_for_date(dataset_id, trade_date)
    prepared = [
        (row, all_features.get(str(row["code"]), {}))
        for row in rows
        if STRICT_FEATURES.issubset(all_features.get(str(row["code"]), {}))
    ]
    pct_values = [_float(features["pct_chg"]) for _, features in prepared]
    turnover_values = [_float(features["turnover"]) for _, features in prepared]
    result = []
    for row, features in prepared:
        pct_rank = _percentile(_float(features["pct_chg"]), pct_values)
        turnover_rank = _percentile(_float(features["turnover"]), turnover_values)
        score = (
            _float(features["score"])
            + _float(features["news_score"]) * 1.2
            + pct_rank * 5
            + turnover_rank * 2
        )
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
                evidence={
                    "proxy_only": False,
                    "parameter_version": parameter_version,
                    "pct_rank": pct_rank,
                    "turnover_rank": turnover_rank,
                },
            )
        )
    return result


def _price_core_candidates(
    store: HistoricalStore,
    dataset_id: str,
    trade_date: str,
    rows: list[dict],
    parameter_version: str,
) -> list[Candidate]:
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
        atr = _atr14(history)
        prepared.append((row, history, closes, amounts, returns, atr))
    pct_values = [item[4][-1] for item in prepared]
    amount_values = [item[3][-1] for item in prepared]
    result = []
    for row, history, closes, amounts, returns, atr in prepared:
        close = closes[-1]
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        trend = close > ma5 > ma10 > ma20
        breakout = close >= max(closes[-21:-1])
        pct_rank = _percentile(returns[-1], pct_values)
        amount_rank = _percentile(amounts[-1], amount_values)
        score = 70 + 10 * int(trend) + 8 * int(breakout) + 5 * pct_rank + 2 * amount_rank
        board = board_type(str(row["code"]), close, atr)
        stop = initial_stop_price(close, min(closes[-10:]), atr, board)
        position = risk_position_pct(close, stop, board, 10, "NORMAL")
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
                mode="short",
                market_regime="NORMAL",
                industry="unknown",
                theme="unknown",
                evidence={
                    "proxy_only": True,
                    "parameter_version": parameter_version,
                    "trend": trend,
                    "breakout": breakout,
                    "pct_rank": pct_rank,
                    "amount_rank": amount_rank,
                },
            )
        )
    return result


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
