"""The simulated world.

The environment decides what *actually* happens when an action is taken. It is
the referee, and it is deliberately written so that no arm can influence it
except through the actions it takes.

Harm detection here is independent of the kernel, and that independence is
enforced by construction: this module imports nothing from ``vasool.kernel``.
Harms are derived in ``physics_facts`` from *hidden* state — what is actually
true — rather than from the provider's error code, which is only a claim about
what is true.

The difference is load-bearing. An earlier version imported the kernel's own
quiet-hours function and evidence reader, so four of six harms restated the
kernel's rules and an arm scoring zero proved only that the check had been
written. Now, when the generator emits a misleading error code, the kernel and
the environment genuinely disagree — and the ledger records what happened, not
what the kernel believed would happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from vasool.bench import physics_facts
from vasool.bench.hidden import HiddenState, Physics, uniform01
from vasool.core.policy import Costs, Policy
from vasool.core.types import (
    CONTACT_INTERVENTIONS,
    MONEY_MOVING,
    RETRY_INTERVENTIONS,
    ActionOutcome,
    ActionProposal,
    CaseState,
    FailureClass,
    FailureEvent,
    Intervention,
)
from vasool.executor.backend import PaymentsBackend


@dataclass
class HarmTally:
    double_collect_attempt: int = 0
    contact_to_opted_out: int = 0
    quiet_hours_violation: int = 0
    over_contacted: int = 0
    futile_retry: int = 0
    risk_retry_strike: int = 0

    def add(self, name: str) -> None:
        setattr(self, name, getattr(self, name) + 1)

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)

    def priced(self, costs: Costs) -> int:
        return sum(costs.harm(k) * v for k, v in self.__dict__.items())


class Environment(PaymentsBackend):
    """Simulator that also satisfies the PaymentsBackend interface.

    Implementing the same interface as the live Razorpay backends is not a
    convenience — it means the arms, the Gate and the executor are byte-identical
    between a 500-case benchmark and a real test-mode transaction.
    """

    name = "simulated"

    def __init__(
        self,
        hidden: dict[str, HiddenState],
        physics: Physics,
        policy: Policy,
        costs: Costs,
        master_seed: int,
        oob_settlements: dict[str, datetime],
    ) -> None:
        self.hidden = hidden
        self.physics = physics
        self.policy = policy
        self.costs = costs
        self.master_seed = master_seed
        self.oob_settlements = oob_settlements
        self.harms = HarmTally()
        self.settled: dict[str, int] = {}     # order_id -> paise collected
        self.oob_collected: dict[str, int] = {}
        self.clock: Optional[datetime] = None

    # -- PaymentsBackend ----------------------------------------------------

    def is_settled(self, case: CaseState) -> bool:
        """A fresh read, including money that arrived without us."""
        order_id = case.event.order_id
        self._maybe_settle_out_of_band(case)
        return order_id in self.settled

    def reconcile(
        self, idempotency_key: str, case: CaseState, now: datetime,
    ) -> Optional[ActionOutcome]:
        """The simulator never leaves a write half-applied.

        Fault injection wraps this backend to produce genuine unknown outcomes;
        see vasool/faults/inject.py.
        """
        return None

    def execute(
        self,
        proposal: ActionProposal,
        case: CaseState,
        idempotency_key: str,
        now: datetime,
    ) -> ActionOutcome:
        self.clock = now
        h = self.hidden[case.case_id]
        event = case.event
        cost = self.costs.cost_of(proposal.intervention, proposal.channel)
        harms: list[str] = []

        if proposal.intervention in (Intervention.WAIT, Intervention.STOP):
            return ActionOutcome(False, False, 0, 0, "no-op")

        self._maybe_settle_out_of_band(case)

        # --- harm: collecting money that is already in the bank ------------
        already_settled = event.order_id in self.settled
        if already_settled and proposal.intervention in MONEY_MOVING:
            self.harms.add("double_collect_attempt")
            harms.append("double_collect_attempt")

        # --- harm: reaching a person we should not reach -------------------
        if proposal.intervention in CONTACT_INTERVENTIONS:
            if h.opted_out:
                self.harms.add("contact_to_opted_out")
                harms.append("contact_to_opted_out")
                h.customer_lost = True
            if physics_facts.is_antisocial_hour(
                now, event.customer.tz_offset_minutes
            ):
                self.harms.add("quiet_hours_violation")
                harms.append("quiet_hours_violation")
            if h.contacts_made >= h.patience:
                self.harms.add("over_contacted")
                harms.append("over_contacted")
                h.customer_lost = True
            h.contacts_made += 1

        # --- harm: replaying an instrument that cannot work ----------------
        # Derived from hidden truth, not from the kernel's reading of the
        # error code. When the generator emits a misleading code the two
        # disagree — and the harm ledger should see the disagreement rather
        # than inherit the kernel's blind spot.
        if proposal.intervention in RETRY_INTERVENTIONS:
            if physics_facts.replay_is_harmful(h):
                self.harms.add("risk_retry_strike")
                harms.append("risk_retry_strike")
            elif (
                proposal.intervention is Intervention.RETRY_SAME_RAIL
                and physics_facts.replay_is_futile(h)
            ):
                self.harms.add("futile_retry")
                harms.append("futile_retry")

        prob = self._p_success(h, event, proposal, now)
        roll = uniform01(
            self.master_seed, case.case_id, case.attempts,
            proposal.intervention.value,
        )
        succeeded = roll < prob

        collected = 0
        if succeeded:
            collected = proposal.amount_paise
            self.settled[event.order_id] = self.settled.get(event.order_id, 0) + collected
            if proposal.intervention is Intervention.INSTRUMENT_REFRESH:
                h.instrument_refreshed = True
                h.instrument_alive = True
            if proposal.intervention is Intervention.MANDATE_REREGISTER:
                h.mandate_restored = True

        return ActionOutcome(
            executed=True,
            succeeded=succeeded,
            collected_paise=collected,
            cost_paise=cost,
            detail=f"p={prob:.3f} roll={roll:.3f}",
            harms=tuple(harms),
            provider_ref=f"sim_{idempotency_key[:12]}",
        )

    # -- physics ------------------------------------------------------------

    def _maybe_settle_out_of_band(self, case: CaseState) -> None:
        """Some customers just pay, on their own schedule and not on ours.

        This used to fire only when an arm advanced the clock, which made it
        endogenous: a busier arm realised more out-of-band settlements than a
        quieter one, and against a configured 9% the arms saw 19-21 of 45. The
        direction was conservative — it understated I1's value — but it meant
        "9% settle out of band" described the config rather than the run.

        The settlement now happens at its scheduled moment regardless of what
        any arm does, evaluated against the case's own timeline. Every arm sees
        the same settlements at the same times.
        """
        when = self.oob_settlements.get(case.case_id)
        if when is None:
            return
        order_id = case.event.order_id
        if order_id in self.settled:
            return
        # Exogenous: judged against the case's clock, which the environment
        # advances for every case at the horizon regardless of arm activity.
        reference = self.clock if self.clock is not None else when
        if reference < when:
            return
        self.settled[order_id] = case.event.amount_paise
        self.oob_collected[case.case_id] = case.event.amount_paise

    def settle_all_due(self, cases: dict[str, CaseState], now: datetime) -> None:
        """Advance out-of-band settlement for every case, not just active ones.

        Called by the runner at the end of each case so the realised rate
        matches the configured one instead of tracking how busy an arm was.
        """
        previous, self.clock = self.clock, now
        for case in cases.values():
            self._maybe_settle_out_of_band(case)
        self.clock = previous

    def _p_success(
        self, h: HiddenState, event: FailureEvent, proposal: ActionProposal,
        now: datetime,
    ) -> float:
        p = self.physics
        cls = h.true_class
        iv = proposal.intervention

        if iv in RETRY_INTERVENTIONS:
            return self._p_retry(h, event, proposal, now)

        # Every remaining intervention needs a human on the other end.
        if h.customer_lost or not h.reachable:
            return 0.0
        intent = h.intent(event.failed_at, now)
        responsiveness = h.responsiveness.get(proposal.channel, 0.10)

        if iv is Intervention.HANDOFF_HUMAN:
            return p.handoff_success * intent

        base = responsiveness * intent

        if iv is Intervention.PAYMENT_LINK:
            return base * (1.0 if h.funds_ok(now) else 0.12)

        if iv is Intervention.PART_PAYMENT_LINK:
            ratio = proposal.amount_paise / max(1, event.amount_paise)
            fit = 1.0 if ratio <= h.max_part_ratio else 0.45
            liquidity = 1.0 if h.funds_ok(now) else p.part_payment_funds_bonus
            return base * fit * liquidity

        if iv is Intervention.INSTRUMENT_REFRESH:
            relevant = 1.0 if cls in (
                FailureClass.INSTRUMENT_DEAD, FailureClass.RESTRICTION,
            ) else 0.30
            return base * p.instrument_refresh_uptake * relevant

        if iv is Intervention.MANDATE_REREGISTER:
            relevant = 1.0 if cls is FailureClass.MANDATE_INVALID else 0.20
            return base * p.instrument_refresh_uptake * relevant

        return 0.0

    def _p_retry(
        self, h: HiddenState, event: FailureEvent, proposal: ActionProposal,
        now: datetime,
    ) -> float:
        """Auto-retries do not need the customer, only a working instrument."""
        p = self.physics
        cls = h.true_class
        same_rail = proposal.intervention is Intervention.RETRY_SAME_RAIL

        if cls is FailureClass.RISK_DECLINED:
            return 0.0
        if not same_rail and not h.alt_rail_works:
            return 0.0

        if same_rail:
            if not (h.instrument_alive or h.instrument_refreshed):
                return 0.0
            if cls in (FailureClass.MANDATE_INVALID,) and not h.mandate_restored:
                return 0.0
            if cls is FailureClass.RESTRICTION:
                return 0.0

        blocked = False
        if cls is FailureClass.INSUFFICIENT_FUNDS:
            blocked = not h.funds_ok(now)
        elif cls in (FailureClass.ISSUER_DOWN, FailureClass.RAIL_TIMEOUT):
            blocked = same_rail and not h.outage_cleared(now)
        elif cls is FailureClass.LIMIT_EXCEEDED:
            same_day = now.date() == event.failed_at.date()
            blocked = same_rail and same_day
        elif cls in (FailureClass.AUTH_ABANDONED, FailureClass.AUTH_FAILED):
            # The customer was the missing ingredient; a silent replay rarely
            # supplies one.
            return 0.15 * (p.retry_success_when_clear if h.funds_ok(now) else 0.3)

        return p.retry_success_when_clear if not blocked else p.retry_success_when_blocked
