"""ADR-001 Day 2: thin-client happy path, dry run to the confirmation gate.

Drives the agent's own OAuth-authenticated Food session end to end:

    get_addresses -> search_restaurants -> get_restaurant_menu
        -> update_food_cart -> get_food_cart -> STOP

It stops at the human-confirmation gate: `place_food_order` is never called
(and isn't even implemented in `FoodClient` yet — Day 4's job). This proves
the transport works for every step *except* the irreversible, real-money one.

Run (token from Day 1 is reused headlessly if still valid, ~5-day life):

    python -m scripts.day2_happy_path
    python -m scripts.day2_happy_path --query biryani --address-index 0
    python -m scripts.day2_happy_path --keep-cart      # don't flush at the end

A single sub-<CAP> item is added purely to exercise the cart+pricing path.
By default the cart is flushed again before exit so the real account is left
clean; pass --keep-cart to inspect it in the Swiggy app.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Iterator

import anyio

from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

from agent.config import load_settings
from agent.food_client import FoodClient, FoodClientError
from agent.mcp_client import food_session
from scripts._errgroup import leaves

# Self-imposed safety cap on order value. Swiggy USED to reject placement at
# >= ₹1000 as a beta guardrail, but on 2026-08-21 that restriction was removed
# from place_food_order's live schema ("no value ceiling" — verified against the
# refreshed tools_snapshot). With the platform's guardrail gone, we keep our own
# hard rail so a miscomputed cart can never place a large real-money order. The
# gate below checks the cart against it before the (not-taken) placement step.
ORDER_VALUE_CAP = 400


class HappyPathError(Exception):
    """A recoverable, expected stop (no address, nothing open, etc.).

    Deliberately not a RuntimeError subclass: `sys.exit()` raised from inside
    the anyio task group that `food_session` opens gets wrapped in a
    BaseExceptionGroup and prints a full traceback. Raising a plain exception
    that `main()`'s `except*` unwraps keeps expected stops to one clean line.
    """


def _iter_menu_items(categories: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield every item across categories and their nested subcategories."""
    for category in categories:
        for item in category.get("items", []) or []:
            yield item
        for sub in category.get("subcategories", []) or []:
            for item in sub.get("items", []) or []:
                yield item


def _pick_clean_item(categories: list[dict[str, Any]], max_price: int) -> dict[str, Any] | None:
    """Prefer an in-stock item with no variants and no addons under max_price.

    A fully "clean" item (no variants, no addons) can be added with just
    menu_item_id + quantity, keeping Day 2 free of the variant/addon selection
    logic that belongs to a later day. Fall back to any in-stock, variant-free
    item if nothing is perfectly clean.
    """
    fallback: dict[str, Any] | None = None
    for item in _iter_menu_items(categories):
        if not item.get("inStock"):
            continue
        price = item.get("price")
        if not isinstance(price, (int, float)) or price >= max_price:
            continue
        if item.get("hasVariants"):
            continue
        if fallback is None:
            fallback = item
        if not item.get("hasAddons"):
            return item
    return fallback


def _print_addresses(addresses: list[dict[str, Any]]) -> None:
    print("\nSaved delivery addresses:")
    for i, addr in enumerate(addresses):
        tag = addr.get("addressTag") or addr.get("addressCategory") or ""
        print(f"  [{i}] {tag}: {addr.get('addressLine', '<no line>')}  (id={addr.get('id')})")


