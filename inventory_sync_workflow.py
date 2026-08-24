"""Read-only remote workflow for maintenance-mode inventory previews."""

import json

from sqlalchemy.orm import Session

from database import engine
from inventory_mirror_service import build_inventory_mirror_preview
from inventory_sync_service import inventory_sync_lease
from manapool_service import (
    get_all_seller_inventory,
    get_seller_order,
    get_seller_orders,
)
from models import AppSetting, Batch, InventoryCard, PickAllocation, RemoteProductBinding
import order_service
from order_service import ingest_manapool_orders


GO_LIVE_SETTING_KEY = "manapool_go_live_at"


def _setting(session, key):
    row = session.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else None


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
            batches = {batch.id: batch for batch in session.query(Batch).all()}
            allocations = session.query(PickAllocation).all()
            mtgjson_override_product_ids = {}
            for binding in session.query(RemoteProductBinding).filter(
                RemoteProductBinding.binding_status == "validated",
                RemoteProductBinding.mtgjson_override_confirmed_at.isnot(None),
            ).all():
                for card_id in json.loads(binding.local_card_ids_json or "[]"):
                    mtgjson_override_product_ids[card_id] = binding.product_id
            preview = build_inventory_mirror_preview(
                cards, batches, allocations, remote_inventory,
                fail_closed_on_unresolved=fail_closed_on_unresolved,
                mtgjson_override_product_ids=mtgjson_override_product_ids,
            )
            preview["order_ingestion"] = ingestion
            return preview
