"""Run every arm over one batch and print the table.

Usage:
    python -m vasool.bench.report --split test --n 500
    python -m vasool.bench.report --split dev --n 120 --arms A,B,E
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from vasool.bench.arms.base import Arm, CronDiagnoser
from vasool.bench.generator import generate
from vasool.bench.runner import ArmResult, run_arm
from vasool.core import env
from vasool.core.policy import Costs, Policy
from vasool.core.types import rupees
from vasool.diagnosis.cache import ResponseCache
from vasool.diagnosis.llm import LLMDiagnoser, RulesDiagnoser

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"


def build_arms(policy: Policy, keys: set[str], cache: ResponseCache) -> list[Arm]:
    catalogue = {
        "A": lambda: Arm(
            "A", "cron", CronDiagnoser(policy), use_gate=False, allow_repair=False,
            description="fixed retry schedule at T+24/72/120h; cause never read",
        ),
        "B": lambda: Arm(
            "B", "rules", RulesDiagnoser(policy), use_gate=False, allow_repair=False,
            description="deterministic taxonomy -> intervention, no kernel",
        ),
        "C": lambda: Arm(
            "C", "raw-agent", LLMDiagnoser(policy, cache=cache),
            use_gate=False, allow_repair=False,
            description="model proposes, executor obeys — no kernel",
        ),
        "D": lambda: Arm(
            "D", "vasool", LLMDiagnoser(policy, cache=cache),
            use_gate=True, allow_repair=True,
            description="model proposes, kernel decides",
        ),
        "E": lambda: Arm(
            "E", "rules+gate", RulesDiagnoser(policy), use_gate=True, allow_repair=False,
            description="ablation: kernel without the model",
        ),
    }
    return [catalogue[k]() for k in sorted(keys) if k in catalogue]


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render(results: list[ArmResult], console: Console, batch_label: str) -> None:
    money = Table(title=f"Recovery — {batch_label}", header_style="bold")
    money.add_column("Metric", no_wrap=True)
    for r in results:
        money.add_column(f"{r.arm_key} {r.arm_label}", justify="right")

    rows: list[tuple[str, list[str]]] = [
        ("Recovery rate", [_fmt_pct(r.recovery_rate) for r in results]),
        ("Value recovered", [rupees(r.recovered_paise) for r in results]),
        ("of value at risk", [_fmt_pct(r.recovery_value_rate) for r in results]),
        ("Actions executed", [f"{r.actions_executed:,}" for r in results]),
        ("Recovered per action", [rupees(int(r.paise_per_action)) for r in results]),
        ("Contacts made", [f"{r.contacts_made:,}" for r in results]),
        ("Contacts per recovery", [f"{r.contacts_per_recovery:.2f}" for r in results]),
        ("Median hours to recovery", [
            "—" if r.median_time_to_recovery is None else f"{r.median_time_to_recovery:.1f}"
            for r in results]),
        ("Action spend", [rupees(r.action_spend_paise) for r in results]),
        ("Diagnosis accuracy", [_fmt_pct(r.classification_accuracy) for r in results]),
    ]
    for label, values in rows:
        money.add_row(label, *values)
    console.print(money)
    console.print()

    harm = Table(title="Harm ledger — what it cost to get there",
                 header_style="bold red")
    harm.add_column("Harm", no_wrap=True)
    for r in results:
        harm.add_column(f"{r.arm_key} {r.arm_label}", justify="right")

    harm_keys = sorted({k for r in results for k in r.harms})
    for key in harm_keys:
        harm.add_row(
            key.replace("_", " "),
            *[f"{r.harms.get(key, 0):,}" for r in results],
        )
    harm.add_row(
        "[bold]priced harm cost[/bold]",
        *[f"[bold]{rupees(r.harm_cost_paise)}[/bold]" for r in results],
    )
    harm.add_row(
        "double charged",
        *[rupees(r.double_collected_paise) for r in results],
    )
    harm.add_row(
        "[bold]net value[/bold]",
        *[f"[bold]{rupees(r.net_value_paise)}[/bold]" for r in results],
    )
    console.print(harm)
    console.print()

    ops = Table(title="Operational", header_style="bold")
    ops.add_column("Metric", no_wrap=True)
    for r in results:
        ops.add_column(f"{r.arm_key} {r.arm_label}", justify="right")
    ops.add_row("Cases stopped deliberately", *[f"{r.stopped_cases:,}" for r in results])
    ops.add_row("Cases abandoned at horizon", *[f"{r.abandoned_cases:,}" for r in results])
    ops.add_row("Closed: settled elsewhere, no credit taken",
                *[f"{r.closed_settled_elsewhere:,}" for r in results])
    ops.add_row("Closed: double charged", *[
        (f"[red]{r.double_collected_cases:,}[/red]" if r.double_collected_cases
         else "0") for r in results])
    ops.add_row("[bold]Cases accounted for[/bold]", *[
        f"[bold]{r.closed_cases:,}/{r.n_cases:,}[/bold]" for r in results])
    ops.add_row("Degraded decisions", *[f"{r.degraded_decisions:,}" for r in results])
    ops.add_row("Ledger records", *[f"{r.ledger_records:,}" for r in results])
    ops.add_row("Ledger chain valid", *["yes" if r.ledger_valid else "NO" for r in results])
    ops.add_row("Settled out of band (uncredited)",
                *[f"{r.out_of_band_cases}" for r in results])
    console.print(ops)

    gated = [r for r in results if r.denials]
    if gated:
        console.print()
        den = Table(title="Kernel denials by reason code", header_style="bold cyan")
        den.add_column("Reason", no_wrap=True)
        for r in gated:
            den.add_column(f"{r.arm_key} {r.arm_label}", justify="right")
        for key in sorted({k for r in gated for k in r.denials}):
            den.add_row(key, *[f"{r.denials.get(key, 0):,}" for r in gated])
        console.print(den)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vasool four-arm benchmark")
    parser.add_argument("--split", default="test", choices=["dev", "test"])
    parser.add_argument("--n", type=int, default=None, help="cases (default: config)")
    parser.add_argument("--arms", default="A,B,C,D,E")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--ledger", action="store_true", help="write ledger files")
    parser.add_argument("--out", default=None, help="write results JSON here")
    args = parser.parse_args(argv)

    env.load()
    console = Console()
    policy, costs = Policy.load(), Costs.load()
    batch = generate(args.split, n_cases=args.n)
    cache = ResponseCache(enabled=not args.no_cache)
    arms = build_arms(policy, set(args.arms.upper().split(",")), cache)

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_key and any(a.diagnoser.name == "llm" for a in arms):
        console.print(
            "[yellow]No ANTHROPIC_API_KEY set.[/yellow] Model-backed arms will run "
            f"from the response cache ({len(cache)} entries) and fall back to the "
            "deterministic path on a miss. Every such decision is counted under "
            "'Degraded decisions' below.\n"
        )

    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results: list[ArmResult] = []
    for arm in arms:
        console.print(f"[dim]running arm {arm.key} ({arm.label})…[/dim]")
        ledger_path = RUNS_DIR / f"{stamp}-{arm.key}-{arm.label}.jsonl" if args.ledger else None
        results.append(run_arm(arm, batch, policy, costs, ledger_path))
        if hasattr(arm.diagnoser, "close"):
            arm.diagnoser.close()
    console.print()

    label = (
        f"{batch.split} split · seed {batch.seed} · {len(batch.events)} cases · "
        f"{rupees(batch.total_at_risk_paise)} at risk"
    )
    render(results, console, label)

    payload = {
        "split": batch.split,
        "seed": batch.seed,
        "n_cases": len(batch.events),
        "at_risk_paise": batch.total_at_risk_paise,
        "generated_at": stamp,
        "api_key_present": has_key,
        "arms": [asdict(r) for r in results],
    }
    out = Path(args.out) if args.out else RUNS_DIR / f"{stamp}-results.json"
    out.write_text(json.dumps(payload, indent=2, default=str))
    console.print(f"\n[dim]results -> {out}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
