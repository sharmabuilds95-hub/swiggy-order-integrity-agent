"""Pure billing/spec reconciliation: expected (at gate) vs actual (post-delivery).

This is the core IP of Concept 11 (ADR-001 Day 3). Given an `ExpectationRecord`
frozen at the confirmation gate and one order dict from `get_food_orders`, it
returns a list of `Discrepancy` objects. It is a **pure function**: no I/O, no
clock, no network. That is what lets it be proven for ₹0 against anonymized
fixtures and hand-mutated discrepancy copies, with no order ever placed.

## The honest ceiling — read before trusting a "clean" result

The Food API records what was **ordered and charged**, and a *coarse* delivery
status ("Delivered") — but has **no per-item "what actually arrived" manifest**
(vault `Swiggy-Builders-API-Reference` §5c). So this engine detects
**billing/spec** discrepancies only:

  - item ordered-vs-charged identity, quantity, presence  (SPEC)
  - per-line price drift, order-total overcharge, coupon not applied  (BILLING)

It **cannot** detect a physical discrepancy — a missing, wrong, substituted, or
cold item that Swiggy still recorded as ordered. Those need a user report; the
agent's value there is evidence-packaging and frictionless filing (Day 4), not
detection. A zero-discrepancy result means "the bill matches the agreement,"
never "the food was correct."

## Confidence labelling (zero-hallucination protocol)

Each discrepancy carries a `confidence`:

  - "high"     — item identity / quantity / presence. Unambiguous: both sides
                 key on the same menu item id, so a mismatch is real.
  - "inferred" — anything resting on a price *basis* being equivalent across
                 the two payloads. We compare cart per-line `subtotal` against
                 order per-line `subtotal`, and cart `to_pay` against order
                 `orderTotal`, as the best-matched pairs — but that equivalence
                 has NOT yet been cross-checked against a real placed order
                 (no order placed through Day 3). Day 4's first real order
                 confirms or corrects the basis. Until then these are flagged,
                 not asserted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.expectation import ExpectationRecord, _int, _num

# Discrepancy kinds.
MISSING_ITEM = "MISSING_ITEM"
EXTRA_ITEM = "EXTRA_ITEM"
QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
ITEM_PRICE_MISMATCH = "ITEM_PRICE_MISMATCH"
TOTAL_OVERCHARGE = "TOTAL_OVERCHARGE"
TOTAL_UNDERCHARGE = "TOTAL_UNDERCHARGE"
COUPON_NOT_APPLIED = "COUPON_NOT_APPLIED"

# Severity ranking, high number = surface first.
_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "info": 0}

DEFAULT_MONEY_TOLERANCE = 1.0  # rupees; absorbs rounding, not real drift


@dataclass(frozen=True)
class ActualItem:
    item_id: str
    name: str
    quantity: int
    subtotal: float | None
    total: float | None
    packing_charges: float | None


@dataclass(frozen=True)
class ActualOrder:
    """A normalized view of one `get_food_orders` order, ready to reconcile."""

    order_id: str
    restaurant_id: str
    restaurant_name: str
    order_total: float | None
    status: str
    delivery_status: str
    items: tuple[ActualItem, ...]

    def item_by_id(self) -> dict[str, ActualItem]:
        merged: dict[str, ActualItem] = {}
        for it in self.items:
            existing = merged.get(it.item_id)
            if existing is None:
                merged[it.item_id] = it
            else:
                merged[it.item_id] = ActualItem(
                    item_id=it.item_id,
                    name=existing.name,
                    quantity=existing.quantity + it.quantity,
                    subtotal=_sum_opt(existing.subtotal, it.subtotal),
                    total=_sum_opt(existing.total, it.total),
                    packing_charges=_sum_opt(existing.packing_charges, it.packing_charges),
                )
        return merged


@dataclass(frozen=True)
class Discrepancy:
    kind: str
    severity: str
    confidence: str
    message: str
    item_id: str | None = None
    item_name: str | None = None
    expected: Any = None
    actual: Any = None
    delta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "confidence": self.confidence,
            "message": self.message,
            "item_id": self.item_id,
            "item_name": self.item_name,
            "expected": self.expected,
            "actual": self.actual,
            "delta": self.delta,
        }


def _sum_opt(a: float | None, b: float | None) -> float | None:
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)


def _order_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull per-item detail from a get_food_orders order.

    The itemized source of truth is the REORDER CTA payload at
    `actions[].reorderMeta.orderItems`; a flattened top-level `items` (as the
    saved real fixture carries) is accepted too. First non-empty wins.
    """
    top = order.get("items")
    if top:
        return top
    for action in order.get("actions", []) or []:
        meta = action.get("reorderMeta") or {}
        order_items = meta.get("orderItems")
        if order_items:
            return order_items
    return []


