"""Day-4 evidence-packet assembly, proven for ₹0 (no order, no filing)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent.evidence import build_evidence_packet, to_report_error_args
from agent.expectation import build_expectation
from agent.reconcile import MISSING_ITEM, normalize_actual_order, reconcile

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED_AT = "2026-08-20T13:15:00+05:30"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def expected():
    return build_expectation(_load("cart_clean.json"), captured_at=CAPTURED_AT)


@pytest.fixture
def order_missing_item():
    order = _load("actual_order_clean.json")
    order["items"] = [i for i in order["items"] if i["itemId"] != "1002"]
    return order


def test_packet_carries_ids_and_discrepancy(expected, order_missing_item):
    actual = normalize_actual_order(order_missing_item)
    disc = reconcile(expected, actual)
    packet = build_evidence_packet(expected, actual, disc)
    assert packet.order_id == "900000000000001"
    assert packet.restaurant_id == "77001"
    assert packet.simulated is False
    assert MISSING_ITEM in packet.headline
    assert any("MISSING_ITEM" in line for line in packet.lines)


def test_report_error_args_mapping(expected, order_missing_item):
    actual = normalize_actual_order(order_missing_item)
    disc = reconcile(expected, actual)
    args = to_report_error_args(build_evidence_packet(expected, actual, disc))
    assert args["domain"] == "food"
    assert args["tool"] == "get_food_orders"
    assert args["toolContext"] == {"orderId": "900000000000001", "restaurantId": "77001"}
    assert "SIMULATED" not in args["errorMessage"]  # real report carries no marker


def test_simulated_marker_is_loud_everywhere(expected, order_missing_item):
    actual = normalize_actual_order(order_missing_item)
    disc = reconcile(expected, actual)
    packet = build_evidence_packet(expected, actual, disc, simulated=True)
    args = to_report_error_args(packet)
    assert packet.headline.startswith("[SIMULATED]")
    # Every free-text field must flag the simulation so it can't be filed as real.
    assert "SIMULATED" in args["errorMessage"]
    assert "SIMULATED" in args["flowDescription"]
    assert "SIMULATED" in args["userNotes"]


def test_clean_order_packet_has_no_discrepancy(expected):
    actual = normalize_actual_order(_load("actual_order_clean.json"))
    packet = build_evidence_packet(expected, actual, reconcile(expected, actual))
    assert packet.discrepancies == ()
    assert "No billing/spec discrepancy" in packet.headline
