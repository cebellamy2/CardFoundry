"""Single authoritative preview-first production batch import workflow."""

from collections import Counter
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func

from catalog_resolution_service import resolve_catalog_bindings
from import_service import (
    clean_value, decode_csv, detect_bought_price_column, detect_price_column,
    normalized_condition_id, normalized_finish_id, normalized_language_id,
    parse_price,
)
from inventory_enrichment_service import enrich_inventory_cards
from models import Batch, ImportRecord, InventoryCard, RemoteProductBinding


WORKFLOW_VERSION = "production-import-v1"


class ProductionImportError(RuntimeError):
    pass


def _stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _validation_evidence_hash(evidence: dict) -> str:
    """Hash stable identity evidence, excluding request-time API metadata."""
    stable = dict(evidence)
    stable["binding_groups"] = [
        {key: value for key, value in group.items() if key != "catalog_as_of"}
        for group in evidence.get("binding_groups") or []
    ]
    return _stable_hash(stable)


def parse_production_csv(contents: bytes, default_condition="LP") -> dict:
    text = decode_csv(contents)
    reader = csv.DictReader(io.StringIO(text))
    columns = list(reader.fieldnames or [])
    if not columns:
        raise ProductionImportError("CSV headers not found")
    source_rows = list(reader)
    price_column = detect_price_column(columns)
    bought_column = detect_bought_price_column(columns)
    errors, warnings, physical = [], [], []
    for row_number, row in enumerate(source_rows, start=2):
        if not clean_value(row, "Name"):
            errors.append(f"Row {row_number}: Name is required")
            continue
        quantity_text = clean_value(row, "Quantity") or "1"
        try:
            quantity = int(quantity_text)
            if quantity < 1 or str(quantity) != quantity_text.strip():
                raise ValueError
        except ValueError:
            errors.append(f"Row {row_number}: Quantity must be a positive integer")
            continue
        condition = (
            clean_value(row, "Condition")
            or clean_value(row, "Condition ID")
            or default_condition
        )
        normalized = {
            "source_row": row_number,
            "name": clean_value(row, "Name"),
            "set_code": clean_value(row, "Set code"),
            "collector_number": clean_value(row, "Collector number"),
            "source_location": clean_value(row, "Location"),
            "finish": clean_value(row, "Finish") or clean_value(row, "Foil"),
            "scryfall_id": clean_value(row, "Scryfall ID"),
            "explicit_mtgjson_id": (
                clean_value(row, "MTGJSON ID") or clean_value(row, "MTGJSON UUID")
            ),
            "language_id": normalized_language_id(row),
            "condition": condition,
            "condition_id": normalized_condition_id(condition),
            "finish_id": normalized_finish_id(
                clean_value(row, "Finish") or clean_value(row, "Foil")
            ),
            "price": parse_price(row.get(price_column)) if price_column else None,
            "bought_price": (
                parse_price(row.get(bought_column)) if bought_column else None
            ),
            "scan_order": clean_value(row, "Scan Order"),
        }
        if normalized["bought_price"] is None:
            normalized["bought_price"] = normalized["price"]
        missing = [field for field in (
            "name", "set_code", "collector_number", "scryfall_id",
            "language_id", "condition_id", "finish_id",
        ) if not normalized[field]]
        if missing:
            errors.append(f"Row {row_number}: missing identity: {', '.join(missing)}")
            continue
        if normalized["price"] is None:
            errors.append(f"Row {row_number}: valid price column/value is required")
            continue
        for copy_number in range(1, quantity + 1):
            physical.append({**normalized, "copy_number": copy_number})
    if "Language" not in columns and "Language ID" not in columns:
        warnings.append("Missing language defaults to EN")
    if "Condition" not in columns and "Condition ID" not in columns:
        warnings.append(f"Missing condition defaults to {default_condition}")
    if "Quantity" not in columns:
        warnings.append("No Quantity column; each CSV row represents one physical card")
    return {
        "csv_text": text,
        "columns": columns,
        "csv_row_count": len(source_rows),
        "physical_rows": physical,
        "physical_card_count": len(physical),
        "price_column": price_column,
        "bought_price_column": bought_column,
        "warnings": warnings,
        "errors": errors,
    }


def _catalog_payload(scryfall_ids, languages, catalog_lookup) -> dict:
    combined = {"meta": {}, "data": []}
    ids = sorted(set(scryfall_ids))
    for start in range(0, len(ids), 100):
        payload = catalog_lookup(ids[start:start + 100], languages=languages)
        combined["data"].extend(payload.get("data") or [])
        if payload.get("meta"):
            combined["meta"] = payload["meta"]
    return combined


