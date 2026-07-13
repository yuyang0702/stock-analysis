from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import math


def attainable_sell_target(current_qty: int, target_qty: int, closeable_qty: int) -> tuple[int | None, str]:
    required = max(0, int(current_qty) - max(0, int(target_qty)))
    closeable = max(0, int(closeable_qty))
    if required == 0:
        return int(target_qty), ""
    if closeable == 0:
        return None, "t_plus_one"
    sell_qty = min(required, closeable)
    return int(current_qty) - sell_qty, "" if sell_qty == required else "partial_sellable"


@dataclass(frozen=True)
class MarketRegimeState:
    current: str = "NORMAL"
    candidate: str = ""
    confirmations: int = 0

    def advance(self, observed: str) -> "MarketRegimeState":
        observed = observed if observed in {"NORMAL", "CAUTION", "RISK_OFF"} else "NORMAL"
        if observed == self.current:
            return MarketRegimeState(self.current, "", 0)
        confirmations = self.confirmations + 1 if observed == self.candidate else 1
        threshold = 3 if observed == "NORMAL" else 2
        return MarketRegimeState(observed, "", 0) if confirmations >= threshold else MarketRegimeState(
            self.current, observed, confirmations,
        )


def tradability_reject_reason(row: Mapping[str, Any]) -> str:
    def flag(name: str) -> bool:
        value = row.get(name)
        return value is not None and not (isinstance(value, float) and math.isnan(value)) and bool(value)

    if flag("paused"):
        return "buy_suspended"
    if flag("is_st") or "ST" in str(row.get("name") or "").upper():
        return "buy_st"
    if flag("delisting"):
        return "buy_delisting"
    if flag("special_listing_stage"):
        return "buy_special_listing_stage"
    if 0 < float(row.get("listing_days") or 0) < 5:
        return "buy_special_listing_stage"
    if float(row.get("quote_age_sec") or 0) > 120:
        return "buy_quote_stale"
    entry = float(row.get("entry_price") or 0)
    price = float(row.get("price") or 0)
    atr = float(row.get("atr14") or 0)
    if entry > 0 and price > entry * (1 + min(0.02, 0.5 * atr / entry if atr > 0 else 0.02)):
        return "buy_chasing"
    if row.get("amount") is not None and float(row.get("amount") or 0) < 20_000_000:
        return "buy_illiquid"
    return ""
