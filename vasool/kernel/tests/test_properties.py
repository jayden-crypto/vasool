"""Property tests.

Unit tests check the cases I thought of. These check the cases I did not.

The first property is the important one. Over arbitrary proposals — any
intervention, any channel, any amount up to a billion paise, any currency, any
case history — nothing the Gate approves ever collects more than the order.
That statement is what makes prompt injection a non-event rather than a
filtering problem: it holds no matter what the model was persuaded to ask for.
"""

from __future__ import annotations

from datetime import timedelta

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from vasool.core.policy import Costs, Policy
from vasool.core.types import (
    MONEY_MOVING,
    ActionProposal,
    CaseState,
    Channel,
    ContactRecord,
    Diagnosis,
    FailureClass,
    Intervention,
)
from vasool.kernel.gate import Gate
from vasool.kernel.invariants import ALL_INVARIANTS, GateContext, action_key
from vasool.kernel.tests import factories as f

POLICY = Policy.load()
COSTS = Costs.load()

interventions = st.sampled_from(list(Intervention))
channels = st.sampled_from(list(Channel))
classes = st.sampled_from(list(FailureClass))
amounts = st.integers(min_value=0, max_value=10**9)
order_amounts = st.integers(min_value=10_000, max_value=5_000_000)
currencies = st.sampled_from(["INR", "USD", "EUR", "GBP"])
sources = st.sampled_from(["llm", "rules", "fallback", "none"])
SETTINGS = settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow],
                    deadline=None)


def _build(order_amount, intervention, channel, amount, currency, confidence,
           source, failure_class, attempts, contacts, hours_open, settled):
    event = f.event(amount_paise=order_amount)
    case = CaseState(case_id="case_prop", event=event, opened_at=event.failed_at)
    case.attempts = attempts
    now = event.failed_at + timedelta(hours=hours_open)
    for i in range(contacts):
        case.contacts.append(ContactRecord(
            at=now - timedelta(hours=30 * (i + 1)), channel=Channel.SMS,
            intervention=Intervention.PAYMENT_LINK))
    proposal = ActionProposal(
        case_id="case_prop", intervention=intervention, channel=channel,
        amount_paise=amount, currency=currency, scheduled_for=now,
        diagnosis=Diagnosis(failure_class, confidence, "prop", (), source),
    )
    gate = Gate(POLICY, COSTS, lambda _c: settled)
    return gate, proposal, case, now


@given(
    order_amount=order_amounts, intervention=interventions, channel=channels,
    amount=amounts, currency=currencies,
    confidence=st.floats(0.0, 1.0), source=sources, failure_class=classes,
    attempts=st.integers(0, 8), contacts=st.integers(0, 5),
    hours_open=st.floats(0.0, 800.0), settled=st.booleans(),
)
@SETTINGS
def test_an_approved_action_never_collects_more_than_the_order(**kw):
    gate, proposal, case, now = _build(**kw)
    if gate.review(proposal, case, now).allowed:
        assert proposal.amount_paise <= case.event.amount_paise


@given(
    order_amount=order_amounts, intervention=interventions, channel=channels,
    amount=amounts, currency=currencies,
    confidence=st.floats(0.0, 1.0), source=sources, failure_class=classes,
    attempts=st.integers(0, 8), contacts=st.integers(0, 5),
    hours_open=st.floats(0.0, 800.0), settled=st.booleans(),
)
@SETTINGS
def test_an_approved_action_never_lands_on_a_settled_order(**kw):
    gate, proposal, case, now = _build(**kw)
    if kw["settled"] and proposal.intervention in MONEY_MOVING:
        assert not gate.review(proposal, case, now).allowed


@given(
    order_amount=order_amounts, intervention=interventions, channel=channels,
    amount=amounts, currency=currencies,
    confidence=st.floats(0.0, 1.0), source=sources, failure_class=classes,
    attempts=st.integers(0, 8), contacts=st.integers(0, 5),
    hours_open=st.floats(0.0, 800.0), settled=st.booleans(),
)
@SETTINGS
def test_an_approved_action_never_contacts_a_customer_who_opted_out(**kw):
    gate, proposal, case, now = _build(**kw)
    object.__setattr__(case.event.customer, "opted_out", True)
    from vasool.core.types import CONTACT_INTERVENTIONS
    if proposal.intervention in CONTACT_INTERVENTIONS:
        assert not gate.review(proposal, case, now).allowed


@given(
    order_amount=order_amounts, intervention=interventions, channel=channels,
    amount=amounts, currency=currencies,
    confidence=st.floats(0.0, 1.0), source=sources, failure_class=classes,
    attempts=st.integers(0, 8), contacts=st.integers(0, 5),
    hours_open=st.floats(0.0, 800.0), settled=st.booleans(),
)
@SETTINGS
def test_the_gate_agrees_with_every_invariant_it_runs(**kw):
    """The Gate is exactly the conjunction of its parts — no extra leniency."""
    gate, proposal, case, now = _build(**kw)
    review = gate.review(proposal, case, now)
    ctx = GateContext(proposal=proposal, case=case, now=now, policy=POLICY,
                      costs=COSTS, live_settled=kw["settled"])
    individually = all(check(ctx).allowed for _, check in ALL_INVARIANTS)
    assert review.allowed == individually


@given(
    order_amount=order_amounts, intervention=interventions, channel=channels,
    amount=amounts, currency=currencies,
    confidence=st.floats(0.0, 1.0), source=sources, failure_class=classes,
    attempts=st.integers(0, 8), contacts=st.integers(0, 5),
    hours_open=st.floats(0.0, 800.0), settled=st.booleans(),
)
@SETTINGS
def test_a_denial_always_names_at_least_one_reason_and_one_invariant(**kw):
    gate, proposal, case, now = _build(**kw)
    review = gate.review(proposal, case, now)
    if not review.allowed:
        assert review.verdict.denials
        assert review.verdict.invariant_ids


@given(
    intervention=interventions, amount=amounts,
    hours_a=st.integers(0, 400), hours_b=st.integers(0, 400),
    ordinal_a=st.integers(0, 12), ordinal_b=st.integers(0, 12),
)
@settings(max_examples=400, deadline=None)
def test_idempotency_keys_depend_on_the_decision_and_not_on_the_clock(
    intervention, amount, hours_a, hours_b, ordinal_a, ordinal_b,
):
    """The property the old test got backwards.

    It asserted keys collide iff the timestamps are bit-identical — which
    encoded the weakness rather than the guarantee, because a restarted process
    re-deriving the same decision gets a new timestamp. Keys must depend on
    *which decision this is*, and not at all on when it was computed.
    """
    a = f.proposal(intervention, amount_paise=amount,
                   when=f.T0 + timedelta(hours=hours_a))
    b = f.proposal(intervention, amount_paise=amount,
                   when=f.T0 + timedelta(hours=hours_b))
    assert (action_key(a, ordinal_a) == action_key(b, ordinal_b)) == (
        ordinal_a == ordinal_b)


@given(case_a=st.text(min_size=1, max_size=12), case_b=st.text(min_size=1, max_size=12))
@settings(max_examples=200, deadline=None)
def test_idempotency_keys_do_not_collide_across_cases(case_a, case_b):
    assume(case_a != case_b)
    assert action_key(f.proposal(case_id=case_a), 0) != action_key(
        f.proposal(case_id=case_b), 0)
