"""One-time backfill: populate color on InventoryCard and OrderItem rows
created before this column existed (or before it was renamed from the
short-lived "color identity" version of this column -- see the rename in
database.py for why any old values needed to be invalidated first).

Fetches fresh Scryfall card data for every distinct scryfall_id missing
color, rather than guessing -- a scryfall_id that can no longer be resolved
is skipped and reported; its rows are left blank for a future run.
"""

import json

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from legacy_import_service import fetch_scryfall_cards, scryfall_card_colors, wubrg_color_string
import logging

from print_treatment_service import backfill_print_treatments
from models import InventoryCard, OrderItem

logger = logging.getLogger("cardfoundry")


def find_unresolved_scryfall_ids(session: Session) -> set[str]:
    """Every distinct scryfall_id missing color OR type_line.

    type_line joined the predicate when the packing slip's (L)/(C) marker
    needed it. It costs NOTHING extra: this backfill already fetches the
    whole Scryfall card and previously used only `colors`.
    """
    inventory_ids = (
        session.query(InventoryCard.scryfall_id)
        .filter(
            InventoryCard.scryfall_id.isnot(None),
            or_(InventoryCard.color.is_(None), InventoryCard.type_line.is_(None)),
        )
        .distinct()
    )
    item_ids = (
        session.query(OrderItem.scryfall_id)
        .filter(
            OrderItem.scryfall_id.isnot(None),
            or_(OrderItem.color.is_(None), OrderItem.type_line.is_(None)),
        )
        .distinct()
    )
    return {row[0] for row in inventory_ids} | {row[0] for row in item_ids}


# ★ WHY FORWARD FILL LIVES HERE AND NOT AT INTAKE (2026-10-13). This job
# already runs hourly, already batches Scryfall's 75-per-call collection
# endpoint, already honours its 429s, and is already additive-only. Adding
# a Scryfall call to the import path instead would put a third-party
# network dependency inside intake, where a transient failure would either
# fail the import or be swallowed -- and intake is the one place an
# operator is standing there waiting. An hour's delay on a cosmetic filter
# is a far better trade than a brittle import.
#
# It is capped per run so one tick can never turn into a thousand Scryfall
# calls after a big import; the next tick picks up the rest, because
# printing_ids_needing_treatment asks what is still missing.
PRINT_TREATMENT_IDS_PER_RUN = 1500



def _fill_print_treatments(session: Session, scryfall_lookup) -> dict:
    """Store treatments for any printing that has none yet.

    Never raises: a Scryfall problem must not stop the colour backfill
    this job exists for. The printings simply keep no row and the next
    tick tries again.
    """
    try:
        return backfill_print_treatments(
            session, scryfall_lookup=scryfall_lookup, apply=True,
            limit=PRINT_TREATMENT_IDS_PER_RUN,
        )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "colour backfill: the print-treatment fill failed and is left for "
            "the next run (%s: %s); the colour fill below is unaffected.",
            type(exc).__name__, exc,
        )
        return {"pending": None, "stored": 0, "error": f"{type(exc).__name__}: {exc}"}

def backfill_color(session: Session, scryfall_lookup=fetch_scryfall_cards) -> dict:
    treatments = _fill_print_treatments(session, scryfall_lookup)

    scryfall_ids = sorted(find_unresolved_scryfall_ids(session))
    if not scryfall_ids:
        return {
            "backfilled_cards": 0,
            "backfilled_items": 0,
            "backfilled_card_type_lines": 0,
            "backfilled_item_type_lines": 0,
            "unresolved": [],
            "print_treatments": treatments,
        }

    result = scryfall_lookup(scryfall_ids)
    cards_by_id = result[0] if isinstance(result, tuple) else result

    resolved = {
        scryfall_id: {
            "color": wubrg_color_string(scryfall_card_colors(card)),
            "type_line": card.get("type_line"),
        }
        for scryfall_id, card in cards_by_id.items()
    }
    unresolved = sorted(set(scryfall_ids) - set(resolved))

    # Each field is filled only where it is still NULL, so a row that
    # already has a colour keeps it and only gains the type line.
    backfilled_cards = 0
    backfilled_card_type_lines = 0
    for card in session.query(InventoryCard).filter(
        InventoryCard.scryfall_id.in_(resolved),
        or_(InventoryCard.color.is_(None), InventoryCard.type_line.is_(None)),
    ):
        values = resolved[card.scryfall_id]
        if card.color is None:
            card.color = values["color"]
            backfilled_cards += 1
        if card.type_line is None and values["type_line"]:
            card.type_line = values["type_line"]
            backfilled_card_type_lines += 1

    backfilled_items = 0
    backfilled_item_type_lines = 0
    for item in session.query(OrderItem).filter(
        OrderItem.scryfall_id.in_(resolved),
        or_(OrderItem.color.is_(None), OrderItem.type_line.is_(None)),
    ):
        values = resolved[item.scryfall_id]
        if item.color is None:
            item.color = values["color"]
            backfilled_items += 1
        if item.type_line is None and values["type_line"]:
            item.type_line = values["type_line"]
            backfilled_item_type_lines += 1

    return {
        "backfilled_cards": backfilled_cards,
        "backfilled_items": backfilled_items,
        "backfilled_card_type_lines": backfilled_card_type_lines,
        "backfilled_item_type_lines": backfilled_item_type_lines,
        "unresolved": unresolved,
    }


def main():
    with inventory_sync_lease():
        with Session(engine) as session:
            result = backfill_color(session)
            session.commit()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
