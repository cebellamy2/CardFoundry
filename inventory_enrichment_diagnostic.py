"""Read-only-by-default exact-variant enrichment diagnostic."""

import argparse
import json

from sqlalchemy.orm import Session

from database import engine, upgrade_existing_database
from inventory_enrichment_service import enrich_inventory_cards
from manapool_service import get_all_seller_inventory
from models import Batch, InventoryCard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--persist-local",
        action="store_true",
        help="Fill missing local metadata after unambiguous matching.",
    )
    args = parser.parse_args()
    upgrade_existing_database()

    seller_inventory = get_all_seller_inventory(min_quantity=0)
    with Session(engine) as session:
        cards = (
            session.query(InventoryCard)
            .join(Batch, InventoryCard.batch_id == Batch.id)
            .filter(
                InventoryCard.status == "available",
                Batch.is_archived == False,
            )
            .order_by(InventoryCard.id)
            .all()
        )
        report = enrich_inventory_cards(cards, seller_inventory, persist=args.persist_local)
        if args.persist_local:
            session.commit()
        print(json.dumps({
            "mode": "persist-local" if args.persist_local else "read-only",
            "summary": report["summary"],
            "examples": report["examples"],
        }, indent=2))


if __name__ == "__main__":
    main()
