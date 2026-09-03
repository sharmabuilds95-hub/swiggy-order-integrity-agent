"""Deterministic demo scenarios for the voice-facing MCP server (Day 5).

The voice agent must reconcile the SAME real-shaped payloads the CLI agent
works on, but a live demo cannot depend on a Swiggy OAuth round-trip mid-call
(fragile, and it would place the agent one slip away from a real-money action
on camera). So this module serves the two anonymized fixtures the test suite
already trusts, plus one hand-mutated overcharge built from them, entirely from
memory. No network, no clock, no order ever placed.

Every actual-order payload here is either an existing committed fixture or a
pure mutation of one. The overcharge is the demo's whole point: it gives the
voice agent a real, speakable billing discrepancy to find and read back.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


# The cart the user confirmed at the gate. Both demo orders were placed from
# this same agreement, so it is the single expectation basis for both.
CART_AT_GATE: dict[str, Any] = _load("cart_clean.json")
CAPTURED_AT = "2026-08-20T13:10:00+05:30"


def _overcharged_order() -> dict[str, Any]:
    """cart_clean's twin, mutated: Paneer Bowl re-priced 300 -> 360, and the
    order total dragged 505 -> 565 to match. Yields one ITEM_PRICE_MISMATCH
    (high/inferred) and one TOTAL_OVERCHARGE."""
    order = copy.deepcopy(_load("actual_order_clean.json"))
    order["orderId"] = "900000000000002"
    order["orderTotal"] = "₹565"
    for item in order["items"]:
        if item["itemId"] == "1002":  # Paneer Bowl x2
            item["subtotal"] = "360"
            item["total"] = "360"
    order["orderedTime"] = "August 20, 8:40 PM"
    return order


# order_id -> (actual order payload, one-line human label for list_recent_orders)
ORDERS: dict[str, tuple[dict[str, Any], str]] = {
    "900000000000001": (_load("actual_order_clean.json"), "Test Kitchen, Aug 20 1:15 PM, paid 505"),
    "900000000000002": (_overcharged_order(), "Test Kitchen, Aug 20 8:40 PM, paid 565"),
}
