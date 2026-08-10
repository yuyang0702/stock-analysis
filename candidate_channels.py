"""Bounded momentum, wave3 and limit-down candidate channel composition."""

import math

if "FACTOR_PATH_MOMENTUM" not in globals():
    from factor_contracts import (
        FACTOR_PATH_LIMITDOWN,
        FACTOR_PATH_MOMENTUM,
        FACTOR_PATH_WAVE3,
        FactorContractError,
        factor_clean_code,
        factor_number,
    )
if "evaluate_wave3_factor" not in globals():
    from factor_wave3 import evaluate_wave3_factor
if "evaluate_limitdown_exhaustion_factor" not in globals():
    from factor_limitdown import evaluate_limitdown_exhaustion_factor


CANDIDATE_CHANNEL_VERSION = "2026-08-10.1"
CANDIDATE_CHANNEL_DEFAULTS = {
    "momentum_max": 30,
    "wave3_max": 10,
    "limitdown_max": 5,
    "total_max": 45,
    "factor_screen_max": 60,
}
FACTOR_FEATURE_DEFAULTS = {
    "factor_setup_id": "",
    "factor_state": "baseline",
    "factor_score": 0.0,
    "factor_rejection_code": "",
    "factor_triggered": False,
    "simulation_only": False,
    "factor_position_cap_pct": 0.0,
    "factor_risk_budget_pct": 0.0,
    "factor_max_hold_days": 0,
    "factor_max_concurrent": 0,
    "factor_max_new_per_day": 0,
    "industry_relative_strength": 0.0,
    "wave3_structure_score": 0.0,
    "wave3_price_volume_score": 0.0,
    "wave3_duration_similarity": 0.0,
    "wave3_gain_similarity": 0.0,
    "wave3_gain1_pct": 0.0,
    "wave3_gain2_pct": 0.0,
    "wave3_pullback1_pct": 0.0,
    "wave3_pullback2_pct": 0.0,
    "wave3_pullback_volume_ratio1": 0.0,
    "wave3_pullback_volume_ratio2": 0.0,
    "wave3_setup_age_days": 0,
    "limitdown_locked_days": 0,
    "limitdown_event_age_days": 0,
    "limitdown_turnover_ratio": 0.0,
    "limitdown_close_location": 0.0,
    "limitdown_rebound_from_low_pct": 0.0,
    "limitdown_avg_amount_20d": 0.0,
    "limitdown_next_day_gap_pct": 0.0,
    "limitdown_next_day_vwap": 0.0,
    "limitdown_next_day_low": 0.0,
    "limitdown_resealed_last_60m": False,
}


class CandidateChannelError(ValueError):
    """Stable fail-closed channel composition error."""


