"""Deterministic diagnosis and planning.

This module has two jobs, and it matters that they are the same code:

1. It is the **rules baseline** (arm B) the LLM has to beat. It is written to
   be genuinely competent — the classifier a good engineer produces in an
   afternoon, with a real keyword table over issuer prose, not a strawman. If
   the model cannot beat this, the model does not belong in the architecture,
   and the benchmark should say so.
2. It is the **degraded path**. When the model returns malformed output, times
   out, or trips the circuit breaker, decisions keep being made here and the
   case is tagged rather than dropped.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from vasool.core.policy import Policy
from vasool.core.types import (
    ActionProposal,
    CaseState,
    Channel,
    Diagnosis,
    FailureClass,
    FailureEvent,
    Intervention,
)
from vasool.kernel import raw_evidence

# Reason code -> class. Unambiguous codes resolve here with no text analysis.
_REASON_TO_CLASS: dict[str, FailureClass] = {
    "insufficient_funds": FailureClass.INSUFFICIENT_FUNDS,
    "issuer_down": FailureClass.ISSUER_DOWN,
    "gateway_timeout": FailureClass.RAIL_TIMEOUT,
    "card_expired": FailureClass.INSTRUMENT_DEAD,
    "invalid_card": FailureClass.INSTRUMENT_DEAD,
    "card_disabled": FailureClass.INSTRUMENT_DEAD,
    "token_expired": FailureClass.INSTRUMENT_DEAD,
    "invalid_token": FailureClass.INSTRUMENT_DEAD,
    "card_number_invalid": FailureClass.INSTRUMENT_DEAD,
    "authentication_abandoned": FailureClass.AUTH_ABANDONED,
    "authentication_failed": FailureClass.AUTH_FAILED,
    "risk_declined": FailureClass.RISK_DECLINED,
    "suspected_fraud": FailureClass.RISK_DECLINED,
    "payment_risk_check_failed": FailureClass.RISK_DECLINED,
    "declined_by_risk_engine": FailureClass.RISK_DECLINED,
    "transaction_limit_exceeded": FailureClass.LIMIT_EXCEEDED,
    "upi_limit_exceeded": FailureClass.LIMIT_EXCEEDED,
    "mandate_revoked": FailureClass.MANDATE_INVALID,
    "mandate_paused": FailureClass.MANDATE_INVALID,
    "emandate_not_active": FailureClass.MANDATE_INVALID,
    "mandate_cancelled": FailureClass.MANDATE_INVALID,
    "card_not_enabled_for_online": FailureClass.RESTRICTION,
    "international_transaction_not_allowed": FailureClass.RESTRICTION,
    "card_not_enabled_for_recurring": FailureClass.RESTRICTION,
}

# Keyword table over issuer prose, ordered most specific first. Used when the
# reason code is uninformative. Each entry is (needles, class, weight).
_KEYWORDS: list[tuple[tuple[str, ...], FailureClass, float]] = [
    (("not sufficient", "insufficient", "balance low", "low balance",
      "funds unavailable", "balance insufficient"), FailureClass.INSUFFICIENT_FUNDS, 0.86),
    (("bank down", "bank server not responding", "host offline", "issuer unreachable",
      "bank is currently unavailable", "remitter bank"), FailureClass.ISSUER_DOWN, 0.86),
    (("timed out", "timeout", "no response received", "did not respond",
      "indeterminate"), FailureClass.RAIL_TIMEOUT, 0.84),
    (("expired", "hotlisted", "blocked / hotlisted", "invalid card number",
      "re-tokenisation", "no longer valid"), FailureClass.INSTRUMENT_DEAD, 0.86),
    (("did not complete otp", "window closed", "cancelled at bank",
      "abandoned", "session expired"), FailureClass.AUTH_ABANDONED, 0.84),
    (("incorrect otp", "cvv mismatch", "authentication failed", "wrong upi pin",
      "wrong pin"), FailureClass.AUTH_FAILED, 0.84),
    (("risk engine", "fraud", "risk policy", "refer to issuer"),
     FailureClass.RISK_DECLINED, 0.82),
    (("limit exceeded", "limit reached", "exceeds permitted", "limit breached",
      "ceiling"), FailureClass.LIMIT_EXCEEDED, 0.84),
    (("mandate", "standing instruction", "debit authorisation",
      "autopay"), FailureClass.MANDATE_INVALID, 0.84),
    (("not enabled for online", "international transactions disabled",
      "e-commerce usage blocked", "not enabled for recurring"),
     FailureClass.RESTRICTION, 0.84),
]


def classify_from_code(event: FailureEvent) -> Diagnosis | None:
    """Classification from the machine-readable reason code alone."""
    reason = (event.error.reason or "").strip().lower()
    cls = _REASON_TO_CLASS.get(reason)
    if cls is None:
        return None
    return Diagnosis(
        failure_class=cls, confidence=0.90,
        rationale=f"reason code '{reason}' maps directly",
        evidence_fields=("error.reason",), source="rules",
    )


def classify_from_message(event: FailureEvent) -> Diagnosis | None:
    """Classification from the issuer's free text alone."""
    message = (event.error.issuer_message or "").lower()
    for needles, cls, weight in _KEYWORDS:
        hit = next((n for n in needles if n in message), None)
        if hit is not None:
            return Diagnosis(
                failure_class=cls, confidence=weight,
                rationale=f"issuer message matched '{hit}'",
                evidence_fields=("error.issuer_message",), source="rules",
            )
    return None


