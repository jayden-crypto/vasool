"""Razorpay test-mode backend over the REST API.

The live counterpart to the simulator. Same interface, so the Gate, the
executor and the arms are unchanged between a 500-case benchmark and a real
transaction against a test-mode account.

Idempotency is real here, not simulated: every payment link carries a
``reference_id`` derived from the action key, and Razorpay rejects a duplicate.
That rejection is what makes ``reconcile`` able to answer the only question
that matters after a timeout — did this action already happen?

Credentials come from RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET. Use test keys
(``rzp_test_…``); the client refuses to start against a live key unless you set
VASOOL_ALLOW_LIVE_KEYS=1, because nothing in this project should touch real
money by accident.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from vasool.core.types import (
    RETRY_INTERVENTIONS,
    ActionOutcome,
    ActionProposal,
    CaseState,
    Intervention,
)
from vasool.executor.backend import (
    ProviderError,
    ReconciliationUnknown,
    SettlementUnknown,
    UnknownOutcome,
)

API_ROOT = "https://api.razorpay.com/v1"

#: Interventions that replay a stored instrument need a saved token and a
#: recurring-enabled account. A plain test-mode account has neither, so rather
#: than fake it, the live backend says so.
UNSUPPORTED_LIVE = RETRY_INTERVENTIONS

LINK_DESCRIPTION = {
    Intervention.PAYMENT_LINK: "Complete your payment",
    Intervention.PART_PAYMENT_LINK: "Pay part of your order now",
    Intervention.INSTRUMENT_REFRESH: "Update your payment method",
    Intervention.MANDATE_REREGISTER: "Re-authorise your subscription",
}


class RazorpayRestBackend:
    name = "razorpay-rest"

    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None,
        timeout: float = 20.0,
        session: Any = None,
    ) -> None:
        self.key_id = key_id or os.environ.get("RAZORPAY_KEY_ID", "")
        self.key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET", "")
        if not (self.key_id and self.key_secret):
            raise RuntimeError(
                "Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test keys)."
            )
        if not self.key_id.startswith("rzp_test_") and \
                os.environ.get("VASOOL_ALLOW_LIVE_KEYS") != "1":
            raise RuntimeError(
                f"{self.key_id[:12]}… is not a test key. Refusing to start. "
                "Set VASOOL_ALLOW_LIVE_KEYS=1 only if you really mean it."
            )
        self.timeout = timeout
        if session is not None:
            self._session = session
        else:
            import requests
            self._session = requests.Session()
            self._session.auth = (self.key_id, self.key_secret)

    # -- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Issue one request, classifying failures by what they actually prove.

        The distinction that matters is whether the request could have changed
        state. A 502 on a POST and a connection reset mid-flight are *unknown
        outcomes*, not clean failures — the write may well have landed. An
        earlier version raised ProviderError for both, whose docstring says
        "safe to treat as a non-event", so genuinely ambiguous writes took the
        ignore-it path with no reconciliation.
        """
        import requests
        writes = method.upper() in ("POST", "PUT", "PATCH", "DELETE")
        try:
            response = self._session.request(
                method, f"{API_ROOT}{path}", timeout=self.timeout, **kwargs,
            )
        except requests.Timeout as exc:
            raise UnknownOutcome("", f"{method} {path} timed out") from exc
        except requests.RequestException as exc:
            if writes:
                raise UnknownOutcome("", f"{method} {path}: {exc}") from exc
            raise ProviderError(f"{method} {path}: {exc}") from exc

        if response.status_code >= 500:
            if writes:
                raise UnknownOutcome(
                    "", f"{response.status_code} from Razorpay on {method} {path}")
            raise ProviderError(f"{response.status_code} from Razorpay")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(f"non-JSON response: {response.text[:200]}") from exc
        if response.status_code >= 400:
            error = body.get("error", {})
            raise ProviderError(
                f"{error.get('code', response.status_code)}: "
                f"{error.get('description', body)}"
            )
        return body

    # -- PaymentsBackend ----------------------------------------------------

    def is_settled(self, case: CaseState) -> bool:
        """Fresh read of the order. This is I1's source of truth.

        Raises rather than guessing. An earlier version returned False when the
        read failed, with a comment claiming that was the safe direction. It was
        the opposite: False means "not settled", which lets I1 approve, so an
        unreachable provider silently unlocked exactly the money action I1 is
        there to prevent.
        """
        try:
            order = self._request("GET", f"/orders/{case.event.order_id}")
        except (ProviderError, UnknownOutcome) as exc:
            raise SettlementUnknown(
                f"cannot read order {case.event.order_id}: {exc}"
            ) from exc
        return order.get("status") == "paid" or int(order.get("amount_paid", 0)) > 0

    def execute(
        self, proposal: ActionProposal, case: CaseState,
        idempotency_key: str, now: datetime,
    ) -> ActionOutcome:
        if proposal.intervention in UNSUPPORTED_LIVE:
            raise ProviderError(
                f"{proposal.intervention.value} needs a saved token and a "
                "recurring-enabled account; not available on a plain test key"
            )
        if proposal.intervention not in LINK_DESCRIPTION:
            return ActionOutcome(False, False, 0, 0, "no live action for this intervention")

        payload: dict[str, Any] = {
            "amount": proposal.amount_paise,
            "currency": proposal.currency,
            "accept_partial": proposal.intervention is Intervention.PART_PAYMENT_LINK,
            "description": LINK_DESCRIPTION[proposal.intervention],
            "reference_id": idempotency_key,
            "notify": {"sms": False, "email": False},   # we drive our own channel
            "reminder_enable": False,
            "notes": {
                "vasool_case": case.case_id,
                "vasool_intervention": proposal.intervention.value,
                "vasool_diagnosis": proposal.diagnosis.failure_class.value,
                "vasool_diagnosis_source": proposal.diagnosis.source,
                "vasool_original_order": case.event.order_id,
            },
        }
        if proposal.intervention is Intervention.PART_PAYMENT_LINK:
            payload["first_min_partial_amount"] = proposal.amount_paise

        try:
            link = self._request("POST", "/payment_links", json=payload)
        except UnknownOutcome as exc:
            raise UnknownOutcome(idempotency_key, exc.detail) from exc
        except ProviderError as exc:
            # A duplicate reference_id means this exact action already ran.
            if "reference_id" in str(exc).lower():
                existing = self._find_by_reference(idempotency_key)
                if existing is not None:
                    return self._outcome_from_link(existing, proposal, replay=True)
            raise

        return self._outcome_from_link(link, proposal, replay=False)

    def reconcile(
        self, idempotency_key: str, case: CaseState, now: datetime,
    ) -> Optional[ActionOutcome]:
        """None here means *provably absent*, and nothing else."""
        link = self._find_by_reference(idempotency_key)
        return None if link is None else self._outcome_from_link(link, None, replay=True)

    # -- helpers ------------------------------------------------------------

    def _find_by_reference(self, reference_id: str) -> Optional[dict[str, Any]]:
        """Look up what a key did. Raises rather than guessing.

        Swallowing the error and returning None turned "could not read" into
        "does not exist", which the executor then acts on by replaying a money
        action — and records as ``action_absent`` in the ledger.
        """
        if not reference_id:
            raise ReconciliationUnknown("no reference id to look up")
        try:
            body = self._request(
                "GET", "/payment_links", params={"reference_id": reference_id},
            )
        except (ProviderError, UnknownOutcome) as exc:
            raise ReconciliationUnknown(
                f"cannot read payment_links for {reference_id}: {exc}"
            ) from exc
        items = body.get("payment_links") or body.get("items") or []
        return items[0] if items else None

    @staticmethod
    def _outcome_from_link(
        link: dict[str, Any], proposal: Optional[ActionProposal], replay: bool,
    ) -> ActionOutcome:
        paid = int(link.get("amount_paid", 0))
        status = link.get("status", "created")
        return ActionOutcome(
            executed=True,
            succeeded=status == "paid" and paid > 0,
            collected_paise=paid,
            cost_paise=0,          # real link creation is free; channel cost is ours
            detail=(
                f"payment_link {link.get('id')} status={status}"
                + (" (recovered by reference_id)" if replay else "")
                + (f" url={link.get('short_url')}" if link.get("short_url") else "")
            ),
            provider_ref=link.get("id"),
        )
