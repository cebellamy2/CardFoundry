"""One-time repair: reset InventoryCard.color and OrderItem.color to NULL
so backfill_color.py re-resolves every row with the fixed capture logic
(scryfall_card_colors()'s double-faced-card fallback, wubrg_color_string()'s
canonical ordering -- see CHANGELOG 1.39.2).

backfill_color.py only fills in rows where color IS NULL; it silently
skips rows that already hold a (possibly wrong) value. Existing production
data may have wrong values from before the 1.39.2 fix -- colorless for
double-faced cards, alphabetically- rather than WUBRG-ordered letters for
multicolor cards -- so those rows must be reset before backfill_color.py
can re-resolve them.
"""

import json

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from models import InventoryCard, OrderItem


def reset_color(session: Session) -> dict:
    cards = session.query(InventoryCard).filter(InventoryCard.color.isnot(None)).update(
        {InventoryCard.color: None}, synchronize_session=False,
    )
    items = session.query(OrderItem).filter(OrderItem.color.isnot(None)).update(
        {OrderItem.color: None}, synchronize_session=False,
    )
    return {"reset_cards": cards, "reset_items": items}


def main():
    with inventory_sync_lease():
        with Session(engine) as session:
            result = reset_color(session)
            session.commit()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
