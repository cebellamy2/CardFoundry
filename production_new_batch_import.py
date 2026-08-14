"""CLI wrapper around CardFoundry's authoritative production import service."""

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from database import engine, initialize_database
from inventory_sync_service import inventory_sync_lease
from legacy_import_service import fetch_scryfall_cards
from manapool_service import get_all_seller_inventory, get_single_catalog_by_scryfall_ids
from production_import_service import (
    build_production_import_preview,
    commit_production_import,
)


def run_import(
    source: Path, batch_code: str, source_location: str,
    approved_sha256: str, expected_rows: int, default_condition: str,
    audit_dir: Path,
) -> dict:
    contents = source.read_bytes()
    seller_inventory = get_all_seller_inventory(min_quantity=0)
    with Session(engine) as session:
        preview = build_production_import_preview(
            session, contents, source.name, batch_code, source_location,
            seller_inventory, get_single_catalog_by_scryfall_ids,
            default_condition=default_condition,
            scryfall_lookup=fetch_scryfall_cards,
        )
    if preview["source_hash"] != approved_sha256:
        raise RuntimeError("Source hash differs from the approved preview")
    if preview["csv_row_count"] != expected_rows:
        raise RuntimeError("CSV row count differs from the approved preview")
    with inventory_sync_lease():
        with Session(engine) as session:
            with session.begin():
                return commit_production_import(session, preview, contents, audit_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--batch-code", required=True)
    parser.add_argument("--source-location", required=True)
    parser.add_argument("--approved-sha256", required=True)
    parser.add_argument("--expected-rows", required=True, type=int)
    parser.add_argument("--default-condition", required=True)
    parser.add_argument("--audit-dir", default="audits")
    args = parser.parse_args()
    initialize_database()
    result = run_import(
        Path(args.source), args.batch_code, args.source_location,
        args.approved_sha256, args.expected_rows, args.default_condition,
        Path(args.audit_dir),
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
