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
    _pick_clean_item,
    _print_addresses,
    _print_cart_summary,
)

CAPTURED_AT_DEMO = "2026-08-21T12:00:00+05:30"
SIM_OVERCHARGE = 40.0  # rupees; the labelled simulated overcharge for --simulate


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
    item = _pick_clean_item(menu.get("categories", []), ORDER_VALUE_CAP)
    if item is None:
        raise HappyPathError(f"No clean in-stock item under ₹{ORDER_VALUE_CAP} on page 1.")
    print(f"-> Item: {item.get('name')} (₹{item.get('price')})")

    await food.update_food_cart(
        restaurant_id=rid, address_id=address_id,
        cart_items=[{"menu_item_id": str(item["id"]), "quantity": 1}], restaurant_name=rname,
    )
    cart = await food.get_food_cart(address_id, restaurant_name=rname)
    to_pay = _print_cart_summary(cart)

    if to_pay is None or to_pay >= ORDER_VALUE_CAP:
        # Never place above our own rail, regardless of --confirm.
        await food.flush_food_cart()
        raise HappyPathError(
            f"to_pay ₹{to_pay} is not below the self-imposed ₹{ORDER_VALUE_CAP} cap — "
            f"refusing to place. Cart flushed."
        )

    payment = await food.get_payment_options(address_id)
    print(f"\nPayment options payload keys: {list(payment.keys())}")

    print("\n" + "=" * 60)
    print("HUMAN CONFIRMATION GATE — real order placement")
    print(f"  Restaurant: {rname}")
    print(f"  To pay:     ₹{to_pay}  (< ₹{ORDER_VALUE_CAP} self-imposed cap OK)")
    print(f"  Address:    {address_id}")
    print(f"  Payment:    Cash on Delivery")
    print("=" * 60)

    if not args.confirm:
        await food.flush_food_cart()
        print("\nDry run (no --confirm): cart flushed, nothing placed.")
        return

    # --confirm passed: place the real order (COD). IRREVERSIBLE.
    print("\n--confirm set: placing REAL order (Cash on Delivery) ...")
    result = await food.place_food_order(address_id, "Cash", note_to_restaurant=args.note)
    status = str(result.get("status", "")).upper()
    print(f"place_food_order returned status={status or '<none>'}")
    print(json.dumps(result, ensure_ascii=False, indent=1)[:1500])
    if status == "PENDING_PAYMENT":
        print("\nPENDING_PAYMENT — order NOT yet placed; complete payment, then confirm_order.")


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
    parser.add_argument("--confirm", action="store_true", help="With --place: actually place the real order (COD).")
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
