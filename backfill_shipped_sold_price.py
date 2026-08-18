"""One-time backfill: populate OrderItem.price_cents and InventoryCard.sold_price
for cards that shipped through the Mana Pool order pipeline before ingestion
started capturing price_cents.

Re-fetches each affected order fresh from Mana Pool (GET /seller/orders/{id})
rather than guessing at a value -- an order that can no longer be fetched, or
a line that can't be matched in the fresh response, is skipped and reported;
its card's sold_price is left blank.
"""

import json

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from manapool_service import get_seller_order
from models import InventoryCard, OrderItem, PickAllocation, SalesOrder


def _identity(value_dict: dict) -> tuple:
    return (
        str(value_dict.get("mtgjson_id") or "").upper(),
        str(value_dict.get("language_id") or "").upper(),
        str(value_dict.get("condition_id") or "").upper(),
        str(value_dict.get("finish_id") or "").upper(),
    )


def find_unpriced_shipped_cards(session: Session):
    return (
        session.query(InventoryCard, OrderItem, SalesOrder)
        .join(PickAllocation, PickAllocation.inventory_card_id == InventoryCard.id)
        .join(OrderItem, OrderItem.id == PickAllocation.order_item_id)
        .join(SalesOrder, SalesOrder.id == OrderItem.order_id)
        .filter(
            InventoryCard.status == "sold",
            InventoryCard.sold_price.is_(None),
            SalesOrder.source == "manapool",
        )
        .all()
    )


def backfill_sold_price(session: Session, detail_loader=get_seller_order) -> dict:
    by_order: dict[str, list] = {}
    for card, order_item, order in find_unpriced_shipped_cards(session):
        by_order.setdefault(order.external_order_id, []).append((card, order_item))

    backfilled = []
    unfetchable = []
    unmatched = []

    for external_order_id, pairs in by_order.items():
        try:
            response = detail_loader(external_order_id)
        except Exception as exc:
            unfetchable.append({"external_order_id": external_order_id, "error": str(exc)})
            continue

        detail = response.get("order") or response
        price_by_identity = {}
        for remote_item in detail.get("items") or []:
            single = (remote_item.get("product") or {}).get("single") or {}
            if not single:
                continue
            price_by_identity[_identity(single)] = remote_item.get("price_cents")

        for card, order_item in pairs:
            key = _identity({
                "mtgjson_id": order_item.mtgjson_id, "language_id": order_item.language_id,
                "condition_id": order_item.condition_id, "finish_id": order_item.finish_id,
            })
            price_cents = price_by_identity.get(key)
            if price_cents is None:
                unmatched.append({
                    "card_id": card.id, "name": card.name, "external_order_id": external_order_id,
                    "order_item_id": order_item.id,
                })
                continue
            order_item.price_cents = int(price_cents)
            card.sold_price = int(price_cents) / 100
            backfilled.append({
                "card_id": card.id, "name": card.name, "external_order_id": external_order_id,
                "sold_price": card.sold_price,
            })

    return {"backfilled": backfilled, "unfetchable": unfetchable, "unmatched": unmatched}


def main():
    with inventory_sync_lease():
        with Session(engine) as session:
            result = backfill_sold_price(session)
            session.commit()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
