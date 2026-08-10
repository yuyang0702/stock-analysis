"""Shared pure candidate selection and scoring helpers."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from candidate_channels import (
    CANDIDATE_CHANNEL_DEFAULTS,
    build_factor_channel_rows,
    merge_candidate_channels,
)
from strategy_snapshot_runtime import (
    build_candidate_pool_frame,
    score_candidate_frame as _portable_score_candidate_frame,
)


@dataclass(frozen=True)
class CandidatePoolConfig:
    mode: str
    min_price: float
    min_amount: float
    limit: int


def build_candidate_pool(
    frame: pd.DataFrame, config: CandidatePoolConfig
) -> pd.DataFrame:
    return build_candidate_pool_frame(frame, config)


def score_candidate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return _portable_score_candidate_frame(frame)


def build_multipath_candidate_pool(
    frame: pd.DataFrame,
    config: CandidatePoolConfig,
    *,
    decision_at: str,
    history_provider,
    intraday_provider=None,
    market_state: str = "NORMAL",
    disclosure_provider=None,
    wave3_enabled: bool = True,
    limitdown_enabled: bool = True,
    channel_settings: dict[str, int] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Build the bounded 30 + 10 + 5 candidate union.

    The existing momentum pool is preserved byte-for-behaviour.  The two new
    channels are evaluated from explicit point-in-time providers and deduped by
    stock code with their full attribution retained.
    """
    settings = dict(CANDIDATE_CHANNEL_DEFAULTS)
    settings.update(dict(channel_settings or {}))
    momentum_config = CandidatePoolConfig(
        config.mode,
        config.min_price,
        config.min_amount,
        min(int(config.limit), int(settings["momentum_max"])),
    )
    momentum = build_candidate_pool(frame, momentum_config)
    factors, audit = build_factor_channel_rows(
        frame,
        decision_at,
        history_provider,
        intraday_provider,
        market_state=market_state,
        disclosure_provider=disclosure_provider,
        wave3_enabled=wave3_enabled,
        limitdown_enabled=limitdown_enabled,
        settings=settings,
    )
    result = merge_candidate_channels(momentum, factors, settings)
    audit_by_code: dict[str, list[dict[str, object]]] = {}
    for item in audit:
        audit_by_code.setdefault(str(item.get("code") or ""), []).append(item)
    if not result.empty:
        result["factor_screen_audit"] = result["code"].map(
            lambda value: audit_by_code.get(
                "".join(filter(str.isdigit, str(value or "")))[:6], []
            )
        )
    return result, audit
