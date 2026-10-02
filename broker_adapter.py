"""Platform-neutral broker boundary kept outside strategy and risk logic.

The adapter is intentionally small at this stage.  Strategy code produces a
validated :class:`ExecutionIntent`; an adapter may later submit it to QMT or a
broker API.  Until then ``ManualAdapter`` only returns a reviewable order
ticket and never claims that a broker accepted the order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from execution_contracts import BrokerSnapshot, ExecutionIntent


class BrokerAdapterError(RuntimeError):
    """Base error for adapter availability or contract failures."""


class BrokerAdapterUnavailable(BrokerAdapterError):
    """Raised when an operation is not enabled for the current adapter."""


@dataclass(frozen=True)
class SubmissionReceipt:
    """Normalized result of a submission attempt.

    ``manual_confirmation_required`` is deliberately distinct from
    ``submitted`` and ``unknown``.  It means no broker request was made.
    """

    adapter: str
    client_order_id: str
    status: str
    broker_order_id: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        for name in ("adapter", "client_order_id", "status"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            object.__setattr__(self, name, value)
        status = self.status.lower()
        if status not in {
            "manual_confirmation_required",
            "submitted",
            "unknown",
            "rejected",
        }:
            raise ValueError("unsupported submission status")
        object.__setattr__(self, "status", status)
        if self.broker_order_id is not None:
            broker_order_id = str(self.broker_order_id).strip()
            object.__setattr__(self, "broker_order_id", broker_order_id or None)
        object.__setattr__(self, "message", str(self.message or "").strip())
        if self.status == "manual_confirmation_required" and self.broker_order_id:
            raise ValueError("manual receipt cannot contain a broker_order_id")
        if self.status in {"unknown", "rejected"} and not self.message:
            raise ValueError(f"{self.status} receipt requires a message")

    @property
    def broker_request_made(self) -> bool:
        return self.status in {"submitted", "unknown", "rejected"}

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "client_order_id": self.client_order_id,
            "status": self.status,
            "broker_order_id": self.broker_order_id,
            "message": self.message,
            "broker_request_made": self.broker_request_made,
        }


class BrokerAdapter(Protocol):
    """Minimal boundary future broker implementations must satisfy."""

    adapter_name: str

    def get_snapshot(self, *, account_scope_id: str) -> BrokerSnapshot:
        """Return one normalized, hashed account/position/order snapshot."""

    def submit_intent(self, intent: ExecutionIntent) -> SubmissionReceipt:
        """Submit exactly one already-admitted execution intent."""

    def cancel_order(self, *, broker_order_id: str) -> SubmissionReceipt:
        """Cancel one broker order and return a normalized receipt."""

    def capabilities(self) -> Mapping[str, bool]:
        """Return explicit feature flags without implying availability."""


class ManualAdapter:
    """Safe placeholder that creates a handoff ticket and never submits."""

    adapter_name = "manual"

    def get_snapshot(self, *, account_scope_id: str) -> BrokerSnapshot:
        raise BrokerAdapterUnavailable(
            "manual adapter cannot fetch a broker snapshot; import a verified snapshot"
        )

    def submit_intent(self, intent: ExecutionIntent) -> SubmissionReceipt:
        if not isinstance(intent, ExecutionIntent):
            raise TypeError("submit_intent requires a validated ExecutionIntent")
        return SubmissionReceipt(
            adapter=self.adapter_name,
            client_order_id=intent.client_order_id,
            status="manual_confirmation_required",
            message="No broker request was made; copy the exact intent to the broker platform.",
        )

    def cancel_order(self, *, broker_order_id: str) -> SubmissionReceipt:
        raise BrokerAdapterUnavailable(
            "manual adapter cannot cancel a broker order; cancel it on the broker platform"
        )

    def capabilities(self) -> Mapping[str, bool]:
        return {
            "snapshot": False,
            "submit": False,
            "cancel": False,
            "manual_ticket": True,
        }
