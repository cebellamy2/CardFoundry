"""What a Shopify customer chose at checkout: pickup, or shipping.

★ WHY THIS IS A SERVICE AND NOT A FIELD ON THE PAYLOAD. Phase 1 Shopify
is pickup only, paid at pickup (operator), and
shopify_pickup_service.is_pickup_order says so by fiat: every Shopify
order IS a pickup order. On 2026-11-03 the first real test order proved
what that costs -- #1001 was checked out with a $8.00 "Standard"
shipping line and CardFoundry still called it a pickup, so it had no
Ship by date, no shipping address on the page and nothing that would
ever have made somebody post it. A customer who pays for shipping and
gets a pickup reservation is the operator finding out from the customer.

★ THE DOCUMENTED SOURCE OF TRUTH IS OUT OF REACH, and Shopify says so
twice over. The delivery method lives on the fulfillment order
(`deliveryMethod { methodType }`, values SHIPPING / PICK_UP / LOCAL /
PICKUP_POINT / RETAIL / NONE), and this app has no fulfillment-order
scope: `fulfillmentOrders` comes back ACCESS_DENIED, verified against
the live store on 2026-11-03. Shopify's split-carts guidance is also
explicit that the obvious workarounds are wrong -- it says not to infer
the delivery method from shipping lines or "the presence of a shipping
address", and to use the fulfillment order's deliveryMethod instead.
So an inference from shippingLine.code, shippingAddress or
requiresShipping is not a shortcut this module is allowed to take, and
nothing here takes one.

★ WHAT IS IN REACH: SHOPIFY'S OWN FILTER. The documented `orders` query
takes `delivery_method:`, which its reference describes as filtering by
the delivery methodType field -- the same field, reached through the
search index instead of the object. Its values are hyphenated
(`shipping`, `pick-up`, `local`, `pickup-point`, `retail`, `none`), NOT
the GraphQL enum's `PICK_UP`. Verified working under this app's scopes
on 2026-11-03: `delivery_method:shipping` returned order #1001 and
`delivery_method:pick-up` returned nothing, and the filter combines with
an `(id:... OR id:...)` group.

★ ONE HTTP CALL PER CHUNK, TWO ALIASES. Asking "which of these chose
shipping" and "which chose pickup" are two filters and one document, so
a batch of orders costs one round trip rather than two per order.

★ AND AN UNCLASSIFIED BUCKET THAT IS REPORTED. An order in neither
answer is stored as "unclassified" and counted in the log, never
quietly assumed to be a pickup -- a route-classifying read with no
unclassified bucket hides exactly the cases nobody predicted (local
delivery, a retail order, a method Shopify adds later).

Reads only. Makes no Shopify write of any kind.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

import shopify_service
from shopify_order_service import SOURCE, canonical_external_order_id

logger = logging.getLogger("cardfoundry")

# Shopify's own filter values, not the GraphQL enum's spelling.
METHOD_SHIPPING = "shipping"
METHOD_PICKUP = "pick-up"
# Ours, for "Shopify's filter put this order in neither bucket".
METHOD_UNCLASSIFIED = "unclassified"

# The methods that mean the customer is NOT collecting in person, and so
# the ones phase 1 has nothing built for. `local` (local delivery) and
# `pickup-point` (a carrier's pickup point) are both somebody delivering
# to somebody, and neither is this shop's counter.
NOT_PICKUP_METHODS = (METHOD_SHIPPING,)

# How many orders one read asks about. Shopify's search string is bounded
# in practice rather than by a documented limit, and 25 keeps the query
# short while making the common case -- one or two new orders a tick --
# a single call.
IDS_PER_READ = 25

DELIVERY_QUERY = """
query CardFoundryShopifyDeliveryMethod($shipping: String!, $pickup: String!) {
  shipping: orders(first: %d, query: $shipping) { nodes { id } }
  pickup: orders(first: %d, query: $pickup) { nodes { id } }
}
""" % (IDS_PER_READ, IDS_PER_READ)


def _id_group(ids) -> str:
    """`(id:123 OR id:456)`, Shopify's own search syntax."""
    return "(" + " OR ".join(f"id:{value}" for value in ids) + ")"


