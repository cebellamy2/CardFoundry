"""One-time correction: move InventoryCards sitting in the wrong leg_*
batch to their correct legacy color/type category.

A double-faced card (e.g. Aang, Swift Savior or Invasion of Ixalan) read
as colorless at legacy-import time -- Scryfall leaves `colors` null at
the top level for transform/modal-DFC layouts, and classify_legacy_batch()
didn't fall back to the front face until the fix in scryfall_card_colors()
(see CHANGELOG 1.39.2). That landed every double-faced legacy card in
leg_c/leg_foil_c regardless of its real color.

Re-fetches fresh Scryfall data for every card currently in a leg_* batch,
recomputes its category with the fixed classification logic, and moves it
to the correct batch when that disagrees with where it currently lives.
"""

import json

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from legacy_import_service import (
    LEGACY_BATCH_ORDER,
    classify_legacy_batch,
    fetch_scryfall_cards,
)
from models import Batch, InventoryCard


def find_legacy_cards(session: Session):
    return (
        session.query(InventoryCard, Batch)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(Batch.batch_code.in_(LEGACY_BATCH_ORDER))
        .all()
    )


def recategorize_legacy_batches(session: Session, scryfall_lookup=fetch_scryfall_cards) -> dict:
    rows = find_legacy_cards(session)
    scryfall_ids = sorted({card.scryfall_id for card, _ in rows if card.scryfall_id})

    cards_by_id = {}
    if scryfall_ids:
        result = scryfall_lookup(scryfall_ids)
        cards_by_id = result[0] if isinstance(result, tuple) else result

    batches_by_code = {
        batch.batch_code: batch
        for batch in session.query(Batch).filter(Batch.batch_code.in_(LEGACY_BATCH_ORDER))
    }

    moved = []
    unresolved = []
    unchanged = 0

    for card, current_batch in rows:
        if not card.scryfall_id:
            unresolved.append({
                "card_id": card.id, "name": card.name, "reason": "no scryfall_id",
            })
            continue
        scryfall_card = cards_by_id.get(card.scryfall_id)
        if not scryfall_card:
            unresolved.append({
                "card_id": card.id, "name": card.name, "reason": "scryfall lookup failed",
            })
            continue

        correct_batch_code = classify_legacy_batch({"finish": card.finish or ""}, scryfall_card)
        if correct_batch_code == current_batch.batch_code:
            unchanged += 1
            continue

        target_batch = batches_by_code.get(correct_batch_code)
        if not target_batch:
            target_batch = Batch(batch_code=correct_batch_code, is_archived=False)
            session.add(target_batch)
            session.flush()
            batches_by_code[correct_batch_code] = target_batch

        moved.append({
            "card_id": card.id, "name": card.name,
            "from_batch": current_batch.batch_code, "to_batch": correct_batch_code,
        })
        card.batch_id = target_batch.id

    return {"moved": moved, "unresolved": unresolved, "unchanged": unchanged}


def main():
    with inventory_sync_lease():
        with Session(engine) as session:
            result = recategorize_legacy_batches(session)
            session.commit()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