def normalize_actual_order(order: dict[str, Any]) -> ActualOrder:
    """Coerce one raw `get_food_orders` order into an ActualOrder.

    Handles the payload's stringified numbers ("1", "184") and rupee-glyph
    totals ("₹470"), and reads items from either the flattened `items` list or
    the nested `actions[].reorderMeta.orderItems`.
    """
    items: list[ActualItem] = []
    for raw in _order_items(order):
        item_id = raw.get("itemId", raw.get("menu_item_id"))
        if item_id is None:
            continue
        items.append(
            ActualItem(
                item_id=str(item_id),
                name=str(raw.get("name", "")),
                quantity=_int(raw.get("quantity"), default=1),
                subtotal=_num(raw.get("subtotal")),
                total=_num(raw.get("total")),
                packing_charges=_num(raw.get("packingCharges")),
            )
        )
    return ActualOrder(
        order_id=str(order.get("orderId", "")),
        restaurant_id=str(order.get("restaurantId", "")),
        restaurant_name=str(order.get("restaurantName", "")),
        order_total=_num(order.get("orderTotal")),
        status=str(order.get("orderStatus", "")),
        delivery_status=str(order.get("orderDeliveryStatus", "")),
        items=tuple(items),
    )


def _expected_basis(item) -> float | None:
    """Best per-line figure to compare against an order line's `subtotal`.

    Prefer cart `subtotal`, NOT `final_price`. Live probe (2026-08-21): a
    50%-off item showed cart `subtotal` = 330 (list, price*qty) but
    `final_price` = 164 (discounted line). The order-history payload records the
    per-line **list** price in its `subtotal` too and books the discount only at
    the order-total level (real fixture, order 3: item subtotal 339, orderTotal
    168). So `subtotal`<->`subtotal` compares list-vs-list and stays silent on a
    legitimate offer; the discount is caught by the to_pay<->orderTotal check.
    Fall back to `line_final_price` only when the cart omitted a subtotal.
    """
    return item.subtotal if item.subtotal is not None else item.line_final_price


