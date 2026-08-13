"""Rehearse the production reset and legacy rebuild in an isolated database."""

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session

from catalog_resolution_service import (
    persist_validated_bindings,
    resolve_catalog_bindings,
)
from inventory_enrichment_service import enrich_inventory_cards
from legacy_import_service import (
    LEGACY_BATCH_ORDER,
    build_legacy_plan,
    import_legacy_plan,
)
from manapool_service import (
    get_all_seller_inventory,
    get_single_catalog_by_scryfall_ids,
)
from models import Batch, InventoryCard, RemoteProductBinding
from production_reset_service import RESET_CONFIRMATION, execute_production_reset


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_clone(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
        source_db.backup(target_db)


def _source_duplicate_summary(csv_text: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    identities = Counter(
        (
            (row.get("Scryfall ID") or "").strip().lower(),
            (row.get("Language") or "").strip().upper() or "EN",
            (row.get("Condition") or "").strip().upper(),
            (row.get("Foil") or "").strip().lower(),
        )
        for row in rows
    )
    repeated = [count for count in identities.values() if count > 1]
    return {
        "source_rows": len(rows),
        "distinct_exact_variant_rows": len(identities),
        "repeated_exact_variant_groups": len(repeated),
        "additional_repeated_rows": sum(count - 1 for count in repeated),
    }


def _catalog_resolution(cards: list[InventoryCard]) -> dict:
    by_scryfall = {}
    for card in cards:
        if card.scryfall_id:
            by_scryfall.setdefault(card.scryfall_id, []).append(card)

    combined = {"meta": {}, "data": []}
    ids = sorted(by_scryfall)
    for start in range(0, len(ids), 100):
        payload = get_single_catalog_by_scryfall_ids(ids[start : start + 100])
        combined["data"].extend(payload.get("data") or [])
        if payload.get("meta"):
            combined["meta"] = payload["meta"]
    return resolve_catalog_bindings(cards, combined)


def run_rehearsal(real_database: Path, legacy_csv: Path) -> dict:
    real_database = real_database.resolve()
    legacy_csv = legacy_csv.resolve()
    if not real_database.is_file() or not legacy_csv.is_file():
        raise RuntimeError("The real database and legacy CSV must both exist")

    real_hash_before = _sha256(real_database)
    source_bytes = legacy_csv.read_bytes()
    csv_text = source_bytes.decode("utf-8-sig")
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    with tempfile.TemporaryDirectory(prefix="cardfoundry-production-rehearsal-") as temp:
        temp_path = Path(temp)
        rehearsal_database = temp_path / "cardfoundry-rehearsal.db"
        if rehearsal_database.resolve() == real_database:
            raise RuntimeError("Rehearsal database resolved to the real database")
        _sqlite_clone(real_database, rehearsal_database)
        rehearsal_engine = create_engine(
            f"sqlite:///{rehearsal_database}",
            connect_args={"check_same_thread": False},
        )

        reset = execute_production_reset(
            rehearsal_engine,
            RESET_CONFIRMATION,
            backup_dir=temp_path / "backups",
        )

        with Session(rehearsal_engine) as session:
            plan = build_legacy_plan(session, csv_text)
            if plan["source_physical_total"] != 6372:
                raise RuntimeError("Legacy source physical total is not 6,372")
            if plan["source_row_count"] != 4481:
                raise RuntimeError("Legacy source row total is not 4,481")
            if plan["unresolved_total"]:
                raise RuntimeError(
                    "Legacy plan contains unresolved Scryfall cards"
                )
            expected_single_cards = (
                plan["source_physical_total"] - plan["non_single_total"]
            )
            inserted = import_legacy_plan(
                session, plan, legacy_csv.name, source_hash,
            )
            if inserted != expected_single_cards:
                raise RuntimeError(
                    f"Legacy import inserted {inserted}, expected "
                    f"{expected_single_cards} single cards"
                )
            session.commit()

        # Seller inventory and catalog calls are GET-only. All persistence below
        # targets only the temporary rehearsal database.
        seller_inventory = get_all_seller_inventory(min_quantity=0)
        with Session(rehearsal_engine) as session:
            cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
            enrichment = enrich_inventory_cards(cards, seller_inventory, persist=True)
            session.commit()

            cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
            unresolved = [
                card for card in cards
                if not all((card.mtgjson_id, card.language_id, card.condition_id, card.finish_id))
            ]
            catalog = _catalog_resolution(unresolved) if unresolved else {
                "rows": [],
                "summary": {
                    "physical_cards": 0,
                    "validated_physical_cards": 0,
                    "validated_remote_bindings": 0,
                    "held_physical_cards": 0,
                },
            }
            bindings_created = persist_validated_bindings(session, catalog)
            session.commit()

            physical = session.query(func.count(InventoryCard.id)).scalar()
            available = session.query(func.count(InventoryCard.id)).filter(
                InventoryCard.status == "available"
            ).scalar()
            batches = session.query(Batch).order_by(Batch.batch_code).all()
            fully_canonical = session.query(func.count(InventoryCard.id)).filter(
                InventoryCard.mtgjson_id.isnot(None),
                InventoryCard.language_id.isnot(None),
                InventoryCard.condition_id.isnot(None),
                InventoryCard.finish_id.isnot(None),
            ).scalar()
            missing_normalized = session.query(func.count(InventoryCard.id)).filter(
                (InventoryCard.language_id.is_(None))
                | (InventoryCard.condition_id.is_(None))
                | (InventoryCard.finish_id.is_(None))
            ).scalar()
            test_batches = session.query(Batch).filter(
                Batch.batch_code.in_(["CAM_TEST", "0.0.17_TEST"])
            ).count()
            binding_count = session.query(func.count(RemoteProductBinding.id)).scalar()

        with sqlite3.connect(rehearsal_database) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]

        application = subprocess.run(
            [sys.executable, "-c", "import main"],
            cwd=temp_path,
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent)},
            capture_output=True,
            text=True,
        )
        if application.returncode:
            raise RuntimeError("Application import failed: " + application.stderr[-2000:])

        result = {
            "real_database": str(real_database),
            "real_database_hash_before": real_hash_before,
            "real_database_hash_after": _sha256(real_database),
            "real_database_unchanged": real_hash_before == _sha256(real_database),
            "temporary_reset": reset,
            "legacy_source": {
                "filename": legacy_csv.name,
                "sha256": source_hash,
                "expected_rows": 4481,
                "expected_physical_cards": 6372,
                "non_single_physical_items": plan["non_single_total"],
                "non_single_rows": plan["non_single_rows"],
                **_source_duplicate_summary(csv_text),
            },
            "reconstructed": {
                "physical_cards": physical,
                "active_available_cards": available,
                "batch_count": len(batches),
                "batch_codes": [batch.batch_code for batch in batches],
                "expected_batch_codes": LEGACY_BATCH_ORDER,
                "test_batches_surviving": test_batches,
                "fully_canonical_rows": fully_canonical,
                "missing_normalized_metadata": missing_normalized,
                "remote_product_bindings": binding_count,
                "new_remote_bindings_created": bindings_created,
                "seller_enrichment": enrichment["summary"],
                "catalog_resolution": catalog["summary"],
            },
            "integrity_check": integrity,
            "application_import": "passed",
            "mana_pool_writes": 0,
        }
        result["passed"] = all((
            result["real_database_unchanged"], physical == 6372,
            available == 6372, len(batches) == len(LEGACY_BATCH_ORDER),
            [batch.batch_code for batch in batches] == sorted(LEGACY_BATCH_ORDER),
            test_batches == 0, missing_normalized == 0,
            integrity == "ok",
        ))
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="cardfoundry.db")
    parser.add_argument("--legacy-csv", required=True)
    parser.add_argument("--output", help="Optional JSON report path")
    args = parser.parse_args()
    result = run_rehearsal(Path(args.database), Path(args.legacy_csv))
    rendered = json.dumps(result, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
