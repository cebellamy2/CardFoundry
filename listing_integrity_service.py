"""Standing checks that a Mana Pool listing and local stock still agree.

TWO CHECKS, AND THE RULE THEY BOTH USE. The v1.180.0 sweep that motivated
this walked RemoteProductBinding.local_card_ids_json, and 11 of the 14
"orphans" it reported were false positives. Membership is bookkeeping
that goes stale; the system's actual truth is
_desired_quantity_for_binding, which counts AVAILABLE cards matching the
binding's four-key identity and ignores membership entirely whenever the
binding has an mtgjson_id. A check that disagrees with the function doing
the writing will cry wolf until nobody reads it.

So both checks below ask the push function what it would write, and
compare that with what Mana Pool actually has.

  over-listed  Mana Pool advertises MORE than we can sell. This is the
               oversell direction and the one that costs money and
               reputation -- the 2026-09-07 incident, six real orders
               against stock that was not there.
  drifted      A card is attached to a binding whose identity is not the
               card's. No remote consequence on its own, because the
               quantity rule keys on identity; it is a correctness
               warning about the bookkeeping the correction paths rely
               on, and it is how a card ends up backing the wrong
               listing later.

UNDER-listing is deliberately NOT reported. 128 rows were under-listed
when this shipped -- Mana Pool advertising fewer than we hold -- which is
lost sales, not oversell, a different problem with a different fix, and
folding it in here would bury the dangerous direction under the harmless
one.
"""

import json
import logging

from sqlalchemy.orm import Session

from manapool_quantity_push_service import _desired_quantity_for_binding
from models import InventoryCard, RemoteProductBinding

logger = logging.getLogger("cardfoundry")

# Greppable in the Railway logs. One marker per check.
OVER_LISTED_MARKER = "LISTING_OVER_LISTED"
IDENTITY_DRIFT_MARKER = "LISTING_IDENTITY_DRIFT"

IDENTITY_KEYS = ("mtgjson_id", "language_id", "condition_id", "finish_id")


def over_listed_rows(session: Session, remote_inventory: list) -> list[dict]:
    """Listings advertising more than the system would write for them.

    Takes the seller inventory the caller already read -- this runs inside
    a sync that has just paginated it, and re-reading would double the
    cost of the run's single largest call for no new information.

    Includes live listings with NO binding at all: those cannot be counted
    by identity, and a listing nothing local can even address is the worst
    case of the class, not an exempt one.
    """
    rows = []
    bound_products = set()
    for binding in session.query(RemoteProductBinding).filter_by(provider="manapool"):
        bound_products.add(str(binding.product_id))
        live = next((i for i in remote_inventory
                     if str(i.get("product_id")) == str(binding.product_id)), None)
        have = (live or {}).get("quantity") or 0
        if have <= 0:
            continue
        want = _desired_quantity_for_binding(session, binding)
        if have <= want:
            continue
        single = (live.get("product") or {}).get("single") or {}
        rows.append({
            "product_id": binding.product_id,
            "binding_id": binding.id,
            "name": single.get("name"),
            "set_code": single.get("set"),
            "collector_number": single.get("number"),
            "identity": f"{binding.language_id}/{binding.condition_id}/{binding.finish_id}",
            "listed_quantity": have,
            "sellable_quantity": want,
            "price_cents": live.get("price_cents"),
            "reason": (
                "nothing sellable is behind this listing"
                if want == 0 else
                f"{have} listed but only {want} sellable"
            ),
        })

    for live in remote_inventory:
        if (live.get("quantity") or 0) <= 0:
            continue
        if str(live.get("product_id")) in bound_products:
            continue
        single = (live.get("product") or {}).get("single") or {}
        matches = _available_matching(session, single)
        if matches:
            # A real card is behind it; the listing is simply not bound to
            # anything locally. Not an over-listing, and zeroing it would
            # delist stock we hold -- it needs a binding, not a takedown.
            continue
        rows.append({
            "product_id": live.get("product_id"),
            "binding_id": None,
            "name": single.get("name"),
            "set_code": single.get("set"),
            "collector_number": single.get("number"),
            "identity": f"{single.get('language_id')}/{single.get('condition_id')}"
                        f"/{single.get('finish_id')}",
            "listed_quantity": live.get("quantity") or 0,
            "sellable_quantity": 0,
            "price_cents": live.get("price_cents"),
            "reason": "no binding and no matching card in inventory",
        })
    return sorted(rows, key=lambda r: -(r.get("price_cents") or 0))


def _available_matching(session: Session, single: dict) -> list:
    if not single.get("mtgjson_id"):
        return []
    return (
        session.query(InventoryCard)
        .filter(
            InventoryCard.status == "available",
            InventoryCard.mtgjson_id.ilike(single.get("mtgjson_id")),
            InventoryCard.language_id.ilike(single.get("language_id") or ""),
            InventoryCard.condition_id.ilike(single.get("condition_id") or ""),
            InventoryCard.finish_id.ilike(single.get("finish_id") or ""),
        ).all()
    )


def identity_drift_rows(session: Session) -> list[dict]:
    """Cards attached to a binding whose identity is not their own.

    Bookkeeping, not a live listing error -- but it is what the identity
    correction paths read, so a drifted row is a correction waiting to
    push the wrong listing. Only cards still in inventory are reported: a
    removed card's stale membership costs nothing and clearing it would be
    rewriting history.
    """
    rows = []
    for binding in session.query(RemoteProductBinding).filter_by(provider="manapool"):
        for card_id in json.loads(binding.local_card_ids_json or "[]"):
            card = session.get(InventoryCard, card_id)
            if not card or card.status != "available":
                continue
            # Only keys the binding actually ASSERTS. A binding with no
            # mtgjson_id is not drifted, it is an override binding --
            # _desired_quantity_for_binding deliberately counts those by
            # membership instead of identity, so they work correctly.
            # Comparing a NULL against the card's real value reported
            # three perfectly good bindings as drifted the moment they
            # were created (2026-09-17).
            differs = [
                key for key in IDENTITY_KEYS
                if getattr(binding, key, None)
                and str(getattr(card, key, None) or "").upper()
                != str(getattr(binding, key, None) or "").upper()
            ]
            if not differs:
                continue
            rows.append({
                "card_id": card.id,
                "name": card.name,
                "binding_id": binding.id,
                "product_id": binding.product_id,
                "differs_on": differs,
                "card_identity": "/".join(
                    str(getattr(card, k, None) or "-") for k in IDENTITY_KEYS[1:]),
                "binding_identity": "/".join(
                    str(getattr(binding, k, None) or "-") for k in IDENTITY_KEYS[1:]),
                "reason": "this card is attached to a listing for a different "
                          + ", ".join(k.replace("_id", "") for k in differs),
            })
    return rows


def log_listing_integrity(session: Session, remote_inventory: list) -> dict:
    """Both counts, on one line each, for a sync run's summary."""
    over = over_listed_rows(session, remote_inventory)
    drift = identity_drift_rows(session)
    if over:
        logger.warning(
            "%s: %s listing(s) advertise more than is sellable; highest value "
            "%s at %s cents",
            OVER_LISTED_MARKER, len(over), over[0]["name"], over[0]["price_cents"],
        )
    else:
        logger.info("%s: none", OVER_LISTED_MARKER)
    if drift:
        logger.warning(
            "%s: %s available card(s) attached to a binding with a different identity",
            IDENTITY_DRIFT_MARKER, len(drift),
        )
    else:
        logger.info("%s: none", IDENTITY_DRIFT_MARKER)
    return {"over_listed": over, "identity_drift": drift}
