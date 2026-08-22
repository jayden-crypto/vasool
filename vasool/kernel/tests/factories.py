"""Small builders so tests read like statements about behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta

from vasool.core.types import (
    ActionProposal,
    CaseState,
    Channel,
    CustomerProfile,
    Diagnosis,
    FailureClass,
    FailureEvent,
    Intervention,
    MerchantConfig,
    Rail,
    RazorpayError,
)

T0 = datetime(2026, 9, 1, 6, 30)          # 12:00 IST, comfortably outside quiet hours


def customer(**over) -> CustomerProfile:
    defaults = dict(
        customer_id="cust_1", contact_hash="ct_1", opted_out=False,
        dnd_registered=False, prior_successful_payments=3,
        prior_failed_payments=1, prior_recoveries=0,
        saved_rails=(Rail.CARD,), tz_offset_minutes=330,
    )
    defaults.update(over)
    return CustomerProfile(**defaults)


def merchant(**over) -> MerchantConfig:
    defaults = dict(
        merchant_id="mrch_1", name="Test Co", category="ecommerce",
        allows_part_payment=True, part_payment_floor_ratio=0.3,
        preferred_channels=(Channel.WHATSAPP, Channel.EMAIL),
    )
    defaults.update(over)
    return MerchantConfig(**defaults)


def error(reason: str = "insufficient_funds", **over) -> RazorpayError:
    defaults = dict(
        code="BAD_REQUEST_ERROR", description="Payment failed.", source="bank",
        step="payment_authorization", reason=reason,
        issuer_message="DECLINE - NOT SUFFICIENT FUNDS",
    )
    defaults.update(over)
    return RazorpayError(**defaults)


def event(amount_paise: int = 250000, **over) -> FailureEvent:
    defaults = dict(
        event_id="evt_1", payment_id="pay_1", order_id="order_1",
        amount_paise=amount_paise, currency="INR", rail=Rail.CARD,
        failed_at=T0 - timedelta(hours=2), error=error(),
        customer=customer(), merchant=merchant(), attempt_index=0,
        subscription_id=None,
    )
    defaults.update(over)
    return FailureEvent(**defaults)


def case(**over) -> CaseState:
    ev = over.pop("event", None) or event()
    state = CaseState(case_id="case_0001", event=ev, opened_at=ev.failed_at)
    for key, value in over.items():
        setattr(state, key, value)
    return state


def diagnosis(
    failure_class: FailureClass = FailureClass.INSUFFICIENT_FUNDS,
    confidence: float = 0.9, source: str = "rules",
) -> Diagnosis:
    return Diagnosis(
        failure_class=failure_class, confidence=confidence,
        rationale="test", evidence_fields=("error.reason",), source=source,
    )


def proposal(
    intervention: Intervention = Intervention.PAYMENT_LINK,
    amount_paise: int | None = None,
    channel: Channel = Channel.WHATSAPP,
    when: datetime = T0,
    currency: str = "INR",
    diag: Diagnosis | None = None,
    case_id: str = "case_0001",
) -> ActionProposal:
    return ActionProposal(
        case_id=case_id, intervention=intervention, channel=channel,
        amount_paise=250000 if amount_paise is None else amount_paise,
        currency=currency, scheduled_for=when,
        diagnosis=diag or diagnosis(),
    )
