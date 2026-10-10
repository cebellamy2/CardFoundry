"""Read-only analysis of what deleting a batch would touch.

Slice 1 of Delete batch. There is deliberately NO delete path here: this
module only looks, and the /admin screen it feeds only reports. The point
of shipping it first is that everything after it becomes checkable --
slice 2's apply has to agree with these numbers.

★ WHY FOREIGN KEYS CANNOT BE RELIED ON. `PRAGMA foreign_keys` reads 0 on
the live app connection (database.py turns it off), so a DELETE neither
cascades nor blocks nor warns -- it silently orphans every referencing
row. Every table that points at a batch or its cards therefore has to be
enumerated by hand, which is what REFERENCING_TABLES is for. The schema
will not help, and it will not complain.

★ WHY THE POST-DELETE QUANTITY IS MEASURED, NOT CALCULATED. Mana Pool
counts a listing's quantity by four-key IDENTITY, not by batch
membership, and 271 available identities in production are shared across
more than one batch. So "remove the batch's cards from Mana Pool" can
never mean "zero the listing" -- that would delist copies living in other
batches. The right number is what _desired_quantity_for_binding returns
once the cards are gone, so this module gets it by deleting them inside a
SAVEPOINT it then ROLLS BACK, and calling that real function. No
subtraction arithmetic of our own, and the preview is therefore
guaranteed to equal what the apply will compute.

★ WHY THE LIVE QUANTITY IS READ IN BULK. inventory_listing_status is a
cache written only by mirror reconciliation and goes stale, so it must
not be the source here. Reading GET /seller/inventory/product/... per
binding would be one call per identity -- 556 calls for the largest
batch. get_all_seller_inventory() walks the seller's whole inventory with
cursor pagination in a fixed handful of calls instead, whatever the batch
size, and it is just as live.
"""

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from inventory_mirror_service import SELLABLE_STATUS
from manapool_quantity_push_service import (
    _desired_quantity_for_binding, bindings_backing_card,
)
from models import (
    Batch, FulfillmentException, ImportRecord, InventoryCard,
    InventoryChangeLog, InventoryListingStatus, InventoryPriceHistory,
    OrderItem, PendingImport, PickAllocation, SalesOrder, ScanCaptureJob,
    ScanIntakeProvenance,
)

logger = logging.getLogger("cardfoundry")

# Statuses that need a per-card decision (delete / archive / move) rather
# than going quietly with the batch. They carry history worth keeping:
# a sold card is a real sale, and a removed/unsellable one records a
# deliberate judgement someone already made.
DECISION_STATUSES = ("sold", "removed", "unsellable")
# "Unshipped" for this screen. `packed` is listed separately because it is
# unshipped and yet CANNOT be resolved: mark_fulfillment_exception accepts
# allocated/picked only, so a packed card has no way out but shipping or
# unpacking the wave.
RESOLVABLE_ALLOCATION_STATUSES = ("allocated", "picked")
PACKED_ALLOCATION_STATUS = "packed"
CONSIGNMENT_CODE_PREFIX = "CON_"


@dataclass
class Refusal:
    code: str
    summary: str
    detail: str = ""
    orders: list = field(default_factory=list)


def _card_ids(session: Session, batch_id: int) -> list[int]:
    return [
        row[0] for row in session.query(InventoryCard.id)
        .filter(InventoryCard.batch_id == batch_id).all()
    ]


