"""Portable contracts shared by multipath factor discovery and replay.

This module intentionally stays compatible with JoinQuant's Python 3.6
runtime.  It has no network, database, account or credential dependency.
"""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone


FACTOR_CONTRACT_VERSION = "2026-08-10.1"
SHANGHAI_TZ = timezone(timedelta(hours=8))
FACTOR_PATH_MOMENTUM = "momentum_v1"
FACTOR_PATH_WAVE3 = "wave3_v1"
FACTOR_PATH_LIMITDOWN = "limitdown_exhaustion_v1"
FACTOR_PATHS = (
    FACTOR_PATH_MOMENTUM,
    FACTOR_PATH_WAVE3,
    FACTOR_PATH_LIMITDOWN,
)


class FactorContractError(ValueError):
    """Stable fail-closed factor contract error."""


def factor_canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def factor_canonical_sha256(value):
    return hashlib.sha256(
        factor_canonical_json(value).encode("utf-8")
    ).hexdigest()


def factor_number(value, default=0.0):
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def factor_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def factor_clean_code(value):
    digits = "".join(filter(str.isdigit, str(value or "")))[:6]
    return digits.zfill(6) if digits else ""


def factor_aware_datetime(value, field_name="decision_at"):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = factor_text(value).replace("Z", "+00:00")
        if len(text) < 6 or text[-6] not in ("+", "-") or text[-3] != ":":
            raise FactorContractError("TIMEZONE_REQUIRED: " + field_name)
        body = text[:-6]
        sign = 1 if text[-6] == "+" else -1
        try:
            offset = sign * (int(text[-5:-3]) * 60 + int(text[-2:]))
        except (TypeError, ValueError):
            raise FactorContractError("INVALID_TIMESTAMP: " + field_name)
        parsed = None
        for pattern in (
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(body, pattern)
                break
            except ValueError:
                pass
        if parsed is None:
            raise FactorContractError("INVALID_TIMESTAMP: " + field_name)
        parsed = parsed.replace(tzinfo=timezone(timedelta(minutes=offset)))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FactorContractError("TIMEZONE_REQUIRED: " + field_name)
    return parsed.astimezone(SHANGHAI_TZ)


def factor_available_at(row):
    if not isinstance(row, dict):
        try:
            row = dict(row)
        except Exception:
            raise FactorContractError("BAR_MUST_BE_MAPPING")
    value = row.get("available_at") or row.get("time") or row.get("date")
    text = factor_text(value)
    if not text:
        raise FactorContractError("BAR_AVAILABLE_AT_REQUIRED")
    if len(text) == 10:
        text += "T15:00:00+08:00"
    elif len(text) == 19 and text[10] in ("T", " "):
        text += "+08:00"
    return factor_aware_datetime(text, "bar.available_at")


def normalize_factor_bars(rows, decision_at, require_prior_day=False):
    """Normalize sorted point-in-time bars and reject any future evidence."""
    decision = factor_aware_datetime(decision_at)
    normalized = []
    for raw in list(rows or ()):
        row = dict(raw)
        available = factor_available_at(row)
        if available > decision:
            raise FactorContractError("FACTOR_BAR_FROM_FUTURE")
        if require_prior_day and available.date() >= decision.date():
            raise FactorContractError("DAILY_BAR_NOT_COMPLETED_BEFORE_DECISION")
        item = dict(row)
        item["available_at"] = available.isoformat()
        for key in (
            "open", "high", "low", "close", "volume", "money", "amount",
            "turnover", "pre_close", "prev_close", "high_limit", "low_limit",
            "factor",
        ):
            if key in item:
                item[key] = factor_number(item.get(key), None)
        normalized.append(item)
    normalized.sort(key=lambda item: item["available_at"])
    for previous, current in zip(normalized, normalized[1:]):
        if current["available_at"] <= previous["available_at"]:
            raise FactorContractError("FACTOR_BARS_NOT_STRICTLY_ORDERED")
    return normalized


class FactorDecision(object):
    """Portable immutable-by-convention result from one factor evaluator."""

    __slots__ = (
        "path", "setup_id", "eligible", "triggered", "score", "state",
        "rejection_code", "reasons", "features", "entry_price", "stop_loss",
        "take_profit", "position_cap_pct", "risk_budget_pct", "max_hold_days",
        "max_concurrent", "max_new_per_day", "simulation_only",
    )

    def __init__(
        self,
        path,
        setup_id="",
        eligible=False,
        triggered=False,
        score=0.0,
        state="invalid",
        rejection_code="",
        reasons=(),
        features=None,
        entry_price=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        position_cap_pct=0.0,
        risk_budget_pct=0.0,
        max_hold_days=0,
        max_concurrent=0,
        max_new_per_day=0,
        simulation_only=True,
    ):
        if path not in FACTOR_PATHS:
            raise FactorContractError("UNKNOWN_FACTOR_PATH: " + str(path))
        score_value = factor_number(score, -1.0)
        if score_value < 0 or score_value > 100:
            raise FactorContractError("INVALID_FACTOR_SCORE")
        self.path = path
        self.setup_id = factor_text(setup_id)
        self.eligible = bool(eligible)
        self.triggered = bool(triggered)
        self.score = round(score_value, 6)
        self.state = factor_text(state) or "invalid"
        self.rejection_code = factor_text(rejection_code)
        self.reasons = tuple(factor_text(value) for value in reasons if factor_text(value))
        self.features = dict(features or {})
        self.entry_price = round(max(factor_number(entry_price), 0.0), 4)
        self.stop_loss = round(max(factor_number(stop_loss), 0.0), 4)
        self.take_profit = round(max(factor_number(take_profit), 0.0), 4)
        self.position_cap_pct = round(max(factor_number(position_cap_pct), 0.0), 4)
        self.risk_budget_pct = round(max(factor_number(risk_budget_pct), 0.0), 4)
        self.max_hold_days = max(0, int(factor_number(max_hold_days)))
        self.max_concurrent = max(0, int(factor_number(max_concurrent)))
        self.max_new_per_day = max(0, int(factor_number(max_new_per_day)))
        self.simulation_only = bool(simulation_only)
        if self.triggered and not self.eligible:
            raise FactorContractError("TRIGGERED_FACTOR_MUST_BE_ELIGIBLE")
        if self.triggered and not (
            0 < self.stop_loss < self.entry_price < self.take_profit
        ):
            raise FactorContractError("TRIGGERED_FACTOR_PRICE_PLAN_INVALID")

    def to_dict(self):
        return {
            "path": self.path,
            "setup_id": self.setup_id,
            "eligible": self.eligible,
            "triggered": self.triggered,
            "score": self.score,
            "state": self.state,
            "rejection_code": self.rejection_code,
            "reasons": list(self.reasons),
            "features": dict(self.features),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "position_cap_pct": self.position_cap_pct,
            "risk_budget_pct": self.risk_budget_pct,
            "max_hold_days": self.max_hold_days,
            "max_concurrent": self.max_concurrent,
            "max_new_per_day": self.max_new_per_day,
            "simulation_only": self.simulation_only,
        }


def factor_setup_id(path, code, anchors, version=FACTOR_CONTRACT_VERSION):
    payload = {
        "path": path,
        "code": factor_clean_code(code),
        "anchors": list(anchors),
        "version": version,
    }
    return path + ":" + factor_canonical_sha256(payload)[:20]
