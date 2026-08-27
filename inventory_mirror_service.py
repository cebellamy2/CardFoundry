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


def _mtgjson_override_key(product_id, language_id, condition_id, finish_id) -> tuple[str, str, str, str]:
    """Substitute for canonical_key()/remote_key() when an operator has
    explicitly confirmed a card's printing will never carry a documented
    MTGJSON identity (see RemoteProductBinding.mtgjson_override_confirmed_at)
    -- groups and matches by the bound Mana Pool product_id instead, so the
    card stays tracked by every future sync rather than becoming permanently
    unmanaged. Embedding product_id in the mtgjson_id slot keeps the key the
    same shape as CANONICAL_FIELDS so no other grouping logic needs to know
    the difference.
    """
    return (
        f"__mtgjson_override__:{product_id}",
        normalized_language_id({"Language ID": language_id}),
        str(condition_id or "").strip().upper(),
        str(finish_id or "").strip().upper(),
    )


def _scryfall_fallback_key(scryfall_id, language_id, condition_id, finish_id) -> tuple[str, str, str, str]:
    """Substitute for canonical_key()/remote_key() when a card has no
    mtgjson_id and no RemoteProductBinding at all -- see
    pending_first_listing_card_ids on build_inventory_mirror_preview.
    scryfall_id is precise enough to key an exact scryfall_id + variant
    match directly (this isn't resolving which Mana Pool product a
    printing belongs to -- that's the ambiguity MTGJSON exists to guard
    against -- it's just recognizing one already-known scryfall_id
    against a remote item that carries that same scryfall_id).
    """
    return (
        f"__scryfall__:{str(scryfall_id or '').strip().lower()}",
        normalized_language_id({"Language ID": language_id}),
        str(condition_id or "").strip().upper(),
        str(finish_id or "").strip().upper(),
    )


def crosscheck(name, set_code, collector_number) -> tuple[str, str, str]:
    return (
        str(name or "").strip().casefold(),
        str(set_code or "").strip().upper(),
        str(collector_number or "").strip().upper(),
    )


def _display_name(local, remote) -> str:
    """A human name for a canonical-identity row -- every row carries an
    mtgjson_id, which means nothing to a person reading a table. Unions
    local card name(s) with the remote listing's name(s) rather than
    preferring one side: an ordinary matched row has one name either
    way, a remote-only row has no local side to draw from, and an
    ambiguous_identity row's differing names *are* the ambiguity --
    joining both surfaces it instead of arbitrarily hiding one."""
    names = {card.name for card in local if card.name} | {
        str(((item.get("product") or {}).get("single") or {}).get("name") or "")
        for item in remote
    } - {""}
    return " / ".join(sorted(names))


