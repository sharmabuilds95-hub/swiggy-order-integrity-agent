"""Local persistence for OAuth client registration + tokens, per MCP server.

Implements the `mcp.client.auth.TokenStorage` protocol (structurally — no
inheritance needed). Backed by a single JSON file per server under
`.mcp-auth/` (gitignored), so re-running the agent within a token's 5-day
lifetime doesn't force a fresh phone+OTP login every time.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


class FileTokenStorage:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)
