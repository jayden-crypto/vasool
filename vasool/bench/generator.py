"""Batch generator.

Builds a reproducible set of failed payments and their hidden ground truth from
``config/generator.yaml``. Two disjoint splits come out of the same config:

* ``dev``  — what you iterate against.
* ``test`` — a held-out seed, touched once, for the numbers you report.

Nothing about an arm is visible here. The generator does not know which
architectures will be run against the batch it produces.
"""

from __future__ import annotations

import math
import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from vasool.bench import issuer_messages as im
from vasool.bench.hidden import HiddenState, Physics
from vasool.core.types import (
    Channel,
    CustomerProfile,
    FailureClass,
    FailureEvent,
    MerchantConfig,
    Rail,
    RazorpayError,
)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "generator.yaml"
EPOCH = datetime(2026, 8, 3, 4, 30)   # a Monday, 10:00 IST

_ID_ALPHABET = string.ascii_letters + string.digits

MERCHANTS = [
    MerchantConfig("mrch_LKj4Yz1QpR8vTa", "Kettle & Co", "food_delivery", True, 0.40,
                   (Channel.WHATSAPP, Channel.SMS)),
    MerchantConfig("mrch_9QwErTy2UiOpAs", "Nimbus Learning", "education", True, 0.30,
                   (Channel.EMAIL, Channel.WHATSAPP)),
    MerchantConfig("mrch_Zx3CvBnM4LkJhG", "Halo Fitness", "subscription_fitness", False, 1.0,
                   (Channel.SMS, Channel.WHATSAPP)),
    MerchantConfig("mrch_Pq7WsXeD5RfVtG", "Arcadia Retail", "ecommerce", True, 0.50,
                   (Channel.SMS, Channel.EMAIL)),
    MerchantConfig("mrch_Mn2BvCx8ZaSdFg", "Loom Media", "saas", True, 0.35,
                   (Channel.EMAIL,)),
]


@dataclass(frozen=True)
class Batch:
    events: list[FailureEvent]
    hidden: dict[str, HiddenState]
    oob_settlements: dict[str, datetime]
    physics: Physics
    seed: int
    split: str
    config: dict[str, Any]

    @property
    def total_at_risk_paise(self) -> int:
        return sum(e.amount_paise for e in self.events)


def _rid(rng: random.Random, prefix: str) -> str:
    return prefix + "".join(rng.choice(_ID_ALPHABET) for _ in range(14))


