"""Issuer message bank.

Banks describe the same condition in wildly different words, and a large share
of declines arrive with a generic reason code and the actual cause sitting in
free text. That inconsistency is the reason a diagnosis layer exists at all, so
the benchmark reproduces it rather than handing every arm a clean label.

Strings are modelled on the phrasing Indian issuers and PSPs actually emit.
``do not honour`` appears under two different causes on purpose — it is the
canonical ambiguous decline, and any classifier that treats it as decisive is
wrong.
"""

from __future__ import annotations

from vasool.core.types import FailureClass

#: Canonical machine-readable reason per class, as Razorpay would surface it.
CLEAN_REASON: dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS: "insufficient_funds",
    FailureClass.ISSUER_DOWN: "issuer_down",
    FailureClass.RAIL_TIMEOUT: "gateway_timeout",
    FailureClass.INSTRUMENT_DEAD: "card_expired",
    FailureClass.AUTH_ABANDONED: "authentication_abandoned",
    FailureClass.AUTH_FAILED: "authentication_failed",
    FailureClass.RISK_DECLINED: "risk_declined",
    FailureClass.LIMIT_EXCEEDED: "transaction_limit_exceeded",
    FailureClass.MANDATE_INVALID: "mandate_revoked",
    FailureClass.RESTRICTION: "card_not_enabled_for_online",
}

#: Alternate clean codes, so the taxonomy is not a one-to-one code lookup.
CLEAN_ALTERNATES: dict[FailureClass, list[str]] = {
    FailureClass.INSTRUMENT_DEAD: [
        "card_expired", "invalid_card", "card_disabled", "token_expired",
    ],
    FailureClass.MANDATE_INVALID: [
        "mandate_revoked", "mandate_paused", "emandate_not_active",
    ],
    FailureClass.RESTRICTION: [
        "card_not_enabled_for_online", "international_transaction_not_allowed",
        "card_not_enabled_for_recurring",
    ],
    FailureClass.RISK_DECLINED: [
        "risk_declined", "suspected_fraud", "payment_risk_check_failed",
    ],
}

MESSAGES: dict[FailureClass, list[str]] = {
    FailureClass.INSUFFICIENT_FUNDS: [
        "Txn declined by issuing bank - available balance low",
        "DECLINE - NOT SUFFICIENT FUNDS",
        "Insufficient balance in account. Please retry after adding funds.",
        "Bank declined: funds unavailable at time of debit",
        "do not honour",
        "A/c balance insufficient for the requested debit amount",
    ],
    FailureClass.ISSUER_DOWN: [
        "Issuer bank is currently unavailable, please retry",
        "RB-101 remitter bank down",
        "UPI: remitter bank server not responding",
        "Bank host offline - please attempt after some time",
        "Issuer unreachable (NPCI response 91)",
    ],
    FailureClass.RAIL_TIMEOUT: [
        "Request timed out at acquirer",
        "No response received from gateway within TAT",
        "Transaction timed out awaiting issuer response",
        "Gateway did not respond, status indeterminate",
    ],
    FailureClass.INSTRUMENT_DEAD: [
        "Card has expired",
        "Invalid card number",
        "Card is blocked / hotlisted by issuer",
        "Saved token no longer valid, re-tokenisation required",
        "Expired card - please use another card",
    ],
    FailureClass.AUTH_ABANDONED: [
        "Customer did not complete OTP within session",
        "3DS authentication window closed by user",
        "User cancelled at bank page",
        "Authentication abandoned by cardholder",
        "Session expired before OTP submission",
    ],
    FailureClass.AUTH_FAILED: [
        "Incorrect OTP entered 3 times",
        "CVV mismatch",
        "Authentication failed at issuer ACS",
        "Wrong UPI PIN entered",
    ],
    FailureClass.RISK_DECLINED: [
        "Transaction declined by issuer risk engine",
        "do not honour",
        "Blocked by bank fraud rules",
        "Declined - risk policy violation",
        "Suspected fraudulent transaction, refer to issuer",
    ],
    FailureClass.LIMIT_EXCEEDED: [
        "Per transaction limit exceeded",
        "Daily UPI limit reached for this VPA",
        "Amount exceeds permitted ceiling for card",
        "Debit limit breached for the day",
    ],
    FailureClass.MANDATE_INVALID: [
        "Mandate not active",
        "E-mandate revoked by customer",
        "UPI Autopay mandate paused",
        "Standing instruction cancelled at bank",
        "No active debit authorisation found for this subscription",
    ],
    FailureClass.RESTRICTION: [
        "Card not enabled for online transactions",
        "International transactions disabled on this card",
        "E-commerce usage blocked by cardholder",
        "Card not enabled for recurring payments",
    ],
}

DESCRIPTION: dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS: "Your payment could not be completed.",
    FailureClass.ISSUER_DOWN: "Payment failed at the bank's end.",
    FailureClass.RAIL_TIMEOUT: "Payment processing was interrupted.",
    FailureClass.INSTRUMENT_DEAD: "Payment failed with the saved instrument.",
    FailureClass.AUTH_ABANDONED: "Payment was not authenticated.",
    FailureClass.AUTH_FAILED: "Payment authentication was unsuccessful.",
    FailureClass.RISK_DECLINED: "Payment was declined.",
    FailureClass.LIMIT_EXCEEDED: "Payment was declined by the bank.",
    FailureClass.MANDATE_INVALID: "Recurring debit could not be processed.",
    FailureClass.RESTRICTION: "Payment was declined by the bank.",
}

ERROR_SOURCE: dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS: "bank",
    FailureClass.ISSUER_DOWN: "bank",
    FailureClass.RAIL_TIMEOUT: "gateway",
    FailureClass.INSTRUMENT_DEAD: "customer",
    FailureClass.AUTH_ABANDONED: "customer",
    FailureClass.AUTH_FAILED: "customer",
    FailureClass.RISK_DECLINED: "bank",
    FailureClass.LIMIT_EXCEEDED: "bank",
    FailureClass.MANDATE_INVALID: "bank",
    FailureClass.RESTRICTION: "bank",
}

ERROR_STEP: dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS: "payment_authorization",
    FailureClass.ISSUER_DOWN: "payment_authorization",
    FailureClass.RAIL_TIMEOUT: "payment_authorization",
    FailureClass.INSTRUMENT_DEAD: "payment_initiation",
    FailureClass.AUTH_ABANDONED: "payment_authentication",
    FailureClass.AUTH_FAILED: "payment_authentication",
    FailureClass.RISK_DECLINED: "payment_authorization",
    FailureClass.LIMIT_EXCEEDED: "payment_authorization",
    FailureClass.MANDATE_INVALID: "payment_initiation",
    FailureClass.RESTRICTION: "payment_authorization",
}
