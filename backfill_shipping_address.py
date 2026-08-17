"""One-time backfill: populate SalesOrder shipping-address fields for Mana
Pool orders ingested before order sync started capturing them.

Re-fetches each affected order fresh from Mana Pool (GET /seller/orders/{id})
rather than guessing -- an order that can no longer be fetched, or whose
fresh payload still has no shipping_address, is skipped and reported.
"""

import json

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from manapool_service import get_seller_order
from models import SalesOrder


def find_orders_missing_shipping_address(session: Session):
    return (
        session.query(SalesOrder)
        .filter(
            SalesOrder.source == "manapool",
            SalesOrder.shipping_line1.is_(None),
        )
        .all()
    )


def backfill_shipping_address(session: Session, detail_loader=get_seller_order) -> dict:
    backfilled = []
    unfetchable = []
    missing_field = []

    for order in find_orders_missing_shipping_address(session):
        try:
            response = detail_loader(order.external_order_id)
        except Exception as exc:
            unfetchable.append({"order_id": order.id, "external_order_id": order.external_order_id, "error": str(exc)})
            continue

        detail = response.get("order") or response
        address = detail.get("shipping_address")
        if not address:
            missing_field.append({"order_id": order.id, "external_order_id": order.external_order_id})
            continue

        order.shipping_name = address.get("name")
        order.shipping_line1 = address.get("line1")
        order.shipping_line2 = address.get("line2")
        order.shipping_city = address.get("city")
        order.shipping_state = address.get("state")
        order.shipping_postal_code = address.get("postal_code")
        order.shipping_country = address.get("country")
        backfilled.append({"order_id": order.id, "external_order_id": order.external_order_id})

    return {"backfilled": backfilled, "unfetchable": unfetchable, "missing_field": missing_field}


def main():
    with inventory_sync_lease():
        with Session(engine) as session:
            result = backfill_shipping_address(session)
            session.commit()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
