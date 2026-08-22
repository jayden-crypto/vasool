"""Domain types shared by every layer.

Everything here is a plain value object. Nothing in this module performs I/O,
calls a model, or touches money — that separation is the point of the project.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

# --------------------------------------------------------------------------
# Money. Always integer paise. Floats are never allowed to represent money.
# --------------------------------------------------------------------------

def rupees(paise: int) -> str:
    """Render paise as a rupee string for human output."""
    return f"₹{paise / 100:,.2f}"


# --------------------------------------------------------------------------
# The closed failure taxonomy.
#
# Closed is load-bearing: the kernel re-derives futility from raw evidence and
# the diagnosis layer is schema-constrained to these members, so a model cannot
# invent a class that no policy covers.
# --------------------------------------------------------------------------

class FailureClass(str, Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"   # soft decline, resolves with time
    ISSUER_DOWN = "ISSUER_DOWN"                 # transient issuer/gateway outage
    RAIL_TIMEOUT = "RAIL_TIMEOUT"               # network/gateway timeout, no verdict
    INSTRUMENT_DEAD = "INSTRUMENT_DEAD"         # expired/invalid card or token
    AUTH_ABANDONED = "AUTH_ABANDONED"           # customer dropped at 3DS/OTP
    AUTH_FAILED = "AUTH_FAILED"                 # wrong OTP/CVV, customer present
    RISK_DECLINED = "RISK_DECLINED"             # issuer or platform risk decline
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"           # per-txn or daily rail limit
    MANDATE_INVALID = "MANDATE_INVALID"         # emandate revoked/paused
    RESTRICTION = "RESTRICTION"                 # intl/online disabled on instrument
    UNKNOWN = "UNKNOWN"                         # evidence insufficient


#: Classes where *any* replay of the same instrument has probability zero.
#: The kernel enforces this from raw evidence, never from the model's claim.
FUTILE_FOR_RETRY: frozenset[FailureClass] = frozenset({
    FailureClass.INSTRUMENT_DEAD,
    FailureClass.MANDATE_INVALID,
    FailureClass.RESTRICTION,
})

#: Classes where retrying actively harms the merchant (decline-rate damage,
#: issuer-side flagging) on top of being useless.
HARMFUL_TO_RETRY: frozenset[FailureClass] = frozenset({
    FailureClass.RISK_DECLINED,
})


class Intervention(str, Enum):
    WAIT = "WAIT"                                 # do nothing now, re-evaluate later
    RETRY_SAME_RAIL = "RETRY_SAME_RAIL"           # replay saved instrument
    RETRY_ALT_RAIL = "RETRY_ALT_RAIL"             # switch card <-> UPI
    PAYMENT_LINK = "PAYMENT_LINK"                 # contact + hosted link, full amount
    PART_PAYMENT_LINK = "PART_PAYMENT_LINK"       # contact + link, reduced amount
    INSTRUMENT_REFRESH = "INSTRUMENT_REFRESH"     # contact + update card/token
    MANDATE_REREGISTER = "MANDATE_REREGISTER"     # contact + new authorisation
    HANDOFF_HUMAN = "HANDOFF_HUMAN"               # escalate to a person
    STOP = "STOP"                                 # terminate the case


#: Interventions that replay an instrument without the customer present.
RETRY_INTERVENTIONS: frozenset[Intervention] = frozenset({
    Intervention.RETRY_SAME_RAIL,
    Intervention.RETRY_ALT_RAIL,
})

#: Interventions that reach out to a human being. These consume contact budget,
#: are gated by consent, and are blocked during quiet hours.
CONTACT_INTERVENTIONS: frozenset[Intervention] = frozenset({
    Intervention.PAYMENT_LINK,
    Intervention.PART_PAYMENT_LINK,
    Intervention.INSTRUMENT_REFRESH,
    Intervention.MANDATE_REREGISTER,
    Intervention.HANDOFF_HUMAN,
})

#: Interventions that can move money on their own.
MONEY_MOVING: frozenset[Intervention] = RETRY_INTERVENTIONS | {
    Intervention.PAYMENT_LINK,
    Intervention.PART_PAYMENT_LINK,
    Intervention.INSTRUMENT_REFRESH,
    Intervention.MANDATE_REREGISTER,
}


class Channel(str, Enum):
    NONE = "NONE"
    SMS = "SMS"
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    VOICE = "VOICE"


class Rail(str, Enum):
    CARD = "CARD"
    UPI = "UPI"
    NETBANKING = "NETBANKING"
    EMANDATE = "EMANDATE"


# --------------------------------------------------------------------------
# Observable evidence. This is the *entire* view an arm gets. Anything the
# simulator knows that is not on these objects is hidden by construction.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ContactRecord:
    at: datetime
    channel: Channel
    intervention: Intervention


@dataclass(frozen=True)
class CustomerProfile:
    customer_id: str
    contact_hash: str                  # stands in for phone/email
    opted_out: bool                    # observable consent state
    dnd_registered: bool
    prior_successful_payments: int
    prior_failed_payments: int
    prior_recoveries: int
    saved_rails: tuple[Rail, ...]      # what instruments exist on file
    tz_offset_minutes: int = 330       # IST


@dataclass(frozen=True)
class MerchantConfig:
    merchant_id: str
    name: str
    category: str
    allows_part_payment: bool
    part_payment_floor_ratio: float    # smallest acceptable fraction of order
    preferred_channels: tuple[Channel, ...]


@dataclass(frozen=True)
class RazorpayError:
    """The shape Razorpay actually hands you on a failed payment.

    ``issuer_message`` is the messy part: banks phrase the same condition a
    dozen different ways, and that inconsistency is the language problem the
    diagnosis layer exists to solve.
    """
    code: str                # BAD_REQUEST_ERROR | GATEWAY_ERROR | SERVER_ERROR
    description: str
    source: str              # customer | bank | gateway | business
    step: str                # payment_authentication | payment_authorization | ...
    reason: str              # payment_failed | insufficient_funds | ...
    issuer_message: str = ""


@dataclass(frozen=True)
class FailureEvent:
    event_id: str
    payment_id: str
    order_id: str
    amount_paise: int
    currency: str
    rail: Rail
    failed_at: datetime
    error: RazorpayError
    customer: CustomerProfile
    merchant: MerchantConfig
    attempt_index: int = 0             # how many times this order already failed
    subscription_id: Optional[str] = None

    def evidence_digest(self) -> str:
        """Stable hash of everything a diagnoser is allowed to see.

        Used as the LLM response-cache key, which is what makes a benchmark run
        replayable byte-for-byte without an API key.
        """
        payload = {
            "amount_paise": self.amount_paise,
            "currency": self.currency,
            "rail": self.rail.value,
            "attempt_index": self.attempt_index,
            "has_subscription": self.subscription_id is not None,
            "error": asdict(self.error),
            "customer": {
                "opted_out": self.customer.opted_out,
                "dnd": self.customer.dnd_registered,
                "prior_ok": self.customer.prior_successful_payments,
                "prior_fail": self.customer.prior_failed_payments,
                "prior_recoveries": self.customer.prior_recoveries,
                "rails": [r.value for r in self.customer.saved_rails],
            },
            "merchant": {
                "category": self.merchant.category,
                "part_payment": self.merchant.allows_part_payment,
                "floor": self.merchant.part_payment_floor_ratio,
            },
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# Case state — the running record the kernel reasons over.
# --------------------------------------------------------------------------

class CaseStatus(str, Enum):
    OPEN = "OPEN"
    RECOVERED = "RECOVERED"
    STOPPED = "STOPPED"          # deliberate halt by stopping rule
    ABANDONED = "ABANDONED"      # horizon reached with no resolution
    LOST = "LOST"                # customer permanently disengaged
    DOUBLE_CHARGED = "DOUBLE_CHARGED"  # collected on an order already settled


@dataclass
class CaseState:
    case_id: str
    event: FailureEvent
    status: CaseStatus = CaseStatus.OPEN
    collected_paise: int = 0
    attempts: int = 0                       # money actions executed
    contacts: list[ContactRecord] = field(default_factory=list)
    executed_keys: set[str] = field(default_factory=set)
    spend_paise: int = 0                    # cost of actions taken
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    next_decision_at: Optional[datetime] = None
    degraded_decisions: int = 0             # decisions made on the fallback path
    notes: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status == CaseStatus.OPEN

    @property
    def settled(self) -> bool:
        """True once money has arrived through any channel."""
        return self.collected_paise > 0

    def contacts_since(self, since: datetime) -> int:
        return sum(1 for c in self.contacts if c.at >= since)

    def last_contact_at(self) -> Optional[datetime]:
        return self.contacts[-1].at if self.contacts else None


# --------------------------------------------------------------------------
# The proposal / verdict / outcome triad that crosses the authority boundary.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Diagnosis:
    """What the reasoning zone produces. Carries no authority whatsoever."""
    failure_class: FailureClass
    confidence: float
    rationale: str
    evidence_fields: tuple[str, ...]   # which observable fields justify the call
    source: str                        # "llm" | "fallback" | "rules"


@dataclass(frozen=True)
class ActionProposal:
    """A document, not a call. The executor will not act on this directly."""
    case_id: str
    intervention: Intervention
    channel: Channel
    amount_paise: int
    currency: str
    scheduled_for: datetime
    diagnosis: Diagnosis
    rationale: str = ""
    next_review_after: timedelta = timedelta(hours=24)

    def idempotency_seed(self) -> str:
        """Identity of this *intent*, not of the attempt that carries it.

        Deriving the key from a counter that advances after execution was a bug:
        a duplicate delivery of the same decision computed a different key and
        sailed past the duplicate check. The key must be a function of the
        decision — case, action, amount and the moment it was scheduled for —
        so that two deliveries of one decision collide and two genuinely
        different decisions do not.
        """
        return (
            f"{self.case_id}:{self.intervention.value}:{self.amount_paise}"
            f":{self.currency}:{self.scheduled_for.isoformat()}"
        )


class Denial(str, Enum):
    """Every rejection is a named, machine-readable reason code."""
    ALREADY_COLLECTED = "ALREADY_COLLECTED"
    DUPLICATE_ACTION = "DUPLICATE_ACTION"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    AMOUNT_EXCEEDS_ORDER = "AMOUNT_EXCEEDS_ORDER"
    AMOUNT_BELOW_FLOOR = "AMOUNT_BELOW_FLOOR"
    PART_PAYMENT_NOT_ALLOWED = "PART_PAYMENT_NOT_ALLOWED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
    CONTACT_BUDGET_EXCEEDED = "CONTACT_BUDGET_EXCEEDED"
    COOLING_OFF = "COOLING_OFF"
    QUIET_HOURS = "QUIET_HOURS"
    FUTILE_RETRY = "FUTILE_RETRY"
    HARMFUL_RETRY = "HARMFUL_RETRY"
    ATTEMPT_CAP_REACHED = "ATTEMPT_CAP_REACHED"
    BELOW_STOPPING_THRESHOLD = "BELOW_STOPPING_THRESHOLD"
    CHANNEL_NOT_PERMITTED = "CHANNEL_NOT_PERMITTED"
    HORIZON_EXCEEDED = "HORIZON_EXCEEDED"
    UNAUDITED = "UNAUDITED"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    denials: tuple[Denial, ...] = ()
    invariant_ids: tuple[str, ...] = ()   # which invariants fired
    detail: str = ""

    @staticmethod
    def allow() -> "Verdict":
        return Verdict(allowed=True)

    @staticmethod
    def deny(denial: Denial, invariant_id: str, detail: str = "") -> "Verdict":
        return Verdict(False, (denial,), (invariant_id,), detail)

    def merge(self, other: "Verdict") -> "Verdict":
        if self.allowed and other.allowed:
            return Verdict.allow()
        return Verdict(
            allowed=False,
            denials=self.denials + other.denials,
            invariant_ids=self.invariant_ids + other.invariant_ids,
            detail="; ".join(d for d in (self.detail, other.detail) if d),
        )


@dataclass(frozen=True)
class ActionOutcome:
    executed: bool
    succeeded: bool
    collected_paise: int
    cost_paise: int
    detail: str = ""
    harms: tuple[str, ...] = ()
    provider_ref: Optional[str] = None


def canonical(obj: Any) -> str:
    """Deterministic JSON for hashing. Sorted keys, no whitespace, enum-safe."""
    def default(o: Any) -> Any:
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, timedelta):
            return o.total_seconds()
        if isinstance(o, (set, frozenset)):
            return sorted(str(x) for x in o)
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        raise TypeError(f"not serialisable: {type(o)}")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=default)
