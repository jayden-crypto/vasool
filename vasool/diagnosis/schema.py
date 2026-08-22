"""The contract across the authority boundary.

The reasoning zone emits exactly this and nothing else. It is a closed schema
over a closed taxonomy: a model cannot name a failure class no policy covers,
cannot invent an intervention the executor has no code path for, and cannot
attach anything resembling a credential or a raw API call.

Passing validation is not approval. Everything here still goes to the Gate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FailureClassName = Literal[
    "INSUFFICIENT_FUNDS", "ISSUER_DOWN", "RAIL_TIMEOUT", "INSTRUMENT_DEAD",
    "AUTH_ABANDONED", "AUTH_FAILED", "RISK_DECLINED", "LIMIT_EXCEEDED",
    "MANDATE_INVALID", "RESTRICTION", "UNKNOWN",
]

InterventionName = Literal[
    "WAIT", "RETRY_SAME_RAIL", "RETRY_ALT_RAIL", "PAYMENT_LINK",
    "PART_PAYMENT_LINK", "INSTRUMENT_REFRESH", "MANDATE_REREGISTER",
    "HANDOFF_HUMAN", "STOP",
]

ChannelName = Literal["NONE", "SMS", "EMAIL", "WHATSAPP", "VOICE"]


class Proposal(BaseModel):
    """One diagnosis and one proposed action. A document, not a call."""

    failure_class: FailureClassName = Field(
        description="The single cause that best explains this failure."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Calibrated probability that failure_class is correct.",
    )
    evidence_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Which observed fields justify the classification, e.g. "
            "'error.reason', 'error.issuer_message', 'customer.saved_rails'."
        ),
    )
    rationale: str = Field(
        max_length=600,
        description="Why this cause and this action, in two sentences.",
    )
    intervention: InterventionName = Field(
        description="The action most likely to recover the money."
    )
    channel: ChannelName = Field(
        description="Contact channel, or NONE for actions that reach no human."
    )
    amount_paise: int = Field(
        ge=0,
        description=(
            "Amount to collect, in paise. Equal to the order amount except for "
            "PART_PAYMENT_LINK. Never greater than the order."
        ),
    )
    delay_hours: float = Field(
        ge=0.0, le=504.0,
        description="Hours to wait before the action should run.",
    )
