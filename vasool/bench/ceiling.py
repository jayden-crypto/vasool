"""How much of the batch is actually decidable.

A classification accuracy is meaningless without knowing the ceiling. Some of
these cases carry evidence that does not determine the answer — most obviously
"do not honour", which issuers use for both a thin balance and a risk decline,
and which appears in the message bank under both causes on purpose.

No classifier can resolve those. Reporting 86.6% against an implied ceiling of
100% understates every arm; reporting it against the real ceiling is the honest
comparison, and it tells you how much headroom is left before more model is
wasted spend.

    python -m vasool.bench.ceiling --split test
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from rich.console import Console
from rich.table import Table

from vasool.bench import issuer_messages as im
from vasool.bench.generator import generate
from vasool.core.types import FailureClass


#: Messages that appear under more than one cause in the bank.
def ambiguous_messages() -> dict[str, set[FailureClass]]:
    seen: dict[str, set[FailureClass]] = defaultdict(set)
    for cls, messages in im.MESSAGES.items():
        for message in messages:
            seen[message].add(cls)
    return {m: c for m, c in seen.items() if len(c) > 1}


def analyse(split: str, n: int | None = None) -> dict[str, object]:
    batch = generate(split, n_cases=n)
    ambiguous = ambiguous_messages()

    styles: dict[str, int] = defaultdict(int)
    undecidable = 0
    undecidable_by_style: dict[str, int] = defaultdict(int)
    contradictions = 0

    for event, hidden in zip(batch.events, batch.hidden.values()):
        reason = event.error.reason
        message = event.error.issuer_message
        canonical = im.CLEAN_REASON.get(hidden.true_class)
        alternates = im.CLEAN_ALTERNATES.get(hidden.true_class, [canonical])

        if reason == "payment_failed":
            style = "generic_with_prose"
        elif reason in alternates or reason == canonical:
            style = "clean_code"
        else:
            style = "misleading"
            contradictions += 1
        styles[style] += 1

        # A case is undecidable when the code carries no signal and the message
        # is one the bank uses for more than one cause.
        if style == "generic_with_prose" and message in ambiguous:
            undecidable += 1
            undecidable_by_style[style] += 1

    total = len(batch.events)
    return {
        "split": batch.split, "seed": batch.seed, "total": total,
        "styles": dict(styles), "contradictions": contradictions,
        "undecidable": undecidable,
        "ceiling": (total - undecidable) / total if total else 0.0,
        "ambiguous_messages": ambiguous,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decidability ceiling")
    parser.add_argument("--split", default="test", choices=["dev", "test"])
    parser.add_argument("--n", type=int, default=None)
    args = parser.parse_args(argv)

    console = Console()
    result = analyse(args.split, args.n)

    table = Table(title=f"Evidence quality — {result['split']} split, "
                        f"seed {result['seed']}")
    table.add_column("Evidence style")
    table.add_column("Cases", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("What it tests", no_wrap=False)

    descriptions = {
        "clean_code": "reason code states the cause — a lookup table suffices",
        "generic_with_prose": "cause only in the issuer's free text",
        "misleading": "reason code contradicts the message — judgment required",
    }
    for style in ("clean_code", "generic_with_prose", "misleading"):
        count = result["styles"].get(style, 0)
        table.add_row(style, f"{count:,}",
                      f"{count / result['total'] * 100:.1f}%",
                      descriptions[style])
    console.print(table)

    console.print(
        f"\n[bold]Undecidable from the evidence:[/bold] {result['undecidable']} "
        f"of {result['total']} "
        f"({result['undecidable'] / result['total'] * 100:.1f}%)"
    )
    console.print(
        f"[bold]Classification ceiling:[/bold] "
        f"[green]{result['ceiling'] * 100:.1f}%[/green]  "
        "[dim]— no classifier, of any size, can beat this on this batch[/dim]\n"
    )
    console.print("[dim]Messages the bank uses for more than one cause:[/dim]")
    for message, classes in result["ambiguous_messages"].items():
        console.print(f'  [yellow]"{message}"[/yellow] → '
                      + ", ".join(sorted(c.value for c in classes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
