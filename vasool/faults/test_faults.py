"""Every fault scenario, as a test.

The demo is for a human; this is for CI. Same scenarios, same assertions, so a
regression in failure handling breaks the build rather than being noticed on
camera.
"""

from __future__ import annotations

import pytest

from vasool.faults import demo


@pytest.mark.parametrize(
    "scenario", demo.SCENARIOS, ids=[s.__name__ for s in demo.SCENARIOS],
)
def test_fault_scenario(scenario):
    result = scenario()
    assert result.passed, (
        f"{result.name}\n  claim: {result.claim}\n  observed: " +
        "\n            ".join(result.observed)
    )
