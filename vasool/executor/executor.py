"""The executor — where I8 is enforced and where the unknown-outcome problem
is handled.

Ordering here is the invariant: ledger record, then action, then outcome
record. A crash at any point leaves a chain that says exactly how far we got.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vasool.core.policy import Costs
from vasool.core.types import (
    CONTACT_INTERVENTIONS,
    RETRY_INTERVENTIONS,
    ActionOutcome,
    ActionProposal,
    CaseState,
    CaseStatus,
    ContactRecord,
)
from vasool.executor.backend import (
    PaymentsBackend,
    ProviderError,
    ReconciliationUnknown,
    UnknownOutcome,
)
from vasool.executor.ledger import Ledger
from vasool.kernel.gate import Review
from vasool.kernel.invariants import action_key, i8_audit_before_action


@dataclass
class ExecutionStats:
    executed: int = 0
    denied: int = 0
    unknown_outcomes: int = 0
    reconciled_landed: int = 0
    reconciled_absent: int = 0
    unresolved: int = 0
    provider_errors: int = 0


class Executor:
    def __init__(
        self,
        backend: PaymentsBackend,
        ledger: Ledger,
        costs: Costs,
    ) -> None:
        self.backend = backend
        self.ledger = ledger
        self.costs = costs
        self.stats = ExecutionStats()

    # -- denial path --------------------------------------------------------

    def record_denial(
        self, proposal: ActionProposal, case: CaseState, review: Review,
        now: datetime,
    ) -> None:
        self.stats.denied += 1
        self.ledger.append(
            "denied", case.case_id,
            {
                "intervention": proposal.intervention.value,
                "channel": proposal.channel.value,
                "amount_paise": proposal.amount_paise,
                "diagnosis": proposal.diagnosis.failure_class.value,
                "diagnosis_source": proposal.diagnosis.source,
                "confidence": round(proposal.diagnosis.confidence, 3),
                "denials": [d.value for d in review.verdict.denials],
                "invariants": list(review.verdict.invariant_ids),
                "detail": review.verdict.detail,
            },
            at=now,
        )

    # -- execution path -----------------------------------------------------

    def execute(
        self, proposal: ActionProposal, case: CaseState, now: datetime,
    ) -> ActionOutcome:
        """Execute an approved proposal. Assumes the Gate already allowed it."""
        key = action_key(proposal)

        # I8. Write-ahead: the record of intent exists before anything happens.
        receipt = self.ledger.append(
            "intent", case.case_id,
            {
                "idempotency_key": key,
                "intervention": proposal.intervention.value,
                "channel": proposal.channel.value,
                "amount_paise": proposal.amount_paise,
                "currency": proposal.currency,
                "diagnosis": proposal.diagnosis.failure_class.value,
                "diagnosis_source": proposal.diagnosis.source,
                "confidence": round(proposal.diagnosis.confidence, 3),
                "evidence_fields": list(proposal.diagnosis.evidence_fields),
                "rationale": proposal.rationale[:400],
            },
            at=now,
        )
        audit = i8_audit_before_action(receipt)
        if not audit.allowed:                       # pragma: no cover - defensive
            raise RuntimeError("I8 violated: action attempted without a receipt")

        try:
            outcome = self.backend.execute(proposal, case, key, now)
        except UnknownOutcome as exc:
            outcome = self._resolve_unknown(exc, proposal, case, now, receipt)
        except ProviderError as exc:
            self.stats.provider_errors += 1
            self.ledger.append(
                "provider_error", case.case_id,
                {"idempotency_key": key, "detail": str(exc)}, at=now,
            )
            outcome = ActionOutcome(
                executed=False, succeeded=False, collected_paise=0,
                cost_paise=0, detail=f"provider error: {exc}",
            )

        self._apply(proposal, case, outcome, key, now)
        self.ledger.append(
            "outcome", case.case_id,
            {
                "idempotency_key": key,
                "executed": outcome.executed,
                "succeeded": outcome.succeeded,
                "collected_paise": outcome.collected_paise,
                "cost_paise": outcome.cost_paise,
                "harms": list(outcome.harms),
                "detail": outcome.detail,
                "provider_ref": outcome.provider_ref,
                "case_status": case.status.value,
            },
            at=now,
        )
        if outcome.executed:
            self.stats.executed += 1
        return outcome

    # -- the dangerous case -------------------------------------------------

    def _resolve_unknown(
        self, exc: UnknownOutcome, proposal: ActionProposal, case: CaseState,
        now: datetime, receipt: str,
    ) -> ActionOutcome:
        """A write timed out. Find out what actually happened before acting.

        The wrong move here — and the common one — is to retry. A timeout on a
        write is not a decline; the action may well have landed. We ask the
        provider what the idempotency key did, and only replay if it provably
        did nothing.
        """
        self.stats.unknown_outcomes += 1
        self.ledger.append(
            "unknown_outcome", case.case_id,
            {"idempotency_key": exc.idempotency_key, "detail": exc.detail,
             "resolution": "reconciling"},
            at=now,
        )

        try:
            landed = self.backend.reconcile(exc.idempotency_key, case, now)
        except ReconciliationUnknown as unresolved:
            # The read we use to recover from a failed write has itself failed —
            # the expected case, since we are here because the provider is
            # unreachable. Absence is not proven, so replaying is not safe. Stop,
            # record the ambiguity honestly, and leave it for a human.
            self.stats.unresolved += 1
            self.ledger.append(
                "unresolved", case.case_id,
                {"idempotency_key": exc.idempotency_key,
                 "resolution": "reconcile_failed_outcome_unknown",
                 "replaying": False, "detail": str(unresolved)},
                at=now,
            )
            return ActionOutcome(
                executed=False, succeeded=False, collected_paise=0, cost_paise=0,
                detail=f"outcome unknown and unreconcilable: {unresolved}",
                harms=("unresolved_write",),
            )
        if landed is not None:
            self.stats.reconciled_landed += 1
            self.ledger.append(
                "reconciled", case.case_id,
                {"idempotency_key": exc.idempotency_key,
                 "resolution": "action_landed", "succeeded": landed.succeeded},
                at=now,
            )
            return landed

        self.stats.reconciled_absent += 1
        self.ledger.append(
            "reconciled", case.case_id,
            {"idempotency_key": exc.idempotency_key,
             "resolution": "action_absent", "replaying_same_key": True},
            at=now,
        )
        try:
            return self.backend.execute(proposal, case, exc.idempotency_key, now)
        except (UnknownOutcome, ProviderError) as second:
            return ActionOutcome(
                executed=False, succeeded=False, collected_paise=0, cost_paise=0,
                detail=f"unresolved after reconcile: {second}",
            )

    # -- state transition ---------------------------------------------------

    def _apply(
        self, proposal: ActionProposal, case: CaseState, outcome: ActionOutcome,
        key: str, now: datetime,
    ) -> None:
        if not outcome.executed:
            return

        case.attempts += 1
        case.executed_keys.add(key)
        case.spend_paise += outcome.cost_paise

        if proposal.intervention in RETRY_INTERVENTIONS:
            case.notes.append(f"retry:{proposal.intervention.value}:{now.isoformat()}")
        if proposal.intervention in CONTACT_INTERVENTIONS:
            case.contacts.append(
                ContactRecord(at=now, channel=proposal.channel,
                              intervention=proposal.intervention)
            )

        if outcome.succeeded and outcome.collected_paise > 0:
            case.collected_paise += outcome.collected_paise
            case.status = CaseStatus.RECOVERED
            case.closed_at = now
