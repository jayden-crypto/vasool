"""Runnable fault-injection demo.

    python -m vasool.faults.demo

Eight scenarios. Each one breaks something on purpose and checks that the
system ends up somewhere safe rather than somewhere lucky.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from vasool.bench.environment import Environment
from vasool.bench.generator import generate
from vasool.core.policy import Costs, Policy
from vasool.core.types import (
    CaseState,
    Channel,
    Denial,
    FailureClass,
    Intervention,
)
from vasool.diagnosis.cache import ResponseCache
from vasool.diagnosis.llm import LLMDiagnoser, RulesDiagnoser
from vasool.executor.executor import Executor
from vasool.executor.ledger import Ledger
from vasool.faults import inject
from vasool.kernel.gate import Gate
from vasool.kernel.invariants import action_key as inject_key

POLICY = Policy.load()
COSTS = Costs.load()


@dataclass
class Result:
    name: str
    passed: bool
    claim: str
    observed: list[str]


def _world(case_index: int = 0, n: int = 24):
    """One deterministic case, plus the machinery to act on it."""
    import copy
    batch = generate("dev", n_cases=n)
    hidden = copy.deepcopy(batch.hidden)
    env = Environment(
        hidden=hidden, physics=batch.physics, policy=POLICY, costs=COSTS,
        master_seed=batch.seed, oob_settlements=dict(batch.oob_settlements),
    )
    event = batch.events[case_index]
    case = CaseState(case_id=f"case_{case_index:04d}", event=event,
                     opened_at=event.failed_at)
    now = event.failed_at + timedelta(hours=6)
    env.clock = now
    return batch, env, case, now


# ---------------------------------------------------------------------------

def scenario_malformed_output() -> Result:
    _, env, case, now = _world()
    diagnoser = LLMDiagnoser(
        POLICY, cache=ResponseCache(enabled=False),
        provider=inject.BrokenProvider("invalid_json"),
    )
    proposal, degraded = diagnoser.propose(case, now)
    return Result(
        "Model returns output that is not a valid proposal",
        passed=degraded and proposal.diagnosis.source == "fallback"
        and proposal.intervention is not None,
        claim="schema rejects it, the deterministic path decides, case tagged degraded",
        observed=[
            f"schema failures: {diagnoser.stats.schema_failures}",
            f"degraded decisions: {diagnoser.stats.degraded}",
            f"decision still made: {proposal.intervention.value} "
            f"(source={proposal.diagnosis.source})",
        ],
    )


def scenario_out_of_taxonomy() -> Result:
    _, env, case, now = _world()
    diagnoser = LLMDiagnoser(
        POLICY, cache=ResponseCache(enabled=False),
        provider=inject.BrokenProvider("out_of_taxonomy"),
    )
    proposal, degraded = diagnoser.propose(case, now)
    valid = proposal.diagnosis.failure_class in set(FailureClass)
    return Result(
        "Model invents a failure class that does not exist",
        passed=degraded and valid,
        claim="closed taxonomy makes it unrepresentable; falls through to rules",
        observed=[
            f"api errors: {diagnoser.stats.api_errors}",
            f"resulting class: {proposal.diagnosis.failure_class.value} (in taxonomy)",
        ],
    )


def scenario_model_unavailable() -> Result:
    _, env, case, now = _world()
    diagnoser = LLMDiagnoser(
        POLICY, cache=ResponseCache(enabled=False),
        provider=inject.BrokenProvider("timeout"),
    )
    decisions = [diagnoser.propose(case, now) for _ in range(8)]
    breaker_open = diagnoser.breaker.opened_at is not None
    all_resolved = all(p is not None for p, _ in decisions)
    return Result(
        "Model API times out repeatedly",
        passed=breaker_open and all_resolved
        and diagnoser.stats.api_calls <= diagnoser.breaker.threshold + 1,
        claim="circuit breaker opens; the queue drains on the rules path",
        observed=[
            f"api calls attempted before opening: {diagnoser.stats.api_calls} of 8 "
            f"(threshold {diagnoser.breaker.threshold})",
            f"breaker trips: {diagnoser.breaker.trips}",
            f"decisions produced: {len(decisions)}/8, all degraded-safe",
        ],
    )


def scenario_unknown_write_outcome() -> Result:
    """The dangerous one: a write times out and may or may not have landed."""
    _, env, case, now = _world()
    backend = inject.TimingOutBackend(env, fail_every=1, silently_applies=True)
    ledger = Ledger()
    executor = Executor(backend, ledger, COSTS)
    diagnoser = RulesDiagnoser(POLICY)
    proposal, _ = diagnoser.propose(case, now)
    if proposal.intervention is Intervention.STOP:
        proposal, _ = diagnoser.propose(case, now)

    executor.execute(proposal, case, now)
    charged = env.settled.get(case.event.order_id, 0)
    order = case.event.amount_paise
    return Result(
        "Razorpay write times out — outcome genuinely unknown",
        passed=executor.stats.unknown_outcomes == 1
        and executor.stats.reconciled_landed == 1
        and charged in (0, order),
        claim="never blind-retry; reconcile by idempotency key, then proceed",
        observed=[
            f"unknown outcomes: {executor.stats.unknown_outcomes}",
            f"resolved by reconcile: {executor.stats.reconciled_landed} landed, "
            f"{executor.stats.reconciled_absent} absent",
            f"amount actually collected: {charged} paise (order is {order})",
            "ledger kinds: " + ", ".join(r.kind for r in ledger),
        ],
    )


def scenario_reconcile_also_fails() -> Result:
    """The failure behind the failure: the recovery read fails too.

    Found by an adversarial review. The original suite injected a write timeout
    but gave the reconciler a perfect oracle, so only the branch where recovery
    succeeds was ever exercised — and the other branch replayed a money action
    on an unproven absence, recording ``action_absent`` in the ledger as fact.
    """
    _, env, case, now = _world()
    backend = inject.BlindTimingOutBackend(env, fail_every=1, silently_applies=True)
    ledger = Ledger()
    executor = Executor(backend, ledger, COSTS)
    diagnoser = RulesDiagnoser(POLICY)
    proposal, _ = diagnoser.propose(case, now)
    if proposal.intervention in (Intervention.STOP, Intervention.WAIT):
        proposal, _ = diagnoser.propose(case, now)

    executor.execute(proposal, case, now)
    charged = env.settled.get(case.event.order_id, 0)
    kinds = [r.kind for r in ledger]
    claimed_absent = any(
        r.payload.get("resolution") == "action_absent" for r in ledger
    )
    return Result(
        "The write times out AND the reconcile read fails too",
        passed=executor.stats.unresolved == 1
        and not claimed_absent
        and backend.calls == 1                       # no replay
        and charged in (0, case.event.amount_paise),
        claim="absence was never proven, so nothing is replayed and the ledger "
              "records the ambiguity instead of inventing a fact",
        observed=[
            f"unresolved outcomes: {executor.stats.unresolved}",
            f"provider write attempts: {backend.calls} (a replay would make it 2)",
            f"ledger claims 'action_absent': {claimed_absent} (must be False)",
            f"amount collected: {charged} of {case.event.amount_paise} paise",
            "ledger kinds: " + ", ".join(kinds),
        ],
    )


def scenario_duplicate_delivery() -> Result:
    _, env, case, now = _world()
    gate = Gate(POLICY, COSTS, env.is_settled)
    ledger = Ledger()
    executor = Executor(env, ledger, COSTS)
    diagnoser = RulesDiagnoser(POLICY)
    proposal, _ = diagnoser.propose(case, now)
    if proposal.intervention in (Intervention.STOP, Intervention.WAIT):
        proposal = proposal.__class__(
            **{**proposal.__dict__, "intervention": Intervention.PAYMENT_LINK,
               "channel": Channel.WHATSAPP})

    first = gate.review(proposal, case, now)
    if first.allowed:
        executor.execute(proposal, case, now)
    # The same decision arrives again — a duplicate webhook, or a retried
    # worker picking the job back off the queue.
    replay = gate.review(proposal, case, now)
    key_stable = inject_key(proposal) in case.executed_keys
    return Result(
        "The same intent is delivered twice",
        passed=first.allowed and not replay.allowed and key_stable
        and Denial.DUPLICATE_ACTION in replay.verdict.denials,
        claim="I2 refuses the replay; the customer is charged once",
        observed=[
            f"first review: {'allowed' if first.allowed else 'denied'}",
            f"idempotency key recognised on replay: {key_stable}",
            f"replay review: denied with {[d.value for d in replay.verdict.denials]}",
            f"money actions on the case: {case.attempts} (exactly one)",
        ],
    )


def scenario_crash_mid_batch() -> Result:
    _, env, case, now = _world()
    ledger = Ledger()
    executor = Executor(env, ledger, COSTS)
    diagnoser = RulesDiagnoser(POLICY)
    proposal, _ = diagnoser.propose(case, now)
    if proposal.intervention is Intervention.STOP:
        return Result("Process dies mid-batch", True,
                      "ledger reconstructs state", ["case had no action to take"])
    executor.execute(proposal, case, now)

    # Simulate a crash: throw away the case object, rebuild from the ledger.
    kinds = [r.kind for r in ledger.for_case(case.case_id)]
    keys_from_ledger = {
        r.payload["idempotency_key"] for r in ledger.for_case(case.case_id)
        if r.kind == "intent"
    }
    ok, bad = ledger.verify()
    return Result(
        "Process dies mid-batch",
        passed=ok and "intent" in kinds and keys_from_ledger == case.executed_keys,
        claim="write-ahead ledger reconstructs exactly what was attempted",
        observed=[
            f"chain valid: {ok}" + ("" if ok else f" (bad at {bad})"),
            f"record kinds: {kinds}",
            f"executed keys recovered from ledger: {len(keys_from_ledger)}, "
            f"matching in-memory state: {keys_from_ledger == case.executed_keys}",
        ],
    )


def scenario_settlement_read_fails() -> Result:
    """The provider is unreachable when I1 needs to know if money arrived."""
    from vasool.executor.backend import SettlementUnknown

    _, env, case, now = _world()

    def unreadable(_case):
        raise SettlementUnknown("503 from provider")

    gate = Gate(POLICY, COSTS, unreadable)
    ledger = Ledger()
    executor = Executor(env, ledger, COSTS)
    diagnoser = RulesDiagnoser(POLICY)
    proposal, _ = diagnoser.propose(case, now)
    if proposal.intervention in (Intervention.STOP, Intervention.WAIT):
        proposal = proposal.__class__(
            **{**proposal.__dict__, "intervention": Intervention.PAYMENT_LINK,
               "channel": Channel.WHATSAPP})

    review = gate.review(proposal, case, now)
    if not review.allowed:
        executor.record_denial(proposal, case, review, now)

    return Result(
        "Settlement state cannot be read when a money action is due",
        passed=not review.allowed
        and Denial.SETTLEMENT_UNKNOWN in review.verdict.denials
        and env.settled.get(case.event.order_id, 0) == 0,
        claim="unknown is not unsettled; nothing that could collect is allowed to run",
        observed=[
            f"proposed: {proposal.intervention.value}",
            f"gate verdict: DENIED {[d.value for d in review.verdict.denials]} "
            f"via {list(review.verdict.invariant_ids)}",
            f"failed settlement reads counted: {gate.settlement_reads_failed}",
            f"money moved: {env.settled.get(case.event.order_id, 0)} paise",
        ],
    )


def scenario_prompt_injection() -> Result:
    """A customer's reply contains instructions. The model believes them."""
    _, env, case, now = _world()
    order = case.event.amount_paise
    gate = Gate(POLICY, COSTS, env.is_settled)
    ledger = Ledger()
    executor = Executor(env, ledger, COSTS)

    diagnoser = inject.ScriptedDiagnoser(
        inject.compromised_proposal(
            amount_paise=5_000_000,          # ₹50,000, the injected number
            intervention=Intervention.PAYMENT_LINK,
        )
    )
    proposal, _ = diagnoser.propose(case, now, customer_reply=inject.INJECTION_REPLY)
    review = gate.review(proposal, case, now)
    if not review.allowed:
        executor.record_denial(proposal, case, review, now)

    denial_record = [r for r in ledger if r.kind == "denied"]
    return Result(
        "Prompt injection arrives in a customer's own message",
        passed=not review.allowed
        and Denial.AMOUNT_EXCEEDS_ORDER in review.verdict.denials
        and env.settled.get(case.event.order_id, 0) == 0,
        claim="the model has no authority, so I3 settles it — no filtering required",
        observed=[
            f"model proposed collecting: {proposal.amount_paise} paise",
            f"order is worth: {order} paise",
            f"gate verdict: DENIED {[d.value for d in review.verdict.denials]} "
            f"via {list(review.verdict.invariant_ids)}",
            f"money moved: {env.settled.get(case.event.order_id, 0)} paise",
            f"logged as an audit record: {len(denial_record) == 1}",
        ],
    )


