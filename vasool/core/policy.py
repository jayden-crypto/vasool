"""Policy and cost configuration, loaded from committed YAML.

Kept deliberately dumb: this module reads files and validates ranges. All
enforcement lives in vasool/kernel/invariants.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vasool.core.types import Channel, Intervention

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@dataclass(frozen=True)
class Policy:
    max_contacts_per_window: int
    contact_window_hours: int
    min_hours_between_contacts: int
    quiet_hours_start_local: int
    quiet_hours_end_local: int
    permitted_channels: frozenset[Channel]

    max_money_actions: int
    max_auto_retries: int
    horizon_days: int

    allow_part_payment: bool
    absolute_part_payment_floor_ratio: float
    permitted_currencies: frozenset[str]

    min_ev_to_cost_ratio: float
    min_success_probability: float

    @staticmethod
    def load(path: Path | None = None) -> "Policy":
        path = path or (CONFIG_DIR / "policy.yaml")
        raw: dict[str, Any] = yaml.safe_load(path.read_text())
        c, a, m, s = raw["contact"], raw["attempts"], raw["amounts"], raw["stopping"]
        policy = Policy(
            max_contacts_per_window=int(c["max_contacts_per_window"]),
            contact_window_hours=int(c["contact_window_hours"]),
            min_hours_between_contacts=int(c["min_hours_between_contacts"]),
            quiet_hours_start_local=int(c["quiet_hours_start_local"]),
            quiet_hours_end_local=int(c["quiet_hours_end_local"]),
            permitted_channels=frozenset(Channel(x) for x in c["permitted_channels"]),
            max_money_actions=int(a["max_money_actions"]),
            max_auto_retries=int(a["max_auto_retries"]),
            horizon_days=int(a["horizon_days"]),
            allow_part_payment=bool(m["allow_part_payment"]),
            absolute_part_payment_floor_ratio=float(m["absolute_part_payment_floor_ratio"]),
            permitted_currencies=frozenset(m["permitted_currencies"]),
            min_ev_to_cost_ratio=float(s["min_ev_to_cost_ratio"]),
            min_success_probability=float(s["min_success_probability"]),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if not 0 < self.absolute_part_payment_floor_ratio <= 1:
            raise ValueError("part payment floor must be in (0, 1]")
        if not 0 <= self.quiet_hours_start_local <= 23:
            raise ValueError("quiet hours start out of range")
        if not 0 <= self.quiet_hours_end_local <= 23:
            raise ValueError("quiet hours end out of range")
        if self.max_money_actions < 1:
            raise ValueError("max_money_actions must be >= 1")
        if self.min_ev_to_cost_ratio <= 0:
            raise ValueError("min_ev_to_cost_ratio must be positive")


@dataclass(frozen=True)
class Costs:
    action_cost_paise: dict[str, int]
    channel_cost_paise: dict[str, int]
    harm_cost_paise: dict[str, int]

    @staticmethod
    def load(path: Path | None = None) -> "Costs":
        path = path or (CONFIG_DIR / "costs.yaml")
        raw = yaml.safe_load(path.read_text())
        return Costs(
            action_cost_paise=dict(raw["action_cost_paise"]),
            channel_cost_paise=dict(raw["channel_cost_paise"]),
            harm_cost_paise=dict(raw["harm_cost_paise"]),
        )

    def cost_of(self, intervention: Intervention, channel: Channel) -> int:
        return (
            self.action_cost_paise.get(intervention.value, 0)
            + self.channel_cost_paise.get(channel.value, 0)
        )

    def harm(self, name: str) -> int:
        return self.harm_cost_paise.get(name, 0)
