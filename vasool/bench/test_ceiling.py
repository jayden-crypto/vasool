"""The ceiling has to be real, or reporting accuracy against it is worse than
reporting it against 100%."""

from __future__ import annotations

from vasool.bench.ceiling import ambiguous_messages, analyse
from vasool.bench.generator import generate
from vasool.diagnosis.fallback import classify


def test_the_bank_contains_a_genuinely_ambiguous_message():
    """'do not honour' is used for both a thin balance and a risk decline.

    If this ever becomes unambiguous the benchmark has lost the one case where
    the correct answer is to say UNKNOWN.
    """
    ambiguous = ambiguous_messages()
    assert "do not honour" in ambiguous
    assert len(ambiguous["do not honour"]) >= 2


def test_the_ceiling_is_below_one_and_above_the_baseline():
    result = analyse("test", n=500)
    assert 0.90 < result["ceiling"] < 1.0
    assert result["undecidable"] > 0


def test_no_classifier_in_the_repo_exceeds_the_ceiling():
    """A measured accuracy above the stated ceiling would mean the ceiling is
    wrong, and every accuracy figure in the README with it."""
    batch = generate("test", n_cases=500)
    correct = sum(
        1 for event, hidden in zip(batch.events, batch.hidden.values())
        if classify(event).failure_class == hidden.true_class
    )
    accuracy = correct / len(batch.events)
    assert accuracy <= analyse("test", n=500)["ceiling"] + 1e-9


def test_the_headroom_is_concentrated_in_the_contradictory_cases():
    """The model's job is specifically the cases where the code lies.

    If the rules baseline started failing somewhere else, the argument for
    having a model at all would need rewriting.
    """
    result = analyse("test", n=500)
    batch = generate("test", n_cases=500)
    misses = sum(
        1 for event, hidden in zip(batch.events, batch.hidden.values())
        if classify(event).failure_class != hidden.true_class
    )
    contradictions = result["contradictions"]
    # Nearly every miss should be a contradiction or an undecidable case.
    assert misses <= contradictions + result["undecidable"] + 5
