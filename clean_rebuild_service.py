"""Pure planning, stale validation, and disabled execution for a store-off rebuild."""

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone

from inventory_mirror_service import ACTIVE_ALLOCATION_STATUSES, KNOWN_STATUSES, canonical_key


REBUILD_CONFIRMATION = "STORE IS OFF - BLANK AND REBUILD INVENTORY"
MAINTENANCE_EXECUTOR_ENABLED = False
HARD_HELD_NAMES = {"Monstrous Vortex", "Peregrine Drake"}
EXPECTED_LAUNCH_HOLDS = HARD_HELD_NAMES | {
    "Jadar, Ghoulcaller of Nephalia", "Scrawling Crawler", "Valor",
}


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def remote_identity(item):
    single = ((item.get("product") or {}).get("single") or {})
    return {
        "name": single.get("name"), "set_code": single.get("set"),
        "collector_number": single.get("number"), "scryfall_id": single.get("scryfall_id"),
        "mtgjson_id": single.get("mtgjson_id"), "language_id": single.get("language_id"),
        "condition_id": single.get("condition_id"), "finish_id": single.get("finish_id"),
    }


def _remote_canonical_key(item):
    identity = remote_identity(item)
    values = tuple(str(identity.get(field) or "").strip().upper() for field in (
        "mtgjson_id", "language_id", "condition_id", "finish_id",
    ))
    return values if all(values) else None


