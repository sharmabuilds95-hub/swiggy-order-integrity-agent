"""ADR-001 Day 4: the resolution path, behind mandatory confirmation gates.

Two modes, each stopping at a hard gate before anything irreversible:

  --simulate  (default)  Resolution demo on a REAL past order. Pull order
              history, reconcile it against a synthesized expectation into which
              ONE clearly-labelled *simulated* discrepancy is injected, assemble
              the evidence packet, and PREVIEW the report_error filing. Never
              calls report_error — filing a simulated complaint to Swiggy would
              be dishonest (ADR-001's demo-path rule: label it, never action it).
              Free reads only; nothing placed, nothing filed.

  --place     Drive a real cart (one clean item under the self-imposed ₹400 cap)
              to the placement confirmation gate and STOP. Without --confirm it
              behaves like Day 2 (dry run + flush). With --confirm it places ONE
              real order (COD) — real food, real money — after echoing exactly
              what will happen. --confirm must be passed deliberately; the agent
              never passes it without an explicit human go in the moment.

Run:
    python -m scripts.day4_resolution --simulate
    python -m scripts.day4_resolution --place                 # dry run to gate
    python -m scripts.day4_resolution --place --confirm        # REAL order (COD)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any

import anyio

from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

from agent.config import load_settings
from agent.evidence import build_evidence_packet, to_report_error_args
from agent.expectation import ExpectationRecord, ExpectedItem
from agent.food_client import FoodClient, FoodClientError
from agent.mcp_client import food_session
from agent.reconcile import ActualOrder, normalize_actual_order, reconcile
from scripts._errgroup import leaves
from scripts.day2_happy_path import (
    ORDER_VALUE_CAP,
    HappyPathError,
    _iter_menu_items,
    _print_addresses,
    _print_cart_summary,
)


def _pick_cheapest_clean_item(categories: list, max_price: int) -> dict | None:
    """Cheapest in-stock, variant-free item under max_price (prefer no addons).

    Unlike day2's first-match picker, this minimizes item price to give the
    lowest possible cart total for a deliberately tiny real order.
    """
    best: dict | None = None
    for item in _iter_menu_items(categories):
        if not item.get("inStock") or item.get("hasVariants"):
            continue
        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0 or price >= max_price:
            continue
        if best is None or price < best.get("price", 1e9) or (
            price == best.get("price") and not item.get("hasAddons") and best.get("hasAddons")
        ):
            best = item
    return best

CAPTURED_AT_DEMO = "2026-08-21T12:00:00+05:30"
SIM_OVERCHARGE = 40.0  # rupees; the labelled simulated overcharge for --simulate

# UPI payment status polling (check_payment_status long-polls ~19s/call, so a
# handful of iterations covers a few minutes; a headless client must loop).
UPI_POLL_MAX_ATTEMPTS = 15
UPI_TERMINAL_OK = {"SUCCESS", "PAID", "COMPLETED"}
UPI_TERMINAL_FAIL = {"FAILED", "CANCELLED", "EXPIRED", "DECLINED"}


def _first_present(obj: Any, keys: tuple[str, ...]) -> Any:
    """Depth-first search a nested payload for the first of `keys` (case-insensitive).

    The PENDING_PAYMENT response echoes identifiers (paasId, orderId, cartId,
    lat, lng) that confirm_order / check_payment_status need, but the exact
    nesting isn't known until a real placement — so we search rather than assume
    a path. Returns the value, or None.
    """
    wanted = {k.lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in wanted and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            found = _first_present(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _first_present(v, keys)
            if found is not None:
                return found
    return None


def _summarize_payment_options(po: dict) -> tuple[bool, bool, list[str]]:
    """Return (upi_available, cod_available, upi_app_names) from get_payment_options."""
    cod = bool((po.get("cod") or {}).get("available"))
    upi_apps: list[str] = []
    for m in po.get("allMethods", []) or []:
        if str(m.get("groupName", "")).upper() == "UPI" and m.get("enabled"):
            upi_apps.append(str(m.get("displayName", m.get("id", "?"))))
    desktop = ((po.get("platforms") or {}).get("desktop") or {}).get("methods", []) or []
    upi_available = bool(upi_apps) or any(m.get("kind") == "qr" for m in desktop)
    return upi_available, cod, upi_apps


def _expectation_from_actual(actual: ActualOrder, captured_at: str) -> ExpectationRecord:
    """Synthesize a matching expectation from a past order (baseline: clean).

    We never captured the original cart for a historical order, so for the demo
    we reconstruct what an at-gate expectation *would* have been (item list,
    per-line list price = order subtotal, to_pay = order total). Reconciling this
    against the same order yields zero discrepancies — a clean baseline to then
    perturb with one labelled simulated fault.
    """
    items = tuple(
        ExpectedItem(
            item_id=it.item_id,
            name=it.name,
            quantity=it.quantity,
            line_final_price=it.subtotal,
            subtotal=it.subtotal,
            total=it.total,
        )
        for it in actual.items
    )
    return ExpectationRecord(
        captured_at=captured_at,
        restaurant_name=actual.restaurant_name,
        items=items,
        to_pay=actual.order_total,
    )


async def run_simulate(food: FoodClient, address_id: str) -> None:
    resp = await food.get_food_orders(address_id)
    orders = resp.get("orders", []) or []
    delivered = [o for o in orders if str(o.get("orderDeliveryStatus", "")).lower() == "delivered"]
    if not delivered:
        raise HappyPathError("No delivered orders in history to run the simulation against.")
    actual = normalize_actual_order(delivered[0])
    print(f"Using REAL past order {actual.order_id} @ {actual.restaurant_name} "
          f"(total ₹{actual.order_total}, {len(actual.items)} items).")

    # Baseline expectation (reconciles clean), then inject ONE simulated fault:
    # pretend we agreed to pay SIM_OVERCHARGE less -> a simulated overcharge.
    baseline = _expectation_from_actual(actual, CAPTURED_AT_DEMO)
    if baseline.to_pay is None:
        raise HappyPathError("Order total unreadable; cannot run the simulated-overcharge demo.")
    expected = dataclasses.replace(baseline, to_pay=baseline.to_pay - SIM_OVERCHARGE)

    discrepancies = reconcile(expected, actual)
    packet = build_evidence_packet(expected, actual, discrepancies, simulated=True)

    print("\n" + "=" * 60)
    print("SIMULATED DISCREPANCY — demo only, NOT a real defect on this order")
    print("=" * 60)
    print(f"Injected: agreed ₹{expected.to_pay} vs recorded ₹{actual.order_total} "
          f"(simulated ₹{SIM_OVERCHARGE} overcharge)")
    print(f"\nHeadline: {packet.headline}")
    print("Evidence:")
    for line in packet.lines:
        print(f"  {line}")

    print("\n--- report_error PREVIEW (NOT sent — simulated) ---")
    print(json.dumps(to_report_error_args(packet), ensure_ascii=False, indent=1))
    print("\nGATE: a real discrepancy would file this via report_error only after")
    print("explicit user confirmation. Simulated packets are never filed.")


async def run_place(food: FoodClient, address_id: str, args: argparse.Namespace) -> None:
    search = await food.search_restaurants(address_id, args.query)
    open_r = [r for r in search.get("restaurants", []) if r.get("availabilityStatus") == "OPEN"]
    if not open_r:
        raise HappyPathError(f"No OPEN restaurants for query {args.query!r}.")
    restaurant = open_r[0]
    rid, rname = str(restaurant["id"]), restaurant.get("name", "")
    print(f"-> Restaurant: {rname} (id={rid})")

    menu = await food.get_restaurant_menu(address_id, rid, page=1, page_size=8)
    item = _pick_cheapest_clean_item(menu.get("categories", []), ORDER_VALUE_CAP)
    if item is None:
        raise HappyPathError(f"No clean in-stock item under ₹{ORDER_VALUE_CAP} on page 1.")
    print(f"-> Item (cheapest clean): {item.get('name')} (₹{item.get('price')})")

    # Effective budget: the hard ₹400 rail, tightened by an optional --max-to-pay.
    budget = ORDER_VALUE_CAP if args.max_to_pay is None else min(ORDER_VALUE_CAP, args.max_to_pay)

    await food.update_food_cart(
        restaurant_id=rid, address_id=address_id,
        cart_items=[{"menu_item_id": str(item["id"]), "quantity": 1}], restaurant_name=rname,
    )
    cart = await food.get_food_cart(address_id, restaurant_name=rname)
    to_pay = _print_cart_summary(cart)

    if to_pay is None or to_pay >= budget:
        # Never place above the effective budget (hard ₹400 rail or tighter).
        await food.flush_food_cart()
        raise HappyPathError(
            f"Cheapest cart to_pay ₹{to_pay} is not below the ₹{budget} budget "
            f"(item ₹{item.get('price')} + taxes/delivery/fees) — refusing to place. "
            f"Cart flushed. Relax --max-to-pay or accept a higher total to proceed."
        )

    payment = await food.get_payment_options(address_id)
    upi_ok, cod_ok, upi_apps = _summarize_payment_options(payment)
    method = "UPI" if args.pay == "upi" else "Cash on Delivery"

    print("\n" + "=" * 60)
    print("HUMAN CONFIRMATION GATE — real order placement")
    print(f"  Restaurant: {rname}")
    print(f"  To pay:     ₹{to_pay}  (< ₹{budget} budget OK; hard rail ₹{ORDER_VALUE_CAP})")
    print(f"  Address:    {address_id}")
    print(f"  Payment:    {method}")
    if args.pay == "upi":
        print(f"  UPI apps available: {', '.join(upi_apps) or '(none listed)'}")
        print(f"  Desktop flow: agent requests a QR; you scan it with your phone's UPI app.")
    print("=" * 60)

    if args.pay == "upi" and not upi_ok:
        await food.flush_food_cart()
        raise HappyPathError("UPI not available for this cart/account per get_payment_options. Cart flushed.")
    if args.pay == "cod" and not cod_ok:
        await food.flush_food_cart()
        raise HappyPathError("COD not available for this cart/account per get_payment_options. Cart flushed.")

    if not args.confirm:
        await food.flush_food_cart()
        print("\nDry run (no --confirm): cart flushed, nothing placed.")
        return

    if args.pay == "upi":
        await _place_upi(food, address_id, args)
    else:
        await _place_cod(food, address_id, args)


async def _place_cod(food: FoodClient, address_id: str, args: argparse.Namespace) -> None:
    """Place a real COD order. IRREVERSIBLE — reached only past the gate + --confirm."""
    print("\n--confirm set: placing REAL order (Cash on Delivery) ...")
    result = await food.place_food_order(address_id, "Cash", note_to_restaurant=args.note)
    status = str(result.get("status", "")).upper()
    print(f"place_food_order returned status={status or '<none>'}")
    print(json.dumps(result, ensure_ascii=False, indent=1)[:1500])
    if status == "PENDING_PAYMENT":
        print("\nUnexpected PENDING_PAYMENT for COD — order NOT placed. Inspect the payload above.")
    else:
        print("\nCOD order placed. Pay cash on delivery.")


async def _place_upi(food: FoodClient, address_id: str, args: argparse.Namespace) -> None:
    """Place a real UPI order via the desktop-QR path, then poll + confirm.

    Flow (per place_food_order's live schema): place with generateUPIQR -> the
    response is PENDING_PAYMENT and carries a QR + paasId + echo ids. The order
    is NOT placed until check_payment_status reports SUCCESS and confirm_order
    then succeeds — this function never claims success before that. NOTE: the
    post-PENDING_PAYMENT steps are coded against the captured schema and are
    exercised for the first time by a real order; the echo-field extraction is
    defensive (`_first_present`) for exactly that reason.
    """
    print("\n--confirm set: placing REAL order (UPI, desktop QR) ...")
    result = await food.place_food_order(address_id, "UPI", generate_upi_qr=True, note_to_restaurant=args.note)
    status = str(result.get("status", "")).upper()
    print(f"place_food_order returned status={status or '<none>'}")

    if status != "PENDING_PAYMENT":
        print(json.dumps(result, ensure_ascii=False, indent=1)[:1500])
        print("\nExpected PENDING_PAYMENT for UPI but did not get it — NOT claiming placement.")
        return

    paas_id = _first_present(result, ("paasId", "paas_id"))
    order_id = _first_present(result, ("orderId", "order_id"))
    cart_id = _first_present(result, ("cartId", "cart_id"))
    lat = _first_present(result, ("lat", "latitude"))
    lng = _first_present(result, ("lng", "longitude"))

    # Surface the QR / UPI link for the user to pay with their phone.
    qr = _first_present(result, ("qr", "qrString", "qrCode", "upiUri", "intentUrl", "paymentLink", "link"))
    print("\nPENDING_PAYMENT — order NOT placed yet. Pay in your UPI app:")
    print(f"  QR/link: {qr if qr else '(not found in top-level fields — see raw payload below)'}")
    if not qr:
        print(json.dumps(result, ensure_ascii=False, indent=1)[:2000])
    print("  (Fallback: the pending order also appears in the Swiggy app to pay there.)")

    if not paas_id:
        print("\nNo paasId in the response — cannot poll payment status. Inspect payload above.")
        return

    echo = {"orderId": order_id, "addressId": address_id, "cartId": cart_id, "lat": lat, "lng": lng}
    echo = {k: v for k, v in echo.items() if v is not None}

    print(f"\nPolling payment status (up to {UPI_POLL_MAX_ATTEMPTS}×, ~19s each) ...")
    for attempt in range(1, UPI_POLL_MAX_ATTEMPTS + 1):
        ps = await food.check_payment_status(str(paas_id), **echo)
        pstatus = str(ps.get("status", "")).upper()
        print(f"  [{attempt}] status={pstatus or '<none>'}")
        if pstatus in UPI_TERMINAL_OK:
            break
        if pstatus in UPI_TERMINAL_FAIL:
            print(f"\nPayment {pstatus} — order not placed.")
            return
    else:
        print("\nPayment not confirmed within the polling window — NOT claiming placement.")
        return

    if not order_id:
        print("\nPayment succeeded but no orderId to confirm — inspect the Swiggy app.")
        return
    confirm = await food.confirm_order(str(order_id), **{k: v for k, v in echo.items() if k != "orderId"})
    print(f"\nconfirm_order returned: {json.dumps(confirm, ensure_ascii=False)[:800]}")
    print("Order confirmed — placement complete.")


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    print(f"Connecting to {settings.food_server_url} (reusing token if valid) ...")
    async with food_session(settings) as session:
        food = FoodClient(session)
        addresses = (await food.get_addresses()).get("addresses", [])
        if not addresses:
            raise HappyPathError("No saved addresses on this account.")
        _print_addresses(addresses)
        address_id = addresses[args.address_index]["id"]
        print(f"\n-> Using address id={address_id}")

        if args.place:
            await run_place(food, address_id, args)
        else:
            await run_simulate(food, address_id)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Day 4: resolution path, behind confirmation gates.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--simulate", action="store_true", help="Resolution demo on a real past order (default).")
    mode.add_argument("--place", action="store_true", help="Drive a real cart to the placement gate.")
    parser.add_argument("--confirm", action="store_true", help="With --place: actually place the real order.")
    parser.add_argument("--pay", choices=("upi", "cod"), default="upi", help="Payment method for --place (default: upi).")
    parser.add_argument("--max-to-pay", type=float, default=None, help="Tighter budget: refuse to place if cart to_pay >= this (still capped at ₹400).")
    parser.add_argument("--query", default="rolls", help="Restaurant search query for --place.")
    parser.add_argument("--address-index", type=int, default=0, help="Index into get_addresses (default 0).")
    parser.add_argument("--note", default=None, help="Optional note to restaurant for --place --confirm.")
    args = parser.parse_args()

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
