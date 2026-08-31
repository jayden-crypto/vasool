"""Evidence router: spend the model only where it is worth spending.

The classification benchmark produced two facts that sit awkwardly together.
The rules baseline is **perfect** on cases where the reason code states the
cause — and scores **zero** on cases where the code contradicts the issuer's own
message, because a lookup table believes the code. A model reverses both: it
wins the contradictory cases and loses the unambiguous ones by second-guessing
evidence that was never in doubt.

Neither is the right diagnoser. The right diagnoser asks who to believe, and
that question is answerable *without a model*: derive a classification from the
reason code, derive one from the issuer's prose, and compare them.

    agree, or only the code speaks   -> trust the code. No model call.
    they disagree                    -> conflict. Ask the model.
    the code says nothing            -> prose only. Ask the model.

The routing decision is deterministic and reads no ground truth, so it is as
auditable as the kernel. It also cuts model spend by more than half, because
most declines are unambiguous and were never worth a model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from vasool.core.policy import Policy
from vasool.core.types import ActionProposal, CaseState, Diagnosis, FailureEvent
from vasool.diagnosis import fallback
from vasool.diagnosis.llm import Diagnoser, DiagnosisStats
from vasool.kernel.gate import Review

ROUTE_CODE = "code"          # unambiguous; the lookup table is perfect here
ROUTE_CONFLICT = "conflict"  # code and prose disagree; judgment required
ROUTE_PROSE = "prose"        # code says nothing; the cause is in free text
ROUTE_NEITHER = "neither"    # no signal at all


def decide_route(event: FailureEvent) -> str:
    """Which diagnoser should answer this case. Pure, deterministic, no model."""
    from_code = fallback.classify_from_code(event)
    from_message = fallback.classify_from_message(event)

    if from_code is None:
        return ROUTE_PROSE if from_message is not None else ROUTE_NEITHER
    if from_message is None:
        return ROUTE_CODE
    if from_code.failure_class is from_message.failure_class:
        return ROUTE_CODE
    return ROUTE_CONFLICT


@dataclass
class RouteStats:
    code: int = 0
    conflict: int = 0
    prose: int = 0
    neither: int = 0
    model_calls_saved: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


#: Which routes escalate to the model.
#:
#: ``conflict_and_prose`` was the first design: ask the model wherever the code
#: is not decisive. ``conflict_only`` narrows it, because the keyword table is
#: measurably *better* than the model at reading prose (86.2% vs 75.9% on the
#: test split) — the model's advantage is specifically adjudicating between two
#: sources that disagree, not reading text.
ESCALATION_MODES = {
    "conflict_and_prose": {ROUTE_CONFLICT, ROUTE_PROSE, ROUTE_NEITHER},
    "conflict_only": {ROUTE_CONFLICT},
}


class RoutedDiagnoser:
    """Rules where they are strong, the model where they are blind."""

    name = "routed"

    def __init__(self, policy: Policy, model: Diagnoser,
                 mode: str = "conflict_and_prose") -> None:
        self.policy = policy
        self.model = model
        self.mode = mode
        self.escalates = ESCALATION_MODES[mode]
        self.stats = DiagnosisStats()
        self.routes = RouteStats()

    @property
    def errors_seen(self) -> dict[str, int]:
        return getattr(self.model, "errors_seen", {})

    @property
    def last_error(self) -> Optional[str]:
        return getattr(self.model, "last_error", None)

    def propose(
        self, case: CaseState, now: datetime,
        customer_reply: Optional[str] = None,
        repair_for: Optional[Review] = None,
    ) -> tuple[ActionProposal, bool]:
        self.stats.decisions += 1
        route = decide_route(case.event)
        setattr(self.routes, route, getattr(self.routes, route) + 1)

        # A repair is always a judgment call, whatever the evidence looked like.
        if route not in self.escalates and repair_for is None:
            self.routes.model_calls_saved += 1
            diagnosis = fallback.classify(case.event)
            routed = Diagnosis(
                failure_class=diagnosis.failure_class,
                confidence=diagnosis.confidence,
                rationale=f"[routed:{route}] {diagnosis.rationale}",
                evidence_fields=diagnosis.evidence_fields,
                source="rules",
            )
            return fallback.plan(routed, case, now, self.policy), False

        proposal, degraded = self.model.propose(
            case, now, customer_reply=customer_reply, repair_for=repair_for)
        inner = getattr(self.model, "stats", None)
        if inner is not None:
            self.stats.api_calls = inner.api_calls
            self.stats.cache_hits = inner.cache_hits
            self.stats.api_errors = inner.api_errors
            self.stats.schema_failures = inner.schema_failures
        if degraded:
            self.stats.degraded += 1
        return proposal, degraded

    def close(self) -> None:
        if hasattr(self.model, "close"):
            self.model.close()