def reconcile(
    expected: ExpectationRecord,
    actual: ActualOrder | dict[str, Any],
    *,
    money_tolerance: float = DEFAULT_MONEY_TOLERANCE,
) -> list[Discrepancy]:
    """Compare an expectation record against an actual order.

    `actual` may be an already-normalized ActualOrder or a raw order dict from
    `get_food_orders` (normalized here for convenience). Returns discrepancies
    ordered most-severe first; an empty list means the *bill* matched the
    agreement (NOT that the food was physically correct — see module docstring).
    """
    if isinstance(actual, dict):
        actual = normalize_actual_order(actual)

    found: list[Discrepancy] = []
    exp_items = expected.item_by_id()
    act_items = actual.item_by_id()

    # --- per-item: presence, quantity, price -------------------------------
    for item_id, exp in exp_items.items():
        act = act_items.get(item_id)
        if act is None:
            found.append(
                Discrepancy(
                    kind=MISSING_ITEM,
                    severity="high",
                    confidence="high",
                    item_id=item_id,
                    item_name=exp.name,
                    expected=exp.quantity,
                    actual=0,
                    message=(
                        f"Ordered {exp.quantity}x {exp.name!r} but it is absent "
                        f"from the recorded order."
                    ),
                )
            )
            continue

        if exp.quantity != act.quantity:
            found.append(
                Discrepancy(
                    kind=QUANTITY_MISMATCH,
                    severity="high",
                    confidence="high",
                    item_id=item_id,
                    item_name=exp.name,
                    expected=exp.quantity,
                    actual=act.quantity,
                    delta=float(act.quantity - exp.quantity),
                    message=(
                        f"{exp.name!r}: agreed {exp.quantity}, recorded {act.quantity}."
                    ),
                )
            )

        exp_price = _expected_basis(exp)
        act_price = act.subtotal
        if (
            exp_price is not None
            and act_price is not None
            and abs(act_price - exp_price) > money_tolerance
        ):
            found.append(
                Discrepancy(
                    kind=ITEM_PRICE_MISMATCH,
                    severity="medium",
                    confidence="inferred",
                    item_id=item_id,
                    item_name=exp.name,
                    expected=exp_price,
                    actual=act_price,
                    delta=round(act_price - exp_price, 2),
                    message=(
                        f"{exp.name!r}: agreed line ₹{exp_price}, charged ₹{act_price} "
                        f"(basis: cart vs order subtotal — see confidence)."
                    ),
                )
            )

    # --- items charged that were never agreed ------------------------------
    for item_id, act in act_items.items():
        if item_id not in exp_items:
            found.append(
                Discrepancy(
                    kind=EXTRA_ITEM,
                    severity="high",
                    confidence="high",
                    item_id=item_id,
                    item_name=act.name,
                    expected=0,
                    actual=act.quantity,
                    delta=act.subtotal,
                    message=(
                        f"Charged {act.quantity}x {act.name!r} which was not in the "
                        f"agreed cart."
                    ),
                )
            )

    # --- order total: overcharge / undercharge -----------------------------
    if expected.to_pay is not None and actual.order_total is not None:
        delta = round(actual.order_total - expected.to_pay, 2)
        if delta > money_tolerance:
            found.append(
                Discrepancy(
                    kind=TOTAL_OVERCHARGE,
                    severity="high",
                    confidence="inferred",
                    expected=expected.to_pay,
                    actual=actual.order_total,
                    delta=delta,
                    message=(
                        f"Charged ₹{actual.order_total} vs agreed ₹{expected.to_pay} "
                        f"(+₹{delta}). Basis: cart to_pay vs order total — see confidence."
                    ),
                )
            )
        elif delta < -money_tolerance:
            found.append(
                Discrepancy(
                    kind=TOTAL_UNDERCHARGE,
                    severity="info",
                    confidence="inferred",
                    expected=expected.to_pay,
                    actual=actual.order_total,
                    delta=delta,
                    message=(
                        f"Charged ₹{actual.order_total} vs agreed ₹{expected.to_pay} "
                        f"({delta}) — charged less than agreed; informational."
                    ),
                )
            )

        # --- coupon promised at the gate but not reflected in the charge ---
        if expected.coupon_applied and expected.coupon_discount > 0:
            # If the order total looks like the pre-coupon amount, the discount
            # never landed. Compare against (to_pay + discount) within tolerance.
            pre_coupon = expected.to_pay + expected.coupon_discount
            if actual.order_total >= pre_coupon - money_tolerance:
                found.append(
                    Discrepancy(
                        kind=COUPON_NOT_APPLIED,
                        severity="high",
                        confidence="inferred",
                        expected=expected.to_pay,
                        actual=actual.order_total,
                        delta=round(actual.order_total - expected.to_pay, 2),
                        message=(
                            f"Coupon worth ₹{expected.coupon_discount} shown at "
                            f"confirmation but the ₹{actual.order_total} charge matches "
                            f"the pre-coupon total — discount appears not applied."
                        ),
                    )
                )

    found.sort(key=lambda d: _SEVERITY_RANK.get(d.severity, 0), reverse=True)
    return found
