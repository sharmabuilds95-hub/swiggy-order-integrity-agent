"""Day-3 detection proof: reconcile() against anonymized fixtures + mutations.

Runs for ₹0 with no order placed. The clean fixtures (cart_clean.json /
actual_order_clean.json) are matched so a correct order yields zero
discrepancies; each test then hand-mutates a deep copy of the actual order (or
the cart) to inject exactly one fault and asserts it is caught — and that the
clean case stays silent, guarding against false positives.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent.expectation import build_expectation
from agent.reconcile import (
    COUPON_NOT_APPLIED,
    EXTRA_ITEM,
    ITEM_PRICE_MISMATCH,
    MISSING_ITEM,
    QUANTITY_MISMATCH,
    TOTAL_OVERCHARGE,
    TOTAL_UNDERCHARGE,
    normalize_actual_order,
    reconcile,
)

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED_AT = "2026-08-20T13:15:00+05:30"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def cart() -> dict:
    return _load("cart_clean.json")


@pytest.fixture
def order() -> dict:
    return _load("actual_order_clean.json")


@pytest.fixture
def expected(cart):
    return build_expectation(cart, captured_at=CAPTURED_AT)


def kinds(discrepancies):
    return {d.kind for d in discrepancies}


# --- the load-bearing negative: a correct order must be silent ------------
def test_clean_order_has_no_discrepancies(expected, order):
    assert reconcile(expected, order) == []


def test_expectation_is_built_from_cart_not_menu(expected):
    # to_pay carried through, discounted line prices captured, coupon flags read.
    assert expected.to_pay == 505
    assert expected.restaurant_name == "Test Kitchen"
    assert {i.item_id for i in expected.items} == {"1001", "1002"}
    assert expected.coupon_applied is False


# --- SPEC discrepancies (high confidence) ---------------------------------
def test_missing_item(expected, order):
    order["items"] = [i for i in order["items"] if i["itemId"] != "1002"]
    result = reconcile(expected, order)
    assert kinds(result) == {MISSING_ITEM}
    assert result[0].item_id == "1002"


def test_extra_item_charged(expected, order):
    order["items"].append(
        {"itemId": "1003", "name": "Sneaky Side", "quantity": "1", "total": "99", "subtotal": "90", "packingCharges": "9"}
    )
    result = reconcile(expected, order)
    assert kinds(result) == {EXTRA_ITEM}
    assert result[0].item_id == "1003"


def test_quantity_mismatch(expected, order):
    for it in order["items"]:
        if it["itemId"] == "1002":
            it["quantity"] = "1"  # agreed 2, recorded 1
    result = reconcile(expected, order)
    assert QUANTITY_MISMATCH in kinds(result)
    q = next(d for d in result if d.kind == QUANTITY_MISMATCH)
    assert q.expected == 2 and q.actual == 1


# --- BILLING discrepancies (inferred basis) -------------------------------
def test_item_price_mismatch_isolated(expected, order):
    # Bump one line's subtotal but keep orderTotal fixed to isolate the signal.
    for it in order["items"]:
        if it["itemId"] == "1001":
            it["subtotal"] = "200"  # agreed 150
    result = reconcile(expected, order)
    assert ITEM_PRICE_MISMATCH in kinds(result)
    d = next(x for x in result if x.kind == ITEM_PRICE_MISMATCH)
    assert d.delta == 50 and d.confidence == "inferred"


def test_total_overcharge(expected, order):
    order["orderTotal"] = "₹600"  # agreed 505
    result = reconcile(expected, order)
    d = next(x for x in result if x.kind == TOTAL_OVERCHARGE)
    assert d.delta == 95 and d.severity == "high" and d.confidence == "inferred"


def test_total_undercharge_is_informational(expected, order):
    order["orderTotal"] = "₹450"  # charged less than agreed
    result = reconcile(expected, order)
    d = next(x for x in result if x.kind == TOTAL_UNDERCHARGE)
    assert d.severity == "info" and d.delta == -55


def test_within_tolerance_is_silent(expected, order):
    order["orderTotal"] = "₹505.5"  # rounding noise, under ₹1 tolerance
    assert reconcile(expected, order) == []


# --- coupon promised at the gate, missing from the charge -----------------
def test_coupon_not_applied(cart, order):
    # Cart shows a ₹50 coupon: to_pay drops to 455, but the order is charged 505.
    cart["data"]["offers"] = {"coupon_applied": True, "coupon_discount": 50, "free_delivery_applied": False}
    cart["data"]["pricing"]["to_pay"] = 455
    expected = build_expectation(cart, captured_at=CAPTURED_AT)
    order["orderTotal"] = "₹505"  # pre-coupon total => discount never landed
    result = reconcile(expected, order)
    assert COUPON_NOT_APPLIED in kinds(result)


def test_coupon_applied_correctly_is_silent(cart, order):
    cart["data"]["offers"] = {"coupon_applied": True, "coupon_discount": 50, "free_delivery_applied": False}
    cart["data"]["pricing"]["to_pay"] = 455
    expected = build_expectation(cart, captured_at=CAPTURED_AT)
    order["orderTotal"] = "₹455"  # discount reflected
    result = reconcile(expected, order)
    assert COUPON_NOT_APPLIED not in kinds(result)


# --- combined faults, ordering, and normalization -------------------------
def test_multiple_faults_sorted_high_severity_first(expected, order):
    order["items"] = [i for i in order["items"] if i["itemId"] != "1002"]  # MISSING (high)
    order["orderTotal"] = "₹450"  # UNDERCHARGE (info)
    result = reconcile(expected, order)
    assert result[0].severity == "high"
    assert result[-1].severity == "info"


def test_normalizes_nested_reordermeta_items(expected):
    # Same clean order but items live under actions[].reorderMeta.orderItems.
    nested = {
        "orderId": "900000000000002",
        "restaurantName": "Test Kitchen",
        "orderTotal": "₹505",
        "orderStatus": "Delivered",
        "orderDeliveryStatus": "delivered",
        "actions": [
            {"reorderMeta": {"orderItems": [
                {"itemId": "1001", "name": "Veg Wrap", "quantity": "1", "subtotal": "150", "total": "160", "packingCharges": "10"},
                {"itemId": "1002", "name": "Paneer Bowl", "quantity": "2", "subtotal": "300", "total": "300", "packingCharges": "0"}
            ]}}
        ],
    }
    normalized = normalize_actual_order(nested)
    assert {i.item_id for i in normalized.items} == {"1001", "1002"}
    assert reconcile(expected, normalized) == []


def test_deepcopy_isolation_sanity(expected, order):
    # Mutating a copy must not disturb the shared clean fixture across tests.
    mutant = copy.deepcopy(order)
    mutant["items"].pop()
    assert reconcile(expected, order) == []
    assert reconcile(expected, mutant) != []
