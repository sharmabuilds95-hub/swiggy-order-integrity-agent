"""Day-5 proof: the MCP server tools return correct, speakable, order-safe output.

Calls the three tool functions the voice agent will invoke and asserts the
demo scenarios behave: the clean order reconciles silently, the overcharge is
caught with confidence labels, and file_complaint stays SIMULATED (no live
report, no order placed) while DEMO_MODE is on. Pure and offline, like the rest
of the suite — no server process, no network.
"""

from __future__ import annotations

import server.app as srv

# MCPServer.tool() registers each tool but leaves the module-level function
# directly callable, so the tests exercise the exact callables the server
# exposes without standing up a server process.


def test_demo_mode_is_on_by_default():
    assert srv.DEMO_MODE is True


def test_list_recent_orders_lists_both():
    out = srv.list_recent_orders()
    ids = {o["order_id"] for o in out["orders"]}
    assert ids == {"900000000000001", "900000000000002"}
    assert "recent order" in out["spoken"]


def test_clean_order_reconciles_silently():
    out = srv.reconcile_order(order_id="900000000000001")
    assert out["found"] is True
    assert out["clean"] is True
    assert out["discrepancies"] == []
    # A clean bill must never be spoken as "the food was correct".
    assert "not" in out["spoken"].lower()


def test_overcharge_is_caught_with_confidence():
    out = srv.reconcile_order(order_id="900000000000002")
    assert out["clean"] is False
    kinds = {d["kind"] for d in out["discrepancies"]}
    assert "TOTAL_OVERCHARGE" in kinds
    for d in out["discrepancies"]:
        assert d["confidence"] in {"high", "inferred", "medium"}


def test_unknown_order_is_handled():
    out = srv.reconcile_order(order_id="does-not-exist")
    assert out["found"] is False


def test_file_complaint_is_simulated_in_demo_mode():
    out = srv.file_complaint(order_id="900000000000002")
    assert out["filed"] is True
    assert out["simulated"] is True
    # The SIMULATED marker must ride along in the rendered report_error args.
    assert "SIMULATED" in out["report_error_args"]["errorMessage"]


def test_file_complaint_declines_when_clean():
    out = srv.file_complaint(order_id="900000000000001")
    assert out["filed"] is False
