"""The Day-3 expectation record: what the user agreed to at the confirmation gate.

This is the piece with no public prior art (ADR-001 Day 3). At the moment the
user confirms an order, we freeze a structured snapshot of *what was agreed* —
items, quantities, per-line prices, and the total to pay — so that after
delivery we can reconcile it against what Swiggy actually recorded and charged.

Critical modelling decisions, each grounded in a live capture (see the vault's
`Swiggy-Builders-API-Reference` §5c, verified 2026-08-19), NOT inferred:

- The record is built from the **cart at confirmation** (`get_food_cart`),
  never the menu listing. Menu `price` != cart `final_price` != `to_pay`: a
  live "50% OFF" offer turned a menu price of 739 into a cart `final_price` of
  368 and a `to_pay` of 435. Building expectations from the menu would make
  every discounted order read as a false price discrepancy.
- Cart item money fields (`subtotal`, `total`, `final_price`) are per **line**
  (all units of that item), matching how the order-history payload reports
  per-line `subtotal`/`total`. We keep all three so reconciliation can compare
  like-for-like.
- This module does no I/O and reads no clock. `captured_at` is injected by the
  caller. That keeps it a pure, deterministically testable transform and lets
  Day 4 persist the record at the gate however it likes.

`to_dict`/`from_dict` are provided because Day 4 persists this record between
the confirmation gate and post-delivery reconciliation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _num(value: Any) -> float | None:
    """Coerce a money-ish value to float. Handles 184, "184", "₹470", None, "".

    Swiggy mixes numeric JSON (cart) and stringified numbers with a rupee glyph
    (order history), so every price crossing this boundary goes through here.
    Returns None for anything it cannot read as a number, so callers can tell a
    genuine 0 from a missing field.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; never a price
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("₹", "").replace(",", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _int(value: Any, default: int = 0) -> int:
    """Coerce a quantity to int. Order history reports quantity as a string."""
    num = _num(value)
    return int(round(num)) if num is not None else default


@dataclass(frozen=True)
class ExpectedItem:
    """One agreed line in the cart at confirmation.

    `line_final_price` is the discounted price the user actually saw for the
    whole line (cart `final_price`). `subtotal`/`total` are kept when present
    because the order-history payload reports per-line `subtotal` (pre-packing)
    and `total` (incl. packing), and matching basis-to-basis avoids false
    price-mismatch findings.
    """

    item_id: str
    name: str
    quantity: int
    line_final_price: float | None
    subtotal: float | None = None
    total: float | None = None


@dataclass(frozen=True)
class ExpectationRecord:
    """A frozen snapshot of what the user agreed to at the confirmation gate."""

    captured_at: str
    restaurant_name: str
    items: tuple[ExpectedItem, ...]
    to_pay: float | None = None
    item_total: float | None = None
    taxes_and_charges: float | None = None
    delivery_charge: float | None = None
    coupon_applied: bool = False
    coupon_discount: float = 0.0
    source: str = "get_food_cart"
    meta: dict[str, Any] = field(default_factory=dict)

    def item_by_id(self) -> dict[str, ExpectedItem]:
        """Index items by id, summing quantities if an id appears twice.

        A well-formed cart lists each item once, but summing defensively means
        a duplicated line can never silently hide half the quantity.
        """
        merged: dict[str, ExpectedItem] = {}
        for it in self.items:
            existing = merged.get(it.item_id)
            if existing is None:
                merged[it.item_id] = it
            else:
                merged[it.item_id] = ExpectedItem(
                    item_id=it.item_id,
                    name=existing.name,
                    quantity=existing.quantity + it.quantity,
                    line_final_price=_sum_opt(existing.line_final_price, it.line_final_price),
                    subtotal=_sum_opt(existing.subtotal, it.subtotal),
                    total=_sum_opt(existing.total, it.total),
                )
        return merged

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExpectationRecord":
        items = tuple(ExpectedItem(**it) for it in data.get("items", []))
        known = {
            "captured_at",
            "restaurant_name",
            "to_pay",
            "item_total",
            "taxes_and_charges",
            "delivery_charge",
            "coupon_applied",
            "coupon_discount",
            "source",
            "meta",
        }
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(items=items, **kwargs)


def _sum_opt(a: float | None, b: float | None) -> float | None:
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)


def build_expectation(cart: dict[str, Any], *, captured_at: str) -> ExpectationRecord:
    """Freeze a `get_food_cart` payload into an ExpectationRecord.

    Tolerant of missing keys — a partial cart yields a partial record rather
    than raising, so a shape drift degrades detection gracefully instead of
    crashing the confirmation gate. `captured_at` is caller-supplied (this
    function reads no clock) so the result is deterministic and testable.
    """
    data = cart.get("data", {}) or {}
    restaurant = (data.get("restaurant") or {}).get("name", "") or ""

    items: list[ExpectedItem] = []
    for raw in data.get("items", []) or []:
        item_id = raw.get("menu_item_id")
        if item_id is None:
            continue
        items.append(
            ExpectedItem(
                item_id=str(item_id),
                name=str(raw.get("name", "")),
                quantity=_int(raw.get("quantity"), default=1),
                line_final_price=_num(raw.get("final_price")),
                subtotal=_num(raw.get("subtotal")),
                total=_num(raw.get("total")),
            )
        )

    pricing = data.get("pricing", {}) or {}
    offers = data.get("offers", {}) or {}
    coupon_discount = _num(offers.get("coupon_discount")) or 0.0

    return ExpectationRecord(
        captured_at=captured_at,
        restaurant_name=restaurant,
        items=tuple(items),
        to_pay=_num(pricing.get("to_pay")),
        item_total=_num(pricing.get("item_total")),
        taxes_and_charges=_num(pricing.get("taxes_and_charges")),
        delivery_charge=_num(pricing.get("delivery_charge")),
        coupon_applied=bool(offers.get("coupon_applied", False)),
        coupon_discount=coupon_discount,
    )