def refusals(session: Session, batch: Batch) -> list[Refusal]:
    """Every reason this batch must not be deleted. Empty means allowed.

    Checked against the batch as it is now, so the screen can be reloaded
    after the operator fixes something.
    """
    found = []
    card_ids = _card_ids(session, batch.id)

    if batch.is_consignment:
        found.append(Refusal(
            code="consigned",
            summary="This is a consignment batch and cannot be deleted here.",
            detail=(
                "Consigned stock is not ours to destroy. Settle or move it "
                "first, or delete the cards individually through the normal "
                "inventory screens."
            ),
        ))
    elif str(batch.batch_code or "").upper().startswith(CONSIGNMENT_CODE_PREFIX):
        # ★ FOUND IN PRODUCTION 2026-10-10. CON_RAU is named like every
        # other consignor batch but carries is_consignment=0 and no
        # consignor_id, so the flag-based refusal above would have let it
        # through -- and two of its cards have already sold. A batch whose
        # name says consignment and whose flag says otherwise is a data
        # inconsistency, not a green light.
        found.append(Refusal(
            code="consignment_code_without_flag",
            summary=(
                f"{batch.batch_code} is named like a consignment batch but is "
                "not flagged as one."
            ),
            detail=(
                "Its name says it holds someone else's cards while its "
                "consignment flag says it does not. Settle which is true "
                "before deleting anything: either flag the batch and assign "
                "its consignor, or rename it."
            ),
        ))

    if card_ids:
        tracked = (
            session.query(func.count(InventoryCard.id))
            .filter(
                InventoryCard.id.in_(card_ids),
                (InventoryCard.consignment_payout_id.isnot(None))
                | (InventoryCard.consignment_amount_owed.isnot(None)),
            ).scalar() or 0
        )
        if tracked:
            # A payout's line items ARE its cards (consignment_service
            # derives them by query), so deleting them would leave a payout
            # with a total and no composition.
            found.append(Refusal(
                code="consignment_tracked_cards",
                summary=f"{tracked} card(s) here are tracked against a consignor payout.",
                detail=(
                    "A payout's line items are the cards themselves, so "
                    "deleting them would leave a payout with a total and "
                    "nothing to explain it."
                ),
            ))

        open_exceptions = (
            session.query(FulfillmentException)
            .filter(
                FulfillmentException.inventory_card_id.in_(card_ids),
                FulfillmentException.inventory_resolution_state == "unresolved",
            ).all()
        )
        if open_exceptions:
            found.append(Refusal(
                code="open_fulfillment_exception",
                summary=f"{len(open_exceptions)} unresolved fulfillment exception(s) involve these cards.",
                detail=(
                    "Close them first. The resolvers read the card's own "
                    "removal reason, so they cannot run once the card is gone."
                ),
                orders=sorted({e.sales_order_id for e in open_exceptions if e.sales_order_id}),
            ))

        packed = (
            session.query(PickAllocation, SalesOrder)
            .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
            .join(SalesOrder, SalesOrder.id == OrderItem.order_id)
            .filter(
                PickAllocation.inventory_card_id.in_(card_ids),
                PickAllocation.status == PACKED_ALLOCATION_STATUS,
            ).all()
        )
        if packed:
            labels = sorted({
                order.external_label or order.external_order_id or str(order.id)
                for _allocation, order in packed
            })
            found.append(Refusal(
                code="packed_allocation",
                summary=f"{len(packed)} card(s) are already packed for a shipment.",
                detail=(
                    "A packed card cannot be substituted or marked missing -- "
                    "the order is already assembled. Ship those orders, or "
                    "undo the wave's packing, then come back. Orders: "
                    + ", ".join(labels)
                ),
                orders=labels,
            ))
    return found


def _live_quantities(live_inventory) -> dict:
    """product_id -> live quantity, from the seller's real listings."""
    quantities = {}
    for item in live_inventory or []:
        product_id = str((item or {}).get("product_id") or "")
        if product_id:
            quantities[product_id] = item.get("quantity")
    return quantities


