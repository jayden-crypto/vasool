"""The Gate — the only thing standing between a proposal and a rupee.

The Gate is deliberately boring. It builds a context (including one *live*
read of provider state), runs the invariants in order, and returns a verdict
with named reason codes. It contains no heuristics, no model, and no way to be
persuaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from vasool.core.policy import Costs, Policy
from vasool.core.types import ActionProposal, CaseState, Verdict
from vasool.kernel.invariants import ALL_INVARIANTS, GateContext


class SettlementReader(Protocol):
    """A fresh read of whether this order has been paid, by any route.

    Implemented against the simulator in benchmarks and against Razorpay's
    orders API in the live path. It must not read from local case state — the
    entire value of I1 is that it catches a case whose local state is stale.
    """
    def __call__(self, case: CaseState) -> bool: ...


@dataclass(frozen=True)
class Review:
    verdict: Verdict
    context: GateContext

    @property
    def allowed(self) -> bool:
        return self.verdict.allowed

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(d.value for d in self.verdict.denials)


class Gate:
    def __init__(
        self,
        policy: Policy,
        costs: Costs,
        settlement_reader: SettlementReader,
    ) -> None:
        self.policy = policy
        self.costs = costs
        self._read_settlement = settlement_reader
        self.reviews_performed = 0
        self.denials_issued = 0

    def review(
        self, proposal: ActionProposal, case: CaseState, now: datetime,
    ) -> Review:
        """Run every invariant and collect *all* denials, not just the first.

        Collecting all of them matters: a repair attempt that fixes one
        violation only to trip another wastes a round trip, and an audit trail
        that shows a single reason under-reports how wrong a proposal was.
        """
        self.reviews_performed += 1
        ctx = GateContext(
            proposal=proposal,
            case=case,
            now=now,
            policy=self.policy,
            costs=self.costs,
            live_settled=self._read_settlement(case),
        )

        verdict = Verdict.allow()
        for _, check in ALL_INVARIANTS:
            verdict = verdict.merge(check(ctx))

        if not verdict.allowed:
            self.denials_issued += 1
        return Review(verdict=verdict, context=ctx)


def repairable(review: Review) -> bool:
    """Is this denial worth handing back to the diagnoser for one more try?

    Only violations a different *proposal* could fix. Consent, settlement and
    budget exhaustion are facts about the world — re-proposing cannot help, and
    asking a model to try again against a wall is how loops are born.
    """
    from vasool.core.types import Denial

    terminal = {
        Denial.ALREADY_COLLECTED,
        Denial.CONSENT_WITHDRAWN,
        Denial.CONTACT_BUDGET_EXCEEDED,
        Denial.ATTEMPT_CAP_REACHED,
        Denial.HORIZON_EXCEEDED,
        Denial.DUPLICATE_ACTION,
    }
    return bool(review.verdict.denials) and not any(
        d in terminal for d in review.verdict.denials
    )
