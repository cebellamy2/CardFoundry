"""Read-only planning for canonical MTGJSON identity backfill."""

import hashlib
import json
from datetime import datetime, timezone

from models import Batch, InventoryCard, RemoteProductBinding


PREVIEW_VERSION = "mtgjson_backfill_preview_v1"


def _text(value) -> str:
    return str(value or "").strip()


def _hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _local_identity(card) -> dict:
    return {
        "name": _text(card.name),
        "set_code": _text(card.set_code).upper(),
        "collector_number": _text(card.collector_number).upper(),
        "scryfall_id": _text(card.scryfall_id).lower(),
        "language_id": _text(card.language_id).upper(),
        "condition_id": _text(card.condition_id).upper(),
        "finish_id": _text(card.finish_id).upper(),
        "mtgjson_id": _text(card.mtgjson_id).lower() or None,
    }


def _seller_identity(item) -> dict:
    single = ((item.get("product") or {}).get("single") or {})
    return {
        "name": _text(single.get("name")),
        "set_code": _text(single.get("set")).upper(),
        "collector_number": _text(single.get("number")).upper(),
        "scryfall_id": _text(single.get("scryfall_id")).lower(),
        "language_id": _text(single.get("language_id")).upper(),
        "condition_id": _text(single.get("condition_id")).upper(),
        "finish_id": _text(single.get("finish_id")).upper(),
        "mtgjson_id": _text(single.get("mtgjson_id")).lower() or None,
    }


def _binding_identity(binding) -> dict:
    try:
        requested = json.loads(binding.requested_identity_json or "{}")
    except (TypeError, ValueError):
        requested = {}
    return {
        "name": _text(requested.get("name")),
        "set_code": _text(requested.get("set_code") or binding.set_code).upper(),
        "collector_number": _text(
            requested.get("collector_number") or binding.collector_number
        ).upper(),
        "scryfall_id": _text(requested.get("scryfall_id") or binding.scryfall_id).lower(),
        "language_id": _text(requested.get("language_id") or binding.language_id).upper(),
        "condition_id": _text(requested.get("condition_id") or binding.condition_id).upper(),
        "finish_id": _text(requested.get("finish_id") or binding.finish_id).upper(),
        "mtgjson_id": _text(binding.mtgjson_id).lower() or None,
    }


def _catalog_by_product(catalog_printings: list[dict]) -> dict[str, list[dict]]:
    result = {}
    for printing in catalog_printings:
        for variant in printing.get("variants") or []:
            product_id = _text(variant.get("product_id"))
            if product_id:
                result.setdefault(product_id, []).append(printing)
    return result


def _binding_card_ids(binding) -> list[int] | None:
    try:
        values = json.loads(binding.local_card_ids_json or "[]")
        return [int(value) for value in values]
    except (TypeError, ValueError):
        return None


