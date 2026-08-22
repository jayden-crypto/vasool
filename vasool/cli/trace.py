"""Render one case's full decision chain.

    python -m vasool.cli.trace runs/<file>.jsonl                 # summary
    python -m vasool.cli.trace runs/<file>.jsonl --case case_0007
    python -m vasool.cli.trace runs/<file>.jsonl --denied        # only refusals

Exists so a reviewer — or a camera — can follow evidence, diagnosis, verdict,
action and outcome for a single case without reading JSON.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from vasool.core.types import rupees
from vasool.executor.ledger import Ledger

KIND_STYLE = {
    "intent": "cyan",
    "outcome": "green",
    "denied": "red",
    "stopped": "yellow",
    "unknown_outcome": "magenta",
    "reconciled": "magenta",
    "provider_error": "red",
}


def _line(record) -> Text:
    text = Text()
    text.append(f"{record.seq:>5}  ", style="dim")
    text.append(f"{record.at[:19]}  ", style="dim")
    text.append(f"{record.kind:<16}", style=KIND_STYLE.get(record.kind, "white"))
    p = record.payload

    if record.kind == "intent":
        text.append(
            f"{p.get('intervention')} via {p.get('channel')} for "
            f"{rupees(int(p.get('amount_paise', 0)))}"
        )
        text.append(
            f"\n       diagnosis: {p.get('diagnosis')} "
            f"({p.get('diagnosis_source')}, conf {p.get('confidence')})",
            style="dim",
        )
        if p.get("evidence_fields"):
            text.append(f"\n       cited: {', '.join(p['evidence_fields'])}", style="dim")
        if p.get("rationale"):
            text.append(f"\n       “{p['rationale'][:140]}”", style="italic dim")
    elif record.kind == "denied":
        text.append(
            f"{p.get('intervention')} — {', '.join(p.get('denials', []))}",
            style="red",
        )
        text.append(
            f"\n       invariants: {', '.join(p.get('invariants', []))}"
            f"  ·  {p.get('detail', '')[:120]}",
            style="dim",
        )
    elif record.kind == "outcome":
        verdict = "recovered" if p.get("succeeded") else "no recovery"
        text.append(f"{verdict}  {rupees(int(p.get('collected_paise', 0)))}")
        if p.get("harms"):
            text.append(f"   HARMS: {', '.join(p['harms'])}", style="bold red")
        text.append(f"\n       {p.get('detail', '')[:120]}", style="dim")
    elif record.kind in ("unknown_outcome", "reconciled"):
        text.append(str(p.get("resolution", "")), style="magenta")
        text.append(f"\n       key {p.get('idempotency_key', '')[:16]}…", style="dim")
    else:
        text.append(str(p)[:140], style="dim")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vasool ledger viewer")
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--case", default=None)
    parser.add_argument("--denied", action="store_true", help="only show refusals")
    parser.add_argument("--harms", action="store_true", help="only cases with harms")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)

    console = Console()
    if not args.ledger.exists():
        console.print(f"[red]no such ledger:[/red] {args.ledger}")
        console.print("[dim]run `make bench-ledger` to produce one[/dim]")
        return 1

    ledger = Ledger.load(args.ledger)
    ok, bad = ledger.verify()

    header = Table(show_header=False, box=None)
    header.add_row("file", str(args.ledger))
    header.add_row("records", f"{len(ledger):,}")
    header.add_row(
        "chain",
        "[green]valid — every record commits to the previous digest[/green]"
        if ok else f"[red]BROKEN at seq {bad}[/red]",
    )
    header.add_row("tip", ledger.tip[:32] + "…")
    console.print(Panel(header, title="Vasool · audit ledger", border_style="cyan"))

    if args.case:
        records = ledger.for_case(args.case)
        if not records:
            console.print(f"[yellow]no records for {args.case}[/yellow]")
            return 1
        console.print(f"\n[bold]{args.case}[/bold]  ({len(records)} records)\n")
        for record in records:
            console.print(_line(record))
            console.print()
        return 0

    if args.denied or args.harms:
        selected = [
            r for r in ledger
            if (args.denied and r.kind == "denied")
            or (args.harms and r.kind == "outcome" and r.payload.get("harms"))
        ]
        console.print(f"\n[bold]{len(selected)} matching records[/bold]\n")
        for record in selected[: args.limit]:
            console.print(_line(record))
            console.print()
        return 0

    kinds = Counter(r.kind for r in ledger)
    summary = Table(title="Records by kind")
    summary.add_column("kind")
    summary.add_column("count", justify="right")
    for kind, count in kinds.most_common():
        summary.add_row(Text(kind, style=KIND_STYLE.get(kind, "white")), f"{count:,}")
    console.print()
    console.print(summary)

    denials = Counter(
        d for r in ledger if r.kind == "denied" for d in r.payload.get("denials", [])
    )
    if denials:
        table = Table(title="Denials by reason code")
        table.add_column("reason")
        table.add_column("count", justify="right")
        for reason, count in denials.most_common():
            table.add_row(reason, f"{count:,}")
        console.print()
        console.print(table)

    interesting = [
        r.case_id for r in ledger
        if r.kind == "outcome" and r.payload.get("harms")
    ] or [r.case_id for r in ledger if r.kind == "denied"]
    if interesting:
        console.print(
            f"\n[dim]try:[/dim] python -m vasool.cli.trace {args.ledger} "
            f"--case {interesting[0]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
