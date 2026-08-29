"""Parallel cache pre-warmer.

The benchmark runner is sequential by necessity — each decision depends on the
outcome of the last one. But the *first* diagnosis for every case depends on
nothing, and on a laptop-hosted model that first pass is most of the wall clock.

So compute those concurrently, write them to the response cache, and let the
sequential run read them back. On a local 7B this is the difference between an
afternoon and a coffee break.

    python -m vasool.diagnosis.warm --split test --n 500 --workers 4
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from vasool.bench.generator import generate
from vasool.core import env
from vasool.core.policy import Policy
from vasool.core.types import CaseState
from vasool.diagnosis import providers
from vasool.diagnosis.cache import ResponseCache
from vasool.diagnosis.llm import LLMDiagnoser


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-warm the diagnosis cache")
    parser.add_argument("--split", default="test", choices=["dev", "test"])
    parser.add_argument("--n", type=int, default=None)
    parser.add_argument("--workers", type=int, default=2,
                        help="keep this at or below the provider's calls/min capacity")
    args = parser.parse_args(argv)

    env.load()
    console = Console()
    provider = providers.resolve()
    if provider is None:
        console.print(
            "[red]No model provider configured.[/red] Set VASOOL_PROVIDER — "
            "see README § Running the model arms for free."
        )
        return 1

    console.print(f"[dim]reasoning zone: {providers.describe()}[/dim]")
    policy = Policy.load()
    batch = generate(args.split, n_cases=args.n)
    cache = ResponseCache()
    before = len(cache)

    # One diagnoser per worker: the circuit breaker and stats are per-instance,
    # and sharing one across threads would make both meaningless.
    diagnosers = [
        LLMDiagnoser(policy, cache=cache) for _ in range(args.workers)
    ]

    def warm(index: int) -> bool:
        event = batch.events[index]
        case = CaseState(case_id=f"case_{index:04d}", event=event,
                         opened_at=event.failed_at)
        now = event.failed_at + timedelta(minutes=5)
        _, degraded = diagnosers[index % args.workers].propose(case, now)
        return not degraded

    started = time.monotonic()
    succeeded = 0
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(), console=console,
    ) as progress:
        task = progress.add_task("warming", total=len(batch.events))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(warm, i): i for i in range(len(batch.events))
            }
            for future in as_completed(futures):
                try:
                    succeeded += 1 if future.result() else 0
                except Exception:
                    pass
                progress.advance(task)

    cache.flush()
    elapsed = time.monotonic() - started
    limited = sum(d.stats.rate_limited for d in diagnosers)
    if limited:
        console.print(
            f"[yellow]{limited} calls gave up after retrying a rate limit.[/yellow] "
            "Lower --workers; the free tier's ceiling is calls-per-minute, and "
            "exceeding it makes the run slower, not faster.\n"
        )
    console.print(
        f"\n[green]{succeeded}[/green]/{len(batch.events)} diagnosed by the model "
        f"({len(batch.events) - succeeded} fell back to the rules path)\n"
        f"cache: {before} → {len(cache)} entries\n"
        f"{elapsed:.0f}s wall clock, {elapsed / max(1, len(batch.events)):.2f}s per case"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