def build_production_import_preview(
    session, contents: bytes, filename: str, batch_code: str,
    source_location: str | None, seller_inventory: list[dict], catalog_lookup,
    default_condition="LP",
) -> dict:
    batch_code = (batch_code or "").strip().upper()
    if not batch_code:
        raise ProductionImportError("Proposed batch name is required")
    parsed = parse_production_csv(contents, default_condition=default_condition)
    errors = list(parsed["errors"])
    if session.query(Batch).filter(Batch.batch_code == batch_code).first():
        errors.append(f"Batch already exists: {batch_code}")
    source_hash = hashlib.sha256(contents).hexdigest()
    if session.query(ImportRecord).filter(
        ImportRecord.file_hash == source_hash, ImportRecord.status == "active",
    ).first():
        errors.append("This exact file is already actively imported")
    if errors:
        raise ProductionImportError("; ".join(errors))

    cards = []
    for index, row in enumerate(parsed["physical_rows"], start=1):
        cards.append(SimpleNamespace(
            id=index, name=row["name"], set_code=row["set_code"],
            collector_number=row["collector_number"], scryfall_id=row["scryfall_id"],
            mtgjson_id=row["explicit_mtgjson_id"], language_id=row["language_id"],
            condition=row["condition"], condition_id=row["condition_id"],
            finish=row["finish"], finish_id=row["finish_id"],
        ))
    enrichment = enrich_inventory_cards(cards, seller_inventory, persist=True)
    if enrichment["summary"]["ambiguous"] or enrichment["summary"]["conflicts"]:
        raise ProductionImportError(
            "Seller identity validation failed: "
            + json.dumps(enrichment["summary"], sort_keys=True)
        )
    unresolved = [card for card in cards if not all((
        card.mtgjson_id, card.language_id, card.condition_id, card.finish_id,
    ))]
    catalog_payload = _catalog_payload(
        [card.scryfall_id for card in unresolved],
        sorted({card.language_id for card in unresolved}), catalog_lookup,
    ) if unresolved else {"meta": {}, "data": []}
    catalog = resolve_catalog_bindings(unresolved, catalog_payload)
    held = [row for row in catalog["rows"] if row["validation_status"] != "validated"]
    if held:
        raise ProductionImportError("Catalog identity validation failed: " + json.dumps(held))

    catalog_by_id = {}
    binding_groups = []
    for proposal in catalog["rows"]:
        binding = proposal["proposed_remote_binding"]
        requested = proposal["requested_variant"]
        group = {
            "preview_card_ids": proposal["inventory_card_ids"],
            "requested_variant": requested,
            "product_type": binding["product_type"],
            "product_id": binding["product_id"],
            "catalog_as_of": binding.get("catalog_as_of"),
        }
        existing_binding = session.query(RemoteProductBinding).filter(
            RemoteProductBinding.provider == "manapool",
            RemoteProductBinding.product_type == binding["product_type"],
            RemoteProductBinding.product_id == binding["product_id"],
        ).one_or_none()
        if existing_binding:
            existing_requested = json.loads(existing_binding.requested_identity_json)
            if existing_requested != requested or existing_binding.binding_status != "validated":
                raise ProductionImportError(
                    f"Existing remote binding conflicts for product {binding['product_id']}"
                )
            group["existing_binding_id"] = existing_binding.id
        binding_groups.append(group)
        for card_id in proposal["inventory_card_ids"]:
            catalog_by_id[card_id] = binding["product_id"]

    normalized_rows = []
    for row, card in zip(parsed["physical_rows"], cards):
        normalized_rows.append({
            **row,
            "mtgjson_id": card.mtgjson_id or None,
            "binding_product_id": catalog_by_id.get(card.id),
        })
    duplicate_counts = Counter(
        (row["scryfall_id"], row["language_id"], row["condition_id"], row["finish_id"])
        for row in normalized_rows
    )
    duplicates = [{
        "identity": "|".join(key), "physical_quantity": count,
    } for key, count in sorted(duplicate_counts.items()) if count > 1]
    existing_total = session.query(func.count(InventoryCard.id)).scalar()
    evidence = {
        "workflow_version": WORKFLOW_VERSION,
        "source_hash": source_hash,
        "filename": filename,
        "batch_code": batch_code,
        "source_location": source_location,
        "normalized_rows": normalized_rows,
        "binding_groups": binding_groups,
        "duplicates": duplicates,
        "existing_inventory_total": existing_total,
    }
    evidence_hash = _validation_evidence_hash(evidence)
    canonical = sum(bool(row["mtgjson_id"]) for row in normalized_rows)
    return {
        "workflow_version": WORKFLOW_VERSION,
        "filename": filename,
        "source_hash": source_hash,
        "batch_code": batch_code,
        "source_location": source_location,
        "csv_text": parsed["csv_text"],
        "columns": parsed["columns"],
        "csv_row_count": parsed["csv_row_count"],
        "physical_card_count": parsed["physical_card_count"],
        "normalized_rows": normalized_rows,
        "canonical_card_count": canonical,
        "validated_net_new_cards": len(normalized_rows) - canonical,
        "validated_net_new_bindings": len(binding_groups),
        "binding_groups": binding_groups,
        "duplicate_groups": duplicates,
        "held": [], "errors": [], "warnings": parsed["warnings"],
        "expected_inventory_total": existing_total + len(normalized_rows),
        "evidence_hash": evidence_hash,
        "evidence": evidence,
        "price_column": parsed["price_column"],
        "bought_price_column": parsed["bought_price_column"],
    }