def _channel_text(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _channel_records(value):
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        return list(value.to_dict("records"))
    return [dict(item) for item in value]


def _channel_factor_columns(decision):
    payload = decision.to_dict()
    result = {
        "factor_path": payload["path"],
        "factor_setup_id": payload["setup_id"],
        "factor_state": payload["state"],
        "factor_score": payload["score"],
        "factor_rejection_code": payload["rejection_code"],
        "factor_triggered": payload["triggered"],
        "simulation_only": payload["simulation_only"],
        "factor_position_cap_pct": payload["position_cap_pct"],
        "factor_risk_budget_pct": payload["risk_budget_pct"],
        "factor_max_hold_days": payload["max_hold_days"],
        "factor_max_concurrent": payload["max_concurrent"],
        "factor_max_new_per_day": payload["max_new_per_day"],
    }
    result.update(payload["features"])
    if payload["triggered"]:
        result.update({
            "entry_price": payload["entry_price"],
            "stop_loss": payload["stop_loss"],
            "take_profit": payload["take_profit"],
            "position_pct": payload["position_cap_pct"],
            "execution_allowed": True,
            "signal_action": "continue",
        })
    return result


def _channel_evaluate(
    evaluator,
    row,
    decision_at,
    history_provider,
    intraday_provider,
    common,
):
    code = factor_clean_code(row.get("code"))
    daily = history_provider(code)
    daily_records = _channel_records(daily)
    kwargs = dict(common)
    kwargs.update({
        "code": code,
        "decision_at": decision_at,
        "daily_bars": daily_records,
        "intraday_bars": (),
    })
    if evaluator is evaluate_wave3_factor:
        kwargs.update({
            "industry_relative_strength": factor_number(
                row.get("industry_relative_strength")
            ),
            "theme_heat_score": factor_number(row.get("theme_heat_score")),
        })
    else:
        kwargs.update({
            "listing_days": max(
                int(factor_number(row.get("listing_days"))), len(daily_records)
            ),
            "is_st": bool(row.get("is_st")) or "ST" in str(row.get("name") or "").upper(),
            "delisting": bool(row.get("delisting")) or "退" in str(row.get("name") or ""),
            "sector_limitdown_acceleration": factor_number(
                row.get("sector_limitdown_acceleration")
            ),
        })
    first = evaluator(**kwargs)
    if not first.eligible or intraday_provider is None:
        return first
    kwargs["intraday_bars"] = _channel_records(intraday_provider(code))
    return evaluator(**kwargs)


def build_factor_channel_rows(
    market_frame,
    decision_at,
    history_provider,
    intraday_provider=None,
    market_state="NORMAL",
    disclosure_provider=None,
    wave3_enabled=True,
    limitdown_enabled=True,
    settings=None,
):
    """Evaluate a bounded liquidity-first screen and return rows plus audit."""
    try:
        import pandas as pd
    except ImportError:
        raise CandidateChannelError("PANDAS_REQUIRED")
    config = dict(CANDIDATE_CHANNEL_DEFAULTS)
    config.update(dict(settings or {}))
    frame = market_frame.copy()
    if frame.empty:
        return pd.DataFrame(), []
    for column in ("amount", "pct_chg", "price"):
        if column not in frame:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    names = frame["name"].astype(str) if "name" in frame else pd.Series("", index=frame.index)
    screen = frame[
        ~names.str.contains("ST|退", regex=True, na=False)
        & (frame["price"] > 0)
        & (frame["amount"] >= 20000000.0)
        & (frame["pct_chg"] >= -8.0)
        & (frame["pct_chg"] <= 9.8)
    ].copy()
    screen = screen.sort_values(
        ["amount", "pct_chg", "code"], ascending=[False, False, True]
    ).head(int(config["factor_screen_max"]))
    results = []
    audit = []
    common = {
        "market_state": market_state,
        "disclosure_risk": "clear",
    }
    for _, source in screen.iterrows():
        row = dict(source)
        code = factor_clean_code(row.get("code"))
        common["disclosure_risk"] = (
            disclosure_provider(code) if callable(disclosure_provider) else "clear"
        )
        evaluators = []
        if wave3_enabled:
            evaluators.append(evaluate_wave3_factor)
        if limitdown_enabled:
            evaluators.append(evaluate_limitdown_exhaustion_factor)
        for evaluator in evaluators:
            try:
                decision = _channel_evaluate(
                    evaluator,
                    row,
                    decision_at,
                    history_provider,
                    intraday_provider,
                    common,
                )
            except FactorContractError:
                raise
            except Exception as exc:
                audit.append({
                    "code": code,
                    "factor": evaluator.__name__,
                    "state": "error",
                    "rejection_code": type(exc).__name__,
                })
                continue
            audit.append({
                "code": code,
                "factor": decision.path,
                "setup_id": decision.setup_id,
                "eligible": decision.eligible,
                "triggered": decision.triggered,
                "state": decision.state,
                "rejection_code": decision.rejection_code,
                "score": decision.score,
                "reasons": list(decision.reasons),
                "features": dict(decision.features),
            })
            if not decision.eligible:
                continue
            prepared = dict(row)
            prepared.update(_channel_factor_columns(decision))
            prepared["score"] = max(
                factor_number(prepared.get("score")), decision.score
            )
            prepared["candidate_channels"] = decision.path
            results.append(prepared)
    if not results:
        return pd.DataFrame(), audit
    result = pd.DataFrame(results)
    result = result.sort_values(
        ["factor_triggered", "factor_score", "code"],
        ascending=[False, False, True],
    )
    bounded = []
    for path, limit in (
        (FACTOR_PATH_WAVE3, int(config["wave3_max"])),
        (FACTOR_PATH_LIMITDOWN, int(config["limitdown_max"])),
    ):
        bounded.append(result[result["factor_path"] == path].head(limit))
    return pd.concat(bounded, ignore_index=True) if bounded else pd.DataFrame(), audit


def merge_candidate_channels(momentum_frame, factor_frame, settings=None):
    """Dedupe by code, preserve all channel attributions and cap at 45 rows."""
    try:
        import pandas as pd
    except ImportError:
        raise CandidateChannelError("PANDAS_REQUIRED")
    config = dict(CANDIDATE_CHANNEL_DEFAULTS)
    config.update(dict(settings or {}))
    momentum = momentum_frame.copy().head(int(config["momentum_max"]))
    if not momentum.empty:
        if "factor_path" not in momentum:
            momentum["factor_path"] = FACTOR_PATH_MOMENTUM
        else:
            momentum["factor_path"] = momentum["factor_path"].fillna(
                FACTOR_PATH_MOMENTUM
            )
        if "candidate_channels" not in momentum:
            momentum["candidate_channels"] = FACTOR_PATH_MOMENTUM
        else:
            momentum["candidate_channels"] = momentum[
                "candidate_channels"
            ].fillna(FACTOR_PATH_MOMENTUM)
        momentum["factor_triggered"] = momentum.get("factor_triggered", False)
        momentum["factor_score"] = momentum.get("factor_score", 0.0)
        momentum["simulation_only"] = momentum.get("simulation_only", False)
    factor = factor_frame.copy()
    combined = pd.concat([momentum, factor], ignore_index=True, sort=False)
    if combined.empty:
        return combined
    combined["code"] = combined["code"].map(factor_clean_code)
    combined["_momentum"] = combined["candidate_channels"].astype(str).map(
        lambda value: int(FACTOR_PATH_MOMENTUM in value.split("|"))
    )
    combined["_priority"] = (
        combined.get("factor_triggered", False).fillna(False).astype(int) * 1000
        + combined["_momentum"] * 100
        + pd.to_numeric(combined.get("factor_score", 0), errors="coerce").fillna(0)
        + pd.to_numeric(combined.get("score", 0), errors="coerce").fillna(0) / 1000.0
    )
    rows = []
    for code, group in combined.groupby("code", sort=True):
        group = group.sort_values("_priority", ascending=False)
        chosen = dict(group.iloc[0])
        channels = sorted({
            str(value) for value in group["candidate_channels"].dropna()
            if str(value)
        })
        chosen["candidate_channels"] = "|".join(channels)
        chosen["candidate_channel_count"] = len(channels)
        attributions = []
        for _, attributed in group.iterrows():
            path = _channel_text(attributed.get("factor_path")) or FACTOR_PATH_MOMENTUM
            if path not in (
                FACTOR_PATH_MOMENTUM, FACTOR_PATH_WAVE3, FACTOR_PATH_LIMITDOWN
            ):
                continue
            item = {
                "factor_path": path,
                "setup_id": _channel_text(attributed.get("factor_setup_id")),
                "state": _channel_text(attributed.get("factor_state")) or "baseline",
                "score": factor_number(attributed.get("factor_score")),
                "triggered": bool(attributed.get("factor_triggered")),
                "rejection_code": _channel_text(
                    attributed.get("factor_rejection_code")
                ),
                "simulation_only": bool(attributed.get("simulation_only")),
            }
            if item not in attributions:
                attributions.append(item)
        chosen["factor_attributions"] = attributions
        rows.append(chosen)
    result = pd.DataFrame(rows).drop(
        columns=["_priority", "_momentum"], errors="ignore"
    )
    result["_triggered"] = result.get("factor_triggered", False).fillna(False).astype(int)
    result["_factor_score"] = pd.to_numeric(
        result.get("factor_score", 0), errors="coerce"
    ).fillna(0)
    result["_score"] = pd.to_numeric(result.get("score", 0), errors="coerce").fillna(0)
    result = result.sort_values(
        ["_triggered", "_factor_score", "_score", "code"],
        ascending=[False, False, False, True],
    ).head(int(config["total_max"])).drop(
        columns=["_triggered", "_factor_score", "_score"], errors="ignore"
    ).reset_index(drop=True)
    for name, default in FACTOR_FEATURE_DEFAULTS.items():
        if name not in result:
            result[name] = default
        elif isinstance(default, str):
            result[name] = result[name].fillna(default)
        else:
            result[name] = result[name].where(result[name].notna(), default)
    result["factor_path"] = result["factor_path"].fillna(FACTOR_PATH_MOMENTUM)
    result["candidate_channels"] = result["candidate_channels"].fillna(
        FACTOR_PATH_MOMENTUM
    )
    return result