def classify(event: FailureEvent) -> Diagnosis:
    """Deterministic classification from evidence.

    Reason code first where it is unambiguous, then a keyword pass over the
    issuer message, then UNKNOWN. Note what it cannot do: weigh a reason code
    against a contradicting message, or read a phrase nobody wrote a rule for.
    Those are exactly the cases the diagnosis layer exists to pick up.
    """
    reason = (event.error.reason or "").strip().lower()
    message = (event.error.issuer_message or "").lower()

    if reason in _REASON_TO_CLASS:
        return Diagnosis(
            failure_class=_REASON_TO_CLASS[reason],
            confidence=0.90,
            rationale=f"reason code '{reason}' maps directly",
            evidence_fields=("error.reason",),
            source="rules",
        )

    for needles, cls, weight in _KEYWORDS:
        if any(n in message for n in needles):
            return Diagnosis(
                failure_class=cls,
                confidence=weight,
                rationale=f"issuer message matched '{next(n for n in needles if n in message)}'",
                evidence_fields=("error.issuer_message",),
                source="rules",
            )

    return Diagnosis(
        failure_class=FailureClass.UNKNOWN,
        confidence=0.30,
        rationale="no reason-code or keyword match",
        evidence_fields=("error.reason", "error.issuer_message"),
        source="rules",
    )


def pick_channel(event: FailureEvent, policy: Policy) -> Channel:
    """Best permitted channel, respecting DND at the channel level."""
    dnd_blocked = {Channel.SMS, Channel.VOICE} if event.customer.dnd_registered else set()
    for channel in event.merchant.preferred_channels:
        if channel in policy.permitted_channels and channel not in dnd_blocked:
            return channel
    for channel in (Channel.WHATSAPP, Channel.EMAIL, Channel.SMS):
        if channel in policy.permitted_channels and channel not in dnd_blocked:
            return channel
    return Channel.EMAIL


def next_sane_send_time(now: datetime, delay: timedelta, tz_offset_minutes: int) -> datetime:
    """Push an outreach to the next civilised local hour.

    Any competent engineer writes this, so the rules baseline gets it too. It
    is not the kernel's quiet-hours invariant: this shifts a send it happens to
    be planning, while I4 refuses one regardless of who asked or why.
    """
    target = now + delay
    for _ in range(14):
        local_hour = (target + timedelta(minutes=tz_offset_minutes)).hour
        if 10 <= local_hour < 20:
            return target
        target += timedelta(hours=1)
    return target


def _part_amount(event: FailureEvent, policy: Policy) -> int:
    floor = max(policy.absolute_part_payment_floor_ratio,
                event.merchant.part_payment_floor_ratio)
    return int(event.amount_paise * max(floor, 0.5))