def read_delivery_methods(external_order_ids, *, client=None
                          ) -> dict[str, str]:
    """canonical order id -> "shipping" / "pick-up" / "unclassified".

    One read per IDS_PER_READ orders. Raises whatever shopify_service
    raises; the recording caller is the one that must not fail.
    """
    wanted = []
    for raw in external_order_ids or []:
        canonical = canonical_external_order_id(raw)
        # Only a numeric id can go in an `id:` filter. A row whose id is
        # not numeric is not something to guess about.
        if canonical and canonical.isdigit() and canonical not in wanted:
            wanted.append(canonical)
    found: dict[str, str] = {}
    for start in range(0, len(wanted), IDS_PER_READ):
        chunk = wanted[start:start + IDS_PER_READ]
        group = _id_group(chunk)
        data = shopify_service.execute(DELIVERY_QUERY, {
            "shipping": f"{group} AND delivery_method:{METHOD_SHIPPING}",
            "pickup": f"{group} AND delivery_method:{METHOD_PICKUP}",
        }, client=client)
        for alias, method in (("shipping", METHOD_SHIPPING),
                              ("pickup", METHOD_PICKUP)):
            for node in ((data or {}).get(alias) or {}).get("nodes") or []:
                canonical = canonical_external_order_id((node or {}).get("id"))
                if canonical:
                    found[canonical] = method
        for canonical in chunk:
            found.setdefault(canonical, METHOD_UNCLASSIFIED)
    return found


def record_delivery_methods(session: Session, orders, *, client=None,
                            store_unclassified: bool = True) -> dict:
    """Store the delivery method on each order that has none yet.

    ★ store_unclassified=False FOR A READER THAT MIGHT BE TOO EARLY.
    This asks a SEARCH filter, and a search index is not promised to be
    current the second a webhook arrives -- the webhook reaches us in
    about four seconds. An order Shopify has not indexed yet comes back
    in neither bucket, and storing "unclassified" for it would be
    recording a timing artefact as an answer and never asking again,
    because the column would no longer be NULL. So the webhook asks and
    stores only a real answer; the hourly poll, an hour later, is the
    one allowed to conclude that neither bucket is the answer.

    ★ NEVER RAISES, because every caller is in the middle of something
    that matters more. An order whose delivery method could not be read
    keeps NULL and is asked about again next time, which is the correct
    "we do not know yet" -- the alternative, writing a guess, is how a
    shipped order becomes a pickup for ever.

    Caller commits.
    """
    rows = [order for order in (orders or [])
            if getattr(order, "source", None) == SOURCE
            and not getattr(order, "shopify_delivery_method", None)]
    if not rows:
        return {"asked": 0, "stored": 0, "by_method": {}}
    try:
        methods = read_delivery_methods(
            [order.external_order_id for order in rows], client=client)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "Shopify delivery method: could not be read for %s order(s), "
            "which keep no stored method and will be asked about again: "
            "%s: %s", len(rows), type(exc).__name__, exc,
        )
        return {"asked": len(rows), "stored": 0, "by_method": {},
                "failed": f"{type(exc).__name__}: {exc}"}
    by_method: dict[str, int] = {}
    stored = 0
    for order in rows:
        canonical = canonical_external_order_id(order.external_order_id)
        method = methods.get(canonical)
        if not method:
            continue
        if method == METHOD_UNCLASSIFIED and not store_unclassified:
            logger.info(
                "Shopify order %s is in neither delivery bucket yet; "
                "leaving it unanswered rather than recording it, because "
                "this reader may be ahead of Shopify's search index.",
                order.external_label or canonical,
            )
            continue
        order.shopify_delivery_method = method
        stored += 1
        by_method[method] = by_method.get(method, 0) + 1
        if method in NOT_PICKUP_METHODS:
            logger.warning(
                "Shopify order %s chose %s, not pickup. It is ingested and "
                "reserved like any other, but phase 1 has no shipping "
                "path, so it is on the Attention page for a person to "
                "decide what happens to it.",
                order.external_label or canonical, method,
            )
        elif method == METHOD_UNCLASSIFIED:
            logger.warning(
                "Shopify order %s is neither %s nor %s by Shopify's own "
                "delivery_method filter. It is recorded as %r rather than "
                "assumed to be a pickup.",
                order.external_label or canonical, METHOD_SHIPPING,
                METHOD_PICKUP, METHOD_UNCLASSIFIED,
            )
    logger.info(
        "Shopify delivery method: %s order(s) asked, %s stored (%s).",
        len(rows), stored,
        ", ".join(f"{name}={count}"
                  for name, count in sorted(by_method.items())) or "none",
    )
    return {"asked": len(rows), "stored": stored, "by_method": by_method}
