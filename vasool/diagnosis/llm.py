"""The reasoning zone.

This module is the only place a language model is consulted, and it holds no
credentials for anything that can move money. It produces an ``ActionProposal``
and hands it across the boundary.

Note what it deliberately does *not* do: it does not clamp, sanitise or correct
the model's proposed amount. A proposal to collect ten times the order value is
passed through verbatim, because the Gate is where that gets caught. Fixing it
here would move the defence into the component that can be talked into things,
and would make the injection test pass for the wrong reason.

Degradation is explicit at every step. Malformed output, a timeout, a rate
limit, or an open circuit breaker all end in a deterministic decision tagged
``degraded`` rather than a dropped case.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

from vasool.core.policy import Policy
from vasool.core.types import (
    ActionProposal,
    CaseState,
    Channel,
    Diagnosis,
    FailureClass,
    Intervention,
)
from vasool.diagnosis import fallback
from vasool.diagnosis import prompt as prompt_mod
from vasool.diagnosis.cache import ResponseCache, key_for
from vasool.diagnosis.providers import Provider, RateLimited, resolve
from vasool.diagnosis.schema import Proposal
from vasool.kernel.gate import Review

DEFAULT_EFFORT = os.environ.get("VASOOL_EFFORT", "low")


@dataclass
class DiagnosisStats:
    decisions: int = 0
    api_calls: int = 0
    cache_hits: int = 0
    schema_failures: int = 0
    repairs_attempted: int = 0
    repairs_succeeded: int = 0
    api_errors: int = 0
    breaker_trips: int = 0
    rate_limited: int = 0
    degraded: int = 0
    out_of_taxonomy: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class Diagnoser(Protocol):
    name: str
    stats: DiagnosisStats

    def propose(
        self, case: CaseState, now: datetime,
        customer_reply: Optional[str] = None,
        repair_for: Optional[Review] = None,
    ) -> tuple[ActionProposal, bool]:
        """Return (proposal, degraded)."""
        ...


class RulesDiagnoser:
    """Arm B's brain, and the floor everything else has to clear."""

    name = "rules"

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.stats = DiagnosisStats()

    def propose(
        self, case: CaseState, now: datetime,
        customer_reply: Optional[str] = None,
        repair_for: Optional[Review] = None,
    ) -> tuple[ActionProposal, bool]:
        self.stats.decisions += 1
        diagnosis = fallback.classify(case.event)
        return fallback.plan(diagnosis, case, now, self.policy), False


class CircuitBreaker:
    """Stop hammering a dependency that is already failing.

    The cooldown is deliberately *two* conditions, not one. A wall-clock
    cooldown alone is wrong for batch work: when the breaker opens, every
    subsequent decision falls to the deterministic path and completes
    instantly, so a whole 200-case run can finish inside the 30-second window.
    A brief hiccup then poisons the entire run, and the report shows a model
    arm that never once called the model.

    So it also half-opens after a number of skipped calls, which lets a batch
    job probe and recover on its own terms rather than on the clock's.
    """

    def __init__(self, threshold: int = 6, cooldown_seconds: float = 30.0,
                 probe_after_skips: int = 25) -> None:
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self.probe_after_skips = probe_after_skips
        self.consecutive_failures = 0
        self.opened_at: Optional[float] = None
        self.trips = 0
        self.skipped_while_open = 0
        self.probes = 0

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.cooldown:
            self._close()
            return False
        self.skipped_while_open += 1
        if self.skipped_while_open >= self.probe_after_skips:
            self.skipped_while_open = 0
            self.probes += 1
            return False                 # half-open: let one through
        return True

    def _close(self) -> None:
        self.opened_at = None
        self.consecutive_failures = 0
        self.skipped_while_open = 0

    def record_success(self) -> None:
        self._close()

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold and self.opened_at is None:
            self.opened_at = time.monotonic()
            self.skips_while_open = 0
            self.trips += 1


