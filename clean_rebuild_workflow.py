"""Read-only orchestration for immutable store-off clean rebuild previews."""

from sqlalchemy.orm import Session

from clean_rebuild_service import (
    MAINTENANCE_EXECUTOR_ENABLED, build_clean_rebuild_preview,
    run_rebuild_steps, validate_rebuild_staleness,
)
from competitor_pricing_service import SELLER_EXCLUSION_ID
from database import engine
from inventory_sync_service import inventory_sync_lease
from inventory_sync_workflow import GO_LIVE_SETTING_KEY, _setting
from manapool_service import (
    get_all_seller_inventory, get_inventory_listings_by_ids,
    get_seller_order, get_seller_orders, optimize_exact_variant_batch_with_conflicts,
)
from models import Batch, InventoryCard, PickAllocation, RemoteProductBinding
from new_listing_pricing_service import price_initial_bindings
from order_service import ingest_manapool_orders


def create_clean_rebuild_preview(
    orders_loader=get_seller_orders, detail_loader=get_seller_order,
    inventory_loader=get_all_seller_inventory,
    optimizer_call=optimize_exact_variant_batch_with_conflicts,
    listings_call=get_inventory_listings_by_ids,
):
    with inventory_sync_lease(ttl_seconds=900):
        return _create_clean_rebuild_preview_locked(
            orders_loader, detail_loader, inventory_loader, optimizer_call, listings_call,
        )


def _create_clean_rebuild_preview_locked(
    orders_loader, detail_loader, inventory_loader, optimizer_call, listings_call,
):
    with Session(engine) as session:
        go_live_at = _setting(session, GO_LIVE_SETTING_KEY)
        if not go_live_at:
            raise ValueError("Mana Pool go-live timestamp is not configured")
        ingestion = ingest_manapool_orders(
            session, (orders_loader(since=go_live_at).get("orders") or []), detail_loader,
        )
        session.commit()
    remote = inventory_loader(min_quantity=0)
    with Session(engine) as session:
        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        batches = {row.id: row for row in session.query(Batch).all()}
        allocations = session.query(PickAllocation).all()
        bindings = session.query(RemoteProductBinding).filter(
            RemoteProductBinding.provider == "manapool",
            RemoteProductBinding.binding_status == "validated",
        ).order_by(RemoteProductBinding.id).all()
        pricing = price_initial_bindings(
            bindings, optimizer_call, listings_call, SELLER_EXCLUSION_ID,
            batch_size=20, undercut_cents=5, floor_cents=65,
        )
        preview = build_clean_rebuild_preview(cards, batches, allocations, remote, bindings, pricing)
        preview["order_ingestion"] = ingestion
        preview["initial_pricing_summary"] = pricing["summary"]
        return preview


def execute_clean_rebuild(reviewed, confirmation, writer):
    """Future store-off executor. Hard-disabled pending separate approval.

    When enabled, the lease covers order reconciliation, complete fresh snapshot
    validation, blank/write/readback, and republish/write/readback. Only
    authoritative seller inventory is used for reconciliation.
    """
    if not MAINTENANCE_EXECUTOR_ENABLED:
        raise RuntimeError("Maintenance rebuild executor is disabled")
    with inventory_sync_lease(ttl_seconds=3600):
        fresh = _create_clean_rebuild_preview_locked(
            get_seller_orders, get_seller_order, get_all_seller_inventory,
            optimize_exact_variant_batch_with_conflicts, get_inventory_listings_by_ids,
        )
        validate_rebuild_staleness(reviewed, fresh, confirmation)
        if not fresh["summary"]["ready"]:
            raise RuntimeError("Fresh rebuild plan is not READY")
        return run_rebuild_steps(fresh, writer, get_all_seller_inventory)
