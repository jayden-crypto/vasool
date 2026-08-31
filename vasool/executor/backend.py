"""The payments backend interface — the only place credentials ever live.

Three implementations share this surface: a simulator (used by the benchmark),
Razorpay's REST API in test mode, and Razorpay's official MCP server. Arms and
the kernel are written against the interface, so switching between a simulated
batch and a live test-mode transaction changes one constructor argument.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from vasool.core.types import ActionOutcome, ActionProposal, CaseState


class UnknownOutcome(Exception):
    """A write was sent and the result is genuinely unknown.

    This is the dangerous failure in payments: a timeout is not a decline. The
    action may have taken effect. Never blind-retry an UnknownOutcome — resolve
    it with ``reconcile`` and the idempotency key first.
    """

    def __init__(self, idempotency_key: str, detail: str = "") -> None:
        super().__init__(f"unknown outcome for {idempotency_key}: {detail}")
        self.idempotency_key = idempotency_key
        self.detail = detail


class ProviderError(Exception):
    """A write definitively failed. Safe to treat as a non-event."""


class SettlementUnknown(Exception):
    """The settlement state of an order could not be read.

    Distinct from "not settled", and the distinction is the whole point. I1
    exists to stop a second collection, so an unreadable provider must block
    money movement rather than wave it through. Collapsing unknown into
    unsettled is a fail-open on the one invariant that guards against
    double-charging a customer.
    """


class PaymentsBackend(Protocol):
    name: str

    def is_settled(self, case: CaseState) -> bool:
        """Fresh read: has this order been paid, through any route?

        Raises ``SettlementUnknown`` when the answer cannot be determined. It
        must never return False to mean "could not tell".
        """
        ...

    def execute(
        self,
        proposal: ActionProposal,
        case: CaseState,
        idempotency_key: str,
        now: datetime,
    ) -> ActionOutcome:
        ...

    def reconcile(
        self, idempotency_key: str, case: CaseState, now: datetime,
    ) -> Optional[ActionOutcome]:
        """Resolve an UnknownOutcome by looking up what the key actually did.

        Returns the outcome if the action landed, or None if it provably did
        not — in which case retrying with the *same* key is safe.
        """
        ...
