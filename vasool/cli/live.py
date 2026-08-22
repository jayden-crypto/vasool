"""One real recovery, end to end, against Razorpay test mode.

    python -m vasool.cli.live                  # REST backend
    python -m vasool.cli.live --mcp            # via Razorpay's official MCP server
    python -m vasool.cli.live --inject         # with the prompt injection attached

Creates a real order, fails it, diagnoses the failure, puts the proposal through
the Gate, and — if allowed — creates a real payment link. Every step is written
to a real ledger. Nothing here is simulated except the failure itself.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from vasool.core.policy import Costs, Policy
from vasool.core.types import (
    CaseState,
    Channel,
    CustomerProfile,
    FailureEvent,
    MerchantConfig,
    Rail,
    RazorpayError,
    rupees,
)
from vasool.diagnosis.llm import LLMDiagnoser, RulesDiagnoser
from vasool.executor.executor import Executor
from vasool.executor.ledger import Ledger
from vasool.faults.inject import INJECTION_REPLY
from vasool.kernel.gate import Gate

RUNS = Path(__file__).resolve().parents[2] / "runs"


def build_case(order_id: str, payment_id: str, amount_paise: int) -> CaseState:
    now = datetime.utcnow()
    event = FailureEvent(
        event_id="evt_live", payment_id=payment_id, order_id=order_id,
        amount_paise=amount_paise, currency="INR", rail=Rail.CARD,
        failed_at=now - timedelta(hours=3),
        error=RazorpayError(
            code="BAD_REQUEST_ERROR",
            description="Your payment could not be completed.",
            source="bank", step="payment_authorization",
            reason="payment_failed",
            issuer_message="do not honour - available balance low",
        ),
        customer=CustomerProfile(
            customer_id="cust_live", contact_hash="ct_live", opted_out=False,
            dnd_registered=False, prior_successful_payments=4,
            prior_failed_payments=1, prior_recoveries=1,
            saved_rails=(Rail.CARD, Rail.UPI), tz_offset_minutes=330,
        ),
        merchant=MerchantConfig(
            merchant_id="mrch_live", name="Live Demo", category="ecommerce",
            allows_part_payment=True, part_payment_floor_ratio=0.4,
            preferred_channels=(Channel.WHATSAPP, Channel.EMAIL),
        ),
        attempt_index=1,
    )
    return CaseState(case_id="case_live", event=event, opened_at=event.failed_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vasool live test-mode demo")
    parser.add_argument("--mcp", action="store_true",
                        help="route through Razorpay's official MCP server")
    parser.add_argument("--inject", action="store_true",
                        help="attach the prompt injection to the customer reply")
    parser.add_argument("--amount", type=int, default=249900, help="paise")
    parser.add_argument("--rules", action="store_true",
                        help="use the deterministic diagnoser instead of the model")
    args = parser.parse_args(argv)

    console = Console()
    policy, costs = Policy.load(), Costs.load()

    if args.mcp:
        from vasool.executor.razorpay_mcp import RazorpayMcpBackend
        backend = RazorpayMcpBackend()
        console.print("[dim]backend: Razorpay official MCP server "
                      "(READ_ONLY=false — writes enabled)[/dim]")
    else:
        from vasool.executor.razorpay_rest import RazorpayRestBackend
        backend = RazorpayRestBackend()
        console.print("[dim]backend: Razorpay REST, test mode[/dim]")

    # A real order, so the settlement read in I1 hits a real object.
    console.print("\n[bold]1. Creating a real test-mode order[/bold]")
    if args.mcp:
        order = backend.client.call_tool("create_order", {
            "amount": args.amount, "currency": "INR",
            "receipt": f"vasool-{datetime.utcnow():%Y%m%d%H%M%S}",
        })
    else:
        order = backend._request("POST", "/orders", json={
            "amount": args.amount, "currency": "INR",
            "receipt": f"vasool-{datetime.utcnow():%Y%m%d%H%M%S}",
        })
    console.print(f"   order: [cyan]{order['id']}[/cyan]  {rupees(args.amount)}")

    case = build_case(order["id"], "pay_simulated_failure", args.amount)

    console.print("\n[bold]2. Diagnosing the failure[/bold]")
    console.print(f"   reason code:    [yellow]{case.event.error.reason}[/yellow] "
                  "(says nothing)")
    console.print(f"   issuer message: [yellow]“{case.event.error.issuer_message}”[/yellow]")

    diagnoser = (
        RulesDiagnoser(policy) if args.rules
        else LLMDiagnoser(policy)
    )
    reply = INJECTION_REPLY if args.inject else None
    if args.inject:
        console.print(Panel(reply, title="customer reply (untrusted)",
                            border_style="red"))

    now = datetime.utcnow()
    proposal, degraded = diagnoser.propose(case, now, customer_reply=reply)

    table = Table(show_header=False, box=None)
    table.add_row("failure class", proposal.diagnosis.failure_class.value)
    table.add_row("confidence", f"{proposal.diagnosis.confidence:.2f}")
    table.add_row("source", proposal.diagnosis.source
                  + (" [red](degraded)[/red]" if degraded else ""))
    table.add_row("evidence cited", ", ".join(proposal.diagnosis.evidence_fields) or "—")
    table.add_row("proposed action", proposal.intervention.value)
    table.add_row("channel", proposal.channel.value)
    table.add_row("amount", rupees(proposal.amount_paise))
    table.add_row("rationale", proposal.rationale[:200] or "—")
    console.print(Panel(table, title="proposal — a document, not a call",
                        border_style="yellow"))

    console.print("\n[bold]3. The Gate[/bold]")
    ledger_path = RUNS / f"live-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    ledger = Ledger(ledger_path)
    executor = Executor(backend, ledger, costs)
    gate = Gate(policy, costs, backend.is_settled)
    review = gate.review(proposal, case, now)

    if not review.allowed:
        executor.record_denial(proposal, case, review, now)
        console.print(Panel(
            f"[bold red]DENIED[/bold red]\n\n"
            f"reason codes: {', '.join(d.value for d in review.verdict.denials)}\n"
            f"invariants:   {', '.join(review.verdict.invariant_ids)}\n"
            f"detail:       {review.verdict.detail}\n\n"
            f"[dim]order amount {rupees(case.event.amount_paise)}; "
            f"proposal asked for {rupees(proposal.amount_paise)}[/dim]\n"
            "[dim]no money moved. the refusal is in the ledger.[/dim]",
            border_style="red",
        ))
    else:
        console.print("   [green]ALLOWED[/green] — all eight invariants clear")
        console.print("\n[bold]4. Executing[/bold]")
        outcome = executor.execute(proposal, case, now)
        console.print(f"   {outcome.detail}")
        if outcome.provider_ref:
            console.print(f"   provider ref: [cyan]{outcome.provider_ref}[/cyan]")

    ok, _ = ledger.verify()
    ledger.close()
    console.print(
        f"\n[bold]5. Ledger[/bold]\n"
        f"   {len(ledger)} records, chain "
        f"{'[green]valid[/green]' if ok else '[red]broken[/red]'}\n"
        f"   {ledger_path}\n\n"
        f"[dim]python -m vasool.cli.trace {ledger_path} --case case_live[/dim]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
