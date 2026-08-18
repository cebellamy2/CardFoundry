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
from inventory_enrichment_service import remote_identity
from models import Batch, ImportRecord, InventoryCard, RemoteProductBinding


WORKFLOW_VERSION = "production-import-v1"

SCRYFALL_LANGUAGE_IDS = {
    "en": "EN", "ja": "JA", "zhs": "CS", "zht": "CT",
    "fr": "FR", "de": "DE", "it": "IT", "ko": "KO",
    "pt": "PT", "ru": "RU", "es": "ES",
    "phyrexian": "PH", "ph": "PH",
}


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
            "explicit_language_id": (
                clean_value(row, "Language ID") or clean_value(row, "Language")
            ),
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
        for copy_number in range(1, quantity + 1):
            physical.append({**normalized, "copy_number": copy_number})
    if "Language" not in columns and "Language ID" not in columns:
        warnings.append("Missing language uses exact Scryfall metadata, then defaults to EN")
    if "Condition" not in columns and "Condition ID" not in columns:
        warnings.append(f"Missing condition defaults to {default_condition}")
    if "Quantity" not in columns:
        warnings.append("No Quantity column; each CSV row represents one physical card")
    missing_prices = sum(row["price"] is None for row in physical)
    if missing_prices:
        warnings.append(
            f"{missing_prices} physical card(s) have blank prices and require review"
        )
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


def _catalog_payload(cards, catalog_lookup) -> dict:
    combined = {"meta": {}, "data": []}
    ids_by_language = {}
    as_of_values = []
    for card in cards:
        ids_by_language.setdefault(card.language_id, set()).add(card.catalog_scryfall_id)
    # Mana Pool's languages query does not behave as an OR filter. Query each
    # exact language independently so one localized card cannot disappear from
    # a mixed-language production batch.
    for language in sorted(ids_by_language):
        ids = sorted(ids_by_language[language])
        for start in range(0, len(ids), 100):
            payload = catalog_lookup(
                ids[start:start + 100], languages=[language],
            )
            combined["data"].extend(payload.get("data") or [])
            as_of = (payload.get("meta") or {}).get("as_of")
            if as_of:
                as_of_values.append(as_of)
    if as_of_values:
        combined["meta"]["as_of"] = max(as_of_values)
    return combined