class LLMDiagnoser:
    """Diagnosis by Claude, with every failure path leading somewhere safe."""

    name = "llm"

    def __init__(
        self,
        policy: Policy,
        provider: Optional[Provider] = None,
        effort: str = DEFAULT_EFFORT,
        cache: Optional[ResponseCache] = None,
        max_repairs: int = 1,
    ) -> None:
        self.policy = policy
        self.provider = provider if provider is not None else resolve()
        self.effort = effort
        self.cache = cache if cache is not None else ResponseCache()
        self.stats = DiagnosisStats()
        self.breaker = CircuitBreaker()
        self.max_repairs = max_repairs
        self.last_error: Optional[str] = None
        self.errors_seen: dict[str, int] = {}

    @property
    def model(self) -> str:
        return getattr(self.provider, "model", "none")

    @property
    def provider_name(self) -> str:
        return getattr(self.provider, "name", "none")

    # -- public -------------------------------------------------------------

    def propose(
        self, case: CaseState, now: datetime,
        customer_reply: Optional[str] = None,
        repair_for: Optional[Review] = None,
    ) -> tuple[ActionProposal, bool]:
        self.stats.decisions += 1
        repair_round = 1 if repair_for is not None else 0
        if repair_for is not None:
            self.stats.repairs_attempted += 1

        raw = self._obtain(case, now, customer_reply, repair_for, repair_round)
        if raw is None:
            self.stats.degraded += 1
            diagnosis = fallback.classify(case.event)
            degraded_diagnosis = Diagnosis(
                failure_class=diagnosis.failure_class,
                confidence=diagnosis.confidence,
                rationale=f"[degraded] {diagnosis.rationale}",
                evidence_fields=diagnosis.evidence_fields,
                source="fallback",
            )
            return (
                fallback.plan(degraded_diagnosis, case, now, self.policy),
                True,
            )

        if repair_for is not None:
            # Counted here, not in _obtain, so a cached repair still registers
            # as an attempt. Otherwise successes outnumber attempts, which is
            # how this shipped a report claiming "531 of 46".
            self.stats.repairs_succeeded += 1
        return self._to_action(raw, case, now), False

    # -- internals ----------------------------------------------------------

    def _obtain(
        self, case: CaseState, now: datetime, customer_reply: Optional[str],
        repair_for: Optional[Review], repair_round: int,
    ) -> Optional[Proposal]:
        digest = case.event.evidence_digest()
        # Case history changes the right answer, so it has to be in the key.
        digest = f"{digest}:{case.attempts}:{len(case.contacts)}"
        if customer_reply:
            digest = f"{digest}:reply{abs(hash(customer_reply)) % 10**8}"
        cache_key = key_for(digest, prompt_mod.PROMPT_VERSION,
                            f"{self.provider_name}:{self.model}",
                            self.effort, repair_round)

        cached = self.cache.get(cache_key)
        if cached is not None:
            self.stats.cache_hits += 1
            try:
                return Proposal.model_validate(cached)
            except Exception:
                self.stats.schema_failures += 1

        if self.breaker.is_open:
            self.stats.breaker_trips += 1
            return None

        if self.provider is None:
            return None

        bundle = prompt_mod.evidence_bundle(case.event, case, now, customer_reply)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": prompt_mod.render(dict(bundle))}
        ]
        messages_repair = repair_for is not None
        if messages_repair:
            messages.append({
                "role": "assistant",
                "content": "I proposed an action that was rejected.",
            })
            messages.append({
                "role": "user",
                "content": prompt_mod.repair_note(
                    [d.value for d in repair_for.verdict.denials],
                    repair_for.verdict.detail,
                ),
            })

        try:
            self.stats.api_calls += 1
            parsed = self.provider.complete(prompt_mod.SYSTEM, messages)
        except RateLimited as limited:
            # A rate limit is NOT a broken dependency. The provider is healthy
            # and telling us we are going too fast. Counting it toward the
            # circuit breaker conflates "it is down" with "you are early", and
            # in a parallel run that mistake is self-reinforcing: throttling
            # opens the breaker, the breaker degrades every case, and the run
            # completes having never spoken to the model.
            self.stats.rate_limited += 1
            self.last_error = f"RateLimited: {limited}"
            self.errors_seen["RateLimited"] = self.errors_seen.get("RateLimited", 0) + 1
            return None
        except Exception as exc:
            # Degrading silently is correct behaviour; degrading *invisibly* is
            # not. Keep the last error so a run can be debugged without
            # re-instrumenting, and count it.
            self.stats.api_errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.errors_seen[type(exc).__name__] = (
                self.errors_seen.get(type(exc).__name__, 0) + 1)
            self.breaker.record_failure()
            return None

        if parsed is None:
            self.stats.schema_failures += 1
            self.breaker.record_failure()
            return None

        self.breaker.record_success()
        self.cache.put(cache_key, parsed.model_dump())
        return parsed

    def _to_action(
        self, raw: Proposal, case: CaseState, now: datetime,
    ) -> ActionProposal:
        """Adapt the schema object into a proposal. No correction happens here.

        The amount, the channel and the intervention are carried across exactly
        as proposed — including when they are wrong. Catching that is the Gate's
        job, and doing it here would defend the system in the component that can
        be argued with.
        """
        try:
            failure_class = FailureClass(raw.failure_class)
        except ValueError:                       # schema makes this unreachable
            self.stats.out_of_taxonomy += 1
            failure_class = FailureClass.UNKNOWN

        diagnosis = Diagnosis(
            failure_class=failure_class,
            confidence=float(raw.confidence),
            rationale=raw.rationale,
            evidence_fields=tuple(raw.evidence_fields[:6]),
            source="llm",
        )
        return ActionProposal(
            case_id=case.case_id,
            decision_ordinal=case.decision_ordinal,
            intervention=Intervention(raw.intervention),
            channel=Channel(raw.channel),
            amount_paise=int(raw.amount_paise),
            currency=case.event.currency,
            scheduled_for=now + timedelta(hours=float(raw.delay_hours)),
            diagnosis=diagnosis,
            rationale=raw.rationale,
        )

    def close(self) -> None:
        self.cache.flush()
