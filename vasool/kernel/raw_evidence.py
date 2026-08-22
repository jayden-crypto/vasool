"""Facts the kernel derives itself, from raw provider evidence only.

This module exists because of one rule: **the kernel never trusts the model's
classification.** A diagnosis is an input to policy, not an authority over it.
Where an error code is unambiguous, the kernel decides for itself and the
model's opinion is irrelevant.

The mapping is deliberately conservative. A code appears here only when it
admits exactly one reading; everything else is left ambiguous and handled by
the diagnosis layer, where being wrong is cheap.
"""

from __future__ import annotations

from dataclasses import dataclass

from vasool.core.types import FailureEvent, Rail

# Reason codes whose instrument is provably unusable. Replaying it cannot
# succeed — not with a better prompt, not on the third try, not ever.
_DEAD_INSTRUMENT = frozenset({
    "card_expired",
    "invalid_card",
    "card_disabled",
    "token_expired",
    "invalid_token",
    "card_number_invalid",
})

# Mandate/subscription authorisations that no longer exist.
_DEAD_MANDATE = frozenset({
    "mandate_revoked",
    "mandate_paused",
    "emandate_not_active",
    "mandate_cancelled",
})

# Instrument works, but is barred from this class of transaction.
_RESTRICTED = frozenset({
    "international_transaction_not_allowed",
    "card_not_enabled_for_online",
    "card_not_enabled_for_recurring",
})

# Risk declines. Replaying these damages the merchant's own decline rate and
# can escalate issuer-side scrutiny, so they are worse than merely useless.
_RISK = frozenset({
    "risk_declined",
    "suspected_fraud",
    "payment_risk_check_failed",
    "declined_by_risk_engine",
})


@dataclass(frozen=True)
class RawFacts:
    """Deterministic facts. No model was consulted to produce any of these."""
    instrument_provably_dead: bool
    mandate_provably_dead: bool
    instrument_restricted: bool
    risk_declined: bool
    reason: str

    @property
    def retry_is_futile(self) -> bool:
        """Same-rail replay has probability zero on the evidence alone."""
        return (
            self.instrument_provably_dead
            or self.mandate_provably_dead
            or self.instrument_restricted
        )

    @property
    def retry_is_harmful(self) -> bool:
        return self.risk_declined


def read(event: FailureEvent) -> RawFacts:
    reason = (event.error.reason or "").strip().lower()
    return RawFacts(
        instrument_provably_dead=reason in _DEAD_INSTRUMENT,
        mandate_provably_dead=reason in _DEAD_MANDATE,
        instrument_restricted=reason in _RESTRICTED,
        risk_declined=reason in _RISK,
        reason=reason,
    )


def alternate_rail_available(event: FailureEvent) -> bool:
    """Does the customer have a second instrument on file to switch to?"""
    return any(r is not event.rail for r in event.customer.saved_rails)


def alternate_rail_for(event: FailureEvent) -> Rail | None:
    for rail in event.customer.saved_rails:
        if rail is not event.rail:
            return rail
    return None
