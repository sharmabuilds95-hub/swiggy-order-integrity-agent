"""Thin typed wrapper over the Food MCP tool surface (ADR-001 Day 2).

Wraps an initialized `mcp.ClientSession` with one coroutine per Food tool used
on the happy path: address selection -> restaurant search -> menu browse ->
cart build -> cart read. It is deliberately *incomplete*: `place_food_order`
is NOT implemented here. Day 2 is a dry run that stops at the human-confirmation
gate; the order-placement call lands on Day 4, behind a mandatory confirmation
gate per `00-MASTER-PROMPT` §5. Leaving it out means Day-2 code physically
cannot place an order, which is a stronger guarantee than a runtime flag.

Every method returns the tool's parsed JSON payload, taken from the
`CallToolResult.structured_content` when the server populates it, otherwise
parsed from the text content. All response shapes this wrapper is built
against were captured from live calls on 2026-08-19 (see the Day-2 build log),
not inferred from memory.
"""

from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession


class FoodClientError(RuntimeError):
    """A Food tool returned is_error=True, or its result could not be parsed."""


class FoodClient:
    """Typed convenience layer over a live, authenticated Food `ClientSession`."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def _call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self._session.call_tool(name, arguments or {})
        if getattr(result, "is_error", False):
            raise FoodClientError(f"{name} failed: {self._text(result) or '<no message>'}")
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured
        text = self._text(result)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise FoodClientError(f"{name} returned non-JSON content: {text[:200]!r}") from exc

    @staticmethod
    def _text(result: Any) -> str:
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    # --- address -----------------------------------------------------------
    async def get_addresses(self, page: int = 1, page_size: int = 10) -> dict:
        return await self._call("get_addresses", {"page": page, "pageSize": page_size})

    # --- discovery ---------------------------------------------------------
    async def search_restaurants(self, address_id: str, query: str, offset: int = 0) -> dict:
        return await self._call(
            "search_restaurants",
            {"addressId": address_id, "query": query, "offset": offset},
        )

    async def search_menu(
        self,
        address_id: str,
        query: str,
        restaurant_id: str | None = None,
        veg_filter: int | None = None,
        offset: int = 0,
    ) -> dict:
        args: dict[str, Any] = {"addressId": address_id, "query": query, "offset": offset}
        if restaurant_id is not None:
            args["restaurantIdOfAddedItem"] = restaurant_id
        if veg_filter is not None:
            args["vegFilter"] = veg_filter
        return await self._call("search_menu", args)

    async def get_restaurant_menu(
        self, address_id: str, restaurant_id: str, page: int = 1, page_size: int = 5
    ) -> dict:
        return await self._call(
            "get_restaurant_menu",
            {"addressId": address_id, "restaurantId": restaurant_id, "page": page, "pageSize": page_size},
        )

    # --- cart --------------------------------------------------------------
    async def update_food_cart(
        self,
        restaurant_id: str,
        address_id: str,
        cart_items: list[dict[str, Any]],
        restaurant_name: str | None = None,
        cutlery_opt_in: bool | None = None,
    ) -> dict:
        args: dict[str, Any] = {
            "restaurantId": restaurant_id,
            "addressId": address_id,
            "cartItems": cart_items,
        }
        if restaurant_name is not None:
            args["restaurantName"] = restaurant_name
        if cutlery_opt_in is not None:
            args["cutleryOptIn"] = cutlery_opt_in
        return await self._call("update_food_cart", args)

    async def get_food_cart(self, address_id: str, restaurant_name: str | None = None) -> dict:
        args: dict[str, Any] = {"addressId": address_id}
        if restaurant_name is not None:
            args["restaurantName"] = restaurant_name
        return await self._call("get_food_cart", args)

    async def flush_food_cart(self) -> dict:
        return await self._call("flush_food_cart", {})

    # NOTE: place_food_order is intentionally absent for Day 2 (dry run to the
    # gate). It arrives on Day 4 with its human-confirmation gate. See module
    # docstring and ADR-001.
