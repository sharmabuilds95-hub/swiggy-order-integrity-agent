# Swiggy Order Integrity Agent

Post-delivery order-integrity agent built on [Swiggy Builders Club](https://mcp.swiggy.com/builders/)'s Food MCP server: captures a structured record of what was confirmed at checkout, reconciles it against what actually arrived, and — with explicit user confirmation — files a well-evidenced complaint via Swiggy's `report_error` tool when something's wrong.

**Status: Day 5.** Days 1-4 built the CLI agent (its own OAuth 2.1 + PKCE client, a pure reconciliation engine, evidence packaging, and a gated real-order path). Day 5 adds a **voice-facing MCP server** (`server/`) that re-exposes the same reconciliation engine as tools a conversational agent can call. v1 scope, day-by-day plan, and the reasoning behind the cut live in a private project vault, not in this repo. Summary: Food server only, post-delivery reconciliation only, no pre-order dietary checks, no Instamart/Dineout.

## Why this exists

Every public Builders Club project so far handles order *placement*. None handle what happens when an order goes wrong — despite Swiggy's own complaint-resolution data showing that's a real, common failure. This is that other half.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
cp .env.example .env         # fill in values once OAuth is wired up — never commit .env
```

OAuth is Swiggy's standard OAuth 2.1 + PKCE with Dynamic Client Registration — no pre-registered client ID needed. See `.mcp.json` for the dev-time MCP client config used to verify live tool schemas before writing the Python client.

## Hard constraints this project respects

- Never places an order without explicit user confirmation.
- Self-imposed ₹400 cap on any real order this project places. Swiggy previously enforced a ₹1000 beta cap on `place_food_order`, but removed it on 2026-08-21 ("no value ceiling" in the live tool schema); with the platform guardrail gone, we keep our own hard rail so a miscomputed cart can't place a large real-money order.
- Non-idempotent order-placement calls are never blind-retried — status is checked before any retry.
- No PII stored beyond session needs.

## Voice agent (Day 5): the same engine, exposed as an MCP server

The CLI agent in `agent/` is an MCP *client* of Swiggy's Food server. `server/` is the mirror image: an MCP *server* that re-exposes the same pure reconciliation engine so a conversational agent — an [ElevenAgents](https://elevenlabs.io/docs/eleven-agents) voice agent over streamable HTTP — can be the caller. A customer talks to the voice agent about a delivery problem; the agent calls these tools mid-conversation and reads the result back in natural speech.

Nothing in `server/` re-implements reconciliation. It wraps `agent.reconcile` and `agent.evidence` verbatim and adds a speakable summary. Three tools, split along the exact line ElevenAgents' own approval model draws:

| Tool | Effect | ElevenAgents approval setting |
|---|---|---|
| `list_recent_orders` | read-only | auto-approve |
| `reconcile_order` | pure, read-only | auto-approve |
| `file_complaint` | irreversible | **require approval** |

The human-in-the-loop gate is not hard-coded here by design: on the voice platform the gate *is* the platform's per-tool approval setting, which is the point being demonstrated — an AI that takes real-world actions needs a confirmation step before the irreversible one fires. In demo mode `file_complaint` never reaches live Swiggy; it renders the exact `report_error` arguments it *would* send, stamped `SIMULATED`, so the confirmation step is real while the side effect is not. No order is ever placed.

```bash
# run the MCP server locally
python -m server.app            # serves streamable HTTP on 0.0.0.0:$PORT (default 8080), path /mcp
```

Set `MCP_AUTH_TOKEN` to require `Authorization: Bearer <token>` once the URL is public; leave it unset for a quick local demo. `DEMO_MODE=0` would switch to the live path (not used for the public demo). Deploys to Railway from `railway.json` / `Procfile`.

## Tests

```bash
pytest            # 26 tests, all pure and offline — no order placed, no network
```

Full platform reference and architecture decisions: private project vault.