def _binding_rows(session: Session, cards, live_inventory) -> list[dict]:
    """Every binding these cards back, with live vs post-delete quantity.

    The post-delete number comes from the real counting function with the
    cards actually removed inside a rolled-back SAVEPOINT -- see the module
    docstring. Nothing is committed.
    """
    bindings = {}
    for card in cards:
        for binding in bindings_backing_card(session, card):
            bindings[binding.id] = binding
    if not bindings:
        return []

    live = _live_quantities(live_inventory)
    before = {bid: _desired_quantity_for_binding(session, b) for bid, b in bindings.items()}

    savepoint = session.begin_nested()
    try:
        for card in cards:
            session.delete(card)
        session.flush()
        after = {bid: _desired_quantity_for_binding(session, b) for bid, b in bindings.items()}
    finally:
        savepoint.rollback()

    rows = []
    for bid, binding in bindings.items():
        rows.append({
            "binding_id": bid,
            "product_id": binding.product_id,
            "name": _binding_display_name(binding),
            "set_code": binding.set_code,
            "collector_number": binding.collector_number,
            "condition_id": binding.condition_id,
            "finish_id": binding.finish_id,
            "language_id": binding.language_id,
            "live_quantity": live.get(str(binding.product_id)),
            "quantity_before": before.get(bid),
            "quantity_after": after.get(bid),
        })
    rows.sort(key=lambda r: (r["set_code"] or "", r["collector_number"] or ""))
    return rows


def _binding_display_name(binding) -> str:
    """The card name for a binding row.

    RemoteProductBinding stores no name column of its own -- the name
    lives inside requested_identity_json -- so read it from there and fall
    back to the identity's set/number, which is always populated.
    """
    try:
        identity = json.loads(binding.requested_identity_json or "{}")
    except (TypeError, ValueError):
        identity = {}
    name = str(identity.get("name") or "").strip()
    if name:
        return name
    return f"{binding.set_code} {binding.collector_number}".strip()


def _shared_identity_rows(session: Session, batch_id: int) -> list[dict]:
    """Identities in this batch that other batches also stock.

    These are the reason the delete recomputes rather than zeroing: the
    listing must keep serving the copies that survive elsewhere.
    """
    inner = (
        session.query(
            func.upper(InventoryCard.mtgjson_id).label("m"),
            func.upper(InventoryCard.language_id).label("l"),
            func.upper(InventoryCard.condition_id).label("c"),
            func.upper(InventoryCard.finish_id).label("f"),
        )
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.batch_id == batch_id,
            InventoryCard.status == SELLABLE_STATUS,
            InventoryCard.mtgjson_id.isnot(None),
        ).distinct().all()
    )
    rows = []
    for m, l, c, f in inner:
        elsewhere = (
            session.query(Batch.batch_code, func.count(InventoryCard.id))
            .join(InventoryCard, InventoryCard.batch_id == Batch.id)
            .filter(
                InventoryCard.batch_id != batch_id,
                InventoryCard.status == SELLABLE_STATUS,
                Batch.is_archived == False,  # noqa: E712 -- SQLAlchemy needs ==
                func.upper(InventoryCard.mtgjson_id) == m,
                func.upper(InventoryCard.language_id) == l,
                func.upper(InventoryCard.condition_id) == c,
                func.upper(InventoryCard.finish_id) == f,
            ).group_by(Batch.id).all()
        )
        if not elsewhere:
            continue
        sample = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.batch_id == batch_id,
                func.upper(InventoryCard.mtgjson_id) == m,
            ).first()
        )
        rows.append({
            "name": sample.name if sample else "(unknown)",
            "condition_id": c, "finish_id": f, "language_id": l,
            "elsewhere": [{"batch_code": code, "count": n} for code, n in elsewhere],
            "elsewhere_total": sum(n for _code, n in elsewhere),
        })
    rows.sort(key=lambda r: -r["elsewhere_total"])
    return rows


