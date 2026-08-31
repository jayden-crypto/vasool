"""Arm definitions.

Five architectures over one environment. Everything except the architecture is
held constant — same batch, same seeds, same model, same effort, same prompt,
same costs. The only thing that varies between arms is the thing being
measured.

    A  cron        fixed retry schedule. no diagnosis, no kernel.
    B  rules       deterministic taxonomy -> intervention. no kernel.
    C  raw-agent   the model proposes and the executor obeys. no kernel.
    D  vasool      the model proposes, the kernel decides.
    E  rules+gate  ablation: kernel without the model.

The pairs that matter:
    B vs E and C vs D  isolate the kernel.
    B vs C and E vs D  isolate the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from vasool.core.policy import Policy
from vasool.core.types import (
    ActionProposal,
    CaseState,
    Channel,
    Diagnosis,
    FailureClass,
    Intervention,
)
from vasool.diagnosis.llm import Diagnoser, DiagnosisStats
from vasool.kernel.gate import Review

#: What arm A believes about every failure, which is nothing.
NO_DIAGNOSIS = Diagnosis(
    failure_class=FailureClass.UNKNOWN,
    confidence=0.0,
    rationale="fixed retry schedule; cause not considered",
    evidence_fields=(),
    source="none",
)


class CronDiagnoser:
    """Arm A. The status quo in most merchant stacks: retry on a timer.

    T+24h, T+72h, T+120h, then give up. It does not read the error, so it
    retries expired cards three times and chases customers who already paid.
    That is not a strawman — it is what a `retry_failed_payments` cron does.
    """

    name = "cron"
    SCHEDULE_HOURS = (24, 72, 120)

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.stats = DiagnosisStats()

    def propose(
        self, case: CaseState, now: datetime,
        customer_reply: Optional[str] = None,
        repair_for: Optional[Review] = None,
    ) -> tuple[ActionProposal, bool]:
        self.stats.decisions += 1
        step = case.attempts
        if step >= len(self.SCHEDULE_HOURS):
            return (
                ActionProposal(
                    case_id=case.case_id,
            decision_ordinal=case.decision_ordinal, intervention=Intervention.STOP,
                    channel=Channel.NONE, amount_paise=0,
                    currency=case.event.currency, scheduled_for=now,
                    diagnosis=NO_DIAGNOSIS, rationale="retry schedule exhausted",
                ),
                False,
            )
        delay = timedelta(hours=self.SCHEDULE_HOURS[step]) - (now - case.event.failed_at)
        if delay.total_seconds() < 0:
            delay = timedelta(0)
        return (
            ActionProposal(
                case_id=case.case_id,
            decision_ordinal=case.decision_ordinal,
                intervention=Intervention.RETRY_SAME_RAIL,
                channel=Channel.NONE,
                amount_paise=case.event.amount_paise,
                currency=case.event.currency,
                scheduled_for=now + delay,
                diagnosis=NO_DIAGNOSIS,
                rationale=f"scheduled retry #{step + 1}",
            ),
            False,
        )


@dataclass(frozen=True)
class Arm:
    key: str
    label: str
    diagnoser: Diagnoser
    use_gate: bool
    allow_repair: bool
    description: str