def build_production_import_preview(
    session, contents: bytes, filename: str, batch_code: str,
    source_location: str | None, seller_inventory: list[dict], catalog_lookup,
    default_condition="LP", price_overrides: dict[int, float] | None = None,
    scryfall_lookup=None, target_batch_id: int | None = None,
) -> dict:
    """`target_batch_id`, when given, attaches this import to an existing
    batch instead of creating a new one -- only permitted when that batch
    currently has zero InventoryCard rows (an "add more to a batch that
    already has cards" flow is a deliberately different, unbuilt feature).
    `batch_code` is then derived from the target batch, not user input.
    """
    parsed = parse_production_csv(contents, default_condition=default_condition)
    errors = list(parsed["errors"])

    target_batch = None
    if target_batch_id is not None:
        target_batch = session.get(Batch, target_batch_id)
        if not target_batch:
            errors.append(f"Target batch #{target_batch_id} not found")
        elif session.query(InventoryCard).filter(
            InventoryCard.batch_id == target_batch_id,
        ).count() > 0:
            errors.append(
                f"Batch {target_batch.batch_code!r} already has cards -- "
                "only an empty batch can be targeted this way"
            )
        else:
            batch_code = target_batch.batch_code
    else:
        batch_code = (batch_code or "").strip().upper()
        if not batch_code:
            raise ProductionImportError("Proposed batch name is required")
        if session.query(Batch).filter(Batch.batch_code == batch_code).first():
            errors.append(f"Batch already exists: {batch_code}")

    source_hash = hashlib.sha256(contents).hexdigest()
    if session.query(ImportRecord).filter(
        ImportRecord.file_hash == source_hash, ImportRecord.status == "active",
    ).first():
        errors.append("This exact file is already actively imported")
    if errors:
        raise ProductionImportError("; ".join(errors))

    if scryfall_lookup:
        requested_ids = sorted({row["scryfall_id"] for row in parsed["physical_rows"]})
        lookup_result = scryfall_lookup(requested_ids)
        cards_by_id = lookup_result[0] if isinstance(lookup_result, tuple) else lookup_result
        for row in parsed["physical_rows"]:
            metadata = cards_by_id.get(row["scryfall_id"])
            if not metadata:
                raise ProductionImportError(
                    f"Row {row['source_row']}: Scryfall printing was not found"
                )
            cross_checks = {
                "name": str(metadata.get("name") or "").casefold() == row["name"].casefold(),
                "set": str(metadata.get("set") or "").upper() == row["set_code"].upper(),
                "collector": str(metadata.get("collector_number") or "").upper()
                == row["collector_number"].upper(),
            }
            if not all(cross_checks.values()):
                raise ProductionImportError(
                    f"Row {row['source_row']}: Scryfall printing metadata conflicts"
                )
            scryfall_lang = str(metadata.get("lang") or "").lower()
            scryfall_language = SCRYFALL_LANGUAGE_IDS.get(scryfall_lang)
            if scryfall_lang and not scryfall_language:
                raise ProductionImportError(
                    f"Row {row['source_row']}: unsupported Scryfall language {scryfall_lang}"
                )
            explicit = str(row.get("explicit_language_id") or "").upper()
            if explicit and scryfall_language and explicit != scryfall_language:
                raise ProductionImportError(
                    f"Row {row['source_row']}: explicit language {explicit} conflicts "
                    f"with Scryfall language {scryfall_language}"
                )
            if not explicit and scryfall_language:
                row["language_id"] = scryfall_language
            row["catalog_scryfall_id"] = row["scryfall_id"]

    price_overrides = {
        int(row_number): float(value)
        for row_number, value in (price_overrides or {}).items()
    }
    for row in parsed["physical_rows"]:
        if row["source_row"] in price_overrides:
            row["price"] = price_overrides[row["source_row"]]
            if row["bought_price"] is None:
                row["bought_price"] = row["price"]

    cards = []
    for index, row in enumerate(parsed["physical_rows"], start=1):
        cards.append(SimpleNamespace(
            id=index, name=row["name"], set_code=row["set_code"],
            collector_number=row["collector_number"], scryfall_id=row["scryfall_id"],
            catalog_scryfall_id=row.get("catalog_scryfall_id") or row["scryfall_id"],
            mtgjson_id=row["explicit_mtgjson_id"], language_id=row["language_id"],
            condition=row["condition"], condition_id=row["condition_id"],
            finish=row["finish"], finish_id=row["finish_id"],
        ))
    enrichment = enrich_inventory_cards(cards, seller_inventory, persist=True)
    if enrichment["summary"]["ambiguous"] or enrichment["summary"]["conflicts"]:
        blocking_examples = {
            category: enrichment["examples"].get(category, [])
            for category in ("ambiguous", "conflict")
            if enrichment["examples"].get(category)
        }
        raise ProductionImportError(
            "Seller identity validation failed: "
            + json.dumps({
                "summary": enrichment["summary"],
                "blocking_rows": blocking_examples,
            }, sort_keys=True)
        )
    # Localized Scryfall records are sometimes grouped under a shared catalog
    # printing UUID by Mana Pool. A unique seller printing-family match can
    # safely supply MTGJSON/catalog-printing identity without borrowing its
    # condition-specific product ID.
    remote_families = {}
    for item in seller_inventory:
        if item.get("product_type") != "mtg_single":
            continue
        identity = remote_identity(item)
        key = (
            identity["name"].casefold(), identity["set_code"],
            identity["collector_number"], identity["language_id"],
            identity["finish_id"],
        )
        remote_families.setdefault(key, []).append(identity)
    for card in cards:
        if card.mtgjson_id:
            continue
        key = (
            card.name.casefold(), card.set_code.upper(),
            card.collector_number.upper(), card.language_id, card.finish_id,
        )
        family = remote_families.get(key, [])
        identities = {
            (item["mtgjson_id"], item["scryfall_id"])
            for item in family if item["mtgjson_id"] and item["scryfall_id"]
        }
        if len(identities) == 1:
            card.mtgjson_id, card.catalog_scryfall_id = next(iter(identities))

    unresolved = [card for card in cards if not all((
        card.mtgjson_id, card.language_id, card.condition_id, card.finish_id,
    ))]
    catalog_payload = _catalog_payload(
        unresolved, catalog_lookup,
    ) if unresolved else {"meta": {}, "data": []}
    catalog = resolve_catalog_bindings(unresolved, catalog_payload)
    held = [row for row in catalog["rows"] if row["validation_status"] != "validated"]
    if held:
        raise ProductionImportError("Catalog identity validation failed: " + json.dumps(held))

    # A validated remote product binding is an accepted identity at import
    # time even without a canonical MTGJSON ID -- resolve_catalog_bindings
    # never returns one (Mana Pool's catalog doesn't supply it), by design;
    # that's exactly what mtgjson_backfill_service.py exists to fill in
    # afterward. Import-time is deliberately more permissive than the
    # stricter canonical-identity requirement sellability_service.py and
    # printing_correction_service.py enforce for cards already in the
    # system -- this is the first stage those depend on being reachable.

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
            if "catalog_scryfall_id" not in existing_requested:
                existing_requested["catalog_scryfall_id"] = existing_requested.get("scryfall_id")
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
            "catalog_scryfall_id": card.catalog_scryfall_id,
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
        "target_batch_id": target_batch_id,
        "source_location": source_location,
        "normalized_rows": normalized_rows,
        "binding_groups": binding_groups,
        "duplicates": duplicates,
        "existing_inventory_total": existing_total,
        "price_overrides": price_overrides,
    }
    evidence_hash = _validation_evidence_hash(evidence)
    canonical = sum(bool(row["mtgjson_id"]) for row in normalized_rows)
    missing_price_rows = []
    for row in normalized_rows:
        if row["price"] is None and not any(
            item["source_row"] == row["source_row"] for item in missing_price_rows
        ):
            missing_price_rows.append({
                "source_row": row["source_row"], "name": row["name"],
                "set_code": row["set_code"],
                "collector_number": row["collector_number"],
                "language_id": row["language_id"],
                "condition_id": row["condition_id"],
                "finish_id": row["finish_id"],
            })
    return {
        "workflow_version": WORKFLOW_VERSION,
        "filename": filename,
        "source_hash": source_hash,
        "batch_code": batch_code,
        "target_batch_id": target_batch_id,
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
        "missing_price_rows": missing_price_rows,
        "price_overrides": price_overrides,
        "ready_to_confirm": not missing_price_rows,
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
    if preview.get("missing_price_rows") or not preview.get("ready_to_confirm"):
        raise ProductionImportError("Every missing price must be resolved before import")

    target_batch_id = preview.get("target_batch_id")
    if target_batch_id is not None:
        batch = session.get(Batch, target_batch_id)
        if not batch:
            raise ProductionImportError("Target batch no longer exists")
        if batch.batch_code != preview["batch_code"]:
            raise ProductionImportError("Target batch code changed after preview")
        if session.query(InventoryCard).filter(
            InventoryCard.batch_id == target_batch_id,
        ).count() > 0:
            raise ProductionImportError(
                "Target batch is no longer empty -- another import landed first"
            )
    else:
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