def commit_production_import(session, preview: dict, contents: bytes, audit_dir: Path) -> dict:
    if preview.get("workflow_version") != WORKFLOW_VERSION:
        raise ProductionImportError("Staged workflow version is not supported")
    if hashlib.sha256(contents).hexdigest() != preview["source_hash"]:
        raise ProductionImportError("Source hash changed after preview")
    if _validation_evidence_hash(preview["evidence"]) != preview["evidence_hash"]:
        raise ProductionImportError("Validation evidence changed after preview")
    if session.query(Batch).filter(Batch.batch_code == preview["batch_code"]).first():
        raise ProductionImportError("Batch appeared after preview")
    batch = Batch(batch_code=preview["batch_code"], is_archived=False)
    session.add(batch); session.flush()
    record = ImportRecord(
        batch_id=batch.id, filename=preview["filename"],
        file_hash=preview["source_hash"], card_count=preview["physical_card_count"],
        price_column=preview["price_column"], status="active",
    )
    session.add(record); session.flush()
    cards = []
    for row in preview["normalized_rows"]:
        card = InventoryCard(
            batch_id=batch.id, import_id=record.id, name=row["name"],
            set_code=row["set_code"], collector_number=row["collector_number"],
            source_location=preview["source_location"] or row["source_location"],
            finish=row["finish"], scryfall_id=row["scryfall_id"],
            mtgjson_id=row["mtgjson_id"], language_id=row["language_id"],
            condition=row["condition"], condition_id=row["condition_id"],
            finish_id=row["finish_id"], price_usd=row["price"],
            bought_in_price=row["bought_price"], current_price=row["price"],
            scan_order=row["scan_order"], status="available",
        )
        session.add(card); cards.append(card)
    session.flush()
    for group in preview["binding_groups"]:
        preview_ids = set(group["preview_card_ids"])
        actual_cards = [cards[index - 1] for index in sorted(preview_ids)]
        requested = group["requested_variant"]
        card_ids = sorted(card.id for card in actual_cards)
        binding_evidence = {
            "requested_variant": requested, "inventory_card_ids": card_ids,
            "product_type": group["product_type"], "product_id": group["product_id"],
            "catalog_as_of": group.get("catalog_as_of"),
        }
        existing_binding_id = group.get("existing_binding_id")
        if existing_binding_id:
            existing = session.get(RemoteProductBinding, existing_binding_id)
            if not existing or existing.product_id != group["product_id"]:
                raise ProductionImportError("Existing remote binding changed after preview")
            prior_ids = json.loads(existing.local_card_ids_json)
            combined_ids = sorted(set(prior_ids + card_ids))
            binding_evidence["inventory_card_ids"] = combined_ids
            existing.local_card_ids_json = json.dumps(combined_ids)
            existing.evidence_hash = _stable_hash(binding_evidence)
            existing.evidence_json = json.dumps(binding_evidence, sort_keys=True)
            existing.validated_at = datetime.now(timezone.utc)
            continue
        session.add(RemoteProductBinding(
            provider="manapool", product_type=group["product_type"],
            product_id=group["product_id"], local_card_ids_json=json.dumps(card_ids),
            requested_identity_json=json.dumps(requested, sort_keys=True),
            scryfall_id=requested["scryfall_id"], mtgjson_id=None,
            language_id=requested["language_id"], condition_id=requested["condition_id"],
            finish_id=requested["finish_id"], set_code=requested["set_code"],
            collector_number=requested["collector_number"], binding_status="validated",
            validated_at=datetime.now(timezone.utc),
            catalog_as_of=group.get("catalog_as_of"),
            evidence_hash=_stable_hash(binding_evidence),
            evidence_json=json.dumps(binding_evidence, sort_keys=True),
        ))
    session.flush()
    result = {
        "status": "completed", "batch_id": batch.id,
        "batch_code": batch.batch_code, "source_filename": preview["filename"],
        "source_sha256": preview["source_hash"],
        "imported_physical_cards": len(cards),
        "inventory_card_ids": [card.id for card in cards],
        "fully_canonical_cards": preview["canonical_card_count"],
        "validated_net_new_physical_cards": preview["validated_net_new_cards"],
        "validated_net_new_remote_bindings": preview["validated_net_new_bindings"],
        "duplicate_variant_groups": preview["duplicate_groups"],
        "unresolved_or_held_cards": 0, "errors": [],
        "total_production_inventory": preview["expected_inventory_total"],
        "evidence_hash": preview["evidence_hash"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "external_write_calls": 0,
    }
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / (
        f"production-new-batch-{batch.batch_code}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}.json"
    )
    audit_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    result["audit_path"] = str(audit_path.resolve())
    return result