def _cross_batch_import_rows(session: Session, batch_id: int) -> list[dict]:
    """Import records belonging to this batch that cards elsewhere still cite.

    ★ MEASURED: 175 cards in production sit in a different batch from
    their import record, because a bulk move changes batch_id and leaves
    import_id alone. Deleting those records with the batch would orphan
    cards that survive. import_id is already nullable (814 cards have it
    null), so the survivors can simply be detached.
    """
    rows = []
    for record in session.query(ImportRecord).filter(ImportRecord.batch_id == batch_id).all():
        outside = (
            session.query(Batch.batch_code, func.count(InventoryCard.id))
            .join(InventoryCard, InventoryCard.batch_id == Batch.id)
            .filter(
                InventoryCard.import_id == record.id,
                InventoryCard.batch_id != batch_id,
            ).group_by(Batch.id).all()
        )
        if outside:
            rows.append({
                "import_id": record.id,
                "filename": record.filename,
                "survivors": [{"batch_code": code, "count": n} for code, n in outside],
                "survivor_total": sum(n for _code, n in outside),
            })
    return rows


def _money(session: Session, batch_id: int) -> dict:
    sold_value = (
        session.query(func.coalesce(func.sum(InventoryCard.sold_price), 0.0))
        .filter(InventoryCard.batch_id == batch_id, InventoryCard.status == "sold")
        .scalar() or 0.0
    )
    available_value = (
        session.query(func.coalesce(func.sum(InventoryCard.current_price), 0.0))
        .filter(InventoryCard.batch_id == batch_id, InventoryCard.status == SELLABLE_STATUS)
        .scalar() or 0.0
    )
    unpriced = (
        session.query(func.count(InventoryCard.id))
        .filter(
            InventoryCard.batch_id == batch_id,
            InventoryCard.status == SELLABLE_STATUS,
            InventoryCard.current_price.is_(None),
        ).scalar() or 0
    )
    return {
        "sold_history": round(float(sold_value), 2),
        "available_stock": round(float(available_value), 2),
        "available_unpriced_cards": unpriced,
    }


# Every table that points at a batch or its cards. Enumerated by hand
# because foreign keys are off -- see the module docstring. The `keep`
# flag records the operator's decision (2026-10-10) that change logs
# survive a delete: they are append-only text, and they are the only
# record that money changed hands.
REFERENCING_TABLES = (
    ("inventory_cards", "card", False),
    ("pick_allocations", "card", False),
    ("inventory_price_history", "card", False),
    ("inventory_listing_status", "card", False),
    ("scan_intake_provenance", "card", False),
    ("fulfillment_exceptions", "card", False),
    ("inventory_change_logs", "card", True),
    ("import_records", "batch", False),
    ("scan_capture_jobs", "batch", False),
    ("pending_imports", "batch", False),
)

_CARD_MODELS = {
    "inventory_cards": (InventoryCard, InventoryCard.id),
    "pick_allocations": (PickAllocation, PickAllocation.inventory_card_id),
    "inventory_price_history": (InventoryPriceHistory, InventoryPriceHistory.inventory_card_id),
    "inventory_listing_status": (InventoryListingStatus, InventoryListingStatus.inventory_card_id),
    "scan_intake_provenance": (ScanIntakeProvenance, ScanIntakeProvenance.inventory_card_id),
    "fulfillment_exceptions": (FulfillmentException, FulfillmentException.inventory_card_id),
    "inventory_change_logs": (InventoryChangeLog, InventoryChangeLog.inventory_card_id),
}
_BATCH_MODELS = {
    "import_records": (ImportRecord, ImportRecord.batch_id),
    "scan_capture_jobs": (ScanCaptureJob, ScanCaptureJob.target_batch_id),
    "pending_imports": (PendingImport, PendingImport.batch_id),
}


def _table_rows(session: Session, batch_id: int, card_ids: list[int]) -> list[dict]:
    rows = []
    for table, scope, keep in REFERENCING_TABLES:
        if scope == "card":
            model, column = _CARD_MODELS[table]
            count = 0
            if card_ids:
                count = (
                    session.query(func.count()).select_from(model)
                    .filter(column.in_(card_ids)).scalar() or 0
                )
        else:
            model, column = _BATCH_MODELS[table]
            count = (
                session.query(func.count()).select_from(model)
                .filter(column == batch_id).scalar() or 0
            )
        rows.append({"table": table, "scope": scope, "rows": count, "kept": keep})
    return rows


