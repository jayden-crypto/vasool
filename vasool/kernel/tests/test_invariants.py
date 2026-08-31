"""Invariant tests.

Each test is named for the thing that would go wrong in production without the
invariant, because that is what the invariant is for.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from vasool.core.policy import Costs, Policy
from vasool.core.types import (
    Channel,
    ContactRecord,
    Denial,
    FailureClass,
    Intervention,
)
from vasool.kernel import invariants as inv
from vasool.kernel.tests import factories as f

POLICY = Policy.load()
COSTS = Costs.load()


def ctx(proposal=None, case=None, now=None, live_settled=False):
    return inv.GateContext(
        proposal=proposal or f.proposal(),
        case=case or f.case(),
        now=now or f.T0,
        policy=POLICY, costs=COSTS, live_settled=live_settled,
    )


# --------------------------------------------------------------------------
# I1
# --------------------------------------------------------------------------

def test_i1_blocks_collecting_money_that_already_arrived():
    verdict = inv.i1_no_double_collect(ctx(live_settled=True))
    assert not verdict.allowed
    assert Denial.ALREADY_COLLECTED in verdict.denials


def test_i1_reads_live_state_not_the_cases_own_belief():
    """The case thinks nothing has been collected. The provider disagrees.

    This is the real shape of the bug: the customer paid through another route
    and our copy of the world is stale.
    """
    stale = f.case()
    assert stale.collected_paise == 0
    assert not inv.i1_no_double_collect(ctx(case=stale, live_settled=True)).allowed


def test_i1_ignores_actions_that_cannot_move_money():
    proposal = f.proposal(Intervention.WAIT, amount_paise=0)
    assert inv.i1_no_double_collect(ctx(proposal=proposal, live_settled=True)).allowed


# --------------------------------------------------------------------------
# I2
# --------------------------------------------------------------------------

def test_i2_same_intent_produces_the_same_key():
    """Two deliveries of one decision must collide."""
    assert inv.action_key(f.proposal()) == inv.action_key(f.proposal())


def test_i2_a_later_decision_produces_a_different_key():
    later = f.proposal(when=f.T0 + timedelta(hours=30))
    assert inv.action_key(f.proposal()) != inv.action_key(later)


def test_i2_a_different_amount_produces_a_different_key():
    assert inv.action_key(f.proposal()) != inv.action_key(
        f.proposal(Intervention.PART_PAYMENT_LINK, amount_paise=100000))


def test_i2_the_key_survives_the_attempt_counter_advancing():
    """Regression: the key must not depend on mutable case state.

    Deriving it from ``case.attempts`` meant a replayed intent computed a
    different key after the first execution and was not recognised as a
    duplicate. Found by the duplicate-delivery fault scenario.
    """
    p = f.proposal()
    before = f.case(attempts=0)
    after = f.case(attempts=3)
    ctx_before = ctx(proposal=p, case=before)
    ctx_after = ctx(proposal=p, case=after)
    before.executed_keys.add(inv.action_key(p))
    after.executed_keys.add(inv.action_key(p))
    assert not inv.i2_idempotent_write(ctx_before).allowed
    assert not inv.i2_idempotent_write(ctx_after).allowed


def test_i2_refuses_a_replay_of_an_executed_action():
    p = f.proposal()
    case = f.case()
    case.executed_keys.add(inv.action_key(p))
    verdict = inv.i2_idempotent_write(ctx(proposal=p, case=case))
    assert not verdict.allowed
    assert Denial.DUPLICATE_ACTION in verdict.denials


# --------------------------------------------------------------------------
# I3 — the invariant that makes prompt injection structurally irrelevant
# --------------------------------------------------------------------------

@pytest.mark.parametrize("amount", [250001, 500000, 5_000_000, 10**9])
def test_i3_refuses_any_amount_above_the_order(amount):
    verdict = inv.i3_amount_conserving(ctx(proposal=f.proposal(amount_paise=amount)))
    assert not verdict.allowed
    assert Denial.AMOUNT_EXCEEDS_ORDER in verdict.denials


def test_i3_refuses_a_short_collection_that_is_not_a_part_payment():
    verdict = inv.i3_amount_conserving(ctx(proposal=f.proposal(amount_paise=1000)))
    assert not verdict.allowed
    assert Denial.AMOUNT_MISMATCH in verdict.denials


def test_i3_refuses_a_currency_swap():
    verdict = inv.i3_amount_conserving(ctx(proposal=f.proposal(currency="USD")))
    assert not verdict.allowed
    assert Denial.CURRENCY_MISMATCH in verdict.denials


def test_i3_allows_a_part_payment_inside_the_floor():
    p = f.proposal(Intervention.PART_PAYMENT_LINK, amount_paise=125000)
    assert inv.i3_amount_conserving(ctx(proposal=p)).allowed


def test_i3_refuses_a_part_payment_below_the_floor():
    p = f.proposal(Intervention.PART_PAYMENT_LINK, amount_paise=10000)
    verdict = inv.i3_amount_conserving(ctx(proposal=p))
    assert not verdict.allowed
    assert Denial.AMOUNT_BELOW_FLOOR in verdict.denials


def test_i3_refuses_part_payment_where_the_merchant_disallows_it():
    ev = f.event(merchant=f.merchant(allows_part_payment=False))
    p = f.proposal(Intervention.PART_PAYMENT_LINK, amount_paise=125000)
    verdict = inv.i3_amount_conserving(ctx(proposal=p, case=f.case(event=ev)))
    assert not verdict.allowed
    assert Denial.PART_PAYMENT_NOT_ALLOWED in verdict.denials


# --------------------------------------------------------------------------
# I4
# --------------------------------------------------------------------------

def test_i4_refuses_outreach_during_quiet_hours():
    # 20:00 UTC == 01:30 IST.
    night = f.T0.replace(hour=20, minute=0)
    verdict = inv.i4_contact_budget(ctx(now=night))
    assert not verdict.allowed
    assert Denial.QUIET_HOURS in verdict.denials


def test_i4_allows_outreach_during_the_day():
    assert inv.i4_contact_budget(ctx()).allowed


def test_i4_enforces_the_rolling_contact_budget():
    case = f.case()
    for hours in (100, 60, 30):
        case.contacts.append(ContactRecord(
            at=f.T0 - timedelta(hours=hours), channel=Channel.SMS,
            intervention=Intervention.PAYMENT_LINK))
    verdict = inv.i4_contact_budget(ctx(case=case))
    assert not verdict.allowed
    assert Denial.CONTACT_BUDGET_EXCEEDED in verdict.denials


def test_i4_enforces_cooling_off_between_contacts():
    case = f.case()
    case.contacts.append(ContactRecord(
        at=f.T0 - timedelta(hours=2), channel=Channel.SMS,
        intervention=Intervention.PAYMENT_LINK))
    verdict = inv.i4_contact_budget(ctx(case=case))
    assert not verdict.allowed
    assert Denial.COOLING_OFF in verdict.denials


def test_i4_refuses_a_channel_outside_policy():
    verdict = inv.i4_contact_budget(ctx(proposal=f.proposal(channel=Channel.VOICE)))
    assert not verdict.allowed
    assert Denial.CHANNEL_NOT_PERMITTED in verdict.denials


def test_i4_ignores_actions_that_reach_nobody():
    p = f.proposal(Intervention.RETRY_SAME_RAIL, channel=Channel.NONE)
    night = f.T0.replace(hour=20)
    assert inv.i4_contact_budget(ctx(proposal=p, now=night)).allowed


# --------------------------------------------------------------------------
# I5
# --------------------------------------------------------------------------

def test_i5_opt_out_blocks_every_channel():
    ev = f.event(customer=f.customer(opted_out=True))
    case = f.case(event=ev)
    for channel in (Channel.SMS, Channel.EMAIL, Channel.WHATSAPP):
        verdict = inv.i5_consent_honored(
            ctx(proposal=f.proposal(channel=channel), case=case))
        assert not verdict.allowed, channel
        assert Denial.CONSENT_WITHDRAWN in verdict.denials


def test_i5_dnd_blocks_only_the_channels_it_covers():
    ev = f.event(customer=f.customer(dnd_registered=True))
    case = f.case(event=ev)
    assert not inv.i5_consent_honored(
        ctx(proposal=f.proposal(channel=Channel.SMS), case=case)).allowed
    assert inv.i5_consent_honored(
        ctx(proposal=f.proposal(channel=Channel.EMAIL), case=case)).allowed


# --------------------------------------------------------------------------
# I6
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "card_expired", "invalid_card", "token_expired", "mandate_revoked",
    "card_not_enabled_for_online",
])
def test_i6_refuses_replaying_a_provably_dead_instrument(reason):
    ev = f.event(error=f.error(reason=reason))
    p = f.proposal(Intervention.RETRY_SAME_RAIL, channel=Channel.NONE)
    verdict = inv.i6_no_futile_retry(ctx(proposal=p, case=f.case(event=ev)))
    assert not verdict.allowed
    assert Denial.FUTILE_RETRY in verdict.denials


def test_i6_refuses_retrying_a_risk_decline():
    ev = f.event(error=f.error(reason="suspected_fraud"))
    p = f.proposal(Intervention.RETRY_SAME_RAIL, channel=Channel.NONE)
    verdict = inv.i6_no_futile_retry(ctx(proposal=p, case=f.case(event=ev)))
    assert not verdict.allowed
    assert Denial.HARMFUL_RETRY in verdict.denials


def test_i6_ignores_a_confident_diagnosis_that_contradicts_the_evidence():
    """The point of I6: the kernel re-derives futility itself.

    A model asserting, with total confidence, that an expired card was really a
    transient outage does not unlock the retry. Raw evidence wins.
    """
    ev = f.event(error=f.error(reason="card_expired"))
    p = f.proposal(
        Intervention.RETRY_SAME_RAIL, channel=Channel.NONE,
        diag=f.diagnosis(FailureClass.ISSUER_DOWN, confidence=1.0, source="llm"),
    )
    assert not inv.i6_no_futile_retry(ctx(proposal=p, case=f.case(event=ev))).allowed


def test_i6_refuses_an_alt_rail_switch_with_no_alt_rail_on_file():
    p = f.proposal(Intervention.RETRY_ALT_RAIL, channel=Channel.NONE)
    verdict = inv.i6_no_futile_retry(ctx(proposal=p))
    assert not verdict.allowed
    assert Denial.FUTILE_RETRY in verdict.denials


def test_i6_allows_an_alt_rail_switch_when_one_exists():
    from vasool.core.types import Rail
    ev = f.event(customer=f.customer(saved_rails=(Rail.CARD, Rail.UPI)))
    p = f.proposal(Intervention.RETRY_ALT_RAIL, channel=Channel.NONE)
    assert inv.i6_no_futile_retry(ctx(proposal=p, case=f.case(event=ev))).allowed


# --------------------------------------------------------------------------
# I7
# --------------------------------------------------------------------------

def test_i7_stops_once_the_attempt_cap_is_reached():
    case = f.case(attempts=POLICY.max_money_actions)
    verdict = inv.i7_stopping_rule(ctx(case=case))
    assert not verdict.allowed
    assert Denial.ATTEMPT_CAP_REACHED in verdict.denials


def test_i7_stops_past_the_horizon():
    now = f.T0 + timedelta(days=POLICY.horizon_days + 1)
    verdict = inv.i7_stopping_rule(ctx(now=now))
    assert not verdict.allowed
    assert Denial.HORIZON_EXCEEDED in verdict.denials


def test_i7_stops_when_the_next_action_costs_more_than_it_is_worth():
    tiny = f.event(amount_paise=200)
    p = f.proposal(amount_paise=200)
    verdict = inv.i7_stopping_rule(ctx(proposal=p, case=f.case(event=tiny)))
    assert not verdict.allowed
    assert Denial.BELOW_STOPPING_THRESHOLD in verdict.denials


def test_i7_model_confidence_can_only_lower_the_prior_never_raise_it():
    """An overconfident model cannot buy itself more budget."""
    rules = inv.success_prior("INSUFFICIENT_FUNDS", Intervention.PAYMENT_LINK,
                              0, 1.0, "rules")
    llm_certain = inv.success_prior("INSUFFICIENT_FUNDS", Intervention.PAYMENT_LINK,
                                    0, 1.0, "llm")
    llm_unsure = inv.success_prior("INSUFFICIENT_FUNDS", Intervention.PAYMENT_LINK,
                                   0, 0.4, "llm")
    assert llm_certain <= rules
    assert llm_unsure < llm_certain


def test_i7_expected_value_decays_with_every_attempt_so_cases_terminate():
    values = [
        inv.success_prior("INSUFFICIENT_FUNDS", Intervention.PAYMENT_LINK, n, 0.9, "rules")
        for n in range(6)
    ]
    assert values == sorted(values, reverse=True)
    assert values[-1] < values[0]


# --------------------------------------------------------------------------
# I8
# --------------------------------------------------------------------------

def test_i8_refuses_an_action_with_no_ledger_receipt():
    verdict = inv.i8_audit_before_action(None)
    assert not verdict.allowed
    assert Denial.UNAUDITED in verdict.denials


def test_i8_accepts_a_receipt():
    assert inv.i8_audit_before_action("a" * 64).allowed


# --------------------------------------------------------------------------
# Circuit breaker — regression for a run that never reached the model
# --------------------------------------------------------------------------

def test_breaker_half_opens_for_batch_work_that_outruns_the_clock():
    """A brief hiccup must not poison a whole batch.

    Regression: when the breaker opened, every later decision fell to the
    deterministic path and returned instantly, so a 200-case run finished
    inside the 30s cooldown. The report showed a model arm with 800 degraded
    decisions and 25 API calls. The breaker has to recover on skip count too.
    """
    from vasool.diagnosis.llm import CircuitBreaker

    breaker = CircuitBreaker(threshold=2, cooldown_seconds=999.0,
                             probe_after_skips=5)
    breaker.record_failure()
    breaker.record_failure()

    # Race through skipped calls the way a degraded batch does. Each `is_open`
    # check counts as one skip, so the fifth is the probe.
    verdicts = [breaker.is_open for _ in range(5)]
    assert verdicts == [True, True, True, True, False]
    assert breaker.probes == 1


def test_breaker_closes_on_a_successful_probe():
    from vasool.diagnosis.llm import CircuitBreaker

    breaker = CircuitBreaker(threshold=1, cooldown_seconds=999.0, probe_after_skips=2)
    breaker.record_failure()
    assert breaker.is_open               # 1st skip
    assert not breaker.is_open           # 2nd skip -> probe
    breaker.record_success()
    assert not breaker.is_open
    assert breaker.consecutive_failures == 0


# --------------------------------------------------------------------------
# Circuit breaker — not an invariant, but the bug it caused was expensive
# --------------------------------------------------------------------------

def test_breaker_reprobes_after_a_bounded_number_of_skips():
    """A wall-clock-only cooldown swallows a batch run.

    Regression: arms C and D once produced 803 and 562 fallback decisions
    against 43 real calls, because thirty seconds of cooldown is hundreds of
    decisions when nothing is waiting on the network.
    """
    from vasool.diagnosis.llm import CircuitBreaker

    breaker = CircuitBreaker(threshold=2, cooldown_seconds=3600,
                             probe_after_skips=5)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open

    # With an hour of cooldown left, a batch must still get probes through.
    skipped = sum(1 for _ in range(30) if breaker.is_open)
    assert skipped < 30, "breaker never re-probed despite a long cooldown"
    assert breaker.probes >= 4, f"only {breaker.probes} probes in 30 attempts"


# --------------------------------------------------------------------------
# Evidence router
# --------------------------------------------------------------------------

def test_router_trusts_an_unambiguous_code_without_a_model():
    from vasool.diagnosis.router import ROUTE_CODE, decide_route
    ev = f.event(error=f.error(reason="card_expired",
                               issuer_message="Card has expired"))
    assert decide_route(ev) == ROUTE_CODE


def test_router_escalates_when_code_and_prose_disagree():
    """The one case the lookup table gets wrong every single time."""
    from vasool.diagnosis.router import ROUTE_CONFLICT, decide_route
    ev = f.event(error=f.error(reason="authentication_failed",
                               issuer_message="Per transaction limit exceeded"))
    assert decide_route(ev) == ROUTE_CONFLICT


def test_router_escalates_when_the_code_says_nothing():
    from vasool.diagnosis.router import ROUTE_PROSE, decide_route
    ev = f.event(error=f.error(reason="payment_failed",
                               issuer_message="DECLINE - NOT SUFFICIENT FUNDS"))
    assert decide_route(ev) == ROUTE_PROSE


def test_router_is_deterministic_and_reads_no_ground_truth():
    from vasool.diagnosis.router import decide_route
    ev = f.event(error=f.error(reason="payment_failed", issuer_message="do not honour"))
    assert decide_route(ev) == decide_route(ev)


# --------------------------------------------------------------------------
# Settlement reads: unknown must never mean unsettled
# --------------------------------------------------------------------------

def _unreadable(_case):
    from vasool.executor.backend import SettlementUnknown
    raise SettlementUnknown("provider returned 503")


def test_a_money_action_is_denied_when_settlement_cannot_be_read():
    """Regression: this is the fail-open an adversarial review found.

    The REST backend used to return False when the order read failed, with a
    comment claiming that was the safe direction. False means 'not settled',
    which lets I1 approve — so an unreachable provider silently unlocked the
    exact action I1 exists to prevent.
    """
    from vasool.kernel.gate import Gate

    gate = Gate(POLICY, COSTS, _unreadable)
    review = gate.review(f.proposal(), f.case(), f.T0)
    assert not review.allowed
    assert Denial.SETTLEMENT_UNKNOWN in review.verdict.denials
    assert "I1" in review.verdict.invariant_ids


@pytest.mark.parametrize("intervention", [
    Intervention.RETRY_SAME_RAIL, Intervention.RETRY_ALT_RAIL,
    Intervention.PAYMENT_LINK, Intervention.PART_PAYMENT_LINK,
    Intervention.INSTRUMENT_REFRESH, Intervention.MANDATE_REREGISTER,
])
def test_no_money_moving_intervention_survives_an_unreadable_provider(intervention):
    from vasool.kernel.gate import Gate

    gate = Gate(POLICY, COSTS, _unreadable)
    proposal = f.proposal(intervention, channel=Channel.WHATSAPP)
    assert not gate.review(proposal, f.case(), f.T0).allowed


def test_actions_that_move_no_money_are_unaffected_by_an_unreadable_provider():
    """A WAIT or a STOP does not need a reachable provider to be safe."""
    from vasool.kernel.gate import Gate

    gate = Gate(POLICY, COSTS, _unreadable)
    for intervention in (Intervention.WAIT, Intervention.STOP):
        proposal = f.proposal(intervention, amount_paise=0, channel=Channel.NONE)
        review = gate.review(proposal, f.case(), f.T0)
        assert review.allowed, intervention


def test_an_unreadable_settlement_is_not_repairable():
    """Re-proposing cannot make a provider reachable, so do not loop on it."""
    from vasool.kernel.gate import Gate, repairable

    gate = Gate(POLICY, COSTS, _unreadable)
    assert not repairable(gate.review(f.proposal(), f.case(), f.T0))


def test_the_gate_counts_failed_settlement_reads():
    from vasool.kernel.gate import Gate

    gate = Gate(POLICY, COSTS, _unreadable)
    gate.review(f.proposal(), f.case(), f.T0)
    assert gate.settlement_reads_failed == 1