def plan(
    diagnosis: Diagnosis, case: CaseState, now: datetime, policy: Policy,
) -> ActionProposal:
    """Cause determines cure. The whole thesis of the product, as a function.

    Escalates on repeat: an intervention that has already been spent on this
    case is not re-proposed, because the interesting question after a failed
    attempt is what to try *instead*.
    """
    event = case.event
    cls = diagnosis.failure_class
    channel = pick_channel(event, policy)
    spent = case.attempts
    has_alt = raw_evidence.alternate_rail_available(event)

    # A plain attempt cap. Not an invariant — just the counter any competent
    # implementation has, so the baseline is a real opponent and not a
    # strawman that loops until the horizon.
    if spent >= policy.max_money_actions:
        return ActionProposal(
            case_id=case.case_id, intervention=Intervention.STOP,
            channel=Channel.NONE, amount_paise=0, currency=event.currency,
            scheduled_for=now, diagnosis=diagnosis,
            rationale=f"attempt cap reached ({spent} actions)",
        )

    def proposal(
        intervention: Intervention, delay: timedelta, amount: int | None = None,
        chan: Channel | None = None, why: str = "",
    ) -> ActionProposal:
        is_contact = intervention in {
            Intervention.PAYMENT_LINK, Intervention.PART_PAYMENT_LINK,
            Intervention.INSTRUMENT_REFRESH, Intervention.MANDATE_REREGISTER,
            Intervention.HANDOFF_HUMAN,
        }
        when = (
            next_sane_send_time(now, delay, event.customer.tz_offset_minutes)
            if is_contact else now + delay
        )
        return ActionProposal(
            case_id=case.case_id,
            intervention=intervention,
            channel=chan if chan is not None else (
                channel if intervention in
                {Intervention.PAYMENT_LINK, Intervention.PART_PAYMENT_LINK,
                 Intervention.INSTRUMENT_REFRESH, Intervention.MANDATE_REREGISTER,
                 Intervention.HANDOFF_HUMAN}
                else Channel.NONE
            ),
            amount_paise=event.amount_paise if amount is None else amount,
            currency=event.currency,
            scheduled_for=when,
            diagnosis=diagnosis,
            rationale=why,
            next_review_after=timedelta(hours=24),
        )

    if cls is FailureClass.RISK_DECLINED:
        return proposal(Intervention.STOP, timedelta(0), amount=0,
                        why="risk declines do not recover; retrying damages decline rate")

    if cls is FailureClass.INSUFFICIENT_FUNDS:
        # Liquidity comes back on a schedule nobody publishes, so give it two
        # spaced attempts before spending a contact.
        if spent == 0:
            return proposal(Intervention.RETRY_SAME_RAIL, timedelta(hours=72),
                            why="wait for liquidity to return before replaying")
        if spent == 1:
            return proposal(Intervention.RETRY_SAME_RAIL, timedelta(hours=96),
                            why="second window; inflows are not weekly-regular")
        if spent == 2 and event.merchant.allows_part_payment:
            return proposal(Intervention.PART_PAYMENT_LINK, timedelta(hours=6),
                            amount=_part_amount(event, policy),
                            why="smaller ask clears a thinner balance")
        return proposal(Intervention.PAYMENT_LINK, timedelta(hours=12),
                        why="hand the customer control of timing")

    if cls in (FailureClass.ISSUER_DOWN, FailureClass.RAIL_TIMEOUT):
        if has_alt and spent == 0:
            return proposal(Intervention.RETRY_ALT_RAIL, timedelta(minutes=45),
                            why="switch rails while intent is still hot")
        if spent <= 2:
            return proposal(Intervention.RETRY_SAME_RAIL,
                            timedelta(hours=2 if spent == 0 else 14),
                            why="transient outage, replay after it clears")
        return proposal(Intervention.PAYMENT_LINK, timedelta(hours=6),
                        why="outage outlived the retry budget")

    if cls is FailureClass.INSTRUMENT_DEAD:
        if has_alt and spent == 0:
            return proposal(Intervention.RETRY_ALT_RAIL, timedelta(minutes=30),
                            why="second instrument on file, no customer action needed")
        return proposal(Intervention.INSTRUMENT_REFRESH, timedelta(hours=1),
                        why="no replay can work; the instrument must be replaced")

    if cls is FailureClass.AUTH_ABANDONED:
        if spent == 0:
            return proposal(Intervention.PAYMENT_LINK, timedelta(minutes=40),
                            why="customer was present and bailed at auth; re-ask while warm")
        return proposal(Intervention.PAYMENT_LINK, timedelta(hours=20),
                        why="second ask on a longer horizon")

    if cls is FailureClass.AUTH_FAILED:
        if spent >= 2 and has_alt:
            return proposal(Intervention.RETRY_ALT_RAIL, timedelta(hours=6),
                            why="a rail with simpler auth may clear where 3DS did not")
        return proposal(Intervention.PAYMENT_LINK, timedelta(hours=2),
                        why="customer wanted to pay but failed auth; give a fresh attempt")

    if cls is FailureClass.LIMIT_EXCEEDED:
        if has_alt and spent == 0:
            return proposal(Intervention.RETRY_ALT_RAIL, timedelta(hours=1),
                            why="different rail carries a different limit")
        if event.merchant.allows_part_payment:
            return proposal(Intervention.PART_PAYMENT_LINK, timedelta(hours=4),
                            amount=_part_amount(event, policy),
                            why="split the amount under the ceiling")
        return proposal(Intervention.RETRY_SAME_RAIL, timedelta(hours=26),
                        why="daily limit resets")

    if cls is FailureClass.MANDATE_INVALID:
        return proposal(Intervention.MANDATE_REREGISTER, timedelta(hours=1),
                        why="authorisation no longer exists; a retry is a category error")

    if cls is FailureClass.RESTRICTION:
        if has_alt:
            return proposal(Intervention.RETRY_ALT_RAIL, timedelta(minutes=30),
                            why="instrument is barred from this transaction type")
        return proposal(Intervention.INSTRUMENT_REFRESH, timedelta(hours=2),
                        why="needs a different instrument entirely")

    return proposal(Intervention.PAYMENT_LINK, timedelta(hours=8),
                    why="cause unclear; hand control to the customer")
