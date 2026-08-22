"""The Gate: eight machine-checkable invariants standing between a proposal
and a rupee.

Every function here is pure. No I/O, no clock reads, no model calls, no
randomness. Everything a check needs arrives on ``GateContext``, which means
each invariant can be property-tested in isolation and none of them can be
talked out of a decision by anything a language model writes.

The load-bearing rule of this module: **a diagnosis is an input, not an
authority.** Where raw provider evidence settles a question (see
``raw_evidence``), the kernel uses its own reading and ignores the model's.

I8 is the one invariant not enforced here. It governs ordering rather than
content — nothing executes before its ledger record exists — so it lives at
the executor boundary in ``vasool/executor/executor.py``. It is stated here so
the eight are documented in one place.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from vasool.core.policy import Costs, Policy
from vasool.core.types import (
    CONTACT_INTERVENTIONS,
    MONEY_MOVING,
    RETRY_INTERVENTIONS,
    ActionProposal,
    CaseState,
    Channel,
    Denial,
    Intervention,
    Verdict,
)
from vasool.kernel import raw_evidence

_PRIORS_PATH = Path(__file__).resolve().parents[2] / "config" / "priors.yaml"


def _load_priors(path: Path | None = None) -> tuple[dict[str, dict[str, float]], float]:
    raw: dict[str, Any] = yaml.safe_load((path or _PRIORS_PATH).read_text())
    decay = float(raw.pop("_attempt_decay", 0.55))
    return {k: {ik: float(iv) for ik, iv in v.items()} for k, v in raw.items()}, decay


PRIORS, ATTEMPT_DECAY = _load_priors()


@dataclass(frozen=True)
class GateContext:
    """Everything the invariants are allowed to look at.

    ``live_settled`` is populated by the caller from a *fresh* read of provider
    state, not from cached case state. That distinction is the whole of I1.
    """
    proposal: ActionProposal
    case: CaseState
    now: datetime
    policy: Policy
    costs: Costs
    live_settled: bool

    @property
    def order_amount_paise(self) -> int:
        return self.case.event.amount_paise


# ---------------------------------------------------------------------------
# I1 — no_double_collect
# ---------------------------------------------------------------------------

def i1_no_double_collect(ctx: GateContext) -> Verdict:
    """Never chase money that has already arrived.

    Prevents: a customer pays through a second channel (or a delayed webhook
    lands) while a recovery workflow is mid-flight, and the workflow collects
    a second time. In production this is the most expensive failure in the
    whole category — it costs the refund, the support contact, and the trust.

    The check reads *live* provider state rather than the case's own belief,
    because the case's belief is exactly what is stale in this scenario.
    """
    if ctx.proposal.intervention not in MONEY_MOVING:
        return Verdict.allow()
    if ctx.live_settled or ctx.case.settled:
        return Verdict.deny(
            Denial.ALREADY_COLLECTED, "I1",
            "order already settled at execution time",
        )
    return Verdict.allow()


# ---------------------------------------------------------------------------
# I2 — idempotent_write
# ---------------------------------------------------------------------------

def action_key(proposal: ActionProposal) -> str:
    """Deterministic idempotency key for one intended money action."""
    return hashlib.sha256(proposal.idempotency_seed().encode()).hexdigest()[:24]


def i2_idempotent_write(ctx: GateContext) -> Verdict:
    """One intent produces at most one money action, forever.

    Prevents: a model retry, a process crash mid-batch, or a duplicate webhook
    turning one recovery into two charges. The key is a function of the
    decision itself, so a second delivery of one decision computes the same key
    and is refused, while a genuinely new decision computes a different one and
    proceeds.
    """
    if ctx.proposal.intervention not in MONEY_MOVING:
        return Verdict.allow()
    key = action_key(ctx.proposal)
    if key in ctx.case.executed_keys:
        return Verdict.deny(
            Denial.DUPLICATE_ACTION, "I2", f"action key {key} already executed",
        )
    return Verdict.allow()


# ---------------------------------------------------------------------------
# I3 — amount_conserving
# ---------------------------------------------------------------------------

def i3_amount_conserving(ctx: GateContext) -> Verdict:
    """A recovery collects the order, or an approved fraction of it. Never more.

    Prevents: any path — a hallucination, a corrupted field, or text a customer
    typed into a reply — from changing what gets charged. This is why prompt
    injection against Vasool fails structurally rather than by filtering: the
    model can be talked into proposing anything, and the number still has to
    equal the order.
    """
    proposal, order = ctx.proposal, ctx.order_amount_paise

    if proposal.currency != ctx.case.event.currency:
        return Verdict.deny(
            Denial.CURRENCY_MISMATCH, "I3",
            f"{proposal.currency} != order {ctx.case.event.currency}",
        )
    if proposal.currency not in ctx.policy.permitted_currencies:
        return Verdict.deny(
            Denial.CURRENCY_MISMATCH, "I3", f"{proposal.currency} not permitted",
        )

    # Universal ceiling. Applies to every intervention, including the ones that
    # are not supposed to move money at all.
    if proposal.amount_paise > order:
        return Verdict.deny(
            Denial.AMOUNT_EXCEEDS_ORDER, "I3",
            f"proposed {proposal.amount_paise} > order {order}",
        )
    if proposal.amount_paise < 0:
        return Verdict.deny(Denial.AMOUNT_MISMATCH, "I3", "negative amount")

    if proposal.intervention is Intervention.PART_PAYMENT_LINK:
        merchant = ctx.case.event.merchant
        if not (ctx.policy.allow_part_payment and merchant.allows_part_payment):
            return Verdict.deny(
                Denial.PART_PAYMENT_NOT_ALLOWED, "I3",
                "part payment disabled by policy or merchant",
            )
        floor_ratio = max(
            ctx.policy.absolute_part_payment_floor_ratio,
            merchant.part_payment_floor_ratio,
        )
        if proposal.amount_paise < int(order * floor_ratio):
            return Verdict.deny(
                Denial.AMOUNT_BELOW_FLOOR, "I3",
                f"{proposal.amount_paise} below floor {int(order * floor_ratio)}",
            )
        return Verdict.allow()

    if proposal.intervention in MONEY_MOVING and proposal.amount_paise != order:
        return Verdict.deny(
            Denial.AMOUNT_MISMATCH, "I3",
            f"proposed {proposal.amount_paise} != order {order}",
        )
    return Verdict.allow()


# ---------------------------------------------------------------------------
# I4 — contact_budget
# ---------------------------------------------------------------------------

def _local_hour(now: datetime, tz_offset_minutes: int) -> int:
    return (now + timedelta(minutes=tz_offset_minutes)).hour


def in_quiet_hours(now: datetime, tz_offset_minutes: int, policy: Policy) -> bool:
    hour = _local_hour(now, tz_offset_minutes)
    start, end = policy.quiet_hours_start_local, policy.quiet_hours_end_local
    if start == end:
        return False
    if start > end:                      # window wraps midnight, the usual case
        return hour >= start or hour < end
    return start <= hour < end


def i4_contact_budget(ctx: GateContext) -> Verdict:
    """Bound how often a human being is reached, and when.

    Prevents: a recovery loop that is individually reasonable at each step and
    collectively harassment. Contact frequency is enforced in code because a
    prompt instruction to "be considerate" is not a rate limit.
    """
    if ctx.proposal.intervention not in CONTACT_INTERVENTIONS:
        return Verdict.allow()

    p, case = ctx.policy, ctx.case
    channel = ctx.proposal.channel

    if channel not in p.permitted_channels:
        return Verdict.deny(
            Denial.CHANNEL_NOT_PERMITTED, "I4", f"{channel.value} not permitted",
        )

    window_start = ctx.now - timedelta(hours=p.contact_window_hours)
    if case.contacts_since(window_start) >= p.max_contacts_per_window:
        return Verdict.deny(
            Denial.CONTACT_BUDGET_EXCEEDED, "I4",
            f"{case.contacts_since(window_start)} contacts in "
            f"{p.contact_window_hours}h window",
        )

    last = case.last_contact_at()
    if last is not None:
        gap_hours = (ctx.now - last).total_seconds() / 3600.0
        if gap_hours < p.min_hours_between_contacts:
            return Verdict.deny(
                Denial.COOLING_OFF, "I4",
                f"{gap_hours:.1f}h since last contact, minimum "
                f"{p.min_hours_between_contacts}h",
            )

    if in_quiet_hours(ctx.now, case.event.customer.tz_offset_minutes, p):
        return Verdict.deny(
            Denial.QUIET_HOURS, "I4",
            f"local hour {_local_hour(ctx.now, case.event.customer.tz_offset_minutes)}",
        )
    return Verdict.allow()


# ---------------------------------------------------------------------------
# I5 — consent_honored
# ---------------------------------------------------------------------------

#: DND is a channel-level registration, not a blanket withdrawal of consent.
_DND_BLOCKED_CHANNELS = frozenset({Channel.SMS, Channel.VOICE})


def i5_consent_honored(ctx: GateContext) -> Verdict:
    """Opt-out is terminal. No intervention may route around it.

    Prevents: the failure mode where an agent finds a "different channel" for a
    customer who asked to be left alone. Opt-out blocks every contact;
    DND blocks the channels it legally covers.
    """
    if ctx.proposal.intervention not in CONTACT_INTERVENTIONS:
        return Verdict.allow()

    customer = ctx.case.event.customer
    if customer.opted_out:
        return Verdict.deny(
            Denial.CONSENT_WITHDRAWN, "I5", "customer has opted out of contact",
        )
    if customer.dnd_registered and ctx.proposal.channel in _DND_BLOCKED_CHANNELS:
        return Verdict.deny(
            Denial.CONSENT_WITHDRAWN, "I5",
            f"DND registered; {ctx.proposal.channel.value} not available",
        )
    return Verdict.allow()


# ---------------------------------------------------------------------------
# I6 — no_futile_retry
# ---------------------------------------------------------------------------

def i6_no_futile_retry(ctx: GateContext) -> Verdict:
    """Do not replay an instrument that provably cannot work.

    Prevents: the single most common waste in production recovery — retrying
    expired cards and revoked mandates on a fixed schedule — and the more
    expensive version, retrying an issuer risk decline, which degrades the
    merchant's own decline rate.

    Note the source of truth: ``raw_evidence`` reads the provider's error code
    directly. The model's classification is not consulted, so a confident wrong
    diagnosis cannot unlock a futile retry.
    """
    if ctx.proposal.intervention not in RETRY_INTERVENTIONS:
        return Verdict.allow()

    event = ctx.case.event
    facts = raw_evidence.read(event)

    if facts.retry_is_harmful:
        return Verdict.deny(
            Denial.HARMFUL_RETRY, "I6",
            f"retry against risk decline ({facts.reason})",
        )

    if ctx.proposal.intervention is Intervention.RETRY_SAME_RAIL and facts.retry_is_futile:
        return Verdict.deny(
            Denial.FUTILE_RETRY, "I6",
            f"same-rail replay impossible ({facts.reason})",
        )

    if ctx.proposal.intervention is Intervention.RETRY_ALT_RAIL:
        if not raw_evidence.alternate_rail_available(event):
            return Verdict.deny(
                Denial.FUTILE_RETRY, "I6", "no alternate instrument on file",
            )

    auto_retries = sum(
        1 for note in ctx.case.notes if note.startswith("retry:")
    )
    if auto_retries >= ctx.policy.max_auto_retries:
        return Verdict.deny(
            Denial.ATTEMPT_CAP_REACHED, "I6",
            f"{auto_retries} auto-retries already spent",
        )
    return Verdict.allow()


# ---------------------------------------------------------------------------
# I7 — stopping_rule
# ---------------------------------------------------------------------------

def success_prior(
    failure_class: str,
    intervention: Intervention,
    attempts_already_spent: int,
    confidence: float,
    diagnosis_source: str,
) -> float:
    """Conservative probability that this action recovers the money.

    Two deliberate properties. The table is committed to the repo, so the
    stopping rule is auditable rather than a hidden knob. And a model's stated
    confidence can only *attenuate* the prior, never raise it — an overconfident
    diagnosis cannot buy itself more budget.
    """
    base = PRIORS.get(failure_class, PRIORS["UNKNOWN"]).get(intervention.value, 0.05)
    decayed = base * (ATTEMPT_DECAY ** max(0, attempts_already_spent))
    if diagnosis_source == "llm":
        decayed *= min(1.0, max(0.0, confidence))
    return decayed


def i7_stopping_rule(ctx: GateContext) -> Verdict:
    """Every case terminates, and stops being worked once it stops paying.

    Prevents: the unbounded loop. Three separate guarantees — a horizon, an
    attempt cap, and an expected-value floor — so termination does not depend
    on the economics being well-calibrated.
    """
    case, p = ctx.case, ctx.policy

    if case.opened_at is not None:
        if ctx.now - case.opened_at > timedelta(days=p.horizon_days):
            return Verdict.deny(
                Denial.HORIZON_EXCEEDED, "I7", f"past {p.horizon_days}d horizon",
            )

    if ctx.proposal.intervention not in MONEY_MOVING:
        return Verdict.allow()

    if case.attempts >= p.max_money_actions:
        return Verdict.deny(
            Denial.ATTEMPT_CAP_REACHED, "I7",
            f"{case.attempts} money actions already spent",
        )

    d = ctx.proposal.diagnosis
    prob = success_prior(
        d.failure_class.value, ctx.proposal.intervention,
        case.attempts, d.confidence, d.source,
    )
    if prob < p.min_success_probability:
        return Verdict.deny(
            Denial.BELOW_STOPPING_THRESHOLD, "I7",
            f"p={prob:.4f} below floor {p.min_success_probability}",
        )

    cost = ctx.costs.cost_of(ctx.proposal.intervention, ctx.proposal.channel)
    expected_value = prob * ctx.proposal.amount_paise
    if cost > 0 and expected_value < cost * p.min_ev_to_cost_ratio:
        return Verdict.deny(
            Denial.BELOW_STOPPING_THRESHOLD, "I7",
            f"EV {expected_value:.0f}p < {p.min_ev_to_cost_ratio}x cost {cost}p",
        )
    return Verdict.allow()


# ---------------------------------------------------------------------------
# I8 — audit_before_action (enforced at the executor boundary)
# ---------------------------------------------------------------------------

def i8_audit_before_action(receipt: str | None) -> Verdict:
    """Nothing executes unlogged.

    Prevents: the gap between "we decided" and "we acted" where an action can
    happen with no record of why. The executor calls this with the ledger
    receipt id for the write-ahead record; without one, the action does not
    happen.
    """
    if not receipt:
        return Verdict.deny(
            Denial.UNAUDITED, "I8", "no write-ahead ledger receipt",
        )
    return Verdict.allow()


#: Registration order matters only for readability of denial lists.
ALL_INVARIANTS: tuple[tuple[str, Callable[[GateContext], Verdict]], ...] = (
    ("I1", i1_no_double_collect),
    ("I2", i2_idempotent_write),
    ("I3", i3_amount_conserving),
    ("I4", i4_contact_budget),
    ("I5", i5_consent_honored),
    ("I6", i6_no_futile_retry),
    ("I7", i7_stopping_rule),
)
