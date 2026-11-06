"""Who is collecting a Shopify order: the buyer's name and phone.

★ WHY A SEPARATE READ AND NOT TWO MORE FIELDS ON THE POLL'S QUERY.
Because shopify_service.execute raises on ANY `errors` entry in the
response body, and asking for `customer { ... }` without the
read_customers scope is exactly such an error. Adding the contact
fields to ORDERS_QUERY would therefore not degrade gracefully -- it
would make the whole order poll raise, on every tick, on any store
whose app lacks the scope. Production points at the dev store, and the
dev app has no read_customers, so that change would have stopped order
intake outright.

One isolated read instead. A denial costs the contact and nothing else:
the order is already ingested, allocated and visible before this runs.
Same shape, and the same reasoning, as shopify_delivery_method_service.

★ NAME AND PHONE ONLY, by the operator's decision (2026-11-06). No
address -- phase 1 is pickup, so there is nothing to post -- and no
email. The purpose is narrow: see who is picking up, and be able to text
one of two friends whose orders collided.

★ PRIVACY IS A HARD RULE IN HERE. A name or a phone is never logged,
never printed, never put in an exception message and never written to an
audit row. The audit records THAT a contact was captured or changed and
which field moved; the value lives in exactly one place, the two columns
on sales_orders. Every log line in this module counts rather than
quotes.

Reads only. Makes no Shopify write of any kind.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

import shopify_service
from models import SalesOrder, ShopifyOrderContactChangeLog
from shopify_order_service import SOURCE, canonical_external_order_id

logger = logging.getLogger("cardfoundry")

# One read per this many orders. Matches the delivery-method reader's
# bound for the same reason: a search query string has a practical
# length limit and a chunk keeps one failure small.
IDS_PER_READ = 25

# ★ `defaultPhoneNumber`, NOT `Customer.phone`. Introspected against the
# live 2026-10 schema on 2026-11-06: `Customer.phone` is DEPRECATED with
# "Use `defaultPhoneNumber.phoneNumber` instead." The deprecated field
# is not read at all, so its removal cannot break this.
CONTACT_QUERY = """
query CardFoundryShopifyOrderContact($query: String!, $first: Int!) {
  orders(first: $first, query: $query) {
    nodes {
      id
      phone
      customer {
        firstName
        lastName
        defaultPhoneNumber { phoneNumber }
      }
      billingAddress { name phone }
    }
  }
}
"""

# What a denial looks like in the message shopify_service raises. Used
# only to say "scope" instead of "unknown" in a counted log line.
DENIAL_MARKERS = ("ACCESS_DENIED", "access denied", "not approved",
                  "protected customer data", "unauthorized")


def _id_group(ids) -> str:
    """`(id:123 OR id:456)`, Shopify's own search syntax."""
    return "(" + " OR ".join(f"id:{value}" for value in ids) + ")"


