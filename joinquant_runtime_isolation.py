from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


LIVE_RUN_TYPE = "sim_trade"
BACKTEST_RUN_TYPES = frozenset({"simple_backtest", "full_backtest"})
RUN_TYPE_HEADER = "X-JoinQuant-Run-Type"
TEMPLATE_VERSION_HEADER = "X-JoinQuant-Template-Version"
PROTOCOL_VERSION_HEADER = "X-JoinQuant-Protocol-Version"
PROTOCOL_VERSION = "1"


@dataclass(frozen=True)
class RuntimeIdentityError(ValueError):
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def validate_request_identity(
    headers: Mapping[str, Any], required_template: str,
) -> dict[str, str]:
    run_type = str(headers.get(RUN_TYPE_HEADER) or "").strip()
    template = str(headers.get(TEMPLATE_VERSION_HEADER) or "").strip()
    protocol = str(headers.get(PROTOCOL_VERSION_HEADER) or "").strip()
    if run_type != LIVE_RUN_TYPE:
        raise RuntimeIdentityError(
            "JOINQUANT_RUNTIME_NOT_SIM_TRADE",
            "production endpoints accept only sim_trade",
        )
    if template != required_template:
        raise RuntimeIdentityError(
            "JOINQUANT_TEMPLATE_VERSION_MISMATCH",
            "request template does not match the deployed template",
        )
    if protocol != PROTOCOL_VERSION:
        raise RuntimeIdentityError(
            "JOINQUANT_PROTOCOL_VERSION_MISMATCH",
            "request protocol does not match the deployed protocol",
        )
    return {
        "run_type": run_type,
        "template_version": template,
        "protocol_version": protocol,
    }


def validate_snapshot_identity(
    payload: Mapping[str, Any], identity: Mapping[str, str],
) -> None:
    run_type = str(
        payload.get("runtime_mode") or payload.get("run_type") or ""
    ).strip()
    template = str(
        payload.get("strategy_template_version")
        or payload.get("template_version")
        or ""
    ).strip()
    protocol = str(payload.get("runtime_protocol_version") or "").strip()
    if run_type != identity["run_type"]:
        raise RuntimeIdentityError(
            "JOINQUANT_SNAPSHOT_RUNTIME_MISMATCH",
            "snapshot runtime does not match the authenticated request runtime",
        )
    if template != identity["template_version"]:
        raise RuntimeIdentityError(
            "JOINQUANT_SNAPSHOT_TEMPLATE_MISMATCH",
            "snapshot template does not match the authenticated request template",
        )
    if protocol != identity["protocol_version"]:
        raise RuntimeIdentityError(
            "JOINQUANT_SNAPSHOT_PROTOCOL_MISMATCH",
            "snapshot protocol does not match the authenticated request protocol",
        )