def build_clean_rebuild_preview(cards, batches, allocations, remote_inventory, bindings, pricing):
    """Build immutable blank/republish payloads from already-fetched evidence."""
    card_by_id = {card.id: card for card in cards}
    invalid_ids = set()
    invalid_reasons = {}
    for card in cards:
        if card.status not in KNOWN_STATUSES:
            invalid_ids.add(card.id); invalid_reasons[card.id] = f"Unknown status {card.status!r}"
    for allocation in allocations:
        if allocation.status in ACTIVE_ALLOCATION_STATUSES:
            card = card_by_id.get(allocation.inventory_card_id)
            if card and card.status == "available":
                invalid_ids.add(card.id); invalid_reasons[card.id] = "Available card has active allocation"

    blank_rows = []
    for item in remote_inventory:
        quantity = int(item.get("quantity") or 0)
        if quantity < 1:
            continue
        blank_rows.append({
            "inventory_id": str(item.get("id") or ""),
            "product_id": str(item.get("product_id") or ""),
            "product_type": str(item.get("product_type") or ""),
            "remote_identity": remote_identity(item),
            "current_quantity": quantity,
            "current_price_cents": int(item.get("price_cents") or 0),
            "proposed_quantity": 0,
            "price_behavior": "preserve_with_null",
            "validation_status": "validated" if item.get("product_id") else "invalid",
        })
    blank_rows.sort(key=lambda row: (row["product_type"], row["product_id"], row["inventory_id"]))

    remote_by_canonical = defaultdict(list)
    remote_by_product = defaultdict(list)
    for item in remote_inventory:
        key = _remote_canonical_key(item)
        if key:
            remote_by_canonical[key].append(item)
        if item.get("product_id"):
            remote_by_product[str(item["product_id"])].append(item)

    binding_by_card = {}
    all_binding_hashes = []
    all_binding_product_ids = set()
    for binding in bindings:
        if binding.binding_status != "validated":
            continue
        all_binding_hashes.append(binding.evidence_hash)
        all_binding_product_ids.add(binding.product_id)
        for card_id in json.loads(binding.local_card_ids_json):
            if card_id in binding_by_card:
                raise ValueError(f"Card {card_id} has multiple validated bindings")
            binding_by_card[card_id] = binding
    price_by_binding = {row["binding_id"]: row for row in pricing.get("results") or []}

    canonical_groups = defaultdict(list)
    bound_groups = defaultdict(list)
    exclusions = []
    eligible_candidates = 0
    for card in cards:
        batch = batches.get(card.batch_id)
        if card.status != "available" or not batch or batch.is_archived:
            continue
        if card.id in invalid_ids:
            exclusions.append(_excluded(card, invalid_reasons[card.id])); continue
        eligible_candidates += 1
        binding = binding_by_card.get(card.id)
        if binding:
            bound_groups[binding.id].append(card); continue
        key = canonical_key(card)
        if key:
            canonical_groups[key].append(card); continue
        reason = "Insufficient identity" if card.name in HARD_HELD_NAMES else "No canonical identity or validated remote binding"
        exclusions.append(_excluded(card, reason))

    republish = []
    for key, local_cards in sorted(canonical_groups.items()):
        matches = remote_by_canonical.get(key, [])
        if len(matches) != 1:
            for card in local_cards:
                exclusions.append(_excluded(card, f"Expected one existing remote variant; found {len(matches)}"))
            continue
        item = matches[0]
        republish.append(_publish_row(
            local_cards, "existing_managed_variant", str(item.get("product_id") or ""),
            item, None, "preserve_with_null",
        ))

    for binding_id, local_cards in sorted(bound_groups.items()):
        binding = next(value for value in bindings if value.id == binding_id)
        price = price_by_binding.get(binding_id)
        if not price or price.get("status") != "priced" or not price.get("evidence_hash"):
            reason = (price or {}).get("reason") or "Verified initial price is absent or stale"
            for card in local_cards:
                exclusions.append(_excluded(card, reason))
            continue
        matches = remote_by_product.get(binding.product_id, [])
        if len(matches) > 1:
            for card in local_cards:
                exclusions.append(_excluded(card, "Multiple seller records use validated product binding"))
            continue
        item = matches[0] if matches else None
        republish.append(_publish_row(
            local_cards, "validated_new_product_binding", binding.product_id,
            item, int(price["target_price_cents"]), "set_verified_initial_price",
            binding.evidence_hash, price["evidence_hash"], json.loads(binding.requested_identity_json),
        ))

    republish.sort(key=lambda row: (row["source_type"], row["product_id"]))
    exclusions.sort(key=lambda row: row["inventory_card_id"])
    local_snapshot = [{
        "id": c.id, "batch_id": c.batch_id, "status": c.status,
        "identity": [getattr(c, field, None) for field in (
            "mtgjson_id", "language_id", "condition_id", "finish_id",
        )],
    } for c in sorted(cards, key=lambda value: value.id)]
    remote_snapshot = [{
        "inventory_id": str(i.get("id") or ""), "product_id": str(i.get("product_id") or ""),
        "quantity": int(i.get("quantity") or 0), "price_cents": int(i.get("price_cents") or 0),
        "effective_as_of": i.get("effective_as_of"),
    } for i in sorted(remote_inventory, key=lambda value: str(value.get("id") or ""))]
    proposed_total = sum(row["desired_quantity"] for row in republish)
    excluded_count = len(exclusions)
    eligible_local_copies = eligible_candidates - excluded_count
    unexpected_exclusions = [row for row in exclusions if row["card"] not in EXPECTED_LAUNCH_HOLDS]
    remote_item_by_id = {str(item.get("id") or ""): item for item in remote_inventory}
    remote_only_positive = sum(
        row["product_id"] not in all_binding_product_ids
        and _remote_canonical_key(remote_item_by_id[row["inventory_id"]]) not in canonical_groups
        for row in blank_rows
    )
    ready = (
        proposed_total == eligible_local_copies
        and not unexpected_exclusions
        and all(row["validation_status"] == "validated" for row in blank_rows)
    )
    blank_payloads = [[{
        "product_type": row["product_type"], "product_id": row["product_id"],
        "price_cents": None, "quantity": 0,
    } for row in blank_rows[start:start + 2000]] for start in range(0, len(blank_rows), 2000)]
    republish_payloads = [[{
        "product_type": "mtg_single", "product_id": row["product_id"],
        "price_cents": row["publish_price_cents"], "quantity": row["desired_quantity"],
    } for row in republish[start:start + 2000]] for start in range(0, len(republish), 2000)]
    return {
        "preview_only": True, "executor_enabled": False,
        "maintenance_warning": "FULL REBUILD IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF.",
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "local_snapshot_hash": stable_hash(local_snapshot),
        "remote_snapshot_hash": stable_hash(remote_snapshot),
        "binding_evidence_hashes": sorted(all_binding_hashes),
        "initial_price_evidence": sorted(
            (row["binding_id"], row["evidence_hash"], row["status"], row["target_price_cents"])
            for row in pricing.get("results") or []
        ),
        "initial_price_rows": pricing.get("results") or [],
        "blank_rows": blank_rows, "republish_rows": republish, "exclusions": exclusions,
        "blank_payloads": blank_payloads, "republish_payloads": republish_payloads,
        "summary": {
            "ready": ready, "positive_listings_to_blank": len(blank_rows),
            "remote_copies_to_blank": sum(row["current_quantity"] for row in blank_rows),
            "blank_writes": len(blank_rows),
            "remote_only_positive_included": remote_only_positive,
            "eligible_variants_to_republish": len(republish), "copies_to_republish": proposed_total,
            "existing_variants": sum(row["source_type"] == "existing_managed_variant" for row in republish),
            "net_new_variants": sum(row["source_type"] == "validated_new_product_binding" for row in republish),
            "republish_writes": len(republish), "excluded_physical_cards": excluded_count,
            "available_local_candidates": eligible_candidates,
            "eligible_local_copies": eligible_local_copies,
            "unexpected_exclusions": len(unexpected_exclusions),
            "quantity_accounting_matches": proposed_total == eligible_local_copies,
        },
    }


