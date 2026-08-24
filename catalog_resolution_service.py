"""Read-only Mana Pool catalog resolution for inventory not yet listed by a seller."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json

from import_service import (
    normalized_condition_id,
    normalized_finish_id,
    normalized_language_id,
)


def _text(value) -> str:
    return str(value or "").strip()


def _printing_key(name, set_code, collector_number, scryfall_id):
    return (
        _text(name).casefold(),
        _text(set_code).upper(),
        _text(collector_number).upper(),
        _text(scryfall_id).lower(),
    )


def requested_variant(card) -> dict:
    """Build the exact local variant under CardFoundry's language policy."""
    return {
        "name": _text(card.name),
        "set_code": _text(card.set_code).upper(),
        "collector_number": _text(card.collector_number).upper(),
        "scryfall_id": _text(card.scryfall_id).lower(),
        "catalog_scryfall_id": _text(
            getattr(card, "catalog_scryfall_id", None) or card.scryfall_id
        ).lower(),
        "language_id": normalized_language_id({"Language ID": card.language_id}),
        "condition_id": normalized_condition_id(card.condition_id or card.condition),
        "finish_id": normalized_finish_id(card.finish_id or card.finish),
    }


def resolve_catalog_bindings(cards, catalog_response: dict) -> dict:
    """Propose exact product bindings; never mutates cards or seller inventory."""
    catalog_by_printing = defaultdict(list)
    for product in catalog_response.get("data") or []:
        key = _printing_key(
            product.get("name"), product.get("set_code"), product.get("number"),
            product.get("scryfall_id"),
        )
        catalog_by_printing[key].append(product)

    grouped_cards = defaultdict(list)
    held = []
    for card in cards:
        requested = requested_variant(card)
        missing = [field for field in (
            "name", "set_code", "collector_number", "scryfall_id",
            "condition_id", "finish_id",
        ) if not requested[field]]
        if missing:
            held.append({
                "inventory_card_ids": [card.id],
                "validation_status": "held",
                "validation_reason": "Missing identity: " + ", ".join(missing),
                "requested_variant": requested,
                "proposed_remote_binding": None,
            })
            continue
        key = (
            _printing_key(
                requested["name"], requested["set_code"],
                requested["collector_number"], requested["catalog_scryfall_id"],
            ),
            requested["language_id"], requested["condition_id"],
            requested["finish_id"],
        )
        grouped_cards[key].append(card)

    proposals = []
    catalog_as_of = (catalog_response.get("meta") or {}).get("as_of")
    for key in sorted(grouped_cards):
        printing_key, language, condition, finish = key
        local_cards = grouped_cards[key]
        products = catalog_by_printing.get(printing_key, [])
        candidates = [
            variant
            for product in products
            for variant in product.get("variants") or []
            if _text(variant.get("product_type")) == "mtg_single"
            and _text(variant.get("language_id")).upper() == language
            and _text(variant.get("condition_id")).upper() == condition
            and _text(variant.get("finish_id")).upper() == finish
            and _text(variant.get("product_id"))
        ]
        requested = requested_variant(local_cards[0])
        if len(products) == 0 and getattr(local_cards[0], "scryfall_verified", False):
            # No seller has ever listed this printing on Mana Pool, but the
            # scryfall_id was independently cross-checked against Scryfall's
            # own API (not just this catalog lookup) -- safe to proceed
            # without a binding. Mana Pool's inventory-write endpoint needs
            # no pre-existing product_id and creates the catalog product as
            # a side effect of the first listing; new_listing_upload_service
            # already handles exactly this case for cards that reach it.
            proposals.append({
                "inventory_card_ids": sorted(card.id for card in local_cards),
                "quantity": len(local_cards),
                "validation_status": "pending_first_listing",
                "validation_reason": (
                    "No existing Mana Pool catalog printing; scryfall_id was "
                    "independently verified against Scryfall -- this seller's "
                    "first listing will create the catalog product on publish"
                ),
                "requested_variant": requested,
                "proposed_remote_binding": None,
                "mtgjson_id": None,
                "mtgjson_status": "deferred_not_returned_by_catalog",
            })
            continue
        if len(products) != 1 or len(candidates) != 1:
            proposals.append({
                "inventory_card_ids": sorted(card.id for card in local_cards),
                "validation_status": "held",
                "validation_reason": (
                    f"Expected one catalog printing and product variant; found "
                    f"{len(products)} printing(s), {len(candidates)} variant(s)"
                ),
                "requested_variant": requested,
                "proposed_remote_binding": None,
            })
            continue
        candidate = candidates[0]
        validated_at = datetime.now(timezone.utc).isoformat()
        evidence = {
            "requested_variant": requested,
            "inventory_card_ids": sorted(card.id for card in local_cards),
            "product_type": "mtg_single",
            "product_id": _text(candidate["product_id"]),
            "catalog_as_of": catalog_as_of,
        }
        evidence_hash = hashlib.sha256(json.dumps(
            evidence, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        proposals.append({
            "inventory_card_ids": sorted(card.id for card in local_cards),
            "quantity": len(local_cards),
            "validation_status": "validated",
            "validation_reason": "Exact catalog printing and product variant matched",
            "requested_variant": requested,
            # Deliberately separate from canonical local identity. This is a
            # reviewed remote binding proposal, not an InventoryCard field.
            "proposed_remote_binding": {
                "provider": "manapool",
                "product_type": "mtg_single",
                "product_id": _text(candidate["product_id"]),
                "catalog_as_of": catalog_as_of,
                "validated_at": validated_at,
                "evidence_hash": evidence_hash,
            },
            "evidence": evidence,
            "mtgjson_id": None,
            "mtgjson_status": "deferred_not_returned_by_catalog",
        })

    rows = sorted(
        proposals + held,
        key=lambda row: tuple(row["inventory_card_ids"]),
    )
    return {
        "preview_only": True,
        "rows": rows,
        "summary": {
            "physical_cards": sum(len(row["inventory_card_ids"]) for row in rows),
            "validated_physical_cards": sum(
                len(row["inventory_card_ids"])
                for row in rows if row["validation_status"] == "validated"
            ),
            "validated_remote_bindings": sum(
                row["validation_status"] == "validated" for row in rows
            ),
            "held_physical_cards": sum(
                len(row["inventory_card_ids"])
                for row in rows if row["validation_status"] == "held"
            ),
            "pending_first_listing_physical_cards": sum(
                len(row["inventory_card_ids"])
                for row in rows if row["validation_status"] == "pending_first_listing"
            ),
        },
    }


def persist_validated_bindings(session, resolution: dict) -> int:
    """Persist only exact validated proposals, rejecting all conflicts."""
    from models import RemoteProductBinding

    rows = [row for row in resolution.get("rows") or [] if row.get("validation_status") == "validated"]
    created = 0
    for row in rows:
        binding = row["proposed_remote_binding"]
        requested = row["requested_variant"]
        evidence_hash = binding["evidence_hash"]
        existing = session.query(RemoteProductBinding).filter(
            RemoteProductBinding.evidence_hash == evidence_hash,
        ).one_or_none()
        if existing:
            continue
        product_conflict = session.query(RemoteProductBinding).filter(
            RemoteProductBinding.provider == binding["provider"],
            RemoteProductBinding.product_type == binding["product_type"],
            RemoteProductBinding.product_id == binding["product_id"],
        ).one_or_none()
        if product_conflict:
            raise ValueError(f"Conflicting binding already exists for product {binding['product_id']}")
        session.add(RemoteProductBinding(
            provider=binding["provider"], product_type=binding["product_type"],
            product_id=binding["product_id"],
            local_card_ids_json=json.dumps(row["inventory_card_ids"]),
            requested_identity_json=json.dumps(requested, sort_keys=True),
            scryfall_id=requested["scryfall_id"], mtgjson_id=row.get("mtgjson_id"),
            language_id=requested["language_id"], condition_id=requested["condition_id"],
            finish_id=requested["finish_id"], set_code=requested["set_code"],
            collector_number=requested["collector_number"], binding_status="validated",
            validated_at=datetime.fromisoformat(binding["validated_at"]),
            catalog_as_of=binding.get("catalog_as_of"), evidence_hash=evidence_hash,
            evidence_json=json.dumps(row["evidence"], sort_keys=True),
            remote_inventory_id=None,
        ))
        created += 1
    return created