def build_delete_preview(
    session: Session, batch_id: int, *, live_inventory_reader=None,
) -> dict:
    """The whole read-only report. Writes nothing, pushes nothing."""
    batch = session.get(Batch, batch_id)
    if not batch:
        return {"found": False, "batch_id": batch_id}

    cards = (
        session.query(InventoryCard)
        .filter(InventoryCard.batch_id == batch_id)
        .order_by(InventoryCard.id).all()
    )
    card_ids = [card.id for card in cards]
    blocking = refusals(session, batch)

    status_counts = dict(
        session.query(InventoryCard.status, func.count(InventoryCard.id))
        .filter(InventoryCard.batch_id == batch_id)
        .group_by(InventoryCard.status).all()
    )

    decisions = [
        {"card_id": c.id, "name": c.name, "set_code": c.set_code,
         "collector_number": c.collector_number, "status": c.status,
         "sold_price": c.sold_price, "removal_reason": c.removal_reason,
         "unsellable_reason": c.unsellable_reason}
        for c in cards if c.status in DECISION_STATUSES
    ]

    unshipped = [
        {"card_id": allocation.inventory_card_id, "name": card.name,
         "allocation_status": allocation.status,
         "order": order.external_label or order.external_order_id or str(order.id),
         "order_id": order.id}
        for allocation, card, order in (
            session.query(PickAllocation, InventoryCard, SalesOrder)
            .join(InventoryCard, InventoryCard.id == PickAllocation.inventory_card_id)
            .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
            .join(SalesOrder, SalesOrder.id == OrderItem.order_id)
            .filter(
                PickAllocation.inventory_card_id.in_(card_ids or [-1]),
                PickAllocation.status.in_(
                    RESOLVABLE_ALLOCATION_STATUSES + (PACKED_ALLOCATION_STATUS,)),
            ).all()
        )
    ]

    # The live read is the only network call this report makes, and it is a
    # GET. Skipped when the batch has no cards, or when a refusal already
    # means nothing can proceed -- no point paying for it.
    binding_rows = []
    live_read_skipped = None
    if not cards:
        live_read_skipped = "the batch has no cards"
    else:
        reader = live_inventory_reader
        if reader is None:
            from manapool_service import get_all_seller_inventory

            def reader():
                # min_quantity=0 so a listing already sitting at zero is
                # still reported rather than looking like "not listed".
                return get_all_seller_inventory(min_quantity=0)
        try:
            binding_rows = _binding_rows(session, cards, reader())
        except Exception as exc:  # noqa: BLE001 -- a read failure must not blank the report
            live_read_skipped = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "batch delete preview: live Mana Pool read failed for batch %s "
                "(%s); the report is shown without live quantities: %s: %s",
                batch_id, batch.batch_code, type(exc).__name__, exc,
            )

    report = {
        "found": True,
        "batch_id": batch.id,
        "batch_code": batch.batch_code,
        "is_archived": bool(batch.is_archived),
        "is_consignment": bool(batch.is_consignment),
        "card_total": len(cards),
        "status_counts": status_counts,
        "refusals": blocking,
        "deletable": not blocking,
        "tables": _table_rows(session, batch.id, card_ids),
        "bindings": binding_rows,
        "live_read_skipped": live_read_skipped,
        "shared_identities": _shared_identity_rows(session, batch.id),
        "cross_batch_imports": _cross_batch_import_rows(session, batch.id),
        "decisions": decisions,
        "unshipped": unshipped,
        "money": _money(session, batch.id),
    }
    logger.info(
        "batch delete preview: batch %s (%s) cards=%s refusals=%s bindings=%s "
        "shared_identities=%s decisions=%s unshipped=%s",
        batch.id, batch.batch_code, len(cards), [r.code for r in blocking],
        len(binding_rows), len(report["shared_identities"]),
        len(decisions), len(unshipped),
    )
    return report