def _excluded(card, reason):
    return {"inventory_card_id": card.id, "card": card.name, "set_code": card.set_code,
            "collector_number": card.collector_number, "condition": card.condition,
            "finish": card.finish, "reason": reason}


def _publish_row(cards, source, product_id, remote, price, behavior, binding_hash=None, price_hash=None, identity=None):
    card = cards[0]
    identity = identity or {}
    return {
        "card": card.name, "set_code": card.set_code, "collector_number": card.collector_number,
        "language_id": identity.get("language_id") or card.language_id or "EN",
        "condition_id": identity.get("condition_id") or card.condition_id,
        "finish_id": identity.get("finish_id") or card.finish_id,
        "local_contributing_card_ids": sorted(c.id for c in cards),
        "desired_quantity": len(cards), "source_type": source, "product_id": product_id,
        "existing_remote_inventory_id": str((remote or {}).get("id") or "") or None,
        "existing_remote_quantity": int((remote or {}).get("quantity") or 0),
        "existing_remote_price_cents": int((remote or {}).get("price_cents") or 0) or None,
        "publish_price_behavior": behavior, "publish_price_cents": price,
        "binding_evidence_hash": binding_hash, "initial_price_evidence_hash": price_hash,
        "validation_status": "validated",
    }


def validate_rebuild_staleness(reviewed, fresh, confirmation):
    if confirmation != REBUILD_CONFIRMATION:
        raise ValueError("Maintenance confirmation did not match")
    for field, message in (
        ("local_snapshot_hash", "Local inventory changed"),
        ("remote_snapshot_hash", "Seller inventory changed"),
        ("binding_evidence_hashes", "Remote binding evidence changed"),
        ("initial_price_evidence", "Initial price evidence changed"),
    ):
        if reviewed[field] != fresh[field]:
            raise ValueError(message)
    return True


def verify_blank_state(plan, seller_inventory):
    by_product = {str(item.get("product_id") or ""): item for item in seller_inventory}
    failures = [row["product_id"] for row in plan["blank_rows"] if int((by_product.get(row["product_id"]) or {}).get("quantity") or 0) != 0]
    if failures:
        raise RuntimeError("Blank verification failed; republish prohibited")
    return True


def verify_republish_state(plan, seller_inventory):
    by_product = {str(item.get("product_id") or ""): item for item in seller_inventory}
    for row in plan["republish_rows"]:
        item = by_product.get(row["product_id"])
        if not item or int(item.get("quantity") or 0) != row["desired_quantity"]:
            raise RuntimeError(f"Republish quantity mismatch for {row['product_id']}")
        if row["publish_price_behavior"] == "set_verified_initial_price" and int(item.get("price_cents") or 0) != row["publish_price_cents"]:
            raise RuntimeError(f"Republish price mismatch for {row['product_id']}")
        if row["publish_price_behavior"] == "preserve_with_null" and int(item.get("price_cents") or 0) != row["existing_remote_price_cents"]:
            raise RuntimeError(f"Preserved price mismatch for {row['product_id']}")
    return True


def run_rebuild_steps(plan, writer, seller_loader):
    for payload in plan["blank_payloads"]:
        writer(payload)
    verify_blank_state(plan, seller_loader(min_quantity=0))
    for payload in plan["republish_payloads"]:
        writer(payload)
    verify_republish_state(plan, seller_loader(min_quantity=0))
    return {"status": "reconciled", "seller_reads": 2}


def execute_maintenance_rebuild(*args, **kwargs):
    if not MAINTENANCE_EXECUTOR_ENABLED:
        raise RuntimeError("Maintenance rebuild executor is disabled")
    raise RuntimeError("Executor enablement requires separate approval")
