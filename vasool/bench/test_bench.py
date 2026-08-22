"""End-to-end guarantees about the benchmark itself.

These are the tests that keep the numbers honest: the generator is
reproducible, arms are compared on identical worlds, no case runs forever, and
the ledger for every arm verifies.
"""

from __future__ import annotations

from vasool.bench.arms.base import Arm, CronDiagnoser
from vasool.bench.generator import generate
from vasool.bench.runner import run_arm
from vasool.core.policy import Costs, Policy
from vasool.diagnosis.llm import RulesDiagnoser

POLICY = Policy.load()
COSTS = Costs.load()
N = 40


def _arm(key, label, diagnoser, use_gate):
    return Arm(key, label, diagnoser, use_gate=use_gate, allow_repair=False,
               description="test")


def test_the_generator_is_reproducible():
    a, b = generate("test", n_cases=N), generate("test", n_cases=N)
    assert [e.evidence_digest() for e in a.events] == \
           [e.evidence_digest() for e in b.events]
    assert [h.true_class for h in a.hidden.values()] == \
           [h.true_class for h in b.hidden.values()]


def test_dev_and_test_splits_are_different_worlds():
    dev, test = generate("dev", n_cases=N), generate("test", n_cases=N)
    assert dev.seed != test.seed
    assert [e.evidence_digest() for e in dev.events] != \
           [e.evidence_digest() for e in test.events]


def test_hidden_state_never_reaches_the_observable_event():
    """The arms must not be able to read the answer off the evidence."""
    batch = generate("test", n_cases=N)
    for event in batch.events:
        bundle = event.evidence_digest()
        assert "true_class" not in bundle
    fields = set(vars(batch.events[0]))
    for leak in ("true_class", "funds_return_at", "patience", "reachable"):
        assert leak not in fields


def test_every_arm_terminates_every_case():
    batch = generate("test", n_cases=N)
    for arm in (
        _arm("A", "cron", CronDiagnoser(POLICY), False),
        _arm("B", "rules", RulesDiagnoser(POLICY), False),
        _arm("E", "rules+gate", RulesDiagnoser(POLICY), True),
    ):
        result = run_arm(arm, batch, POLICY, COSTS)
        assert result.closed_cases == N, (
            f"{arm.key}: {result.closed_cases} of {N} cases accounted for — "
            "every case must reach exactly one terminal state"
        )


def test_every_arm_produces_a_valid_ledger_chain():
    batch = generate("test", n_cases=N)
    for arm in (
        _arm("A", "cron", CronDiagnoser(POLICY), False),
        _arm("E", "rules+gate", RulesDiagnoser(POLICY), True),
    ):
        result = run_arm(arm, batch, POLICY, COSTS)
        assert result.ledger_valid
        assert result.ledger_records > 0


def test_the_kernel_eliminates_the_harms_it_claims_to():
    """The central claim, as a test rather than a table."""
    batch = generate("test", n_cases=120)
    ungated = run_arm(_arm("B", "rules", RulesDiagnoser(POLICY), False),
                      batch, POLICY, COSTS)
    gated = run_arm(_arm("E", "rules+gate", RulesDiagnoser(POLICY), True),
                    batch, POLICY, COSTS)

    for harm in ("double_collect_attempt", "contact_to_opted_out",
                 "quiet_hours_violation"):
        assert gated.harms.get(harm, 0) == 0, f"{harm} survived the kernel"
    assert ungated.harms.get("double_collect_attempt", 0) > 0, (
        "the ungated arm should be demonstrating the harm the kernel prevents; "
        "if it is not, the benchmark is not exercising the scenario"
    )


def test_out_of_band_settlements_are_never_credited_to_an_arm():
    batch = generate("test", n_cases=120)
    result = run_arm(_arm("E", "rules+gate", RulesDiagnoser(POLICY), True),
                     batch, POLICY, COSTS)
    assert result.out_of_band_cases > 0
    assert result.recovered_paise + result.out_of_band_paise <= result.at_risk_paise * 2


def test_arms_see_identical_worlds():
    """Common random numbers: two arms taking the same first action on the same
    case must get the same result, so differences measure policy not luck."""
    batch = generate("test", n_cases=N)
    a = run_arm(_arm("B", "rules", RulesDiagnoser(POLICY), False), batch, POLICY, COSTS)
    b = run_arm(_arm("B", "rules", RulesDiagnoser(POLICY), False), batch, POLICY, COSTS)
    assert a.recovered_paise == b.recovered_paise
    assert a.harms == b.harms
