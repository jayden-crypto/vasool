"""Classification-only benchmark.

The full five-arm run costs ~5.4 model calls per case, because arms C and D
walk whole recovery trajectories. The central claim about the model is narrower
than that and needs exactly one call per case: **given the evidence, does it
name the right cause more often than a lookup table does?**

Isolating it that way makes the experiment affordable on a free tier, and makes
the answer sharper — accuracy is reported per evidence style, so you can see
whether a model wins where it is supposed to (the cases where the reason code
contradicts the issuer's own message) rather than only on aggregate.

    python -m vasool.bench.classify --n 60 --models openai/gpt-oss-120b,qwen/qwen3.8-27b
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from vasool.bench import issuer_messages as im
from vasool.bench.ceiling import analyse
from vasool.bench.generator import generate
from vasool.core import env
from vasool.core.policy import Policy
from vasool.core.types import CaseState, FailureClass
from vasool.diagnosis import providers
from vasool.diagnosis.cache import ResponseCache
from vasool.diagnosis.fallback import classify as rules_classify
from vasool.diagnosis.fallback import classify_adjudicated as fallback_adjudicated
from vasool.diagnosis.llm import LLMDiagnoser

RESULTS = Path(__file__).resolve().parents[2] / "results"


def evidence_style(event, truth: FailureClass) -> str:
    """Which of the three styles this case's evidence falls into."""
    reason = event.error.reason
    if reason == "payment_failed":
        return "generic_prose"
    canonical = im.CLEAN_REASON.get(truth)
    if reason in im.CLEAN_ALTERNATES.get(truth, [canonical]) or reason == canonical:
        return "clean_code"
    return "misleading"


def run_model(model: str, n: int, split: str, console: Console,
              routed: str = "") -> dict[str, Any]:
    batch = generate(split, n_cases=n)
    policy = Policy.load()
    os.environ["VASOOL_MODEL"] = model
    diagnoser: Any = LLMDiagnoser(policy, provider=providers.resolve(),
                                  cache=ResponseCache())
    if routed:
        from vasool.diagnosis.router import RoutedDiagnoser
        diagnoser = RoutedDiagnoser(policy, diagnoser, mode=routed)

    correct: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    started = time.monotonic()

    for index, (event, hidden) in enumerate(
        zip(batch.events, batch.hidden.values())
    ):
        truth = hidden.true_class
        style = evidence_style(event, truth)
        total[style] += 1
        total["all"] += 1

        case = CaseState(case_id=f"case_{index:04d}", event=event,
                         opened_at=event.failed_at)
        proposal, degraded = diagnoser.propose(
            case, event.failed_at + timedelta(minutes=5))
        if degraded:
            continue                      # not counted as correct; tracked below
        if proposal.diagnosis.failure_class == truth:
            correct[style] += 1
            correct["all"] += 1

    diagnoser.close()
    elapsed = time.monotonic() - started
    return {
        "model": (f"routed[{routed}] → {model}") if routed else model,
        "routes": getattr(diagnoser, "routes", None).as_dict() if routed else {},
        "correct": dict(correct),
        "total": dict(total),
        "stats": diagnoser.stats.as_dict(),
        "errors": dict(diagnoser.errors_seen),
        "last_error": diagnoser.last_error,
        "seconds": round(elapsed, 1),
    }


def deterministic_row(n: int, split: str, label: str, fn) -> dict[str, Any]:
    """Score any zero-model classifier over the same batch."""
    batch = generate(split, n_cases=n)
    correct: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for event, hidden in zip(batch.events, batch.hidden.values()):
        truth = hidden.true_class
        style = evidence_style(event, truth)
        total[style] += 1
        total["all"] += 1
        if fn(event).failure_class == truth:
            correct[style] += 1
            correct["all"] += 1
    return {"model": label, "correct": dict(correct), "total": dict(total),
            "stats": {}, "errors": {}, "routes": {}, "seconds": 0.0}


def rules_baseline(n: int, split: str) -> dict[str, Any]:
    batch = generate(split, n_cases=n)
    correct: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for event, hidden in zip(batch.events, batch.hidden.values()):
        truth = hidden.true_class
        style = evidence_style(event, truth)
        total[style] += 1
        total["all"] += 1
        if rules_classify(event).failure_class == truth:
            correct[style] += 1
            correct["all"] += 1
    return {"model": "rules baseline (no model)", "correct": dict(correct),
            "total": dict(total), "stats": {}, "errors": {}, "seconds": 0.0}


def _pct(c: dict, t: dict, key: str) -> str:
    if not t.get(key):
        return "—"
    return f"{c.get(key, 0) / t[key] * 100:.1f}%"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classification-only benchmark")
    parser.add_argument("--n", type=int, default=60)
    parser.add_argument("--split", default="test", choices=["dev", "test"])
    parser.add_argument("--models", default="", help="comma-separated model ids")
    parser.add_argument("--routed", default="",
                        help="comma-separated model ids to run behind the router")
    parser.add_argument("--mode", default="conflict_and_prose",
                        choices=["conflict_and_prose", "conflict_only"])
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    env.load()
    console = Console()
    rows = [
        rules_baseline(args.n, args.split),
        deterministic_row(args.n, args.split,
                          "rules + believe-the-prose (no model)",
                          fallback_adjudicated),
    ]

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    for model in models:
        console.print(f"[dim]running {model}…[/dim]")
        rows.append(run_model(model, args.n, args.split, console))
    for model in [m.strip() for m in args.routed.split(",") if m.strip()]:
        console.print(f"[dim]running routed[{args.mode}] → {model}…[/dim]")
        rows.append(run_model(model, args.n, args.split, console, routed=args.mode))

    ceiling = analyse(args.split, args.n)["ceiling"]

    table = Table(title=f"Diagnosis accuracy — {args.split} split, N={args.n}",
                  header_style="bold")
    table.add_column("Diagnoser", no_wrap=True)
    table.add_column("Overall", justify="right")
    table.add_column("clean code", justify="right")
    table.add_column("prose only", justify="right")
    table.add_column("contradictory", justify="right")
    table.add_column("degraded", justify="right")
    for r in rows:
        c, t = r["correct"], r["total"]
        deg = r["stats"].get("degraded", 0)
        table.add_row(
            r["model"], _pct(c, t, "all"), _pct(c, t, "clean_code"),
            _pct(c, t, "generic_prose"), _pct(c, t, "misleading"),
            f"{deg}" if deg else "—",
        )
    console.print(table)
    console.print(
        f"\n[dim]ceiling for this batch: {ceiling * 100:.1f}%. "
        "'contradictory' is the column the model exists to win.[/dim]"
    )
    for r in rows:
        if r.get("routes"):
            rt = r["routes"]
            done = sum(rt.get(k, 0) for k in ("code", "conflict", "prose", "neither"))
            console.print(
                f"[dim]{r['model']}: {rt.get('model_calls_saved', 0)}/{done} cases "
                f"answered without a model call "
                f"(code {rt.get('code',0)} · conflict {rt.get('conflict',0)} · "
                f"prose {rt.get('prose',0)} · neither {rt.get('neither',0)})[/dim]"
            )
    for r in rows:
        if r["errors"]:
            console.print(f"[yellow]{r['model']}: {r['errors']}[/yellow]")

    payload = {"split": args.split, "n": args.n, "ceiling": ceiling, "rows": rows}
    RESULTS.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else RESULTS / f"classification-n{args.n}.json"
    out.write_text(json.dumps(payload, indent=2, default=str))
    console.print(f"[dim]-> {out}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
