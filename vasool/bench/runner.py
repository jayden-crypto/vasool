"""The scorer.

Runs one arm over one batch and returns everything needed to judge it,
including the things that went badly.

Accounting rule worth stating plainly: money a customer paid on their own is
never credited to an arm, and money collected on an order that was already
settled is counted as a double charge rather than a recovery. Both rules cost
the agent numbers it could otherwise have claimed, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Optional

from vasool.bench.arms.base import Arm
from vasool.bench.environment import Environment
from vasool.bench.generator import Batch
from vasool.core.policy import Costs, Policy
from vasool.core.types import (
    ActionOutcome,
    CaseState,
    CaseStatus,
    Denial,
    Intervention,
)
from vasool.executor.backend import PaymentsBackend
from vasool.executor.executor import Executor
from vasool.executor.ledger import Ledger
from vasool.kernel.gate import Gate, repairable

MAX_STEPS_PER_CASE = 40

#: Denials that end the case. Re-proposing against these cannot help — they are
#: facts about the world, not defects in the proposal.
TERMINAL_DENIALS = frozenset({
    Denial.ALREADY_COLLECTED,
    Denial.CONSENT_WITHDRAWN,
    Denial.CONTACT_BUDGET_EXCEEDED,
    Denial.ATTEMPT_CAP_REACHED,
    Denial.HORIZON_EXCEEDED,
    Denial.BELOW_STOPPING_THRESHOLD,
    Denial.HARMFUL_RETRY,
    Denial.DUPLICATE_ACTION,
})


@dataclass
class ArmResult:
    arm_key: str
    arm_label: str
    n_cases: int
    at_risk_paise: int

    recovered_paise: int = 0
    recovered_cases: int = 0
    double_collected_paise: int = 0
    double_collected_cases: int = 0
    out_of_band_paise: int = 0
    out_of_band_cases: int = 0

    actions_executed: int = 0
    contacts_made: int = 0
    action_spend_paise: int = 0

    stopped_cases: int = 0
    abandoned_cases: int = 0
    closed_settled_elsewhere: int = 0    # customer paid; we noticed and stopped

    time_to_recovery_hours: list[float] = field(default_factory=list)
    harms: dict[str, int] = field(default_factory=dict)
    harm_cost_paise: int = 0
    denials: dict[str, int] = field(default_factory=dict)

    diagnosis_stats: dict[str, int] = field(default_factory=dict)
    execution_stats: dict[str, int] = field(default_factory=dict)
    classification_correct: int = 0
    classification_total: int = 0
    degraded_decisions: int = 0
    ledger_records: int = 0
    ledger_valid: bool = True

    @property
    def closed_cases(self) -> int:
        """Every case ends in exactly one of these buckets. Nothing runs forever."""
        return (
            self.recovered_cases + self.stopped_cases + self.abandoned_cases
            + self.closed_settled_elsewhere + self.double_collected_cases
        )

    @property
    def recovery_rate(self) -> float:
        return self.recovered_cases / self.n_cases if self.n_cases else 0.0

    @property
    def recovery_value_rate(self) -> float:
        return self.recovered_paise / self.at_risk_paise if self.at_risk_paise else 0.0

    @property
    def net_value_paise(self) -> int:
        """Gross recovery minus what it cost and what it broke."""
        return (
            self.recovered_paise
            - self.action_spend_paise
            - self.harm_cost_paise
            - self.double_collected_paise
        )

    @property
    def paise_per_action(self) -> float:
        return self.recovered_paise / self.actions_executed if self.actions_executed else 0.0

    @property
    def contacts_per_recovery(self) -> float:
        return self.contacts_made / self.recovered_cases if self.recovered_cases else 0.0

    @property
    def median_time_to_recovery(self) -> Optional[float]:
        if not self.time_to_recovery_hours:
            return None
        s = sorted(self.time_to_recovery_hours)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    @property
    def classification_accuracy(self) -> Optional[float]:
        if not self.classification_total:
            return None
        return self.classification_correct / self.classification_total


def run_arm(
    arm: Arm,
    batch: Batch,
    policy: Policy,
    costs: Costs,
    ledger_path: Optional[Path] = None,
    backend_factory=None,
) -> ArmResult:
    """Run one architecture over the whole batch."""
    import copy

    hidden = copy.deepcopy(batch.hidden)
    env = Environment(
        hidden=hidden, physics=batch.physics, policy=policy, costs=costs,
        master_seed=batch.seed, oob_settlements=dict(batch.oob_settlements),
    )
    backend: PaymentsBackend = backend_factory(env) if backend_factory else env

    ledger = Ledger(ledger_path)
    executor = Executor(backend=backend, ledger=ledger, costs=costs)
    gate = Gate(policy, costs, backend.is_settled) if arm.use_gate else None

    result = ArmResult(
        arm_key=arm.key, arm_label=arm.label,
        n_cases=len(batch.events), at_risk_paise=batch.total_at_risk_paise,
    )
    denial_counts: dict[str, int] = {}

    for index, event in enumerate(batch.events):
        case_id = f"case_{index:04d}"
        case = CaseState(case_id=case_id, event=event, opened_at=event.failed_at)
        horizon_end = event.failed_at + timedelta(days=policy.horizon_days)
        now = event.failed_at + timedelta(minutes=5)
        env.clock = now

        truth = hidden[case_id].true_class
        first_diagnosis_seen = False

        for _ in range(MAX_STEPS_PER_CASE):
            if not case.is_open or now > horizon_end:
                break
            env.clock = now

            proposal, degraded = arm.diagnoser.propose(case, now)
            if degraded:
                case.degraded_decisions += 1
                result.degraded_decisions += 1

            if not first_diagnosis_seen and proposal.diagnosis.source != "none":
                first_diagnosis_seen = True
                result.classification_total += 1
                if proposal.diagnosis.failure_class == truth:
                    result.classification_correct += 1

            if proposal.intervention is Intervention.STOP:
                case.status = CaseStatus.STOPPED
                case.closed_at = now
                ledger.append("stopped", case_id,
                              {"reason": proposal.rationale[:200]}, at=now)
                break

            act_at = max(proposal.scheduled_for, now)
            if act_at > horizon_end:
                break

            if proposal.intervention is Intervention.WAIT:
                now = act_at + timedelta(hours=1)
                continue

            env.clock = act_at

            if gate is not None:
                review = gate.review(proposal, case, act_at)
                if not review.allowed:
                    executor.record_denial(proposal, case, review, act_at)
                    for d in review.verdict.denials:
                        denial_counts[d.value] = denial_counts.get(d.value, 0) + 1

                    repaired = False
                    if arm.allow_repair and repairable(review):
                        alt, alt_degraded = arm.diagnoser.propose(
                            case, act_at, repair_for=review)
                        if alt_degraded:
                            result.degraded_decisions += 1
                        if alt.intervention is Intervention.STOP:
                            case.status = CaseStatus.STOPPED
                            case.closed_at = act_at
                            break
                        alt_review = gate.review(alt, case, act_at)
                        if alt_review.allowed:
                            proposal, review, repaired = alt, alt_review, True
                        else:
                            for d in alt_review.verdict.denials:
                                denial_counts[d.value] = denial_counts.get(d.value, 0) + 1
                            executor.record_denial(alt, case, alt_review, act_at)

                    if not repaired:
                        if any(d in TERMINAL_DENIALS for d in review.verdict.denials):
                            if Denial.ALREADY_COLLECTED in review.verdict.denials:
                                # The money arrived without us. Close the case,
                                # take no credit, and — the point of I1 — do not
                                # collect a second time.
                                case.status = CaseStatus.RECOVERED
                                case.notes.append("settled_out_of_band")
                                result.closed_settled_elsewhere += 1
                            else:
                                case.status = CaseStatus.STOPPED
                            case.closed_at = act_at
                            break
                        now = act_at + timedelta(hours=6)
                        continue

            was_settled_before = event.order_id in env.settled
            outcome: ActionOutcome = executor.execute(proposal, case, act_at)

            if outcome.executed:
                result.actions_executed += 1
                result.action_spend_paise += outcome.cost_paise
            if outcome.succeeded and outcome.collected_paise:
                if was_settled_before:
                    # Money arrived twice. That is a refund and an apology, not
                    # a recovery, so it is never credited as one.
                    result.double_collected_paise += outcome.collected_paise
                    result.double_collected_cases += 1
                    case.collected_paise -= outcome.collected_paise
                    case.status = CaseStatus.DOUBLE_CHARGED
                else:
                    result.recovered_paise += outcome.collected_paise
                    result.recovered_cases += 1
                    result.time_to_recovery_hours.append(
                        (act_at - event.failed_at).total_seconds() / 3600.0)
                break

            now = act_at + proposal.next_review_after

        if case.is_open:
            case.status = CaseStatus.ABANDONED
        if case.status is CaseStatus.STOPPED:
            result.stopped_cases += 1
        elif case.status is CaseStatus.ABANDONED:
            result.abandoned_cases += 1
        result.contacts_made += len(case.contacts)

    # Out-of-band settlements are the customer's doing. No arm gets the credit.
    result.out_of_band_cases = len(env.oob_collected)
    result.out_of_band_paise = sum(env.oob_collected.values())

    result.harms = env.harms.as_dict()
    result.harm_cost_paise = env.harms.priced(costs)
    result.denials = denial_counts
    result.diagnosis_stats = arm.diagnoser.stats.as_dict()
    result.execution_stats = executor.stats.__dict__.copy()
    result.ledger_records = len(ledger)
    result.ledger_valid = ledger.verify()[0]
    ledger.close()
    return result
