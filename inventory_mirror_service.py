"""Pure preview and stale-validation logic for maintenance inventory mirroring."""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from import_service import normalized_language_id


CANONICAL_FIELDS = ("mtgjson_id", "language_id", "condition_id", "finish_id")
SELLABLE_STATUS = "available"
KNOWN_STATUSES = {"available", "unsellable", "reserved", "sold", "removed"}
ACTIVE_ALLOCATION_STATUSES = {"allocated", "picked", "packed"}
MAINTENANCE_CONFIRMATION = "STORE IS OFF - MIRROR INVENTORY"


def canonical_key(values) -> tuple[str, str, str, str] | None:
    result = tuple(
        normalized_language_id({"Language ID": getattr(values, field, None)})
        if field == "language_id"
        else str(getattr(values, field, None) or "").strip().upper()
        for field in CANONICAL_FIELDS
    )
    return result if all(result) else None


def remote_key(item: dict) -> tuple[str, str, str, str] | None:
    single = ((item.get("product") or {}).get("single") or {})
    result = tuple(str(single.get(field) or "").strip().upper() for field in CANONICAL_FIELDS)
    return result if all(result) else None


def crosscheck(name, set_code, collector_number) -> tuple[str, str, str]:
    return (
        str(name or "").strip().casefold(),
        str(set_code or "").strip().upper(),
        str(collector_number or "").strip().upper(),
    )


