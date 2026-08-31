"""Run the benchmark over many worlds and report a distribution.

Every headline number in this repository came from one seed. That is enough to
say "on this world, E beat B by 1.02x" and not enough to say the difference is
real — a criticism an adversarial review made, correctly.

This draws independent worlds from the same committed configuration and reports
mean, standard deviation and a 95% interval, so the honest question — *is the
gap larger than the noise* — can actually be answered.

    python -m vasool.bench.replicate --reps 12 --n 300
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from vasool.bench.arms.base import Arm, CronDiagnoser
from vasool.bench.generator import generate
from vasool.bench.runner import run_arm
from vasool.core.policy import Costs, Policy
from vasool.diagnosis.llm import RulesDiagnoser

RESULTS = Path(__file__).resolve().parents[2] / "results"


def _arms(policy: Policy) -> list[Arm]:
    return [
        Arm("A", "cron", CronDiagnoser(policy), False, False, "fixed schedule"),
        Arm("B", "rules", RulesDiagnoser(policy), False, False, "no kernel"),
        Arm("E", "rules+gate", RulesDiagnoser(policy), True, False, "kernel"),
    ]


def _ci95(values: list[float]) -> tuple[float, float]:
    """Mean and half-width of a 95% interval. Normal approximation."""
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, 0.0
    return mean, 1.96 * statistics.stdev(values) / (len(values) ** 0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-seed replication")
    parser.add_argument("--reps", type=int, default=12)
    parser.add_argument("--n", type=int, default=300)
    parser.add_argument("--split", default="test", choices=["dev", "test"])
    parser.add_argument("--out", default=str(RESULTS / "replication.json"))
    args = parser.parse_args(argv)

    console = Console()
    policy, costs = Policy.load(), Costs.load()
    net: dict[str, list[float]] = defaultdict(list)
    recovery: dict[str, list[float]] = defaultdict(list)
    ratios: list[float] = []

    with console.status(f"running {args.reps} worlds…"):
        for rep in range(args.reps):
            batch = generate(args.split, n_cases=args.n, replicate=rep)
            per_arm = {}
            for arm in _arms(policy):
                r = run_arm(arm, batch, policy, costs)
                value = r.net_value_paise / 100.0
                net[arm.key].append(value)
                recovery[arm.key].append(r.recovery_rate * 100)
                per_arm[arm.key] = value
            if per_arm.get("B"):
                ratios.append(per_arm["E"] / per_arm["B"])

    table = Table(title=f"{args.reps} independent worlds · N={args.n} each",
                  header_style="bold")
    table.add_column("Arm")
    table.add_column("recovery rate", justify="right")
    table.add_column("net value (₹)", justify="right")
    for key, label in (("A", "cron"), ("B", "rules"), ("E", "rules+kernel")):
        rm, rc = _ci95(recovery[key])
        nm, nc = _ci95(net[key])
        table.add_row(f"{key} {label}", f"{rm:.1f}% ± {rc:.1f}",
                      f"{nm:,.0f} ± {nc:,.0f}")
    console.print(table)

    mean, ci = _ci95(ratios)
    wins = sum(1 for r in ratios if r > 1.0)
    console.print(
        f"\n[bold]E/B net value: {mean:.3f} ± {ci:.3f}[/bold] (95% CI)\n"
        f"E ahead in {wins} of {len(ratios)} worlds\n"
    )
    verdict = ("the gap clears the noise" if mean - ci > 1.0
               else "[yellow]the interval spans 1.0 — the gap is not distinguishable "
                    "from noise at this sample size[/yellow]")
    console.print(verdict)

    Path(args.out).write_text(json.dumps({
        "reps": args.reps, "n": args.n, "split": args.split,
        "net_value_rupees": {k: v for k, v in net.items()},
        "recovery_rate_pct": {k: v for k, v in recovery.items()},
        "e_over_b": {"mean": mean, "ci95": ci, "wins": wins, "of": len(ratios)},
    }, indent=2))
    console.print(f"[dim]-> {args.out}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
