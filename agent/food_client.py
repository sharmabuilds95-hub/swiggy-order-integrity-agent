"""Thin typed wrapper over the Food MCP tool surface (ADR-001 Day 2 + Day 4).

Wraps an initialized `mcp.ClientSession` with one coroutine per Food tool the
build uses: address selection -> restaurant search -> menu browse -> cart build
-> cart read (Day 2), and the placement/payment/confirm + `report_error` surface
(Day 4). The Day-4 methods (`place_food_order`, `get_payment_options`,
`confirm_order`, `check_payment_status`, `report_error`) are the irreversible,
real-money / outward-facing calls: this wrapper only *issues* them — the
mandatory human-confirmation gate (`00-MASTER-PROMPT` §5) lives one layer up in
`scripts/day4_resolution.py`, never inside a wrapper method.

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

    # --- order history (read; Day 3/4 reconciliation source) ---------------
    async def get_food_orders(self, address_id: str) -> dict:
        return await self._call("get_food_orders", {"addressId": address_id})

    # --- payment / placement / confirm (Day 4) -----------------------------
    # These are the irreversible, real-money surface. This wrapper only issues
    # the call; the mandatory human-confirmation gate lives one layer up in the
    # Day-4 script (00-MASTER-PROMPT §5). A UPI-eligible account must pass a
    # paymentMethod; "Cash" (COD) is the credential-free path this project uses.
    async def get_payment_options(self, address_id: str) -> dict:
        return await self._call("get_payment_options", {"addressId": address_id})

    async def place_food_order(
        self,
        address_id: str,
        payment_method: str,
        *,
        intent_app: str | None = None,
        generate_upi_qr: bool | None = None,
        note_to_restaurant: str | None = None,
    ) -> dict:
        """Place the order. IRREVERSIBLE — only call after explicit confirmation.

        Returns the raw placement payload. For Cash the order is placed directly;
        for UPI the response carries status="PENDING_PAYMENT" + paasId and the
        caller must not claim success until check_payment_status -> confirm_order.
        """
        args: dict[str, Any] = {"addressId": address_id, "paymentMethod": payment_method}
        if intent_app is not None:
            args["intentApp"] = intent_app
        if generate_upi_qr is not None:
            args["generateUPIQR"] = generate_upi_qr
        if note_to_restaurant is not None:
            args["noteToRestaurant"] = note_to_restaurant
        return await self._call("place_food_order", args)

    async def check_payment_status(self, paas_id: str, **echo: Any) -> dict:
        return await self._call("check_payment_status", {"paasId": paas_id, **echo})

    async def confirm_order(self, order_id: str, **echo: Any) -> dict:
        return await self._call("confirm_order", {"orderId": order_id, **echo})

    # --- resolution / complaint (Day 4) ------------------------------------
    async def report_error(
        self,
        tool: str,
        error_message: str,
        *,
        domain: str | None = None,
        flow_description: str | None = None,
        tool_context: dict[str, Any] | None = None,
        user_notes: str | None = None,
    ) -> dict:
        """File an order-integrity report. Returns a pre-filled mailto (vault §5b).

        Outward-facing (logs server-side); only call after explicit user
        confirmation, and never for a simulated discrepancy.
        """
        args: dict[str, Any] = {"tool": tool, "errorMessage": error_message}
        if domain is not None:
            args["domain"] = domain
        if flow_description is not None:
            args["flowDescription"] = flow_description
        if tool_context is not None:
            args["toolContext"] = tool_context
        if user_notes is not None:
            args["userNotes"] = user_notes
        return await self._call("report_error", args)