def _hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def build_inventory_mirror_preview(
    cards, batches_by_id, allocations, remote_inventory,
    fail_closed_on_unresolved: bool = True,
    mtgjson_override_product_ids: dict[int, str] | None = None,
    pending_first_listing_card_ids: set[int] | None = None,
):
    """fail_closed_on_unresolved=False skips cards lacking a canonical
    MTGJSON identity instead of aborting the whole preview -- for a
    caller that runs routinely and wants to sync everything resolvable
    now while reporting the rest, rather than the occasional manual
    build where failing the whole job closed is the safer default.
    Skipped cards are reported back via "unresolved_card_ids"; they were
    never going to be grouped by canonical_key() anyway (that already
    excludes them), so relaxing this check changes nothing else about
    the preview.

    ``mtgjson_override_product_ids`` maps InventoryCard.id to the exact
    Mana Pool product_id an operator has explicitly confirmed for a card
    whose printing has no documented MTGJSON identity (see
    RemoteProductBinding.mtgjson_override_confirmed_at). Those cards are
    grouped and matched by that product_id instead of the usual
    mtgjson_id-keyed identity -- on both the local and remote side, and on
    every future run, not just the one that first lists them.

    ``pending_first_listing_card_ids`` is a different, narrower case: a
    card with no mtgjson_id *and* no RemoteProductBinding at all -- e.g.
    a printing correction or import landed it as a genuinely new-to-Mana-
    Pool product (see printing_correction_service.py's pending_first_
    listing resolution). Mana Pool's own write API needs no pre-existing
    product_id, creates the product as a side effect of the first
    listing, and new_listing_upload_service.py's scryfall_id publish path
    already never needed mtgjson_id or a binding either -- the only real
    gap was here, this function refusing to even group such a card so it
    could reach that path. Grouped and matched by (scryfall_id, language,
    condition, finish) instead, on both sides, but only when that exact
    key is one of these specific cards' own -- a random other remote
    listing missing mtgjson_id is never reclassified by coincidence.
    """
    mtgjson_override_product_ids = mtgjson_override_product_ids or {}
    pending_first_listing_card_ids = {
        card.id for card in cards
        if card.id in (pending_first_listing_card_ids or set()) and card.scryfall_id
    }
    pending_first_listing_keys = {
        _scryfall_fallback_key(card.scryfall_id, card.language_id, card.condition_id, card.finish_id)
        for card in cards
        if card.id in pending_first_listing_card_ids
    }
    blocking_card_ids = sorted(
        card.id for card in cards
        if card.status == SELLABLE_STATUS
        and batches_by_id.get(card.batch_id)
        and not batches_by_id[card.batch_id].is_archived
        and canonical_key(card) is None
        and card.id not in mtgjson_override_product_ids
        and card.id not in pending_first_listing_card_ids
    )
    if blocking_card_ids and fail_closed_on_unresolved:
        raise ValueError(
            "Active sellable inventory cards lack canonical MTGJSON identity: "
            + ", ".join(str(card_id) for card_id in blocking_card_ids)
        )

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
    for card in cards:
        batch = batches_by_id.get(card.batch_id)
        if card.id in invalid_card_ids:
            continue
        key = canonical_key(card)
        if not key:
            override_product_id = mtgjson_override_product_ids.get(card.id)
            if override_product_id:
                key = _mtgjson_override_key(
                    override_product_id, card.language_id, card.condition_id, card.finish_id,
                )
            elif card.id in pending_first_listing_card_ids and card.scryfall_id:
                key = _scryfall_fallback_key(
                    card.scryfall_id, card.language_id, card.condition_id, card.finish_id,
                )
            else:
                continue
        local_groups[key].append(card)

    override_product_ids = set(mtgjson_override_product_ids.values())
    remote_groups = defaultdict(list)
    remote_missing = []
    for item in remote_inventory:
        if item.get("product_type") != "mtg_single":
            continue
        key = remote_key(item)
        if not key:
            product_id = str(item.get("product_id") or "")
            single = (item.get("product") or {}).get("single") or {}
            if product_id in override_product_ids:
                key = _mtgjson_override_key(
                    product_id, single.get("language_id"),
                    single.get("condition_id"), single.get("finish_id"),
                )
            else:
                fallback_key = _scryfall_fallback_key(
                    single.get("scryfall_id"), single.get("language_id"),
                    single.get("condition_id"), single.get("finish_id"),
                )
                if fallback_key in pending_first_listing_keys:
                    key = fallback_key
        if key:
            remote_groups[key].append(item)
        else:
            remote_missing.append(item)

    rows = []
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
            "name": _display_name(local, remote),
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
            if not sellable:
                # Every local card under this identity is historical (sold,
                # removed, unsellable, or in an archived batch) -- nothing
                # sellable to list and no remote record to reconcile against,
                # so there's nothing actionable here. Emitting a row anyway
                # would just be a permanent zero-quantity "requires listing"
                # candidate that immediately gets excluded downstream.
                continue
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
            "name": str(((item.get("product") or {}).get("single") or {}).get("name") or ""),
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
        "unresolved_card_ids": blocking_card_ids,
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
