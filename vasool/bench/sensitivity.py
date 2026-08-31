"""How much of the result is the harm-price model?

The kernel's advantage is a net-value figure, and net value subtracts priced
harms whose prices were chosen by the author. That makes "how load-bearing are
those prices" a fair question, and defending the choice of ₹50,000 for a double
collection is a much weaker answer than showing the curve.

    python -m vasool.bench.sensitivity
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

RESULTS = Path(__file__).resolve().parents[2] / "results"


def net_at(arm: dict, multiplier: float) -> int:
    return int(
        arm["recovered_paise"]
        - arm["action_spend_paise"]
        - arm["harm_cost_paise"] * multiplier
        - arm["double_collected_paise"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harm-price sensitivity sweep")
    parser.add_argument("--results", default=str(RESULTS / "test-split-A-B-E.json"))
    parser.add_argument("--out", default=str(RESULTS / "harm-sensitivity.json"))
    args = parser.parse_args(argv)

    console = Console()
    data = json.loads(Path(args.results).read_text())
    arms = {a["arm_key"]: a for a in data["arms"]}
    if not {"B", "E"} <= set(arms):
        console.print("[red]need arms B and E in the results file[/red]")
        return 1

    table = Table(title="Net value vs harm-price multiplier", header_style="bold")
    table.add_column("harm ×", justify="right")
    table.add_column("B rules", justify="right")
    table.add_column("E rules+kernel", justify="right")
    table.add_column("E/B", justify="right")
    table.add_column("winner")

    rows = []
    for m in (2.0, 1.0, 0.5, 0.25, 0.1, 0.0):
        b, e = net_at(arms["B"], m), net_at(arms["E"], m)
        ratio = e / b if b else float("inf")
        rows.append({"multiplier": m, "b_net_paise": b, "e_net_paise": e,
                     "ratio": round(ratio, 3)})
        table.add_row(
            f"{m:.2f}", f"₹{b/100:,.0f}", f"₹{e/100:,.0f}", f"{ratio:.2f}",
            "E" if e > b else "B",
        )
    console.print(table)
    console.print(
        "\n[dim]Read this as the honest form of the claim: the kernel's edge is "
        "whatever you think preventing these harms is worth. At zero it loses.[/dim]"
    )
    Path(args.out).write_text(json.dumps({"source": args.results, "sweep": rows}, indent=2))
    console.print(f"[dim]-> {args.out}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
