from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from strategy_snapshot_runtime import tradability_reject_reason as _portable_tradability_reject_reason


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
    return _portable_tradability_reject_reason(row)
