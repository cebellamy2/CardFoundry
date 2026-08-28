"""Read-only remote workflow for maintenance-mode inventory previews."""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from database import engine
from inventory_mirror_service import (
    build_inventory_mirror_preview,
    listing_status_updates_from_rows,
)
from inventory_sync_service import inventory_sync_lease
from manapool_service import (
    get_all_seller_inventory,
    get_seller_order,
    get_seller_orders,
)
from models import (
    AppSetting, Batch, InventoryCard, InventoryListingStatus, PickAllocation,
    RemoteProductBinding,
)
import order_service
from order_service import ingest_manapool_orders


GO_LIVE_SETTING_KEY = "manapool_go_live_at"


def _setting(session, key):
    row = session.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else None


def _build_mirror_preview_from_snapshot(
    session, cards, remote_inventory, fail_closed_on_unresolved,
):
    batches = {batch.id: batch for batch in session.query(Batch).all()}
    allocations = session.query(PickAllocation).all()
    mtgjson_override_product_ids = {}
    bound_card_ids = set()
    for binding in session.query(RemoteProductBinding).all():
        bound_card_ids.update(json.loads(binding.local_card_ids_json or "[]"))
        if binding.binding_status == "validated" and binding.mtgjson_override_confirmed_at:
            for card_id in json.loads(binding.local_card_ids_json or "[]"):
                mtgjson_override_product_ids[card_id] = binding.product_id
    # A card with no mtgjson_id and no RemoteProductBinding at all (e.g. a
    # printing correction landed it as genuinely new-to-Mana-Pool -- see
    # printing_correction_service.py's pending_first_listing resolution)
    # is eligible for build_inventory_mirror_preview's scryfall_id
    # fallback grouping. Any card with SOME binding, even a held/
    # unvalidated one, is excluded -- that means catalog data was found,
    # just not cleanly resolved, which deserves the existing manual-
    # review path, not an automatic scryfall_id publish.
    pending_first_listing_card_ids = {
        card.id for card in cards
        if card.status == "available"
        and not card.mtgjson_id
        and card.scryfall_id
        and card.id not in bound_card_ids
    }
    return build_inventory_mirror_preview(
        cards, batches, allocations, remote_inventory,
        fail_closed_on_unresolved=fail_closed_on_unresolved,
        mtgjson_override_product_ids=mtgjson_override_product_ids,
        pending_first_listing_card_ids=pending_first_listing_card_ids,
    )


def _persist_listing_status(session, preview):
    """Cache each card's listed/not_listed determination from this fresh
    reconciliation, for display on /inventory -- see
    InventoryListingStatus. Every caller here already holds a fresh
    remote inventory read under inventory_sync_lease, so this is exactly
    as current as the preview itself."""
    updates = listing_status_updates_from_rows(preview.get("rows") or [])
    if not updates:
        return
    existing = {
        row.inventory_card_id: row
        for row in session.query(InventoryListingStatus).filter(
            InventoryListingStatus.inventory_card_id.in_(updates.keys()),
        )
    }
    checked_at = datetime.now()
    for card_id, status in updates.items():
        row = existing.get(card_id)
        if row:
            row.listing_status = status
            row.checked_at = checked_at
        else:
            session.add(InventoryListingStatus(
                inventory_card_id=card_id, listing_status=status, checked_at=checked_at,
            ))


def create_inventory_sync_preview(
    orders_loader=get_seller_orders,
    detail_loader=get_seller_order,
    inventory_loader=get_all_seller_inventory,
    fail_closed_on_unresolved: bool = True,
):
    """Ingest orders and take local/remote snapshots under one lease."""
    with inventory_sync_lease(ttl_seconds=900):
        with Session(engine) as session:
            go_live_at = _setting(session, GO_LIVE_SETTING_KEY)
            if not go_live_at:
                raise ValueError("Mana Pool go-live timestamp is not configured.")
            response = orders_loader(since=go_live_at)
            ingestion = ingest_manapool_orders(
                session,
                response.get("orders") or [],
                detail_loader,
                max_orders=order_service.ORDER_SYNC_MAX_ORDERS_PER_RUN,
            )
            session.commit()

        remote_inventory = inventory_loader(min_quantity=0)
        with Session(engine) as session:
            cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
            preview = _build_mirror_preview_from_snapshot(
                session, cards, remote_inventory, fail_closed_on_unresolved,
            )
            _persist_listing_status(session, preview)
            session.commit()
            preview["order_ingestion"] = ingestion
            return preview


def create_exceptions_review_preview(
    inventory_loader=get_all_seller_inventory,
    fail_closed_on_unresolved: bool = False,
):
    """Local/remote snapshot across every batch -- no order sync, same
    reasoning as create_batch_scoped_mirror_preview, just unscoped. Used
    by the exceptions review page, which is meant to be safe and cheap to
    load often: one remote inventory read, everything else local."""
    with inventory_sync_lease(ttl_seconds=900):
        remote_inventory = inventory_loader(min_quantity=0)
        with Session(engine) as session:
            cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
            preview = _build_mirror_preview_from_snapshot(
                session, cards, remote_inventory, fail_closed_on_unresolved,
            )
            _persist_listing_status(session, preview)
            session.commit()
            preview["order_ingestion"] = None
            return preview


def create_batch_scoped_mirror_preview(
    batch_ids: list[int],
    inventory_loader=get_all_seller_inventory,
    fail_closed_on_unresolved: bool = False,
):
    """Local/remote snapshot for only the given batches -- no order sync.

    build_inventory_mirror_preview has no dependency on order data at all;
    order-sync is only ever bundled into create_inventory_sync_preview for
    a separate reason (keeping local order/fulfillment records fresh),
    unrelated to deciding what needs to be listed. Skipping it here, and
    scoping local cards to just the selected batches, keeps a "get this
    new batch live" run's total Mana Pool call count small and
    predictable instead of scanning (and re-pricing) the whole inventory
    -- live evidence: a full-inventory Perform Sync run tripped Mana
    Pool's rate limit even after every individual call was correctly
    paced and capped.
    """
    if not batch_ids:
        raise ValueError("At least one batch must be selected.")
    with inventory_sync_lease(ttl_seconds=900):
        remote_inventory = inventory_loader(min_quantity=0)
        with Session(engine) as session:
            cards = session.query(InventoryCard).filter(
                InventoryCard.batch_id.in_(batch_ids),
            ).order_by(InventoryCard.id).all()
            preview = _build_mirror_preview_from_snapshot(
                session, cards, remote_inventory, fail_closed_on_unresolved,
            )
            _persist_listing_status(session, preview)
            session.commit()
            preview["order_ingestion"] = None
            preview["scoped_batch_ids"] = sorted(batch_ids)
            return preview
