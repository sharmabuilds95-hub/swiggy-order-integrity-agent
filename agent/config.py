"""Environment configuration for the Swiggy order-integrity agent.

Loads from `.env` (see `.env.example`); never commit `.env` itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    mcp_base_url: str
    food_server_url: str
    redirect_uri: str

    @property
    def redirect_host(self) -> str:
        return urlparse(self.redirect_uri).hostname or "localhost"

    @property
    def redirect_port(self) -> int:
        return urlparse(self.redirect_uri).port or 8765

    @property
    def redirect_path(self) -> str:
        return urlparse(self.redirect_uri).path or "/callback"


def load_settings() -> Settings:
    def _require(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"{name} is not set — copy .env.example to .env and fill it in")
        return value

    return Settings(
        mcp_base_url=_require("SWIGGY_MCP_BASE_URL"),
        food_server_url=_require("SWIGGY_FOOD_SERVER_URL"),
        redirect_uri=_require("SWIGGY_REDIRECT_URI"),
    )
