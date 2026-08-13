"""Guarded production import for the approved cleaned legacy inventory."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import engine
from inventory_enrichment_service import enrich_inventory_cards
from legacy_import_service import (
    LEGACY_BATCH_ORDER,
    build_legacy_plan,
    import_legacy_plan,
)
from manapool_service import get_all_seller_inventory
from models import (
    Batch,
    ImportRecord,
    InventoryCard,
    InventorySyncJob,
    OrderItem,
    PendingImport,
    PendingLegacyImport,
    PickAllocation,
    PickWave,
    PricingJob,
    RemoteProductBinding,
    SalesOrder,
)


APPROVED_SOURCE_SHA256 = (
    "b51691216a060a48abd90c7cc531a8f73f94ede716f9715ef64ec8053d66b72f"
)
EXPECTED_SOURCE_PHYSICAL = 6372
EXPECTED_SOURCE_ROWS = 4481
EXPECTED_SINGLES = 6371
EXPECTED_EXCLUDED = {
    "name": "Warhammer 40000 Commander Deck Forces of the Imperium",
    "quantity": 1,
    "classification": "excluded_sealed_product",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_empty_production_database(session: Session) -> None:
    models = (
        InventoryCard, Batch, ImportRecord, RemoteProductBinding,
        SalesOrder, OrderItem, PickAllocation, PickWave, PricingJob,
        InventorySyncJob, PendingImport, PendingLegacyImport,
    )
    populated = {
        model.__tablename__: session.query(func.count()).select_from(model).scalar()
        for model in models
        if session.query(func.count()).select_from(model).scalar()
    }
    if populated:
        raise RuntimeError(f"Production database is not empty: {populated}")


def _validate_exclusion(plan: dict) -> list[dict]:
    rows = plan.get("non_single_rows") or []
    if len(rows) != 1:
        raise RuntimeError(f"Expected one excluded sealed-product row; found {len(rows)}")
    row = rows[0]
    if row.get("name") != EXPECTED_EXCLUDED["name"] or int(row.get("quantity", 0)) != 1:
        raise RuntimeError(f"Unexpected excluded source row: {row}")
    return [{
        "classification": EXPECTED_EXCLUDED["classification"],
        "name": row["name"],
        "set_code": row.get("set_code"),
        "quantity": int(row["quantity"]),
        "reason": "Sealed product is outside CardFoundry singles inventory",
    }]


def run_import(source: Path, audit_dir: Path) -> dict:
    source = source.resolve()
    source_bytes = source.read_bytes()
    source_hash = _sha256(source_bytes)
    if source_hash != APPROVED_SOURCE_SHA256:
        raise RuntimeError(f"Source SHA-256 is not approved: {source_hash}")
    csv_text = source_bytes.decode("utf-8-sig")

    with Session(engine) as session:
        _require_empty_production_database(session)
        plan = build_legacy_plan(session, csv_text)

    if plan["source_row_count"] != EXPECTED_SOURCE_ROWS:
        raise RuntimeError(f"Source row count is {plan['source_row_count']}, expected 4,481")
    if plan["source_physical_total"] != EXPECTED_SOURCE_PHYSICAL:
        raise RuntimeError(
            f"Source physical count is {plan['source_physical_total']}, expected 6,372"
        )
    if plan["unresolved_total"]:
        raise RuntimeError(f"Unresolved MTG singles: {plan['unresolved_total']}")
    exclusions = _validate_exclusion(plan)
    if plan["planned_import_total"] != EXPECTED_SINGLES:
        raise RuntimeError(
            f"Planned singles are {plan['planned_import_total']}, expected 6,371"
        )

    # GET-only and deliberately completed before opening the local write transaction.
    seller_inventory = get_all_seller_inventory(min_quantity=0)
    completed_at = datetime.now(timezone.utc)

    with Session(engine) as session:
        _require_empty_production_database(session)
        inserted = import_legacy_plan(session, plan, source.name, source_hash)
        session.flush()
        if inserted != EXPECTED_SINGLES:
            raise RuntimeError(f"Imported {inserted} singles, expected 6,371")

        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        enrichment = enrich_inventory_cards(cards, seller_inventory, persist=True)
        session.flush()
        expected_enrichment = {
            "total_active_inventory_rows": EXPECTED_SINGLES,
            "fully_enriched": EXPECTED_SINGLES,
            "missing_language": 0,
            "missing_mtgjson_id": 0,
            "ambiguous": 0,
            "unmapped": 0,
            "conflicts": 0,
            "would_enrich": EXPECTED_SINGLES,
            "fully_enriched_after_enrichment": EXPECTED_SINGLES,
        }
        if enrichment["summary"] != expected_enrichment:
            raise RuntimeError(
                "Canonicalization totals differ: "
                + json.dumps(enrichment["summary"], sort_keys=True)
            )

        batch_rows = session.query(Batch).order_by(Batch.batch_code).all()
        batch_counts = {
            batch.batch_code: session.query(func.count(InventoryCard.id)).filter(
                InventoryCard.batch_id == batch.id
            ).scalar()
            for batch in batch_rows
        }
        if set(batch_counts) != set(LEGACY_BATCH_ORDER) or len(batch_rows) != 16:
            raise RuntimeError(f"Unexpected production batches: {sorted(batch_counts)}")
        if any(batch.is_archived for batch in batch_rows):
            raise RuntimeError("A production legacy batch is archived")
        if session.query(RemoteProductBinding).count():
            raise RuntimeError("Legacy import unexpectedly created remote product bindings")
        if session.query(InventoryCard).filter(InventoryCard.status != "available").count():
            raise RuntimeError("A production legacy card is not available")
        session.commit()

    audit = {
        "status": "completed",
        "classification": "production_legacy_import",
        "source_filename": source.name,
        "source_sha256": source_hash,
        "source_row_count": EXPECTED_SOURCE_ROWS,
        "source_physical_quantity": EXPECTED_SOURCE_PHYSICAL,
        "imported_mtg_singles": EXPECTED_SINGLES,
        "excluded_sealed_product_count": 1,
        "excluded_rows": exclusions,
        "batch_count": 16,
        "batch_counts": batch_counts,
        "canonicalization": enrichment["summary"],
        "remote_product_bindings_created": 0,
        "completed_at": completed_at.isoformat(),
        "external_write_calls": 0,
    }
    audit_dir.mkdir(parents=True, exist_ok=True)
    timestamp = completed_at.strftime("%Y%m%dT%H%M%S.%fZ")
    audit_path = audit_dir / f"production-legacy-import-{timestamp}.json"
    temporary = audit_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    temporary.replace(audit_path)
    return {**audit, "audit_path": str(audit_path.resolve())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--audit-dir", default="audits")
    args = parser.parse_args()
    result = run_import(Path(args.source), Path(args.audit_dir))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
