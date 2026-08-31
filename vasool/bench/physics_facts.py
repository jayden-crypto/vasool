"""Ground truth the environment derives for itself.

The environment grades the arms, so it must not share code with the thing it is
grading. It previously imported ``in_quiet_hours`` from the kernel and
``raw_evidence.read`` from the kernel's own evidence reader, which meant four of
six measured harms restated the kernel's rules rather than measuring anything.
An arm scoring zero on those rows proved the check had been written, not that it
worked.

So these are deliberate re-implementations, written from the *hidden* state and
from first principles rather than from the kernel's reading of observable
evidence. Where the kernel asks "what does the error code say", this asks "what
is actually true" — which is the whole point, because they can differ, and a
world where they never differ cannot test anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from vasool.bench.hidden import HiddenState
from vasool.core.types import FailureClass

#: Local hours during which contacting someone is antisocial. Stated here as a
#: fact about people, not read from policy.yaml — if an operator widened the
#: policy window, the harm should not silently widen with it.
QUIET_FROM_LOCAL_HOUR = 21
QUIET_UNTIL_LOCAL_HOUR = 9


def is_antisocial_hour(now: datetime, tz_offset_minutes: int) -> bool:
    """Would this message wake someone up? Derived independently of I4."""
    local_hour = (now + timedelta(minutes=tz_offset_minutes)).hour
    return local_hour >= QUIET_FROM_LOCAL_HOUR or local_hour < QUIET_UNTIL_LOCAL_HOUR


def replay_is_futile(hidden: HiddenState) -> bool:
    """Would replaying the stored instrument actually fail, in truth?

    Note what this reads: ``HiddenState``. The kernel decides futility from the
    provider's error code, which is a *claim* about the world. This is the
    world. When the generator emits a misleading code the two disagree, and that
    disagreement is exactly what the harm ledger should be able to see.
    """
    if hidden.instrument_refreshed or hidden.mandate_restored:
        return False
    if hidden.true_class in (FailureClass.INSTRUMENT_DEAD, FailureClass.RESTRICTION):
        return not hidden.instrument_alive
    if hidden.true_class is FailureClass.MANDATE_INVALID:
        return True
    return False


def replay_is_harmful(hidden: HiddenState) -> bool:
    """Is this truly a risk decline, whatever the code claimed?"""
    return hidden.true_class is FailureClass.RISK_DECLINED
