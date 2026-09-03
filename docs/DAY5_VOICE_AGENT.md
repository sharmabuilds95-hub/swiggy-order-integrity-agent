# Day 5 — Voice Agent Layer (build log + rebuild recipe)

Written 2026-09-03. This document is the complete, no-guesswork record of the
Day 5 work: what was built, why, how, where it ran, and exactly how to recreate
it from scratch on any host. It carries no secrets — every credential is
generated fresh at deploy time (see below).

## 1. Goal

Turn the existing Swiggy order-integrity engine into something an
[ElevenLabs ElevenAgents](https://elevenlabs.io/docs/eleven-agents) voice agent
can call mid-conversation, so a customer can *talk* to it about a delivery
problem and it reconciles the bill, reads the result back, and files a
complaint only after the human confirms.

Why this design: ElevenAgents lets a voice agent trigger real-world actions via
tools, and its MCP integration has a per-tool **approval model** (auto-approve
vs require-approval). The engine already draws exactly that line — read-only
checks vs an irreversible filing behind a confirmation gate — so the two map
cleanly. The point being demonstrated: an AI that takes real actions needs a
human-in-the-loop step before the irreversible one.

## 2. What was built

A new `server/` package that re-exposes the Day 1–4 engine as an MCP **server**
(the CLI agent in `agent/` is an MCP *client* of Swiggy; this is the mirror).
No reconciliation logic was re-implemented — the server wraps `agent.reconcile`
and `agent.evidence` verbatim and adds speakable phrasing.

Files added:

| File | Purpose |
|---|---|
| `server/app.py` | `MCPServer` with three tools, optional bearer gate, host allowlist, uvicorn entrypoint |
| `server/demo_data.py` | Deterministic demo scenarios built from the committed anonymized fixtures + one hand-mutated overcharge |
| `server/__init__.py` | package marker |
| `tests/test_server_tools.py` | 7 tests for the tool behaviour (26 total in the suite) |
| `Procfile`, `railway.json`, `runtime.txt` | deploy config |

Files modified: `requirements.txt` (added `uvicorn`, `starlette`, `httpx`),
`README.md` (Day 5 section), `.gitignore` (added `scratchpad/`, `server_log.txt`).

Commits (branch `main`):
- `445cbf0` Day 5: voice-facing MCP server
- `dfa16d8` Fix auth gate: pure-ASGI bearer wrapper, not BaseHTTPMiddleware
- `b5d68b8` Fix 421 Misdirected Request: allowlist the public Host

## 3. The three tools

| Tool | Effect | ElevenAgents approval |
|---|---|---|
| `list_recent_orders` | read-only | auto-approve |
| `reconcile_order(order_id)` | pure, read-only | auto-approve |
| `file_complaint(order_id)` | irreversible | **require approval** |

Each returns a dict with a `spoken` field (natural language for the agent to
read) plus structured data. In `DEMO_MODE` (default on) `file_complaint` never
reaches live Swiggy — it renders the exact `report_error` arguments it *would*
send, stamped `SIMULATED`. **No order is ever placed, no live report filed.**

Demo scenarios (`server/demo_data.py`), both derived from committed fixtures:
- order `900000000000001` — clean, reconciles to zero discrepancies.
- order `900000000000002` — `actual_order_clean.json` mutated so Paneer Bowl is
  re-priced 300→360 and the order total 505→565. Yields `TOTAL_OVERCHARGE`
  (high/inferred) + `ITEM_PRICE_MISMATCH`.

## 4. The library: mcp 2.0.0 specifics (important — not the classic FastMCP)

This repo pins `mcp==2.0.0`, whose layout differs from the 1.x SDK:
- The high-level server is `from mcp.server import MCPServer` (not
  `mcp.server.fastmcp.FastMCP`). It has the same ergonomics: `@server.tool()`,
  `server.streamable_http_app(...)`, `run_streamable_http_async()`. The
  `@server.tool()` decorator leaves the module-level function directly callable,
  which is why the tests can call `srv.reconcile_order(...)` directly.
- The client `streamable_http_client(url, http_client=...)` yields **two**
  values `(read, write)`, not three. Custom headers go on the `http_client`
  (`create_mcp_http_client(headers=...)`), there is no `headers=` kwarg.
- `streamable_http_app(transport_security=TransportSecuritySettings(...))` is
  where the DNS-rebinding Host allowlist is configured. Default path is `/mcp`.

## 5. Two bugs found live (and the fixes)

1. **Auth broke the stream.** A Starlette `BaseHTTPMiddleware` bearer gate
   buffers the response body, which breaks the streamable-HTTP / SSE response
   the MCP transport returns — `initialize` failed with a server error once a
   token was set. Fix: a pure-ASGI wrapper (`_bearer_gate`) that only reads the
   request headers from the scope and passes the send/receive channels through
   untouched. Never intercept the stream.
2. **421 Misdirected Request behind a proxy.** mcp 2.0.0's StreamableHTTP has
   DNS-rebinding protection that allowlists `Host` headers, defaulting to
   localhost only. Behind a platform proxy the public Host is rejected with 421
   and `initialize` fails. Fix: pass `TransportSecuritySettings(allowed_hosts=…)`
   including the public host, sourced from `PUBLIC_HOST` and (on Railway) the
   auto-injected `RAILWAY_PUBLIC_DOMAIN`. See `_transport_security()`.

## 6. Data-safety note

`scratchpad/` held **real personal data** from live runs — actual order IDs, an
address ID, GPS coordinates, and UPI transaction data. It was untracked and not
ignored, i.e. one `git add .` from landing on the public repo. It is now in
`.gitignore`. Never commit `scratchpad/`.

## 7. Configuration (environment variables)

| Var | Meaning | Value |
|---|---|---|
| `PORT` | port to bind | injected by the host (default 8080 locally) |
| `DEMO_MODE` | `1` = fixtures + SIMULATED filing (never touches Swiggy) | `1` for the public demo |
| `MCP_AUTH_TOKEN` | if set, requires `Authorization: Bearer <token>` | generate fresh (see below); do NOT commit |
| `PUBLIC_HOST` | bare public hostname, to satisfy the Host allowlist | e.g. `myapp.up.railway.app` |

Generate a token:
```bash
python -c "import secrets; print('el11labs_'+secrets.token_urlsafe(24))"
```

## 8. Run locally

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
pytest                                             # 26 pass, offline, no order placed
python -m server.app                               # serves 0.0.0.0:8080, path /mcp
```

Smoke-test it as a voice agent would (Python):
```python
import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

async def main():
    hc = create_mcp_http_client(headers={"Authorization": "Bearer <token-if-set>"})
    async with hc:
        async with streamable_http_client("http://127.0.0.1:8080/mcp", http_client=hc) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                print([t.name for t in (await s.list_tools()).tools])
                res = await s.call_tool("reconcile_order", {"order_id": "900000000000002"})
                print(res.structured_content["spoken"])
anyio.run(main)
```

## 9. Deploy (host-agnostic)

The app is a standard ASGI web service. Any host that runs a Python web process
works. Requirements: Python 3.12, `pip install -r requirements.txt`, start
command `python -m server.app`, and it must listen on `$PORT`.

Set `DEMO_MODE=1`, `MCP_AUTH_TOKEN=<fresh token>`, and `PUBLIC_HOST=<the host's
public domain>`. Then point ElevenAgents at `https://<domain>/mcp` with an
`Authorization: Bearer <token>` header.

**Railway:** New Project → Deploy from the public GitHub repo → add the three
vars → generate a domain → set `PUBLIC_HOST` to that domain. `railway.json` and
`Procfile` are already in the repo. (`RAILWAY_PUBLIC_DOMAIN` is auto-injected, so
`PUBLIC_HOST` is belt-and-suspenders.)

**Render / Fly.io / Koyeb / Deta / any container host:** same three vars, same
start command. On Render use a Web Service, build `pip install -r
requirements.txt`, start `python -m server.app`. On Fly, a minimal Dockerfile
(`python:3.12-slim`, copy, pip install, `CMD ["python","-m","server.app"]`) and
`fly launch`; set `PUBLIC_HOST` to the `.fly.dev` domain.

## 10. ElevenAgents dashboard wiring

1. Create an agent (blank).
2. Tools → add a custom MCP server: URL `https://<domain>/mcp`, header
   `Authorization: Bearer <token>`. Test the connection; it lists the 3 tools.
3. Approval mode → Fine-Grained: `list_recent_orders` + `reconcile_order`
   auto-approve, `file_complaint` require approval.
4. System prompt: instruct it to list orders, reconcile the chosen one, read it
   back plainly, and only call `file_complaint` on explicit confirmation; and to
   say a clean reconciliation means the bill matched, not that the food was
   correct.
5. Voice-test with "I think my dinner order was overcharged" → it should find
   the ₹60 overcharge on order …002 and ask before filing.
