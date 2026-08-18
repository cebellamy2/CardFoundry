"""One-time backfill: populate color on InventoryCard and OrderItem rows
created before this column existed (or before it was renamed from the
short-lived "color identity" version of this column -- see the rename in
database.py for why any old values needed to be invalidated first).

Fetches fresh Scryfall card data for every distinct scryfall_id missing
color, rather than guessing -- a scryfall_id that can no longer be resolved
is skipped and reported; its rows are left blank for a future run.
"""

import json

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from legacy_import_service import fetch_scryfall_cards, scryfall_card_colors, wubrg_color_string
from models import InventoryCard, OrderItem


def find_unresolved_scryfall_ids(session: Session) -> set[str]:
    inventory_ids = (
        session.query(InventoryCard.scryfall_id)
        .filter(
            InventoryCard.scryfall_id.isnot(None),
            InventoryCard.color.is_(None),
        )
        .distinct()
    )
    item_ids = (
        session.query(OrderItem.scryfall_id)
        .filter(
            OrderItem.scryfall_id.isnot(None),
            OrderItem.color.is_(None),
        )
        .distinct()
    )
    return {row[0] for row in inventory_ids} | {row[0] for row in item_ids}


def backfill_color(session: Session, scryfall_lookup=fetch_scryfall_cards) -> dict:
    scryfall_ids = sorted(find_unresolved_scryfall_ids(session))
    if not scryfall_ids:
        return {"backfilled_cards": 0, "backfilled_items": 0, "unresolved": []}

    result = scryfall_lookup(scryfall_ids)
    cards_by_id = result[0] if isinstance(result, tuple) else result

    resolved = {
        scryfall_id: wubrg_color_string(scryfall_card_colors(card))
        for scryfall_id, card in cards_by_id.items()
    }
    unresolved = sorted(set(scryfall_ids) - set(resolved))

    backfilled_cards = 0
    for card in session.query(InventoryCard).filter(
        InventoryCard.scryfall_id.in_(resolved), InventoryCard.color.is_(None),
    ):
        card.color = resolved[card.scryfall_id]
        backfilled_cards += 1

    backfilled_items = 0
    for item in session.query(OrderItem).filter(
        OrderItem.scryfall_id.in_(resolved), OrderItem.color.is_(None),
    ):
        item.color = resolved[item.scryfall_id]
        backfilled_items += 1

    return {
        "backfilled_cards": backfilled_cards,
        "backfilled_items": backfilled_items,
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