def _print_cart_summary(cart: dict[str, Any]) -> float | None:
    data = cart.get("data", {})
    restaurant = data.get("restaurant", {}).get("name", "<unknown restaurant>")
    print(f"\nCart @ {restaurant}:")
    for item in data.get("items", []) or []:
        qty = item.get("quantity")
        name = item.get("name")
        price = item.get("final_price", item.get("subtotal"))
        print(f"  {qty}x {name}  —  ₹{price}")
    pricing = data.get("pricing", {})
    item_total = pricing.get("item_total")
    taxes = pricing.get("taxes_and_charges")
    to_pay = pricing.get("to_pay")
    print(f"  item_total=₹{item_total}  taxes/charges=₹{taxes}  to_pay=₹{to_pay}")
    return to_pay if isinstance(to_pay, (int, float)) else None


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    print(f"Connecting to {settings.food_server_url} (reusing Day-1 token if valid) ...")

    async with food_session(settings) as session:
        food = FoodClient(session)

        # 1) Address
        addr_resp = await food.get_addresses()
        addresses = addr_resp.get("addresses", [])
        if not addresses:
            raise HappyPathError("No saved addresses on this account — add one in the Swiggy app first.")
        _print_addresses(addresses)
        if args.address_id:
            address = next((a for a in addresses if a.get("id") == args.address_id), None)
            if address is None:
                raise HappyPathError(f"No address with id={args.address_id} on this account.")
        else:
            if args.address_index < 0 or args.address_index >= len(addresses):
                raise HappyPathError(
                    f"--address-index {args.address_index} out of range (0..{len(addresses) - 1})."
                )
            address = addresses[args.address_index]
        address_id = address["id"]
        print(f"\n-> Using address id={address_id}")

        # 2) Restaurant
        search = await food.search_restaurants(address_id, args.query)
        restaurants = search.get("restaurants", [])
        open_restaurants = [r for r in restaurants if r.get("availabilityStatus") == "OPEN"]
        if not open_restaurants:
            raise HappyPathError(f"No OPEN restaurants for query {args.query!r} at this address.")
        restaurant = open_restaurants[0]
        restaurant_id = str(restaurant["id"])
        restaurant_name = restaurant.get("name", "")
        print(f"-> Restaurant: {restaurant_name} (id={restaurant_id}, {restaurant.get('distanceKm')}km, {restaurant.get('availabilityStatus')})")

        # 3) Menu -> pick one clean, cheap item
        menu = await food.get_restaurant_menu(address_id, restaurant_id, page=1, page_size=8)
        item = _pick_clean_item(menu.get("categories", []), args.max_price)
        if item is None:
            raise HappyPathError(
                f"No in-stock, variant-free item under ₹{args.max_price} found on page 1 of the menu."
            )
        menu_item_id = str(item["id"])
        print(f"-> Item: {item.get('name')} (id={menu_item_id}, ₹{item.get('price')}, hasAddons={item.get('hasAddons')})")

        # 4) Build cart
        await food.update_food_cart(
            restaurant_id=restaurant_id,
            address_id=address_id,
            cart_items=[{"menu_item_id": menu_item_id, "quantity": 1}],
            restaurant_name=restaurant_name,
        )
        cart = await food.get_food_cart(address_id, restaurant_name=restaurant_name)
        to_pay = _print_cart_summary(cart)

        # 5) The gate — dry run stops here.
        print("\n" + "=" * 56)
        print("HUMAN CONFIRMATION GATE — dry run: place_food_order NOT called")
        if to_pay is None:
            print(f"  Could not read to_pay from cart; ₹{ORDER_VALUE_CAP} self-imposed cap NOT evaluated.")
        elif to_pay < ORDER_VALUE_CAP:
            print(f"  to_pay ₹{to_pay} < ₹{ORDER_VALUE_CAP} self-imposed cap  ->  placement WOULD be allowed.")
        else:
            print(f"  to_pay ₹{to_pay} >= ₹{ORDER_VALUE_CAP} self-imposed cap  ->  placement WOULD be blocked by us (Swiggy no longer caps).")
        print("  Day 4 wires the real placement call in right here, behind explicit confirmation.")
        print("=" * 56)

        # 6) Leave the account clean unless asked not to.
        if args.keep_cart:
            print("\n--keep-cart set: cart left in place (view it in the Swiggy app).")
        else:
            await food.flush_food_cart()
            print("\nCart flushed — account left clean.")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Day 2 happy path: dry run to the confirmation gate.")
    parser.add_argument("--query", default="rolls", help="Restaurant/cuisine search query (default: rolls).")
    parser.add_argument("--address-index", type=int, default=0, help="Index into get_addresses list (default: 0).")
    parser.add_argument("--address-id", default=None, help="Explicit address id (overrides --address-index).")
    parser.add_argument("--max-price", type=int, default=ORDER_VALUE_CAP, help="Max single-item price to add (default: cap).")
    parser.add_argument("--keep-cart", action="store_true", help="Do not flush the cart at the end.")
    args = parser.parse_args()

    # anyio wraps stream failures in an ExceptionGroup; `except*` unwraps them
    # at any depth and also matches a bare exception (same pattern as Day 1).
    try:
        anyio.run(run, args)
    except* (OAuthFlowError, OAuthRegistrationError, OAuthTokenError) as eg:
        for exc in leaves(eg):
            print(f"\nOAuth failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except* FoodClientError as eg:
        for exc in leaves(eg):
            print(f"\nFood tool error: {exc}", file=sys.stderr)
        sys.exit(1)
    except* HappyPathError as eg:
        for exc in leaves(eg):
            print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
    except* RuntimeError as eg:
        for exc in leaves(eg):
            print(f"\nConfig error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
