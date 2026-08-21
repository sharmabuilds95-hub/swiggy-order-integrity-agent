# Swiggy Order Integrity Agent

Post-delivery order-integrity agent built on [Swiggy Builders Club](https://mcp.swiggy.com/builders/)'s Food MCP server: captures a structured record of what was confirmed at checkout, reconciles it against what actually arrived, and — with explicit user confirmation — files a well-evidenced complaint via Swiggy's `report_error` tool when something's wrong.

**Status: pre-build.** v1 scope, day-by-day plan, and the reasoning behind the cut live in a private project vault, not in this repo (this repo is the code artifact; the vault has the research and decision history). Summary: Food server only, post-delivery reconciliation only, no pre-order dietary checks, no Instamart/Dineout, sandbox/local only for now.

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

Full platform reference and architecture decisions: private project vault.
