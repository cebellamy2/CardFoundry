"""Day-to-day quantity reconciliation between CardFoundry and Mana Pool.

Scoped to identities Mana Pool already lists -- the increase_quantity,
decrease_quantity, and zero_candidate categories from
inventory_mirror_service.build_inventory_mirror_preview(). A brand-new,
never-listed identity is new_listing_upload_service's job instead; this
module never creates a listing, only adjusts one that already exists.

increase_quantity is auto-applied when the gap is explained by cards
that are genuinely sellable right now (see _raisable_gap). The write is
computed as a delta -- fresh remote quantity + fresh new units, clamped
to fresh local desired quantity -- rather than a blind re-assertion of a
stale absolute number, so a concurrent sale is always reflected (Mana
Pool decrements its own quantity immediately on a sale, independent of
whether CardFoundry has ingested that order yet) instead of silently
overwritten.

THE TIMESTAMP GATE IS GONE, DELIBERATELY. Until 2026-09-17 the gate also
required every gap-explaining card to have been imported AFTER the remote
listing's own effective_as_of. That was a reasonable proxy for "new stock
Mana Pool has not seen yet" while listings were touched rarely. It is not
any more: since v1.174.0 the bulk pricing job rewrites every listing three
times a day, so effective_as_of is always hours old while a real card's
imported_at is weeks old, and the gate excluded 138 of 138 genuine
under-listings -- $300.39 of stock Mana Pool was not offering, permanently.

Removing it costs nothing, because the safety property was never actually
carried by that comparison: apply_reconciliation_preview re-reads Mana
Pool's quantity fresh immediately before writing and clamps to the fresh
local desired count. The stale-number risk is handled downstream, at the
moment of the write, by data read seconds earlier. Do not reintroduce a
timestamp check here; fix the downstream re-read instead if it is ever
found wanting.

decrease_quantity/zero_candidate need no such gate: writing a
(possibly slightly stale) lower number is self-correcting, never an
oversell. It does rely on local state being fresh, though -- a Mana
Pool sale reducing true local availability only shows up here once
CardFoundry has ingested that order, which is why apply always
re-ingests orders first.
"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from inventory_mirror_service import SELLABLE_STATUS
from models import (
    Batch, FulfillmentException, InventoryCard, PickAllocation,
    RemoteProductBinding,
)
import order_service
from order_service import ingest_manapool_orders


class InventoryReconciliationError(ValueError):
    pass


# Why an increase was refused. Surfaced per row so an excluded gap is
# explicable rather than silently absent -- the previous gate computed a
# reason and showed it nowhere, which is how 138 rows sat unreconciled
# without anyone being able to see why.
GAP_REFUSALS = {
    "no_gap": "Mana Pool already lists at least the local quantity",
    "not_enough_cards": "Fewer sellable cards locally than the gap claims",
    "unavailable": "A contributing card is no longer available",
    "unpriced": "A contributing card has no price -- it must not be listed",
    "open_exception": "A contributing card is under an unresolved fulfillment exception",
}


def _raisable_gap(session: Session, row: dict) -> tuple[list | None, str | None]:
    """The cards that justify raising this listing, or (None, reason).

    Every guard here is about the cards being genuinely sellable RIGHT
    NOW. What is deliberately NOT here is any comparison against the
    listing's effective_as_of -- see this module's docstring for why that
    check became unpassable and why the downstream fresh re-read is the
    real safety.

    An unpriced card is the guard that does the most work in practice:
    126 of the 138 stuck rows have one, and raising them would put a card
    on sale with no price.
    """
    gap = int(row.get("desired_quantity") or 0) - int(row.get("current_remote_quantity") or 0)
    if gap <= 0:
        return None, "no_gap"
    card_ids = row.get("local_contributing_card_ids") or []
    cards = [
        card for card in (session.get(InventoryCard, card_id) for card_id in card_ids)
        if card is not None
    ]
    if any(card.status != "available" for card in cards):
        return None, "unavailable"
    if len(cards) < gap:
        return None, "not_enough_cards"
    if any(card.current_price is None for card in cards):
        return None, "unpriced"
    if _cards_under_open_exception(session, [card.id for card in cards]):
        return None, "open_exception"
    # Newest first: if more cards back this identity than the gap needs,
    # the most recently acquired are the ones Mana Pool is least likely
    # to already be counting.
    cards.sort(key=lambda card: card.imported_at, reverse=True)
    return [card.id for card in cards[:gap]], None


def _cards_under_open_exception(session: Session, card_ids: list) -> list:
    """Card ids sitting under an unresolved fulfillment exception.

    Such a card is physically in question -- the operator has said it is
    missing or wrong -- so offering another unit of it is the phantom
    stock the exception exists to prevent.
    """
    if not card_ids:
        return []
    return [
        allocation.inventory_card_id
        for allocation, _exception in (
            session.query(PickAllocation, FulfillmentException)
            .join(FulfillmentException,
                  FulfillmentException.pick_allocation_id == PickAllocation.id)
            .filter(PickAllocation.inventory_card_id.in_(card_ids),
                    FulfillmentException.inventory_resolution_state == "unresolved")
            .all()
        )
    ]


def extract_reconciliation_candidates(session: Session, mirror_preview: dict) -> tuple[list[dict], list[dict]]:
    """Build reconciliation candidates from a mirror preview's
    increase_quantity/decrease_quantity/zero_candidate rows.

    Returns (candidates, excluded). An increase candidate carries
    ``batch_codes``/``gap_card_ids`` for the apply-time re-check (the gap
    can span several batches -- batch_codes is informational display
    only, never read by apply); a decrease candidate carries nothing
    extra -- its write quantity is always recomputed fresh at apply time,
    never taken from the preview.
    """
    candidates = []
    excluded = []
    for row in mirror_preview.get("rows") or []:
        category = row.get("category")
        if category not in ("increase_quantity", "decrease_quantity", "zero_candidate"):
            continue
        base = {
            "canonical_identity": row.get("canonical_identity") or {},
            "name": row.get("name") or "",
            "product_id": row.get("remote_product_id"),
            "reviewed_desired_quantity": row.get("desired_quantity"),
            "reviewed_remote_quantity": row.get("current_remote_quantity"),
        }
        if category == "increase_quantity":
            gap_card_ids, refusal = _raisable_gap(session, row)
            if not gap_card_ids:
                excluded.append({
                    **base, "direction": "increase",
                    "refusal": refusal,
                    "reason": GAP_REFUSALS.get(refusal, "Increase refused"),
                })
                continue
            gap_cards = [session.get(InventoryCard, card_id) for card_id in gap_card_ids]
            batch_ids = sorted({card.batch_id for card in gap_cards if card is not None})
            batches = [session.get(Batch, batch_id) for batch_id in batch_ids]
            batch_codes = sorted(batch.batch_code for batch in batches if batch is not None)
            candidates.append({
                **base, "direction": "increase",
                "batch_codes": batch_codes,
                "gap_card_ids": gap_card_ids,
                "gap": len(gap_card_ids),
            })
        else:
            desired = base["reviewed_desired_quantity"] or 0
            remote = base["reviewed_remote_quantity"] or 0
            if desired >= remote:
                # zero_candidate in particular can re-flag the same
                # already-zeroed listing forever: build_inventory_mirror_
                # preview's bound-orphan branch (a bound product_id with
                # no local inventory of any status) categorizes it
                # zero_candidate purely on that shape, with no check that
                # remote is still above 0. Once a prior run has already
                # written it down to 0 (or it was never above the desired
                # count), there's nothing left to decrease -- apply-time
                # re-verification would exclude it anyway (write_quantity
                # >= fresh_remote_quantity), but only after burning the
                # whole batch: confirmed live (2026-09-09), a run whose
                # only eligible candidates were already-zeroed orphans
                # left zero real updates and raised "None of the reviewed
                # rows are still valid to reconcile", crashing the entire
                # perform-sync chain even though nothing needed fixing.
                excluded.append({
                    **base, "direction": "decrease",
                    "reason": "Already at or below desired quantity -- nothing to reconcile",
                })
                continue
            candidates.append({**base, "direction": "decrease"})
    return candidates, excluded


def build_reconciliation_preview(session: Session, mirror_preview: dict) -> dict:
    """Identify reconciliation candidates. No writes, no pricing -- these
    are quantity-only updates; price_cents is never touched.
    """
    candidates, excluded = extract_reconciliation_candidates(session, mirror_preview)
    rows = (
        [{**candidate, "status": "eligible"} for candidate in candidates]
        + [{**row, "status": "excluded"} for row in excluded]
    )
    return {
        "preview_only": True,
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_local_snapshot_hash": mirror_preview.get("local_snapshot_hash"),
        "source_remote_snapshot_hash": mirror_preview.get("remote_snapshot_hash"),
        "rows": rows,
        "summary": {
            "candidates": len(candidates),
            "increase": sum(row["direction"] == "increase" for row in candidates),
            "decrease": sum(row["direction"] == "decrease" for row in candidates),
            "excluded": len(excluded),
        },
    }


def _fresh_desired_quantity(session: Session, identity: dict) -> int:
    return (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.status == SELLABLE_STATUS,
            Batch.is_archived == False,
            func.upper(InventoryCard.mtgjson_id) == str(identity.get("mtgjson_id") or "").upper(),
            func.upper(InventoryCard.language_id) == str(identity.get("language_id") or "").upper(),
            func.upper(InventoryCard.condition_id) == str(identity.get("condition_id") or "").upper(),
            func.upper(InventoryCard.finish_id) == str(identity.get("finish_id") or "").upper(),
        )
        .count()
    )


def _ensure_binding_for_increase(session: Session, row: dict, product_id: str, remote_item: dict) -> str | None:
    """Create a validated RemoteProductBinding for this identity if none
    exists yet -- an increase write is CardFoundry asserting real
    sellable stock against this exact product_id, the same fact a new-
    listing publish or the v1.109 backfill would record as a binding.
    Without it, manapool_quantity_push_service (the immediate per-
    transition push) has no product_id to resolve for this identity and
    silently lands in UnresolvedQuantityPush the next time local stock
    changes -- confirmed live: this exact gap (increase to a listing
    with no binding, then a later removal with no push target) is what
    let a Mana Pool order arrive for stock CardFoundry no longer had
    (order 4050, Blood Money, 2026-09-06/07).

    Never raises and never overwrites an existing binding -- a
    conflicting binding already on this product_id is left alone and
    reported, matching every other binding-creation site's guard
    (backfill_remote_product_bindings.py, production_import_service.py).
    Returns "created", "existing", or "conflict" for the caller to
    report; caller commits.
    """
    identity = row.get("canonical_identity") or {}
    mtgjson_id = str(identity.get("mtgjson_id") or "")
    language_id = str(identity.get("language_id") or "")
    condition_id = str(identity.get("condition_id") or "")
    finish_id = str(identity.get("finish_id") or "")
    if not mtgjson_id:
        return None
    existing = session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
        RemoteProductBinding.binding_status == "validated",
        func.upper(RemoteProductBinding.mtgjson_id) == mtgjson_id.upper(),
        func.upper(RemoteProductBinding.language_id) == language_id.upper(),
        func.upper(RemoteProductBinding.condition_id) == condition_id.upper(),
        func.upper(RemoteProductBinding.finish_id) == finish_id.upper(),
    ).first()
    if existing:
        return "existing"
    conflict = session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
        RemoteProductBinding.product_type == "mtg_single",
        RemoteProductBinding.product_id == product_id,
    ).first()
    if conflict:
        return "conflict"
    single = (remote_item.get("product") or {}).get("single") or {}
    requested_identity = {
        "name": row.get("name") or single.get("name") or "",
        "set_code": single.get("set") or "",
        "collector_number": single.get("number") or "",
        "scryfall_id": single.get("scryfall_id") or "",
        "mtgjson_id": mtgjson_id,
        "language_id": language_id,
        "condition_id": condition_id,
        "finish_id": finish_id,
    }
    evidence = {
        "source": "inventory_reconciliation_service.apply_reconciliation_preview",
        "matched_via": "reconciliation_increase",
        "requested_identity": requested_identity,
        "product_type": "mtg_single",
        "product_id": product_id,
    }
    evidence_hash = hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    now = datetime.now(timezone.utc)
    session.add(RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product_id,
        local_card_ids_json=json.dumps(sorted(row.get("gap_card_ids") or [])),
        requested_identity_json=json.dumps(requested_identity, sort_keys=True),
        scryfall_id=requested_identity["scryfall_id"], mtgjson_id=mtgjson_id,
        language_id=language_id, condition_id=condition_id, finish_id=finish_id,
        set_code=requested_identity["set_code"], collector_number=requested_identity["collector_number"],
        binding_status="validated", validated_at=now,
        catalog_as_of=remote_item.get("effective_as_of"), evidence_hash=evidence_hash,
        evidence_json=json.dumps(evidence, sort_keys=True),
        remote_inventory_id=str(remote_item.get("id") or "") or None,
    ))
    return "created"


def apply_reconciliation_preview(
    session: Session,
    preview: dict,
    orders_loader,
    detail_loader,
    go_live_at: str,
    seller_loader,
    product_writer,
) -> dict:
    """Re-verify every eligible row immediately before writing, then write.

    Re-ingests Mana Pool orders first (same as the maintenance preview
    does), so any sale that reduced true local availability is reflected
    in the fresh desired-quantity recompute below -- this is what keeps
    the decrease direction correct, since that direction has no other
    signal for a Mana Pool-side sale on this identity.

    Increase rows: recompute the traced batch's still-available card
    count fresh, re-read Mana Pool's current quantity fresh, and write
    fresh_remote + fresh_traceable_units (clamped to fresh local desired
    quantity) -- never the stale reviewed absolute number.

    Decrease/zero_candidate rows: recompute local desired quantity fresh
    and write it directly.

    Each row is judged independently -- one row going stale does not
    block its siblings, matching the batch-isolation principle used for
    order-status sync and new-listing publishing.
    """
    eligible_rows = [row for row in preview.get("rows") or [] if row.get("status") == "eligible"]
    if not eligible_rows:
        raise InventoryReconciliationError("This preview has no eligible rows to reconcile.")

    response = orders_loader(since=go_live_at)
    ingest_manapool_orders(
        session, response.get("orders") or [], detail_loader,
        max_orders=order_service.ORDER_SYNC_MAX_ORDERS_PER_RUN,
    )
    session.flush()

    remote_inventory = seller_loader(min_quantity=0)
    remote_by_product = {
        str(item.get("product_id") or ""): item
        for item in remote_inventory if item.get("product_id")
    }

    updates = []
    excluded = []
    binding_outcomes = []
    for row in eligible_rows:
        product_id = row.get("product_id")
        remote_item = remote_by_product.get(product_id)
        if not remote_item:
            excluded.append({**row, "exclusion_reason": "Mana Pool no longer lists this product"})
            continue
        fresh_remote_quantity = int(remote_item.get("quantity") or 0)
        fresh_desired_quantity = _fresh_desired_quantity(session, row["canonical_identity"])

        if row["direction"] == "increase":
            fresh_gap_cards = [
                card for card in (
                    session.get(InventoryCard, card_id) for card_id in row.get("gap_card_ids") or []
                )
                if card is not None and card.status == "available"
            ]
            traceable_units = len(fresh_gap_cards)
            if traceable_units == 0:
                excluded.append({**row, "exclusion_reason": "Traced cards are no longer locally available"})
                continue
            write_quantity = min(fresh_remote_quantity + traceable_units, fresh_desired_quantity)
            if write_quantity <= fresh_remote_quantity:
                excluded.append({**row, "exclusion_reason": "Mana Pool quantity already reflects this increase"})
                continue
            outcome = _ensure_binding_for_increase(session, row, product_id, remote_item)
            if outcome:
                binding_outcomes.append({"product_id": product_id, "name": row.get("name"), "outcome": outcome})
        else:
            write_quantity = fresh_desired_quantity
            if write_quantity >= fresh_remote_quantity:
                excluded.append({**row, "exclusion_reason": "No longer a decrease as of this apply"})
                continue

        updates.append({
            "product_type": "mtg_single",
            "product_id": product_id,
            "price_cents": None,
            "quantity": write_quantity,
        })

    if not updates:
        raise InventoryReconciliationError(
            "None of the reviewed rows are still valid to reconcile -- local availability "
            "or Mana Pool's listed quantities changed since preview. Run a fresh preview."
        )

    session.flush()
    responses = product_writer(updates)

    return {
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "updates": updates,
        "responses": responses,
        "excluded": excluded,
        "binding_outcomes": binding_outcomes,
    }