def build_mtgjson_backfill_preview(
    session, seller_inventory: list[dict], catalog_printings: list[dict] | None = None,
    seller_snapshot_timestamp: str | None = None,
    catalog_snapshot_timestamp: str | None = None,
) -> dict:
    """Classify available/null-MTGJSON cards without mutating the session."""
    with session.no_autoflush:
        candidates = session.query(InventoryCard).join(Batch).filter(
            InventoryCard.status == "available",
            InventoryCard.mtgjson_id.is_(None),
            Batch.is_archived == False,
        ).order_by(InventoryCard.id).all()
        bindings = session.query(RemoteProductBinding).order_by(
            RemoteProductBinding.id
        ).all()

    bindings_by_card = {}
    malformed_binding_ids = set()
    for binding in bindings:
        card_ids = _binding_card_ids(binding)
        if card_ids is None:
            malformed_binding_ids.add(binding.id)
            continue
        for card_id in card_ids:
            bindings_by_card.setdefault(card_id, []).append(binding)

    seller_by_product = {}
    for item in seller_inventory:
        if item.get("product_type") != "mtg_single":
            continue
        product_id = _text(item.get("product_id"))
        if product_id:
            seller_by_product.setdefault(product_id, []).append(item)
    catalog_by_product = _catalog_by_product(catalog_printings or [])

    rows = []
    for card in candidates:
        local = _local_identity(card)
        linked = bindings_by_card.get(card.id, [])
        validated = [row for row in linked if row.binding_status == "validated"]
        base = {
            "inventory_card_id": card.id,
            "batch_id": card.batch_id,
            "import_id": card.import_id,
            "current_identity": local,
            "proposed_mtgjson_id": None,
            "binding_id": None,
            "product_id": None,
            "binding_evidence_hash": None,
            "binding_validated_at": None,
            "binding_catalog_as_of": None,
            "seller_inventory_id": None,
            "seller_effective_as_of": None,
            "seller_evidence_hash": None,
            "catalog_card_id": None,
            "catalog_corroboration": "unavailable",
        }
        if not linked or not validated:
            rows.append({**base, "classification": "binding_invalid",
                         "reason": "No validated RemoteProductBinding references this card."})
            continue
        if len(validated) != 1 or len(linked) != 1:
            rows.append({**base, "classification": "ambiguous",
                         "reason": "Card is referenced by multiple or conflicting bindings."})
            continue
        binding = validated[0]
        base.update({
            "binding_id": binding.id, "product_id": binding.product_id,
            "binding_evidence_hash": binding.evidence_hash,
            "binding_validated_at": binding.validated_at,
            "binding_catalog_as_of": binding.catalog_as_of,
        })
        binding_identity = _binding_identity(binding)
        seller_rows = seller_by_product.get(binding.product_id, [])
        if len(seller_rows) > 1:
            rows.append({**base, "binding_identity": binding_identity,
                         "classification": "ambiguous",
                         "reason": "Multiple seller inventory records use the bound product ID."})
            continue
        if not seller_rows:
            rows.append({**base, "binding_identity": binding_identity,
                         "classification": "missing_documented_mtgjson",
                         "reason": "Bound product is absent from documented seller inventory history."})
            continue
        seller = seller_rows[0]
        remote = _seller_identity(seller)
        base.update({
            "seller_inventory_id": seller.get("id"),
            "seller_effective_as_of": seller.get("effective_as_of"),
            "seller_evidence_hash": _hash(seller),
        })
        if not remote["mtgjson_id"]:
            rows.append({**base, "binding_identity": binding_identity,
                         "seller_identity": remote,
                         "classification": "missing_documented_mtgjson",
                         "reason": "Seller product.single.mtgjson_id is absent."})
            continue

        identity_fields = (
            "name", "set_code", "collector_number", "scryfall_id",
            "language_id", "condition_id", "finish_id",
        )
        conflicts = []
        for field in identity_fields:
            local_value = local[field].casefold() if field == "name" else local[field]
            remote_value = remote[field].casefold() if field == "name" else remote[field]
            binding_value = (
                binding_identity[field].casefold()
                if field == "name" else binding_identity[field]
            )
            if not local_value or not remote_value or local_value != remote_value:
                conflicts.append(field)
            if binding_value and binding_value != local_value:
                conflicts.append(f"binding.{field}")
        if binding_identity["mtgjson_id"] and (
            binding_identity["mtgjson_id"] != remote["mtgjson_id"]
        ):
            conflicts.append("binding.mtgjson_id")

        catalog_rows = catalog_by_product.get(binding.product_id, [])
        if len(catalog_rows) > 1:
            rows.append({**base, "binding_identity": binding_identity,
                         "seller_identity": remote, "classification": "ambiguous",
                         "reason": "Catalog corroboration returned multiple printings."})
            continue
        if catalog_rows:
            catalog_card_id = _text(catalog_rows[0].get("card_id")).lower() or None
            base["catalog_card_id"] = catalog_card_id
            if catalog_card_id:
                base["catalog_corroboration"] = (
                    "match" if catalog_card_id == remote["mtgjson_id"] else "mismatch"
                )
                if catalog_card_id != remote["mtgjson_id"]:
                    conflicts.append("catalog.card_id")

        if conflicts:
            rows.append({**base, "binding_identity": binding_identity,
                         "seller_identity": remote, "classification": "identity_conflict",
                         "reason": "Exact identity disagreement: " + ", ".join(sorted(set(conflicts)))})
            continue
        rows.append({
            **base, "binding_identity": binding_identity, "seller_identity": remote,
            "proposed_mtgjson_id": remote["mtgjson_id"], "classification": "ready",
            "reason": "Exact documented seller identity agrees with card and binding.",
        })

    counts = {name: sum(row["classification"] == name for row in rows) for name in (
        "ready", "missing_documented_mtgjson", "identity_conflict", "ambiguous",
        "binding_invalid",
    )}
    evidence = {
        "preview_version": PREVIEW_VERSION,
        "seller_snapshot_timestamp": seller_snapshot_timestamp,
        "seller_snapshot_hash": _hash(seller_inventory),
        "catalog_snapshot_timestamp": catalog_snapshot_timestamp,
        "catalog_corroboration_hash": _hash(catalog_printings or []),
        "candidate_ids": [row["inventory_card_id"] for row in rows],
        "rows": rows,
    }
    return {
        **evidence,
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {"total_candidates": len(rows), **counts},
        "evidence_hash": _hash(evidence),
        "preview_only": True,
    }
