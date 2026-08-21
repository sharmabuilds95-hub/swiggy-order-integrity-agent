"""Turn detected discrepancies into a filing-ready evidence packet (Day 4).

ADR-001 Day 4: on a detected discrepancy, assemble an evidence packet (order id,
expected vs. observed, timestamps, tracking status) and file it via `report_error`
— behind a mandatory human-confirmation gate, never auto-filed.

This module does the *assembly*, purely: `build_evidence_packet(...)` takes the
expectation record, the actual order, and the reconciliation output and returns
a structured `EvidencePacket`; `to_report_error_args(...)` renders that packet
into the exact keyword arguments `report_error` expects. No I/O, no clock, no
network — so it is unit-testable for ₹0, and the side-effectful filing stays in
the script layer behind the gate.

Honesty rails baked in:
- `report_error` does not silently file a ticket — it returns a pre-filled
  `mailto:` for the user to send and logs server-side (vault §5b). The packet is
  therefore written to be read by a human before anything is sent.
- The `simulated` flag rides along on the packet and is rendered loudly into the
  user-facing text. A simulated discrepancy (ADR-001's demo-path device) must
  never be presented — or filed — as a real one. The filing path refuses to send
  a simulated packet; see `scripts/day4_resolution.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.expectation import ExpectationRecord
from agent.reconcile import ActualOrder, Discrepancy

DOMAIN_FOOD = "food"


@dataclass(frozen=True)
class EvidencePacket:
    """Everything needed to file (or preview) one order-integrity complaint."""

    order_id: str
    restaurant_id: str
    restaurant_name: str
    simulated: bool
    headline: str
    discrepancies: tuple[Discrepancy, ...]
    expected_to_pay: float | None
    actual_order_total: float | None
    captured_at: str
    delivery_status: str
    lines: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "restaurant_id": self.restaurant_id,
            "restaurant_name": self.restaurant_name,
            "simulated": self.simulated,
            "headline": self.headline,
            "discrepancies": [d.to_dict() for d in self.discrepancies],
            "expected_to_pay": self.expected_to_pay,
            "actual_order_total": self.actual_order_total,
            "captured_at": self.captured_at,
            "delivery_status": self.delivery_status,
            "lines": list(self.lines),
        }


def _headline(discrepancies: list[Discrepancy], simulated: bool) -> str:
    prefix = "[SIMULATED] " if simulated else ""
    if not discrepancies:
        return f"{prefix}No billing/spec discrepancy detected."
    top = discrepancies[0]
    n = len(discrepancies)
    more = f" (+{n - 1} more)" if n > 1 else ""
    return f"{prefix}{top.kind}: {top.message}{more}"


def _detail_lines(
    expected: ExpectationRecord,
    actual: ActualOrder,
    discrepancies: list[Discrepancy],
) -> tuple[str, ...]:
    """Human-readable expected-vs-observed detail, one line per discrepancy."""
    lines: list[str] = [
        f"Order {actual.order_id} @ {actual.restaurant_name} — status: {actual.delivery_status or 'unknown'}",
        f"Agreed to pay ₹{expected.to_pay} at confirmation ({expected.captured_at}); "
        f"order recorded total ₹{actual.order_total}.",
    ]
    for d in discrepancies:
        tag = "" if d.confidence == "high" else f" [{d.confidence}-basis]"
        lines.append(f"- {d.kind}{tag}: {d.message}")
    return tuple(lines)


def build_evidence_packet(
    expected: ExpectationRecord,
    actual: ActualOrder,
    discrepancies: list[Discrepancy],
    *,
    simulated: bool = False,
) -> EvidencePacket:
    """Assemble a filing-ready packet from a reconciliation result. Pure."""
    return EvidencePacket(
        order_id=actual.order_id,
        restaurant_id=actual.restaurant_id,
        restaurant_name=actual.restaurant_name,
        simulated=simulated,
        headline=_headline(discrepancies, simulated),
        discrepancies=tuple(discrepancies),
        expected_to_pay=expected.to_pay,
        actual_order_total=actual.order_total,
        captured_at=expected.captured_at,
        delivery_status=actual.delivery_status,
        lines=_detail_lines(expected, actual, discrepancies),
    )


def to_report_error_args(packet: EvidencePacket) -> dict[str, Any]:
    """Render an EvidencePacket into `report_error` keyword arguments.

    `report_error` is a generic support channel keyed on a tool name; we anchor
    it to `get_food_orders` (the read whose data surfaced the discrepancy) and
    carry the real story in errorMessage / flowDescription / userNotes, with the
    identifiers in `toolContext`. The SIMULATED marker, when present, is written
    into every free-text field so it can never be mistaken for a real report.
    """
    marker = "[SIMULATED — demo only, do not action] " if packet.simulated else ""
    tool_context = {"orderId": packet.order_id}
    if packet.restaurant_id:
        tool_context["restaurantId"] = packet.restaurant_id

    body = "\n".join(packet.lines)
    return {
        "tool": "get_food_orders",
        "domain": DOMAIN_FOOD,
        "errorMessage": f"{marker}{packet.headline}",
        "flowDescription": (
            f"{marker}Post-delivery order-integrity check: captured an expectation "
            f"record at cart confirmation, reconciled it against the recorded order, "
            f"and found a billing/spec discrepancy."
        ),
        "toolContext": tool_context,
        "userNotes": f"{marker}Expected vs. observed:\n{body}",
    }
