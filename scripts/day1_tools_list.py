"""ADR-001 Day 1: OAuth 2.1 + PKCE login against the Swiggy Food server, then tools/list.

Run once, interactively (a browser window opens for phone + OTP login):

    python -m scripts.day1_tools_list

Confirms the Food server's real tool inventory from the agent's own OAuth
client — not the dev-time `mcp-remote` tool this project used earlier to
unblock research — settling that on the record per
`Swiggy-Builders-API-Reference.md` §5a.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

from agent.config import load_settings
from agent.mcp_client import food_session

SNAPSHOT_PATH = Path("tools_snapshot") / "food_tools.json"


async def main() -> None:
    # Tool descriptions contain emoji (e.g. 📄); Windows consoles default to
    # cp1252, which can't encode them and raises instead of substituting.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = load_settings()
    print(f"Connecting to {settings.food_server_url} ...")
    print("If this is a new login, a browser window will open for phone + OTP.")

    async with food_session(settings) as session:
        result = await session.list_tools()
        print(f"\nFood server: {len(result.tools)} tools\n")
        for tool in sorted(result.tools, key=lambda t: t.name):
            print(f"- {tool.name}")

        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        snapshot = [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in result.tools
        ]
        SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull names + descriptions + input schemas written to {SNAPSHOT_PATH}")


if __name__ == "__main__":
    anyio.run(main)
