"""Print a read-only MTGJSON backfill preview as JSON."""

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from manapool_service import get_all_seller_inventory, get_single_catalog_by_product_ids
from models import InventoryCard, RemoteProductBinding
from mtgjson_backfill_service import build_mtgjson_backfill_preview


def main():
    database = Path("cardfoundry.db").resolve()
    engine = create_engine(f"sqlite:///file:{database}?mode=ro&uri=true")
    seller = get_all_seller_inventory(min_quantity=0)
    with Session(engine) as session:
        candidate_ids = {
            card.id for card in session.query(InventoryCard).filter(
                InventoryCard.status == "available",
                InventoryCard.mtgjson_id.is_(None),
            )
        }
        product_ids = sorted({
            binding.product_id
            for binding in session.query(RemoteProductBinding).all()
            if binding.binding_status == "validated"
            and any(
                card_id in candidate_ids
                for card_id in json.loads(binding.local_card_ids_json or "[]")
            )
        })
        catalog = []
        catalog_as_of = []
        for start in range(0, len(product_ids), 100):
            payload = get_single_catalog_by_product_ids(product_ids[start:start + 100])
            catalog.extend(payload.get("data") or [])
            if (payload.get("meta") or {}).get("as_of"):
                catalog_as_of.append(payload["meta"]["as_of"])
        result = build_mtgjson_backfill_preview(
            session, seller, catalog, datetime.now(timezone.utc).isoformat(),
            max(catalog_as_of) if catalog_as_of else None,
        )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