def _clean(value) -> str | None:
    """A trimmed string, or None. Never logged by any caller."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def contact_from_node(node: dict | None) -> dict:
    """{"name": str|None, "phone": str|None} from one Order node.

    The precedence is the operator's, in their order:
      name  -- customer firstName + lastName, else billingAddress.name
      phone -- customer defaultPhoneNumber, else billingAddress.phone,
               else the order's own phone
    Anything missing stays None. This never raises and never logs: it is
    handed personal data and its whole job is to not talk about it.
    """
    node = node or {}
    customer = node.get("customer") or {}
    billing = node.get("billingAddress") or {}
    default_phone = customer.get("defaultPhoneNumber") or {}

    first = _clean(customer.get("firstName"))
    last = _clean(customer.get("lastName"))
    if first or last:
        name = " ".join(part for part in (first, last) if part)
    else:
        name = _clean(billing.get("name"))

    phone = (_clean(default_phone.get("phoneNumber"))
             or _clean(billing.get("phone"))
             or _clean(node.get("phone")))
    return {"name": name, "phone": phone}


def read_order_contacts(external_order_ids, *, client=None) -> dict:
    """canonical order id -> {"name", "phone"}.

    One read per IDS_PER_READ orders. Raises whatever shopify_service
    raises -- record_order_contacts is the one that must not fail.
    """
    wanted: list[str] = []
    for raw in external_order_ids or []:
        canonical = canonical_external_order_id(raw)
        # Only a numeric id can go in an `id:` filter; a row whose id is
        # not numeric is not something to guess about.
        if canonical and canonical.isdigit() and canonical not in wanted:
            wanted.append(canonical)
    found: dict[str, dict] = {}
    for start in range(0, len(wanted), IDS_PER_READ):
        chunk = wanted[start:start + IDS_PER_READ]
        data = shopify_service.execute(CONTACT_QUERY, {
            "query": _id_group(chunk),
            "first": len(chunk),
        }, client=client)
        for node in ((data or {}).get("orders") or {}).get("nodes") or []:
            canonical = canonical_external_order_id((node or {}).get("id"))
            if canonical:
                found[canonical] = contact_from_node(node)
    return found


def _describe(before_name, before_phone, name, phone) -> tuple | None:
    """(change_type, fields_changed) or None when nothing moved.

    Compares values but returns only labels, so the caller can audit a
    change without ever holding one in a string it might log.
    """
    moved = []
    if (before_name or None) != (name or None):
        moved.append("name")
    if (before_phone or None) != (phone or None):
        moved.append("phone")
    if not moved:
        return None
    had_before = bool(before_name or before_phone)
    has_after = bool(name or phone)
    if not had_before and has_after:
        change_type = "captured"
    elif had_before and not has_after:
        change_type = "cleared"
    else:
        change_type = "changed"
    return change_type, "+".join(moved)


def record_order_contacts(session: Session, orders, *, client=None,
                          refresh: bool = False, actor: str | None = None
                          ) -> dict:
    """Store each order's buyer name and phone. NEVER raises.

    refresh=False stores a contact only for an order that has neither
    field yet -- the ingest case. refresh=True re-reads and updates one
    that already has a contact, which is how an order whose contact was
    edited in Shopify catches up.

    ★ NEVER RAISES, because every caller is in the middle of something
    that matters more: the order is already ingested and reserved before
    this runs. A contact that could not be read stays NULL and is asked
    about again, which is the correct "we do not know yet".

    ★ AND IT NEVER SAYS WHAT IT STORED. Every log line here counts.

    Caller commits.
    """
    rows = [order for order in (orders or [])
            if getattr(order, "source", None) == SOURCE]
    if not refresh:
        rows = [order for order in rows
                if not getattr(order, "shopify_contact_name", None)
                and not getattr(order, "shopify_contact_phone", None)]
    if not rows:
        return {"asked": 0, "stored": 0, "with_name": 0, "with_phone": 0}
    try:
        contacts = read_order_contacts(
            [order.external_order_id for order in rows], client=client)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        message = f"{type(exc).__name__}: {exc}"
        denied = any(marker in message for marker in DENIAL_MARKERS)
        # ★ ONE LINE, COUNTED, NO VALUES -- and it says which of the two
        # reasons it was, because "no scope" is a permanent condition an
        # operator can fix and a transport error is not.
        logger.warning(
            "Shopify buyer contact: could not be read for %s order(s), "
            "which keep no stored contact and will be asked about again. "
            "Reason: %s. %s",
            len(rows),
            "the app has no read_customers scope on this store"
            if denied else "the read failed",
            message if denied else type(exc).__name__,
        )
        return {"asked": len(rows), "stored": 0, "with_name": 0,
                "with_phone": 0, "failed": message, "denied": denied}

    stored = with_name = with_phone = 0
    for order in rows:
        canonical = canonical_external_order_id(order.external_order_id)
        contact = contacts.get(canonical)
        if contact is None:
            # Shopify returned no node for this id. Not an answer, so
            # nothing is written and it is asked about again.
            continue
        name, phone = contact["name"], contact["phone"]
        if name is None and phone is None and not refresh:
            # Nothing to store and nothing stored: leave NULL rather
            # than write an empty string, which would look like an
            # answer to the next reader.
            continue
        described = _describe(order.shopify_contact_name,
                              order.shopify_contact_phone, name, phone)
        if described is None:
            continue
        change_type, fields_changed = described
        order.shopify_contact_name = name
        order.shopify_contact_phone = phone
        stored += 1
        with_name += 1 if name else 0
        with_phone += 1 if phone else 0
        session.add(ShopifyOrderContactChangeLog(
            order_id=order.id,
            external_order_id=str(order.external_order_id or canonical or ""),
            change_type=change_type,
            fields_changed=fields_changed,
            name_present=bool(name),
            phone_present=bool(phone),
            # ★ A SUMMARY WITH NO VALUE IN IT. Deliberate: see the
            # model's docstring. "name+phone captured" is the whole row.
            change_summary=(
                f"Buyer contact {change_type} from Shopify "
                f"({fields_changed}); name "
                f"{'present' if name else 'empty'}, phone "
                f"{'present' if phone else 'empty'}. Values are not "
                f"recorded here by design."
            ),
            actor=actor,
        ))
    logger.info(
        "Shopify buyer contact: asked about %s order(s), stored %s "
        "(%s with a name, %s with a phone).",
        len(rows), stored, with_name, with_phone,
    )
    return {"asked": len(rows), "stored": stored, "with_name": with_name,
            "with_phone": with_phone}
