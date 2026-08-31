"""Razorpay backend over the official MCP server.

This exists to make the project's central claim concrete rather than rhetorical.
``razorpay/razorpay-mcp-server`` exposes ``create_payment_link``,
``create_refund``, ``initiate_payment`` and around forty more tools to whatever
model you point at it, and its entire permission model is one flag:

    READ_ONLY=true   the agent can look at money and change nothing
    READ_ONLY=false  the agent can create links, issue refunds, initiate payments

There is no third setting. So this backend runs the server with writes enabled
and puts the Gate in front of it. The model still never speaks to this module;
the executor does, and only after a verdict.

Requires the server on PATH, or via Docker:
    VASOOL_MCP_CMD="docker run --rm -i -e RAZORPAY_KEY_ID -e RAZORPAY_KEY_SECRET \\
        razorpay/mcp"
"""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Optional

from vasool.core.types import (
    ActionOutcome,
    ActionProposal,
    CaseState,
    Intervention,
)
from vasool.executor.backend import (
    ProviderError,
    ReconciliationUnknown,
    SettlementUnknown,
    UnknownOutcome,
)
from vasool.executor.razorpay_rest import LINK_DESCRIPTION, UNSUPPORTED_LIVE

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_CMD = os.environ.get("VASOOL_MCP_CMD", "razorpay-mcp-server")


class McpStdioClient:
    """A minimal JSON-RPC-over-stdio MCP client. Newline-delimited messages."""

    def __init__(self, command: str = DEFAULT_CMD, timeout: float = 30.0) -> None:
        self.command = command
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._proc is not None:
            return
        env = dict(os.environ)
        env.setdefault("READ_ONLY", "false")
        self._proc = subprocess.Popen(
            shlex.split(self.command),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        # Drain stderr on a daemon thread. An undrained pipe fills its buffer
        # and deadlocks a chatty server — which, combined with a blocking
        # readline, is a permanent hang with no recovery path.
        self._stderr_tail: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "vasool", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip())
            del self._stderr_tail[:-20]

    def _write(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._write({
                "jsonrpc": "2.0", "id": request_id,
                "method": method, "params": params,
            })
            assert self._proc is not None and self._proc.stdout is not None
            # self.timeout used to be stored and never referenced; readline
            # blocked forever. Enforce it for real.
            deadline = time.monotonic() + self.timeout
            while True:
                if time.monotonic() > deadline:
                    raise ProviderError(
                        f"MCP call '{method}' timed out after {self.timeout}s"
                        + (f"; stderr: {self._stderr_tail[-3:]}"
                           if self._stderr_tail else "")
                    )
                if not self._readable(deadline):
                    continue
                line = self._proc.stdout.readline()
                if not line:
                    raise ProviderError("MCP server closed the connection")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue                       # server log noise on stdout
                if message.get("id") != request_id:
                    continue                       # notification or other reply
                if "error" in message:
                    raise ProviderError(f"MCP error: {message['error']}")
                return message.get("result", {})

    def _readable(self, deadline: float) -> bool:
        """Wait for stdout with a deadline, so a silent server cannot hang us."""
        assert self._proc is not None and self._proc.stdout is not None
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([self._proc.stdout], [], [], min(remaining, 1.0))
        return bool(ready)

    def list_tools(self) -> list[str]:
        self.start()
        result = self._call("tools/list", {})
        return [t["name"] for t in result.get("tools", [])]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.start()
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ProviderError(f"{name}: {result.get('content')}")
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    return {"text": block["text"]}
        return result

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc = None


class RazorpayMcpBackend:
    """The same actions as the REST backend, routed through Razorpay's MCP server."""

    name = "razorpay-mcp"

    def __init__(self, client: Optional[McpStdioClient] = None) -> None:
        self.client = client or McpStdioClient()

    def is_settled(self, case: CaseState) -> bool:
        """See RazorpayRestBackend.is_settled — unknown is not unsettled."""
        try:
            order = self.client.call_tool(
                "fetch_order", {"order_id": case.event.order_id})
        except (ProviderError, BrokenPipeError, TimeoutError) as exc:
            raise SettlementUnknown(
                f"cannot read order {case.event.order_id}: {exc}"
            ) from exc
        return order.get("status") == "paid" or int(order.get("amount_paid", 0)) > 0

    def execute(
        self, proposal: ActionProposal, case: CaseState,
        idempotency_key: str, now: datetime,
    ) -> ActionOutcome:
        if proposal.intervention in UNSUPPORTED_LIVE:
            raise ProviderError(
                f"{proposal.intervention.value} is not available through the "
                "MCP toolset on a plain test key"
            )
        if proposal.intervention not in LINK_DESCRIPTION:
            return ActionOutcome(False, False, 0, 0, "no MCP action for this intervention")

        tool = (
            "create_payment_link_upi"
            if case.event.rail.value == "UPI" else "create_payment_link"
        )
        arguments = {
            "amount": proposal.amount_paise,
            "currency": proposal.currency,
            "description": LINK_DESCRIPTION[proposal.intervention],
            "reference_id": idempotency_key,
            "accept_partial": proposal.intervention is Intervention.PART_PAYMENT_LINK,
            "notes": {
                "vasool_case": case.case_id,
                "vasool_intervention": proposal.intervention.value,
                "vasool_diagnosis": proposal.diagnosis.failure_class.value,
            },
        }
        try:
            link = self.client.call_tool(tool, arguments)
        except ProviderError as exc:
            if "reference_id" in str(exc).lower():
                existing = self._find_by_reference(idempotency_key)
                if existing is not None:
                    return self._outcome(existing, replay=True)
            raise
        except (BrokenPipeError, TimeoutError) as exc:
            raise UnknownOutcome(idempotency_key, str(exc)) from exc

        return self._outcome(link, replay=False)

    def reconcile(
        self, idempotency_key: str, case: CaseState, now: datetime,
    ) -> Optional[ActionOutcome]:
        existing = self._find_by_reference(idempotency_key)
        return self._outcome(existing, replay=True) if existing else None

    def _find_by_reference(self, reference_id: str) -> Optional[dict[str, Any]]:
        """Raises rather than guessing — see RazorpayRestBackend._find_by_reference.

        Known limitation, documented rather than papered over: the MCP toolset
        exposes no filtered lookup, so this fetches a page of links and scans.
        On an account past the first page the target may simply be absent from
        the response, which is indistinguishable from "not created". Absence is
        therefore only trustworthy on a small test account, and this raises
        instead of returning None when the page looks full.
        """
        try:
            body = self.client.call_tool("fetch_all_payment_links", {})
        except (ProviderError, BrokenPipeError, TimeoutError) as exc:
            raise ReconciliationUnknown(
                f"cannot list payment links for {reference_id}: {exc}") from exc
        items = body.get("payment_links") or body.get("items") or []
        for item in items:
            if item.get("reference_id") == reference_id:
                return item
        if len(items) >= 100:                      # a full page: absence proves nothing
            raise ReconciliationUnknown(
                f"{reference_id} not in the first page of {len(items)} links; "
                "the MCP toolset offers no filtered lookup")
        return None

    @staticmethod
    def _outcome(link: dict[str, Any], replay: bool) -> ActionOutcome:
        paid = int(link.get("amount_paid", 0))
        status = link.get("status", "created")
        return ActionOutcome(
            executed=True,
            succeeded=status == "paid" and paid > 0,
            collected_paise=paid,
            cost_paise=0,
            detail=f"mcp payment_link {link.get('id')} status={status}"
                   + (" (recovered by reference_id)" if replay else ""),
            provider_ref=link.get("id"),
        )
