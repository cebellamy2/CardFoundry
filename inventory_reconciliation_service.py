"""Day-to-day quantity reconciliation between CardFoundry and Mana Pool.

Scoped to identities Mana Pool already lists -- the increase_quantity,
decrease_quantity, and zero_candidate categories from
inventory_mirror_service.build_inventory_mirror_preview(). A brand-new,
never-listed identity is new_listing_upload_service's job instead; this
module never creates a listing, only adjusts one that already exists.

increase_quantity is auto-applied *only* when the entire gap between
local and remote quantity is explained by recently-imported cards (see
_traceable_gap): every gap-explaining card must have been imported after
the remote listing's own effective_as_of -- regardless of which batch(es)
it came from, since real stock routinely arrives across several separate
imports over time and gating on a single batch left a growing, silently
-excluded backlog of genuine mismatches (confirmed live: 11 identities
stuck unreconciled for up to two weeks, each spanning 2-5 batches).
Cross-batch is safe on the same terms a single batch already was: the
write is computed as a delta (fresh remote quantity + fresh traceable
new units, clamped to fresh local desired quantity) rather than a blind
re-assertion of a stale absolute number, so a concurrent sale is always
reflected (Mana Pool decrements its own quantity immediately on a sale,
independent of whether CardFoundry has ingested that order yet) instead
of silently overwritten -- that guarantee comes entirely from the
per-card imported-after-effective_as_of check, not from batch
membership, which was only ever informational.

decrease_quantity/zero_candidate need no such gate: writing a
(possibly slightly stale) lower number is self-correcting, never an
oversell. It does rely on local state being fresh, though -- a Mana
Pool sale reducing true local availability only shows up here once
CardFoundry has ingested that order, which is why apply always
re-ingests orders first.
"""

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from inventory_mirror_service import SELLABLE_STATUS
from models import Batch, InventoryCard
import order_service
from order_service import ingest_manapool_orders


class InventoryReconciliationError(ValueError):
    pass


def _parse_effective_as_of(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _traceable_gap(session: Session, row: dict):
    """If row's increase is fully explained by recently-imported stock
    (each gap-explaining card imported after the remote listing was last
    confirmed), return gap_card_ids. Otherwise None -- stays excluded.

    Deliberately batch-agnostic: the safety property this needs -- never
    writing units Mana Pool couldn't already account for -- comes from
    each card's own imported_at postdating effective_as_of, not from all
    the cards sharing one batch_id. Real stock commonly arrives across
    several separate imports before Mana Pool's listing is next touched;
    requiring a single batch left those gaps permanently unreconciled.
    """
    gap = int(row.get("desired_quantity") or 0) - int(row.get("current_remote_quantity") or 0)
    if gap <= 0:
        return None
    card_ids = row.get("local_contributing_card_ids") or []
    cards = [
        card for card in (session.get(InventoryCard, card_id) for card_id in card_ids)
        if card is not None
    ]
    if len(cards) < gap:
        return None
    cards.sort(key=lambda card: card.imported_at, reverse=True)
    gap_cards = cards[:gap]
    effective_dt = _parse_effective_as_of(row.get("effective_as_of"))
    if not effective_dt:
        return None
    # imported_at is naive local time; .astimezone() on a naive datetime
    # correctly localizes it using the system's own timezone rules for
    # that date (DST-aware), no hardcoded offset needed.
    if not all(card.imported_at.astimezone(timezone.utc) > effective_dt for card in gap_cards):
        return None
    return [card.id for card in gap_cards]


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
            "product_id": row.get("remote_product_id"),
            "reviewed_desired_quantity": row.get("desired_quantity"),
            "reviewed_remote_quantity": row.get("current_remote_quantity"),
        }
        if category == "increase_quantity":
            gap_card_ids = _traceable_gap(session, row)
            if not gap_card_ids:
                excluded.append({
                    **base, "direction": "increase",
                    "reason": "Increase is not fully explained by recently-imported stock",
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

    responses = product_writer(updates)

    return {
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "updates": updates,
        "responses": responses,
        "excluded": excluded,
    }
