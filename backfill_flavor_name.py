"""One-time backfill: populate flavor_name on InventoryCard and OrderItem
rows created before this column existed.

Fetches fresh Scryfall card data for every distinct scryfall_id, rather
than guessing -- a scryfall_id that can no longer be resolved is skipped
and reported; its rows are left blank for a future run.

Unlike backfill_color.py, "needs backfill" can't be detected from
flavor_name IS NULL -- NULL is the correct, permanent value for the
~99.7% of cards that have no alternate name at all, not a sign the field
was never checked. Every card with a scryfall_id is targeted instead,
matching this script's own one-time-catch-up framing: every row that
exists today predates this column. After this one run, every future row
gets flavor_name populated at write time (production import, order sync,
printing correction, single-card add), the same four-plus-one sites
color already uses -- no recurring cron is set up here, unlike color's
scheduled_color_backfill.py, since there's no equivalent live-sync-time
best-effort-failure gap to guard against for this field.
"""

import json

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from legacy_import_service import fetch_scryfall_cards, scryfall_card_flavor_name
from models import InventoryCard, OrderItem


def find_candidate_scryfall_ids(session: Session) -> set[str]:
    inventory_ids = (
        session.query(InventoryCard.scryfall_id)
        .filter(InventoryCard.scryfall_id.isnot(None))
        .distinct()
    )
    item_ids = (
        session.query(OrderItem.scryfall_id)
        .filter(OrderItem.scryfall_id.isnot(None))
        .distinct()
    )
    return {row[0] for row in inventory_ids} | {row[0] for row in item_ids}


def backfill_flavor_name(session: Session, scryfall_lookup=fetch_scryfall_cards) -> dict:
    scryfall_ids = sorted(find_candidate_scryfall_ids(session))
    if not scryfall_ids:
        return {"updated_cards": 0, "updated_items": 0, "unresolved": []}

    result = scryfall_lookup(scryfall_ids)
    cards_by_id = result[0] if isinstance(result, tuple) else result

    resolved = {
        scryfall_id: scryfall_card_flavor_name(card)
        for scryfall_id, card in cards_by_id.items()
    }
    unresolved = sorted(set(scryfall_ids) - set(resolved))

    # Only touch rows whose flavor_name would actually change -- most
    # resolve to the same None they already have, and re-writing every
    # one of ~6,000 rows for no-op values would just be noise in the
    # change history for no reason.
    updated_cards = 0
    for card in session.query(InventoryCard).filter(
        InventoryCard.scryfall_id.in_(resolved),
    ):
        new_value = resolved[card.scryfall_id]
        if card.flavor_name != new_value:
            card.flavor_name = new_value
            updated_cards += 1

    updated_items = 0
    for item in session.query(OrderItem).filter(
        OrderItem.scryfall_id.in_(resolved),
    ):
        new_value = resolved[item.scryfall_id]
        if item.flavor_name != new_value:
            item.flavor_name = new_value
            updated_items += 1

    return {
        "updated_cards": updated_cards,
        "updated_items": updated_items,
        "unresolved": unresolved,
    }


def main():
    with inventory_sync_lease():
        with Session(engine) as session:
            result = backfill_flavor_name(session)
            session.commit()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
