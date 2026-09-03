"""Voice-facing MCP server: the order-integrity engine, exposed as tools an
ElevenAgents voice agent can call mid-conversation (ADR-001 Day 5).

The CLI agent in `agent/` is an MCP *client* of Swiggy's Food server. This is
the mirror image: an MCP *server* that re-exposes the same pure reconciliation
engine so a conversational agent (ElevenAgents over streamable HTTP) can be the
caller. Nothing here re-implements reconciliation — it wraps
`agent.reconcile` / `agent.evidence` verbatim.

Three tools, split along the exact line ElevenAgents' own approval model draws:

  list_recent_orders   read-only   -> auto-approve
  reconcile_order      pure, read  -> auto-approve
  file_complaint       irreversible-> REQUIRES APPROVAL (fine-grained tool
                                      approval in the ElevenAgents dashboard)

The confirmation gate is not enforced in this code by design: on the voice
platform the gate IS the platform's per-tool approval setting, which is the
whole point being demonstrated. In demo mode `file_complaint` never reaches
live Swiggy — it renders the exact `report_error` arguments it *would* send,
stamped SIMULATED, so the human-in-the-loop step is real while the side effect
is not.

Run locally:   python -m server.app
Serves streamable HTTP on 0.0.0.0:$PORT (default 8080), path /mcp.
Set MCP_AUTH_TOKEN to require `Authorization: Bearer <token>` (recommended once
the URL is public); leave it unset for a quick local demo.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server import MCPServer

from agent.evidence import build_evidence_packet, to_report_error_args
from agent.expectation import build_expectation
from agent.reconcile import normalize_actual_order, reconcile
from server.demo_data import CART_AT_GATE, CAPTURED_AT, ORDERS

DEMO_MODE = os.environ.get("DEMO_MODE", "1") != "0"

server: MCPServer = MCPServer(
    name="Order Integrity",
    instructions=(
        "Post-delivery food-order integrity. Use list_recent_orders to see the "
        "caller's recent orders, reconcile_order to check one order's bill "
        "against what was agreed at checkout, and file_complaint ONLY after the "
        "caller explicitly confirms they want a complaint filed. A clean "
        "reconciliation means the bill matched the agreement, not that the food "
        "was physically correct."
    ),
    version="0.5.0",
)


def _expectation():
    return build_expectation(CART_AT_GATE, captured_at=CAPTURED_AT)


@server.tool()
def list_recent_orders() -> dict[str, Any]:
    """List the caller's recent food orders. Read-only; safe to auto-approve.

    Returns a short list of {order_id, summary} the caller can pick from. Read
    this back naturally, e.g. 'I can see two recent orders...'.
    """
    orders = [{"order_id": oid, "summary": label} for oid, (_o, label) in ORDERS.items()]
    spoken = "I can see " + _count(len(orders)) + " recent order" + ("s" if len(orders) != 1 else "") + ": " + \
        "; ".join(f"{o['summary']}" for o in orders) + "."
    return {"spoken": spoken, "orders": orders, "demo_mode": DEMO_MODE}


@server.tool()
def reconcile_order(order_id: str) -> dict[str, Any]:
    """Reconcile one order's bill against the checkout agreement. Read-only; pure.

    Returns a spoken summary plus the structured discrepancies, each carrying a
    confidence label ('high' = item identity/quantity, 'inferred' = a price
    basis not yet cross-checked against a live placed order). Auto-approve.
    """
    entry = ORDERS.get(order_id)
    if entry is None:
        return {"spoken": f"I couldn't find an order with id {order_id}.", "found": False}

    actual_raw, _label = entry
    discrepancies = reconcile(_expectation(), actual_raw)

    if not discrepancies:
        return {
            "spoken": (
                "The bill matches what was agreed at checkout, so there is nothing to "
                "dispute on charges. Note this only checks the bill, not whether the "
                "food itself was correct."
            ),
            "found": True,
            "clean": True,
            "discrepancies": [],
        }

    items = [d.to_dict() for d in discrepancies]
    top = discrepancies[0]
    spoken = (
        f"I found {_count(len(discrepancies))} issue"
        + ("s" if len(discrepancies) != 1 else "")
        + f" on this order. The main one: {top.message} "
        + f"That is {top.confidence}-confidence. Would you like me to file a complaint?"
    )
    return {"spoken": spoken, "found": True, "clean": False, "discrepancies": items}


@server.tool()
def file_complaint(order_id: str) -> dict[str, Any]:
    """File a well-evidenced complaint for an order. IRREVERSIBLE — set this tool
    to 'require approval' in ElevenAgents so the caller confirms before it runs.

    Reconciles the order, packages the evidence, and (in demo mode) renders the
    exact report_error arguments it would send to Swiggy, stamped SIMULATED. No
    live report is filed in demo mode and no order is ever placed.
    """
    entry = ORDERS.get(order_id)
    if entry is None:
        return {"spoken": f"I couldn't find an order with id {order_id}.", "filed": False}

    expected = _expectation()
    actual_raw, _label = entry
    actual = normalize_actual_order(actual_raw)
    discrepancies = reconcile(expected, actual)

    if not discrepancies:
        return {
            "spoken": "There is no billing discrepancy on this order, so I won't file a complaint.",
            "filed": False,
        }

    packet = build_evidence_packet(expected, actual, discrepancies, simulated=DEMO_MODE)
    args = to_report_error_args(packet)

    spoken = (
        "Done. " + ("In demo mode I did not send a live report, but here is exactly what "
        "would be filed: " if DEMO_MODE else "Complaint filed: ") + packet.headline
    )
    return {
        "spoken": spoken,
        "filed": True,
        "simulated": DEMO_MODE,
        "evidence": packet.to_dict(),
        "report_error_args": args,
    }


def _count(n: int) -> str:
    return {0: "no", 1: "one", 2: "two", 3: "three"}.get(n, str(n))


def _bearer_gate(inner_app, token: str):
    """Pure-ASGI bearer gate wrapping the streamable-HTTP app.

    Deliberately NOT a Starlette BaseHTTPMiddleware: that buffers the response
    body, which breaks the streamable-HTTP / SSE response the MCP transport
    returns. This wrapper only reads the request headers from the ASGI scope
    and either short-circuits with 401 or hands the untouched send/receive
    channels straight through, so the stream is never intercepted.
    """
    expected = f"Bearer {token}".encode()

    async def gate(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization") != expected:
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await inner_app(scope, receive, send)

    return gate


def _transport_security():
    """Allow the public host we are served under through the SDK's DNS-rebinding
    guard. That guard allowlists Host headers and defaults to localhost only, so
    behind a platform proxy (Railway) the public Host is rejected with 421 unless
    we add it here. PUBLIC_HOST is the bare hostname, e.g.
    'mcp-server-production-e5e3.up.railway.app'.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["127.0.0.1", "localhost"]
    public = os.environ.get("PUBLIC_HOST", "").strip()
    if public:
        hosts += [public, f"{public}:443", f"{public}:80"]
    # RAILWAY_PUBLIC_DOMAIN is injected automatically by Railway.
    railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway and railway not in hosts:
        hosts += [railway, f"{railway}:443"]
    return TransportSecuritySettings(allowed_hosts=hosts)


def _build_app():
    """Return the streamable-HTTP ASGI app, wrapped with an optional bearer gate."""
    app = server.streamable_http_app(transport_security=_transport_security())
    token = os.environ.get("MCP_AUTH_TOKEN")
    if not token:
        return app
    return _bearer_gate(app, token)


app = _build_app()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
