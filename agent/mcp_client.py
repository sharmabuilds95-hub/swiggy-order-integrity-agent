"""OAuth-authenticated MCP client session against a Swiggy server.

Wires the mcp SDK's `OAuthClientProvider` (PKCE + Dynamic Client
Registration — both confirmed live for Swiggy in
`Swiggy-Builders-API-Reference.md` §4) to a local loopback callback and a
Streamable HTTP transport. Per ADR-001 Day 1, this is the one piece
everything else in the build depends on.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import OAuthClientMetadata

from agent.config import Settings
from agent.oauth_callback import LoopbackOAuthFlow
from agent.token_storage import FileTokenStorage

TOKEN_DIR = Path(".mcp-auth")

# Server-level scope model per Swiggy's docs (not read/write-split) — request
# all three since the Food server's tool/resource/prompt surfaces aren't
# separately scoped. Assumption, not yet verified against a real token
# response; revisit if the authorization server rejects or narrows this.
DEFAULT_SCOPE = "mcp:tools mcp:resources mcp:prompts"


def _build_oauth_provider(settings: Settings, server_url: str, storage_filename: str) -> OAuthClientProvider:
    flow = LoopbackOAuthFlow(
        host=settings.redirect_host,
        port=settings.redirect_port,
        callback_path=settings.redirect_path,
    )
    client_metadata = OAuthClientMetadata(
        redirect_uris=[settings.redirect_uri],
        client_name="Swiggy Order Integrity Agent (dev)",
        scope=DEFAULT_SCOPE,
    )
    storage = FileTokenStorage(TOKEN_DIR / storage_filename)
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=flow.redirect,
        callback_handler=flow.wait_for_callback,
    )


@asynccontextmanager
async def food_session(settings: Settings) -> AsyncIterator[ClientSession]:
    """Yield an initialized ClientSession authenticated against the Food server."""
    oauth_provider = _build_oauth_provider(settings, settings.food_server_url, "food-tokens.json")
    http_client = create_mcp_http_client(auth=oauth_provider)
    async with http_client:
        async with streamable_http_client(settings.food_server_url, http_client=http_client) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
