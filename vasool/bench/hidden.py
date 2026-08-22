"""Hidden ground truth and the success model.

Nothing in this module is visible to any arm. Arms see a ``FailureEvent`` and
their own case history; the simulator sees why the payment really died and what
would actually make the customer pay.

Two properties keep the comparison fair:

* **Common random numbers.** Every stochastic draw is derived from
  ``(master_seed, case_id, attempt_index, kind)``. Two arms taking the same
  action on the same case at the same attempt index get the same roll, so the
  measured gap between arms is a difference in policy and not in luck.
* **Arm-independent physics.** The success model is a function of hidden state
  and the action. It has no knowledge of which arm is asking.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from vasool.core.types import Channel, FailureClass


def uniform01(master_seed: int, case_id: str, attempt: int, kind: str) -> float:
    """Deterministic uniform draw. The variance-reduction workhorse."""
    blob = f"{master_seed}:{case_id}:{attempt}:{kind}".encode()
    digest = hashlib.sha256(blob).digest()
    (value,) = struct.unpack(">Q", digest[:8])
    return value / float(1 << 64)


@dataclass
class HiddenState:
    """What is true about a case, and what no arm is allowed to see."""
    true_class: FailureClass
    instrument_alive: bool
    alt_rail_works: bool
    reachable: bool
    patience: int
    intent_half_life_hours: float
    responsiveness: dict[Channel, float]
    funds_return_at: Optional[datetime]     # None => liquidity never returns
    outage_ends_at: Optional[datetime]
    max_part_ratio: float
    opted_out: bool

    # Mutable progress, driven by what an arm actually does.
    customer_lost: bool = False
    contacts_made: int = 0
    instrument_refreshed: bool = False
    mandate_restored: bool = False

    def intent(self, failed_at: datetime, now: datetime) -> float:
        """Willingness to pay, decaying from the moment of failure."""
        hours = max(0.0, (now - failed_at).total_seconds() / 3600.0)
        return 0.5 ** (hours / self.intent_half_life_hours)

    def funds_ok(self, now: datetime) -> bool:
        if self.funds_return_at is None:
            return self.true_class is not FailureClass.INSUFFICIENT_FUNDS
        return now >= self.funds_return_at

    def outage_cleared(self, now: datetime) -> bool:
        return self.outage_ends_at is None or now >= self.outage_ends_at


@dataclass
class Physics:
    """Tunable constants, loaded straight from generator.yaml."""
    retry_success_when_clear: float
    retry_success_when_blocked: float
    part_payment_funds_bonus: float
    instrument_refresh_uptake: float
    handoff_success: float
