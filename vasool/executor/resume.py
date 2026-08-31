"""Rebuild case state from the ledger.

The README claimed the write-ahead ledger let a crashed run reconstruct exactly
what was attempted. That was true of the *data* and false of the *code*:
``Ledger.load`` had one caller, the trace viewer, and there was no resume. An
adversarial review pointed out that the fault scenario certifying crash recovery
read keys out of the still-live in-memory ledger and compared them to the
still-live case — which crashes nothing and proves nothing.

This is the missing half. Given a ledger and the events the cases were opened
for, it returns the case states a resumed process should continue from, so the
same decision is never executed twice across a restart.

The reconstruction is deliberately conservative. An ``intent`` with no matching
``outcome`` is a decision that was in flight when the process died: its key is
marked executed, because the write may well have landed and replaying it is the
failure this whole subsystem exists to prevent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping

from vasool.core.types import (
    CONTACT_INTERVENTIONS,
    CaseState,
    CaseStatus,
    Channel,
    ContactRecord,
    FailureEvent,
    Intervention,
)
from vasool.executor.ledger import Ledger


def rebuild(ledger: Ledger, events: Mapping[str, FailureEvent]) -> dict[str, CaseState]:
    """Reconstruct every case the ledger mentions.

    ``events`` supplies the immutable facts a ledger does not carry — the order,
    the customer, the amount at risk. Everything mutable is derived from the
    chain.
    """
    cases: dict[str, CaseState] = {}
    intents: dict[str, dict] = {}          # idempotency_key -> intent payload
    resolved: set[str] = set()

    for record in ledger:
        event = events.get(record.case_id)
        if event is None:
            continue
        case = cases.get(record.case_id)
        if case is None:
            case = CaseState(case_id=record.case_id, event=event,
                             opened_at=event.failed_at)
            cases[record.case_id] = case

        payload = record.payload
        at = _parse(record.at)

        if record.kind == "intent":
            key = payload.get("idempotency_key", "")
            intents[key] = {**payload, "case_id": record.case_id, "at": at}
            # Assume it landed until an outcome says otherwise. A decision in
            # flight at the moment of a crash must never be replayed.
            case.executed_keys.add(key)
            case.attempts += 1
            case.decision_ordinal = max(
                case.decision_ordinal, int(payload.get("decision_ordinal", 0)) + 1)
            intervention = _intervention(payload.get("intervention"))
            if intervention in CONTACT_INTERVENTIONS:
                case.contacts.append(ContactRecord(
                    at=at, channel=_channel(payload.get("channel")),
                    intervention=intervention))

        elif record.kind == "outcome":
            key = payload.get("idempotency_key", "")
            resolved.add(key)
            case.spend_paise += int(payload.get("cost_paise", 0) or 0)
            if payload.get("succeeded") and payload.get("collected_paise"):
                case.collected_paise += int(payload["collected_paise"])
                case.status = CaseStatus.RECOVERED
                case.closed_at = at
            if not payload.get("executed"):
                # The action provably did not happen; undo the assumption.
                case.attempts = max(0, case.attempts - 1)

        elif record.kind == "stopped":
            case.status = CaseStatus.STOPPED
            case.closed_at = at

        elif record.kind == "unresolved":
            case.notes.append("unresolved_write:" + payload.get("idempotency_key", ""))

    for case in cases.values():
        in_flight = [k for k in case.executed_keys if k not in resolved]
        for key in in_flight:
            case.notes.append(f"in_flight_at_crash:{key}")
    return cases


def in_flight_keys(ledger: Ledger) -> set[str]:
    """Keys with an intent and no outcome — decisions of unknown fate."""
    intents = {r.payload.get("idempotency_key") for r in ledger if r.kind == "intent"}
    done = {r.payload.get("idempotency_key") for r in ledger
            if r.kind in ("outcome", "unresolved")}
    return {k for k in intents - done if k}


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _intervention(name: str | None) -> Intervention | None:
    try:
        return Intervention(name) if name else None
    except ValueError:
        return None


def _channel(name: str | None) -> Channel:
    try:
        return Channel(name) if name else Channel.NONE
    except ValueError:
        return Channel.NONE


def summarise(cases: Iterable[CaseState]) -> dict[str, int]:
    cases = list(cases)
    return {
        "cases": len(cases),
        "open": sum(1 for c in cases if c.is_open),
        "recovered": sum(1 for c in cases if c.status is CaseStatus.RECOVERED),
        "stopped": sum(1 for c in cases if c.status is CaseStatus.STOPPED),
        "actions": sum(c.attempts for c in cases),
        "in_flight": sum(1 for c in cases
                         for n in c.notes if n.startswith("in_flight_at_crash:")),
    }