def _hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def build_inventory_mirror_preview(cards, batches_by_id, allocations, remote_inventory):
    invalid_card_ids = set()
    invalid_reasons = []
    for card in cards:
        if card.status not in KNOWN_STATUSES:
            invalid_card_ids.add(card.id)
            invalid_reasons.append(f"Card {card.id} has unknown status {card.status!r}")
    for allocation in allocations:
        if allocation.status in ACTIVE_ALLOCATION_STATUSES:
            card = next((row for row in cards if row.id == allocation.inventory_card_id), None)
            if card and card.status == SELLABLE_STATUS:
                invalid_card_ids.add(card.id)
                invalid_reasons.append(f"Card {card.id} is available with active allocation {allocation.id}")

    local_groups = defaultdict(list)
    missing_rows = []
    for card in cards:
        batch = batches_by_id.get(card.batch_id)
        if card.id in invalid_card_ids:
            continue
        key = canonical_key(card)
        if not key:
            if card.status == SELLABLE_STATUS and batch and not batch.is_archived:
                missing_rows.append(card)
            continue
        local_groups[key].append(card)

    remote_groups = defaultdict(list)
    remote_missing = []
    for item in remote_inventory:
        if item.get("product_type") != "mtg_single":
            continue
        key = remote_key(item)
        if key:
            remote_groups[key].append(item)
        else:
            remote_missing.append(item)

    rows = []
    for card in missing_rows:
        rows.append({
            "category": "missing_metadata",
            "local_contributing_card_ids": [card.id],
            "reason": "Active sellable card lacks complete canonical identity",
        })
    for reason in invalid_reasons:
        rows.append({"category": "invalid_local_state", "reason": reason})

    for key in sorted(set(local_groups) | set(remote_groups)):
        local = local_groups.get(key, [])
        remote = remote_groups.get(key, [])
        sellable = [
            card for card in local
            if card.status == SELLABLE_STATUS
            and batches_by_id.get(card.batch_id)
            and not batches_by_id[card.batch_id].is_archived
        ]
        local_crosschecks = {crosscheck(c.name, c.set_code, c.collector_number) for c in local}
        remote_crosschecks = {
            crosscheck(
                ((item.get("product") or {}).get("single") or {}).get("name"),
                ((item.get("product") or {}).get("single") or {}).get("set"),
                ((item.get("product") or {}).get("single") or {}).get("number"),
            )
            for item in remote
        }
        evidence = {
            "canonical_identity": dict(zip(CANONICAL_FIELDS, key)),
            "local_contributing_card_ids": sorted(card.id for card in sellable),
            "desired_quantity": len(sellable),
        }
        if local and (len(local_crosschecks) != 1 or (remote and remote_crosschecks != local_crosschecks)):
            rows.append({**evidence, "category": "ambiguous_identity", "reason": "Cross-check metadata conflicts"})
            continue
        if len(remote) > 1:
            rows.append({**evidence, "category": "ambiguous_identity", "reason": "Multiple remote records share canonical identity"})
            continue
        if not local:
            item = remote[0]
            rows.append({
                **evidence,
                **_remote_evidence(item),
                "category": "remote_only_unmanaged",
                "reason": "Remote variant has no canonical local inventory history",
            })
            continue
        if not remote:
            rows.append({
                **evidence,
                "category": "local_only_requires_listing",
                "reason": "Canonical local variant has no remote inventory record",
            })
            continue

        item = remote[0]
        current = int(item.get("quantity") or 0)
        desired = len(sellable)
        if desired == 0 and current != 0:
            category = "zero_candidate"
        elif desired > current:
            category = "increase_quantity"
        elif desired < current:
            category = "decrease_quantity"
        else:
            category = "hold_equal"
        rows.append({**evidence, **_remote_evidence(item), "category": category, "reason": "Exact managed variant validated"})

    for item in remote_missing:
        rows.append({
            **_remote_evidence(item),
            "category": "ambiguous_identity",
            "reason": "Remote inventory lacks complete canonical identity",
        })

    local_snapshot = sorted(
        (tuple((row.get("canonical_identity") or {}).get(field) for field in CANONICAL_FIELDS), row.get("local_contributing_card_ids"), row.get("desired_quantity"))
        for row in rows if row.get("canonical_identity")
    )
    remote_snapshot = sorted(
        (tuple((row.get("canonical_identity") or {}).get(field) for field in CANONICAL_FIELDS), row.get("remote_inventory_id"), row.get("remote_product_id"),
         row.get("current_remote_quantity"), row.get("current_remote_price"), row.get("effective_as_of"))
        for row in rows if row.get("remote_inventory_id")
    )
    counts = Counter(row["category"] for row in rows)
    writable = [row for row in rows if row["category"] in {
        "increase_quantity", "decrease_quantity", "zero_candidate",
    }]
    return {
        "preview_only": True,
        "maintenance_mode_required": True,
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "local_snapshot_hash": _hash(local_snapshot),
        "remote_snapshot_hash": _hash(remote_snapshot),
        "rows": rows,
        "summary": {
            "categories": dict(sorted(counts.items())),
            "exact_quantity_writes": len(writable),
            "managed_remote_variants": sum(
                row["category"] not in {"remote_only_unmanaged", "local_only_requires_listing", "ambiguous_identity", "missing_metadata", "invalid_local_state"}
                for row in rows
            ),
            "unresolved_mappings": sum(
                row["category"] in {"local_only_requires_listing", "ambiguous_identity", "missing_metadata", "invalid_local_state"}
                for row in rows
            ),
        },
    }


def _remote_evidence(item):
    return {
        "remote_inventory_id": str(item.get("id") or ""),
        "remote_product_id": str(item.get("product_id") or ""),
        "current_remote_quantity": int(item.get("quantity") or 0),
        "current_remote_price": int(item.get("price_cents") or 0),
        "effective_as_of": item.get("effective_as_of"),
    }


def validate_reviewed_snapshots(reviewed, fresh, confirmation):
    if confirmation != MAINTENANCE_CONFIRMATION:
        raise ValueError("Maintenance confirmation did not match.")
    if reviewed["local_snapshot_hash"] != fresh["local_snapshot_hash"]:
        raise ValueError("Local inventory changed after preview.")
    if reviewed["remote_snapshot_hash"] != fresh["remote_snapshot_hash"]:
        raise ValueError("Mana Pool inventory changed after preview.")
    if reviewed["rows"] != fresh["rows"]:
        raise ValueError("Reviewed inventory rows changed after preview.")
    return True


def quantity_only_payload(rows):
    return [{
        "product_type": "mtg_single",
        "product_id": row["remote_product_id"],
        "price_cents": None,
        "quantity": row["desired_quantity"],
    } for row in rows if row["category"] in {
        "increase_quantity", "decrease_quantity", "zero_candidate",
    }]