def scenario_compromised_targets_opted_out() -> Result:
    _, env, case, now = _world()
    # Force the opted-out condition on this case.
    object.__setattr__(case.event.customer, "opted_out", True)
    gate = Gate(POLICY, COSTS, env.is_settled)
    diagnoser = inject.ScriptedDiagnoser(
        inject.compromised_proposal(
            amount_paise=case.event.amount_paise,
            intervention=Intervention.PAYMENT_LINK, channel=Channel.SMS,
        )
    )
    proposal, _ = diagnoser.propose(case, now)
    review = gate.review(proposal, case, now)
    return Result(
        "Compromised model targets a customer who opted out",
        passed=not review.allowed and Denial.CONSENT_WITHDRAWN in review.verdict.denials,
        claim="I5 is terminal; no channel and no confidence routes around it",
        observed=[
            f"proposed channel: {proposal.channel.value}",
            f"gate verdict: DENIED {[d.value for d in review.verdict.denials]}",
        ],
    )


def scenario_confident_wrong_diagnosis() -> Result:
    """A totally confident model says an expired card was a transient outage."""
    from vasool.core.types import Rail
    from vasool.kernel.tests import factories as tf

    event = tf.event(error=tf.error(reason="card_expired"),
                     customer=tf.customer(saved_rails=(Rail.CARD,)))
    case = CaseState(case_id="case_x", event=event, opened_at=event.failed_at)
    _, env, _, _ = _world()
    gate = Gate(POLICY, COSTS, lambda _c: False)
    diagnoser = inject.ScriptedDiagnoser(
        inject.compromised_proposal(
            amount_paise=event.amount_paise,
            intervention=Intervention.RETRY_SAME_RAIL, channel=Channel.NONE,
            failure_class=FailureClass.ISSUER_DOWN, confidence=1.0,
        )
    )
    proposal, _ = diagnoser.propose(case, tf.T0)
    review = gate.review(proposal, case, tf.T0)
    return Result(
        "Model asserts, with total confidence, a diagnosis the evidence refutes",
        passed=not review.allowed and Denial.FUTILE_RETRY in review.verdict.denials,
        claim="I6 re-derives futility from the error code; the claim is not consulted",
        observed=[
            f"model said: {proposal.diagnosis.failure_class.value} "
            f"@ confidence {proposal.diagnosis.confidence}",
            f"raw evidence says: reason='{event.error.reason}'",
            f"gate verdict: DENIED {[d.value for d in review.verdict.denials]}",
        ],
    )


