"""Guarded atomic execution of an approved legacy MTGJSON backfill preview."""

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from manapool_service import get_all_seller_inventory, get_single_catalog_by_product_ids
from models import RemoteProductBinding
from mtgjson_backfill_service import (
    BACKFILL_CONFIRMATION,
    build_mtgjson_backfill_preview,
    execute_mtgjson_backfill,
)


EXPECTED_PRODUCTION_CANDIDATES = 138


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--operator-note", default="")
    args = parser.parse_args()

    if args.confirmation != BACKFILL_CONFIRMATION:
        raise SystemExit("Exact MTGJSON backfill confirmation is required; no reads or writes performed.")
    reviewed = json.loads(args.preview.read_text())
    with inventory_sync_lease():
        candidate_ids = set(reviewed.get("candidate_ids") or [])
        seller = get_all_seller_inventory(min_quantity=0)
        with Session(engine) as session:
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
            for start in range(0, len(product_ids), 100):
                payload = get_single_catalog_by_product_ids(product_ids[start:start + 100])
                catalog.extend(payload.get("data") or [])
            fresh = build_mtgjson_backfill_preview(
                session, seller, catalog,
                reviewed.get("seller_snapshot_timestamp"),
                reviewed.get("catalog_snapshot_timestamp"),
            )
            with session.begin_nested():
                result = execute_mtgjson_backfill(
                    session, reviewed, fresh, args.confirmation, args.operator_note,
                    expected_candidate_count=EXPECTED_PRODUCTION_CANDIDATES,
                )
            session.commit()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
