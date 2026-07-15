"""Shared pure candidate selection and scoring helpers."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class CandidatePoolConfig:
    mode: str
    min_price: float
    min_amount: float
    limit: int


def build_candidate_pool(
    frame: pd.DataFrame, config: CandidatePoolConfig
) -> pd.DataFrame:
    active = frame.copy()
    active = active[~active["name"].str.contains("ST|退", regex=True, na=False)]
    active = active[
        (active["price"] >= config.min_price)
        & (active["amount"] >= config.min_amount)
    ]

    if config.mode == "pre":
        active = active[(active["gap"] >= 1.5) & (active["gap"] <= 8.5)]
        active["score"] = (
            active["gap"].rank(pct=True) * 50
            + active["amount"].rank(pct=True) * 50
        )
    elif config.mode == "after":
        active = active[active["pct_chg"] >= 3]
        active["score"] = (
            active["pct_chg"].rank(pct=True) * 55
            + active["amount"].rank(pct=True) * 35
        )
        if "turnover" in active.columns:
            active["score"] += active["turnover"].rank(pct=True).fillna(0) * 10
    else:
        active = active[active["pct_chg"] >= 4]
        active["score"] = (
            active["pct_chg"].rank(pct=True) * 60
            + active["amount"].rank(pct=True) * 40
        )

    return (
        active.sort_values("score", ascending=False)
        .head(int(config.limit))
        .copy()
    )


def score_candidate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["final_score"] = pd.to_numeric(
        result["score"], errors="coerce"
    ).fillna(0)
    news_score = (
        result["news_score"]
        if "news_score" in result.columns
        else pd.Series(0.0, index=result.index)
    )
    result["final_score"] += pd.to_numeric(
        news_score, errors="coerce"
    ).fillna(0) * 1.2
    result["final_score"] += pd.to_numeric(
        result["pct_chg"], errors="coerce"
    ).rank(pct=True).fillna(0) * 5
    if "turnover" in result.columns:
        result["final_score"] += pd.to_numeric(
            result["turnover"], errors="coerce"
        ).rank(pct=True).fillna(0) * 2
    return result
