"""Day-to-day publishing of net-new Mana Pool listings.

Scoped to the ``local_only_requires_listing`` category from an inventory
mirror preview (`inventory_mirror_service.build_inventory_mirror_preview`):
cards CardFoundry already owns and considers sellable, but Mana Pool has
never listed at all. Quantity/decrease reconciliation on already-listed
products is a separate, more cautious concern and is out of scope here.

Preview (pricing) and apply (writing) are deliberately split, matching the
Price Updates page pattern: preview does no writes, apply re-validates
freshness immediately before writing and reports Mana Pool's own per-item
result rather than building separate isolation machinery on top of it.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from catalog_resolution_service import requested_variant
from models import InventoryCard, RemoteProductBinding
from new_listing_pricing_service import price_initial_bindings, price_new_listing_candidates


CANONICAL_FIELDS = ("mtgjson_id", "language_id", "condition_id", "finish_id")


class NewListingUploadError(ValueError):
    pass


def _representative_identity(cards: list) -> dict:
    identity = requested_variant(cards[0])
    identity["mtgjson_id"] = str(cards[0].mtgjson_id or "").strip().upper()
    return identity


def _existing_binding_for_cards(session: Session, card_ids: list[int]):
    """Look for an already-persisted validated binding covering any of these
    cards. Used only as a fallback for the rare card with no scryfall_id --
    this never resolves a *new* binding, since that requires a scryfall_id
    too (see catalog_resolution_service.requested_variant).
    """
    wanted = set(card_ids)
    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.binding_status == "validated",
    ).all():
        if wanted & set(json.loads(binding.local_card_ids_json)):
            return binding
    return None


def extract_new_listing_candidates(session: Session, mirror_preview: dict) -> tuple[list[dict], list[dict]]:
    """Build pricing candidates from a mirror preview's local_only_requires_listing rows.

    Returns (candidates, excluded). Each candidate is
    ``{"key", "identity", "desired_quantity", "card_ids", "path", "product_id"?}``
    where ``path`` is ``"scryfall_id"`` (the common case) or ``"product_id"``
    (only when the card has no scryfall_id and an existing binding covers
    it). ``excluded`` rows carry a ``reason`` and are never written.
    """
    rows = [
        row for row in mirror_preview.get("rows") or []
        if row.get("category") == "local_only_requires_listing"
    ]
    candidates = []
    excluded = []
    for row in rows:
        identity_key = row.get("canonical_identity") or {}
        key = tuple(identity_key.get(field) for field in CANONICAL_FIELDS)
        cards = [
            card for card in (
                session.get(InventoryCard, card_id)
                for card_id in row.get("local_contributing_card_ids") or []
            ) if card is not None
        ]
        card_ids = [card.id for card in cards]
        if not cards:
            excluded.append({
                "key": key, "reason": "No contributing inventory cards found",
                "card_ids": card_ids,
            })
            continue
        identity = _representative_identity(cards)
        desired_quantity = int(row.get("desired_quantity") or len(cards))
        if identity.get("scryfall_id"):
            candidates.append({
                "key": key, "identity": identity, "desired_quantity": desired_quantity,
                "card_ids": card_ids, "path": "scryfall_id",
            })
            continue
        binding = _existing_binding_for_cards(session, card_ids)
        if not binding:
            excluded.append({
                "key": key, "reason": "No scryfall_id and no existing product binding",
                "card_ids": card_ids,
            })
            continue
        candidates.append({
            "key": key, "identity": identity, "desired_quantity": desired_quantity,
            "card_ids": card_ids, "path": "product_id", "product_id": binding.product_id,
            "binding_id": binding.id,
        })
    return candidates, excluded


def build_new_listing_preview(
    session: Session,
    mirror_preview: dict,
    optimizer_call, listings_call, seller_id,
    market_catalog_scryfall_call,
    market_catalog_product_call=None,
    undercut_cents=5, floor_cents=65,
) -> dict:
    """Price every local_only_requires_listing candidate. No writes."""
    candidates, excluded = extract_new_listing_candidates(session, mirror_preview)
    scryfall_candidates = [c for c in candidates if c["path"] == "scryfall_id"]
    binding_candidates = [c for c in candidates if c["path"] == "product_id"]

    by_key = {}

    if scryfall_candidates:
        pricing = price_new_listing_candidates(
            [{"key": c["key"], "identity": c["identity"]} for c in scryfall_candidates],
            optimizer_call, listings_call, seller_id,
            undercut_cents=undercut_cents, floor_cents=floor_cents,
            market_catalog_call=market_catalog_scryfall_call,
        )
        by_key.update({row["key"]: row for row in pricing["results"]})

    if binding_candidates:
        bindings = [
            session.get(RemoteProductBinding, c["binding_id"]) for c in binding_candidates
        ]
        pricing = price_initial_bindings(
            bindings, optimizer_call, listings_call, seller_id,
            undercut_cents=undercut_cents, floor_cents=floor_cents,
            market_catalog_call=market_catalog_product_call,
        )
        binding_id_to_key = {c["binding_id"]: c["key"] for c in binding_candidates}
        for row in pricing["results"]:
            by_key[binding_id_to_key[row["binding_id"]]] = row

    rows = []
    for candidate in candidates:
        price_row = by_key.get(candidate["key"]) or {
            "status": "hold", "reason": "No pricing result", "target_price_cents": None,
        }
        rows.append({**candidate, **price_row})
    for row in excluded:
        rows.append({**row, "status": "excluded"})

    return {
        "preview_only": True,
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_local_snapshot_hash": mirror_preview.get("local_snapshot_hash"),
        "source_remote_snapshot_hash": mirror_preview.get("remote_snapshot_hash"),
        "rows": rows,
        "summary": {
            "candidates": len(candidates),
            "priced": sum(row.get("status") == "priced" for row in rows),
            "held": sum(row.get("status") == "hold" for row in rows),
            "excluded": len(excluded),
        },
    }


def _remote_indexes(remote_inventory: list[dict]) -> tuple[dict, dict]:
    remote_by_scryfall = {}
    remote_by_product = {}
    for item in remote_inventory:
        single = (item.get("product") or {}).get("single") or {}
        scryfall_id = str(single.get("scryfall_id") or "").lower()
        remote_identity = (
            scryfall_id,
            str(single.get("language_id") or "").upper(),
            str(single.get("condition_id") or "").upper(),
            str(single.get("finish_id") or "").upper(),
        )
        if scryfall_id:
            remote_by_scryfall[remote_identity] = item
        product_id = str(item.get("product_id") or "")
        if product_id:
            remote_by_product.setdefault(product_id, []).append(item)
    return remote_by_scryfall, remote_by_product


def apply_new_listing_preview(
    session: Session,
    preview: dict,
    seller_loader,
    scryfall_writer,
    product_writer,
    optimizer_call,
    listings_call,
    seller_id,
    market_catalog_scryfall_call,
    market_catalog_product_call=None,
    undercut_cents=5,
    floor_cents=65,
) -> dict:
    """Write priced rows to Mana Pool.

    Re-validates, immediately before writing, everything the preview
    reviewed:

    1. Local available quantity for each row's cards is unchanged.
    2. Mana Pool still doesn't already list that identity.
    3. The competitor/market price backing the row hasn't moved -- a live
       marketplace price is only trustworthy for a moment, and a stale
       preview can genuinely be wrong minutes later (a cheaper same-or-
       better-condition listing can appear or disappear).

    Unlike the local-availability/already-listed checks (which are a
    correctness concern -- writing on top of stale evidence there could
    double-list or misreport a quantity), a row failing (3) is not written
    but does not block its siblings: nothing is unsafe about publishing
    the other rows at their still-current prices, matching the same
    batch-isolation principle used for order-status sync. Every excluded
    row is reported with why, including the reviewed vs. current price
    where relevant, so the operator can re-preview just those.
    """
    priced_rows = [row for row in preview.get("rows") or [] if row.get("status") == "priced"]
    if not priced_rows:
        raise NewListingUploadError("This preview has no priced rows to publish.")

    remote_by_scryfall, remote_by_product = _remote_indexes(seller_loader(min_quantity=0))

    still_eligible = []
    excluded = []
    for row in priced_rows:
        cards = [session.get(InventoryCard, card_id) for card_id in row.get("card_ids") or []]
        still_available = [card for card in cards if card and card.status == "available"]
        if len(still_available) != row.get("desired_quantity"):
            excluded.append({**row, "exclusion_reason": "Local availability changed since preview"})
            continue
        identity = row["identity"]
        if row["path"] == "scryfall_id":
            remote_identity = (
                str(identity.get("scryfall_id") or "").lower(),
                str(identity.get("language_id") or "").upper(),
                str(identity.get("condition_id") or "").upper(),
                str(identity.get("finish_id") or "").upper(),
            )
            existing = remote_by_scryfall.get(remote_identity)
        else:
            matches = remote_by_product.get(row.get("product_id"))
            existing = matches[0] if matches else None
        if existing and int(existing.get("quantity") or 0) > 0:
            excluded.append({**row, "exclusion_reason": "Mana Pool already lists this identity"})
            continue
        still_eligible.append(row)

    scryfall_eligible = [row for row in still_eligible if row["path"] == "scryfall_id"]
    product_eligible = [row for row in still_eligible if row["path"] == "product_id"]

    fresh_by_key = {}
    if scryfall_eligible:
        fresh_pricing = price_new_listing_candidates(
            [{"key": tuple(row["key"]), "identity": row["identity"]} for row in scryfall_eligible],
            optimizer_call, listings_call, seller_id,
            undercut_cents=undercut_cents, floor_cents=floor_cents,
            market_catalog_call=market_catalog_scryfall_call,
        )
        fresh_by_key.update({row["key"]: row for row in fresh_pricing["results"]})
    if product_eligible:
        bindings = [
            session.get(RemoteProductBinding, row["binding_id"]) for row in product_eligible
        ]
        fresh_pricing = price_initial_bindings(
            bindings, optimizer_call, listings_call, seller_id,
            undercut_cents=undercut_cents, floor_cents=floor_cents,
            market_catalog_call=market_catalog_product_call,
        )
        binding_id_to_key = {row["binding_id"]: tuple(row["key"]) for row in product_eligible}
        for fresh_row in fresh_pricing["results"]:
            fresh_by_key[binding_id_to_key[fresh_row["binding_id"]]] = fresh_row

    fresh_rows = []
    for row in still_eligible:
        fresh = fresh_by_key.get(tuple(row["key"]))
        reviewed_price = int(row["target_price_cents"])
        if not fresh or fresh.get("status") != "priced":
            excluded.append({
                **row, "exclusion_reason": "No longer priceable: "
                + ((fresh or {}).get("reason") or "no current pricing evidence"),
                "reviewed_price_cents": reviewed_price,
            })
            continue
        current_price = int(fresh["target_price_cents"])
        if current_price != reviewed_price:
            excluded.append({
                **row,
                "exclusion_reason": "Price changed since preview",
                "reviewed_price_cents": reviewed_price,
                "current_price_cents": current_price,
            })
            continue
        fresh_rows.append(row)

    if not fresh_rows:
        raise NewListingUploadError(
            f"None of the {len(priced_rows)} reviewed row(s) are still valid to publish "
            "-- local availability, Mana Pool's listings, or competitor prices changed. "
            "Run a fresh preview."
        )

    scryfall_updates = [
        {
            "scryfall_id": row["identity"]["scryfall_id"],
            "language_id": row["identity"]["language_id"],
            "condition_id": row["identity"]["condition_id"],
            "finish_id": row["identity"]["finish_id"],
            "price_cents": int(row["target_price_cents"]),
            "quantity": int(row["desired_quantity"]),
        }
        for row in fresh_rows if row["path"] == "scryfall_id"
    ]
    product_updates = [
        {
            "product_type": "mtg_single",
            "product_id": row["product_id"],
            "price_cents": int(row["target_price_cents"]),
            "quantity": int(row["desired_quantity"]),
        }
        for row in fresh_rows if row["path"] == "product_id"
    ]

    responses = {}
    if scryfall_updates:
        responses["scryfall_id"] = scryfall_writer(scryfall_updates)
    if product_updates:
        responses["product_id"] = product_writer(product_updates)

    return {
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "scryfall_updates": scryfall_updates,
        "product_updates": product_updates,
        "responses": responses,
        "excluded": excluded,
    }
