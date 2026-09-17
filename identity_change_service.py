"""One safe way to change a card's listing identity.

WHY THIS EXISTS. Printing, condition, finish and language are the four
fields a Mana Pool listing is keyed on. Changing any of them locally,
without telling Mana Pool, leaves the OLD listing live at the OLD
product_id with its quantity intact and nothing backing it -- the
remote_only_unmanaged class, which nothing reconciles. That is exactly
the 2026-09-07 incident (v1.119.0's condition backfill orphaned 1,924
listings; six real orders arrived against them).

Two live paths could do it. apply_printing_correction changes all of
them at once, and the card edit form changes condition and finish. Both
now come through here, so there is one rule rather than two that can
drift.

THE RULE. If the card currently backs a Mana Pool listing, that
listing's quantity is reduced on Mana Pool FIRST -- through the existing
quantity-push machinery, recomputing from local state exactly as every
other push does, no arithmetic of our own -- and the local change only
proceeds if that write lands. If Mana Pool refuses, nothing changes at
all: the correction is abandoned, not completed-and-flagged.

The new identity is deliberately NOT listed here. Publishing is the
new-listing path's job on the next sync, and it carries a pricing
decision this function has no business making.
"""

import logging

from sqlalchemy.orm import Session

from manapool_quantity_push_service import (
    QuantityPushFailed,
    bindings_backing_card,
    push_binding_quantity_strict,
)
from models import InventoryListingStatus

logger = logging.getLogger("cardfoundry")

IDENTITY_FIELDS = ("mtgjson_id", "language_id", "condition_id", "finish_id",
                   "scryfall_id", "set_code", "collector_number")


def identity_snapshot(card) -> dict:
    return {field: getattr(card, field, None) for field in IDENTITY_FIELDS}


def identity_would_change(card, after: dict) -> bool:
    """Whether any listing-identity field actually moves.

    Only the four Mana Pool keys on. set_code/collector_number/scryfall_id
    are carried in the snapshot for the audit trail, but a card whose
    printing is re-labelled without its mtgjson identity moving is not a
    different listing and must not pay for a remote write.
    """
    before = identity_snapshot(card)
    return any(
        str(before.get(field) or "").upper() != str(after.get(field) or "").upper()
        for field in ("mtgjson_id", "language_id", "condition_id", "finish_id")
    )


def clear_listing_status(session: Session, card_id: int) -> None:
    """Drop the card's cached listed/not_listed value.

    Deleting rather than setting "not_listed": the cache answers "is this
    card's identity live on Mana Pool", and immediately after a
    correction the honest answer is "not known until the next
    reconciliation". Writing not_listed would be a claim, and the next
    sync overwrites it either way.
    """
    deleted = (
        session.query(InventoryListingStatus)
        .filter(InventoryListingStatus.inventory_card_id == card_id)
        .delete(synchronize_session=False)
    )
    if deleted:
        logger.info(
            "identity correction: cleared cached listing status for card %s", card_id,
        )


def bindings_to_retire(session: Session, card) -> list:
    """The bindings backing the card RIGHT NOW.

    Must be called BEFORE the identity fields change. Once the card has
    moved, an identity lookup finds the NEW binding instead, and pushing
    that one would write the new listing's quantity while leaving the old
    listing exactly as it was -- the orphan this module exists to
    prevent, with an extra remote write for company.
    """
    return bindings_backing_card(session, card)


def retire_old_listings(session: Session, bindings: list, card_id: int) -> dict:
    """Reduce those listings' quantities on Mana Pool.

    Must be called AFTER the identity fields have changed: the push
    recomputes the desired quantity from current local state, so the card
    no longer matching is what makes the number drop. Called before the
    change, it would rewrite the number it already had.

    Raises QuantityPushFailed if Mana Pool will not take the write. The
    caller must let that abort the whole correction.
    """
    if not bindings:
        logger.info(
            "identity correction: card %s backed no validated binding -- "
            "no Mana Pool write needed", card_id,
        )
        return {"pushed": [], "bindings": 0}
    pushed = []
    for binding in bindings:
        quantity = push_binding_quantity_strict(session, binding)
        pushed.append({
            "binding_id": binding.id,
            "product_id": binding.product_id,
            "quantity_written": quantity,
        })
    return {"pushed": pushed, "bindings": len(bindings)}


def identity_would_change_from(before: dict, card) -> bool:
    """Same test as identity_would_change, from a snapshot taken earlier.

    The card edit form mutates the card in place across a long block, so
    there is no "after" dict to compare against -- only the card itself
    and a snapshot from before it was touched.
    """
    return any(
        str(before.get(field) or "").upper()
        != str(getattr(card, field, None) or "").upper()
        for field in ("mtgjson_id", "language_id", "condition_id", "finish_id")
    )