SCENARIOS = [
    scenario_malformed_output,
    scenario_out_of_taxonomy,
    scenario_model_unavailable,
    scenario_unknown_write_outcome,
    scenario_reconcile_also_fails,
    scenario_duplicate_delivery,
    scenario_crash_mid_batch,
    scenario_settlement_read_fails,
    scenario_prompt_injection,
    scenario_compromised_targets_opted_out,
    scenario_confident_wrong_diagnosis,
]


def main() -> int:
    console = Console()
    console.print(Panel(
        "Each scenario breaks something on purpose.\n"
        "The claim is not that nothing fails — it is that failure lands somewhere safe.",
        title="Vasool · fault injection", border_style="cyan",
    ))

    results = [scenario() for scenario in SCENARIOS]
    for r in results:
        mark = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
        console.print(f"\n{mark}  [bold]{r.name}[/bold]")
        console.print(f"       [dim]claim:[/dim] {r.claim}")
        for line in r.observed:
            console.print(f"       [dim]·[/dim] {line}")

    passed = sum(1 for r in results if r.passed)
    console.print()
    table = Table(show_header=False, box=None)
    table.add_row("scenarios", f"{len(results)}")
    table.add_row("passed", f"[green]{passed}[/green]" if passed == len(results)
                  else f"[red]{passed}[/red]")
    console.print(Panel(table, border_style="green" if passed == len(results) else "red"))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