def _amount(rng: random.Random, cfg: dict[str, Any]) -> int:
    """Lognormal around the configured median, clipped to the configured range."""
    lo, med, hi = cfg["min"], cfg["median"], cfg["max"]
    sigma = 0.9
    value = math.exp(rng.gauss(math.log(med), sigma))
    return int(max(lo, min(hi, value)) // 100 * 100)


def _pick_class(rng: random.Random, mixture: dict[str, float]) -> FailureClass:
    roll, cumulative = rng.random(), 0.0
    for name, weight in mixture.items():
        cumulative += weight
        if roll <= cumulative:
            return FailureClass(name)
    return FailureClass(list(mixture)[-1])


def _build_error(
    rng: random.Random, true_class: FailureClass, style_cfg: dict[str, float],
) -> tuple[RazorpayError, str]:
    """Produce the evidence an arm will actually see, and label its style.

    Three styles, in the proportions configured. ``generic_with_prose`` is the
    interesting one: the reason code says nothing and the cause is only legible
    in the issuer's own words.
    """
    roll = rng.random()
    clean_p = style_cfg["clean_code"]
    generic_p = clean_p + style_cfg["generic_with_prose"]

    message = rng.choice(im.MESSAGES[true_class])

    if roll < clean_p:
        style = "clean_code"
        reason = rng.choice(
            im.CLEAN_ALTERNATES.get(true_class, [im.CLEAN_REASON[true_class]])
        )
    elif roll < generic_p:
        style = "generic_with_prose"
        reason = "payment_failed"
    else:
        style = "misleading"
        wrong = rng.choice([c for c in im.CLEAN_REASON if c is not true_class])
        reason = im.CLEAN_REASON[wrong]

    code = {
        "bank": "BAD_REQUEST_ERROR",
        "gateway": "GATEWAY_ERROR",
        "customer": "BAD_REQUEST_ERROR",
    }[im.ERROR_SOURCE[true_class]]

    return RazorpayError(
        code=code,
        description=im.DESCRIPTION[true_class],
        source=im.ERROR_SOURCE[true_class],
        step=im.ERROR_STEP[true_class],
        reason=reason,
        issuer_message=message,
    ), style


def _rail_for(rng: random.Random, true_class: FailureClass) -> Rail:
    if true_class is FailureClass.MANDATE_INVALID:
        return Rail.EMANDATE
    if true_class is FailureClass.RESTRICTION:
        return Rail.CARD
    return rng.choices(
        [Rail.CARD, Rail.UPI, Rail.NETBANKING, Rail.EMANDATE],
        weights=[0.42, 0.38, 0.08, 0.12],
    )[0]


def generate(split: str = "test", path: Path | None = None,
             n_cases: int | None = None, replicate: int = 0) -> Batch:
    """Build a batch.

    ``replicate`` draws a different world from the same configuration, so a
    result can be reported as a distribution rather than a single number. Zero
    is the canonical split; every other value is an independent draw.
    """
    cfg: dict[str, Any] = yaml.safe_load((path or CONFIG_PATH).read_text())
    base_seed = int(cfg["seed"])
    seed = base_seed if split == "test" else base_seed + 7717
    seed += replicate * 104_729                    # a prime, to avoid overlap
    rng = random.Random(seed)

    n = n_cases if n_cases is not None else int(cfg["n_cases"])
    cust_cfg = cfg["customer"]
    resp_cfg = {Channel(k): float(v) for k, v in cfg["channel_responsiveness"].items()}
    phys_cfg = cfg["recovery_physics"]
    physics = Physics(
        retry_success_when_clear=float(phys_cfg["retry_success_when_clear"]),
        retry_success_when_blocked=float(phys_cfg["retry_success_when_blocked"]),
        part_payment_funds_bonus=float(phys_cfg["part_payment_funds_bonus"]),
        instrument_refresh_uptake=float(phys_cfg["instrument_refresh_uptake"]),
        handoff_success=float(phys_cfg["handoff_success"]),
    )

    events: list[FailureEvent] = []
    hidden: dict[str, HiddenState] = {}
    oob: dict[str, datetime] = {}

    for i in range(n):
        case_id = f"case_{i:04d}"
        true_class = _pick_class(rng, cfg["class_mixture"])
        merchant = rng.choice(MERCHANTS)
        rail = _rail_for(rng, true_class)

        has_alt = rng.random() < cust_cfg["alt_rail_rate"]
        saved_rails: tuple[Rail, ...] = (rail,)
        if has_alt:
            alt = Rail.UPI if rail is not Rail.UPI else Rail.CARD
            saved_rails = (rail, alt)

        opted_out = rng.random() < cust_cfg["opt_out_rate"]
        customer = CustomerProfile(
            customer_id=_rid(rng, "cust_"),
            contact_hash=_rid(rng, "ct_"),
            opted_out=opted_out,
            dnd_registered=rng.random() < cust_cfg["dnd_rate"],
            prior_successful_payments=rng.choices(
                [0, 1, 2, 5, 12, 30], weights=[0.18, 0.16, 0.2, 0.2, 0.16, 0.10])[0],
            prior_failed_payments=rng.choices(
                [0, 1, 2, 4], weights=[0.55, 0.26, 0.13, 0.06])[0],
            prior_recoveries=rng.choices([0, 1, 2], weights=[0.74, 0.20, 0.06])[0],
            saved_rails=saved_rails,
            tz_offset_minutes=330,
        )

        error, style = _build_error(rng, true_class, cfg["evidence_style"])
        failed_at = EPOCH + timedelta(
            hours=rng.uniform(0, 24 * 5), minutes=rng.uniform(0, 60),
        )
        amount = _amount(rng, cfg["amount_paise"])

        event = FailureEvent(
            event_id=_rid(rng, "evt_"),
            payment_id=_rid(rng, "pay_"),
            order_id=_rid(rng, "order_"),
            amount_paise=amount,
            currency="INR",
            rail=rail,
            failed_at=failed_at,
            error=error,
            customer=customer,
            merchant=merchant,
            attempt_index=rng.choices([0, 1, 2], weights=[0.72, 0.21, 0.07])[0],
            subscription_id=_rid(rng, "sub_") if rail is Rail.EMANDATE else None,
        )
        events.append(event)

        # ---- hidden ground truth -----------------------------------------
        funds_return_at: datetime | None
        if true_class is FailureClass.INSUFFICIENT_FUNDS:
            if rng.random() < phys_cfg["funds_never_return_rate"]:
                funds_return_at = None
            else:
                funds_return_at = failed_at + timedelta(hours=rng.uniform(
                    phys_cfg["funds_return_hours_min"],
                    phys_cfg["funds_return_hours_max"]))
        else:
            funds_return_at = failed_at

        outage_ends_at = None
        if true_class in (FailureClass.ISSUER_DOWN, FailureClass.RAIL_TIMEOUT):
            outage_ends_at = failed_at + timedelta(minutes=rng.uniform(
                phys_cfg["outage_minutes_min"], phys_cfg["outage_minutes_max"]))

        hidden[case_id] = HiddenState(
            true_class=true_class,
            instrument_alive=true_class not in (
                FailureClass.INSTRUMENT_DEAD, FailureClass.MANDATE_INVALID),
            alt_rail_works=has_alt,
            reachable=rng.random() > cust_cfg["unreachable_rate"],
            patience=rng.randint(cust_cfg["patience_min"], cust_cfg["patience_max"]),
            intent_half_life_hours=rng.uniform(
                cust_cfg["intent_half_life_hours_min"],
                cust_cfg["intent_half_life_hours_max"]),
            responsiveness={
                ch: max(0.01, p * rng.uniform(0.55, 1.45))
                for ch, p in resp_cfg.items()
            },
            funds_return_at=funds_return_at,
            outage_ends_at=outage_ends_at,
            max_part_ratio=rng.uniform(0.35, 0.85),
            opted_out=opted_out,
        )

        if rng.random() < cfg["out_of_band_settlement_rate"]:
            oob[case_id] = failed_at + timedelta(hours=rng.uniform(2, 200))

    return Batch(
        events=events, hidden=hidden, oob_settlements=oob, physics=physics,
        seed=seed, split=split, config=cfg,
    )
