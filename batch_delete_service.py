"""Delete a batch and its cards, for real.

Slice 2 of Delete batch. Scope is deliberately narrow: EVERY card in the
batch must be `available`. Anything else is refused for now -- sold cards
need the per-card archive decision (slice 3) and allocated ones need order
resolution (slice 4).

★ THE DRY RUN IS THIS FUNCTION. delete_batch(apply=False) runs the whole
thing -- refusals, deletes, flush, recompute, push, read-back -- and rolls
back instead of committing, with the push and read-back stubbed. It is the
only way to reach the confirm screen, so the numbers an operator approves
are computed by the code that will act. Same pattern as v2.13.0's
dry-run guard and the leg_land batch move.

★ WHY IT NEVER ZEROES A LISTING. Mana Pool counts a listing by four-key
IDENTITY, not by batch, and 271 available identities in production are
stocked by more than one batch. Zeroing on delete would delist copies that
survive elsewhere. So the push sends what _desired_quantity_for_binding
returns once the cards are gone -- often non-zero. There is no subtraction
arithmetic here and no blanket zero anywhere in this module.

★ WHY NOTHING LOCAL IS DELETED FOR A CARD STILL LIVE ON MANA POOL. The
local deletes happen first but are only FLUSHED, never committed, so the
recompute sees the post-delete state. The commit happens last, and only
if the push AND the read-back both succeeded. Any failure rolls the whole
thing back, so a listing left standing always has its cards still here.

★ FOREIGN KEYS ARE OFF on this database, so nothing cascades, nothing
blocks and nothing warns. Every referencing row is deleted explicitly, in
dependency order, and the three places that point at a deleted row
without owning it are rewritten rather than orphaned:
  - a SURVIVING card's import_id, when its import record belongs to this
    batch (175 cards in production sit in a different batch from their
    import, because a bulk move changes batch_id and leaves import_id)
  - a SURVIVING card's removal_related_inventory_card_id
  - every affected binding's local_card_ids_json
"""

import json
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from batch_delete_preview import (
    DECISION_STATUSES, PACKED_ALLOCATION_STATUS, Refusal,
    RESOLVABLE_ALLOCATION_STATUSES, build_delete_preview, refusals,
)
from inventory_mirror_service import SELLABLE_STATUS
from manapool_quantity_push_service import (
    QuantityPushFailed, push_bindings_strict, verify_pushed_quantities,
)
from models import (
    Batch, FulfillmentException, ImportRecord, InventoryCard,
    InventoryChangeLog, InventoryListingStatus, InventoryPriceHistory,
    OrderItem, PendingImport, PickAllocation, RemoteProductBinding,
    SalesOrder, ScanCaptureJob, ScanIntakeProvenance,
)

logger = logging.getLogger("cardfoundry")

SLICE_2_REFUSAL = "not_all_available"
RELEASED_ALLOCATION_REFUSAL = "released_allocation"
# ★ WHY A RELEASED ALLOCATION BLOCKS A DELETE (operator decision,
# 2026-10-10). Allocation rows are never deleted by the app: a release
# keeps the row as "released" and records where it came from in
# released_from_status, which is the ONLY thing uncancel_order has to
# restore a cancelled order from -- it reclaims exactly
# `PickAllocation.status == "released"` and puts each card back at its own
# pre-release status. Deleting those rows would quietly remove the
# possibility of un-cancelling that order, forever, with nothing to say so.
# So the batch is refused and the rows are left alone.
#
# MEASURED on production 2026-10-10: `released` is the ONLY allocation
# status that ever sits on an `available` card (2 rows), so this refusal
# covers every real case and nothing narrower would.
RESTORABLE_ALLOCATION_STATUS = "released"


class BatchDeleteRefused(RuntimeError):
    """Refused before anything was touched. Nothing is written."""

    def __init__(self, message: str, refusals: list):
        super().__init__(message)
        self.refusals = refusals


class BatchDeleteFailed(RuntimeError):
    """Failed partway and was rolled back completely."""

    def __init__(self, message: str, *, failed_bindings=None):
        super().__init__(message)
        self.failed_bindings = failed_bindings or []


def slice_2_refusals(session: Session, batch: Batch) -> list:
    """Slice 1's refusals, plus slice 2's own all-available limit."""
    found = list(refusals(session, batch))

    released = (
        session.query(PickAllocation, SalesOrder)
        .join(InventoryCard, InventoryCard.id == PickAllocation.inventory_card_id)
        .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
        .join(SalesOrder, SalesOrder.id == OrderItem.order_id)
        .filter(
            InventoryCard.batch_id == batch.id,
            PickAllocation.status == RESTORABLE_ALLOCATION_STATUS,
        ).all()
    )
    if released:
        labels = sorted({
            order.external_label or order.external_order_id or str(order.id)
            for _allocation, order in released
        })
        found.append(Refusal(
            code=RELEASED_ALLOCATION_REFUSAL,
            summary=(
                f"{len(released)} card(s) here still carry a released "
                "allocation from a cancelled order."
            ),
            detail=(
                "Those rows are the only record un-cancelling those orders "
                "could restore from, and deleting them would remove that "
                "possibility for good. They are left untouched. Orders: "
                + ", ".join(labels)
            ),
            orders=labels,
        ))

    rows = (
        session.query(InventoryCard.status, func.count(InventoryCard.id))
        .filter(InventoryCard.batch_id == batch.id)
        .group_by(InventoryCard.status).all()
    )
    other = {status: count for status, count in rows if status != SELLABLE_STATUS}
    if other:
        described = ", ".join(f"{count} {status}" for status, count in sorted(other.items()))
        needs_decision = sorted(s for s in other if s in DECISION_STATUSES)
        found.append(Refusal(
            code=SLICE_2_REFUSAL,
            summary=(
                "Only a batch whose every card is available can be deleted yet "
                f"-- this one also holds {described}."
            ),
            detail=(
                "Cards that have sold, been removed or been marked not-for-sale "
                "need a per-card decision, and cards allocated to an open order "
                "need that order resolved first. Those steps are not built yet."
                + (f" Statuses needing a decision: {', '.join(needs_decision)}."
                   if needs_decision else "")
            ),
        ))
    return found


def _affected_bindings(session: Session, cards: list) -> tuple[list, dict]:
    """Every binding these cards back, and the card ids each one holds.

    ★ BUILT IN ONE PASS. bindings_backing_card scans all bindings and
    JSON-parses local_card_ids_json for EVERY card it is handed -- 8,362
    bindings in production, so a 900-card batch would mean millions of
    parses. This walks the bindings once instead, indexing membership as
    it goes, and resolves identity matches with one query per card.
    """
    from manapool_quantity_push_service import _resolve_binding_for_card

    card_ids = {card.id for card in cards}
    found, membership = {}, {}
    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
    ).all():
        try:
            held = json.loads(binding.local_card_ids_json or "[]")
        except (TypeError, ValueError):
            logger.warning(
                "batch delete: binding %s has unreadable local_card_ids_json; "
                "treating it as holding no cards.", binding.id,
            )
            held = []
        membership[binding.id] = held
        if card_ids.intersection(held):
            found[binding.id] = binding

    for card in cards:
        binding = _resolve_binding_for_card(session, card)
        if binding is not None:
            found[binding.id] = binding
            membership.setdefault(binding.id, [])
    return list(found.values()), membership


def _delete_local_rows(session: Session, batch: Batch, cards: list, membership: dict) -> dict:
    """Every local write, in dependency order. Flushes; never commits."""
    card_ids = [card.id for card in cards]
    counts = {}

    if card_ids:
        # Exceptions reference allocations, so they go first.
        counts["fulfillment_exceptions"] = (
            session.query(FulfillmentException)
            .filter(FulfillmentException.inventory_card_id.in_(card_ids))
            .delete(synchronize_session=False)
        )
        counts["pick_allocations"] = (
            session.query(PickAllocation)
            .filter(PickAllocation.inventory_card_id.in_(card_ids))
            .delete(synchronize_session=False)
        )
        counts["inventory_price_history"] = (
            session.query(InventoryPriceHistory)
            .filter(InventoryPriceHistory.inventory_card_id.in_(card_ids))
            .delete(synchronize_session=False)
        )
        counts["inventory_listing_status"] = (
            session.query(InventoryListingStatus)
            .filter(InventoryListingStatus.inventory_card_id.in_(card_ids))
            .delete(synchronize_session=False)
        )
        counts["scan_intake_provenance"] = (
            session.query(ScanIntakeProvenance)
            .filter(ScanIntakeProvenance.inventory_card_id.in_(card_ids))
            .delete(synchronize_session=False)
        )
        # inventory_change_logs are KEPT by operator decision: they are
        # append-only and the only record that money changed hands. They
        # will reference a card id that no longer resolves.
        counts["inventory_change_logs_kept"] = (
            session.query(func.count()).select_from(InventoryChangeLog)
            .filter(InventoryChangeLog.inventory_card_id.in_(card_ids))
            .scalar() or 0
        )

        # A surviving card pointing at one of these must not be left
        # dangling. Nullable by design, so detach rather than delete.
        counts["removal_related_cleared"] = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.removal_related_inventory_card_id.in_(card_ids),
                InventoryCard.batch_id != batch.id,
            ).update({"removal_related_inventory_card_id": None},
                     synchronize_session=False)
        )

    # Import records: delete only those no surviving card still cites.
    kept_imports, deletable_imports = [], []
    for record in session.query(ImportRecord).filter(ImportRecord.batch_id == batch.id).all():
        outside = (
            session.query(func.count(InventoryCard.id))
            .filter(
                InventoryCard.import_id == record.id,
                InventoryCard.batch_id != batch.id,
            ).scalar() or 0
        )
        if outside:
            kept_imports.append({"import_id": record.id, "survivors": outside})
        else:
            deletable_imports.append(record.id)
    counts["import_records_kept_for_survivors"] = len(kept_imports)
    if kept_imports:
        logger.info(
            "batch delete: keeping %s import record(s) of batch %s still cited by "
            "cards elsewhere: %s", len(kept_imports), batch.batch_code, kept_imports,
        )
    # Detach this batch's own cards from the records that survive, so a
    # kept record is not left pointing at rows about to vanish.
    if card_ids:
        session.query(InventoryCard).filter(
            InventoryCard.id.in_(card_ids),
        ).update({"import_id": None}, synchronize_session=False)

    counts["scan_capture_jobs_detached"] = (
        session.query(ScanCaptureJob)
        .filter(ScanCaptureJob.target_batch_id == batch.id)
        .update({"target_batch_id": None}, synchronize_session=False)
    )
    counts["pending_imports"] = (
        session.query(PendingImport)
        .filter(PendingImport.batch_id == batch.id)
        .delete(synchronize_session=False)
    )

    if card_ids:
        counts["inventory_cards"] = (
            session.query(InventoryCard)
            .filter(InventoryCard.id.in_(card_ids))
            .delete(synchronize_session=False)
        )
    counts["import_records"] = 0
    if deletable_imports:
        counts["import_records"] = (
            session.query(ImportRecord)
            .filter(ImportRecord.id.in_(deletable_imports))
            .delete(synchronize_session=False)
        )

    # Bindings keep a JSON list of card ids with no foreign key at all, so
    # a deleted card stays "held" forever unless this rewrites it.
    rewritten = 0
    doomed = set(card_ids)
    for binding_id, held in membership.items():
        remaining = [cid for cid in held if cid not in doomed]
        if len(remaining) != len(held):
            binding = session.get(RemoteProductBinding, binding_id)
            if binding is not None:
                binding.local_card_ids_json = json.dumps(remaining)
                rewritten += 1
    counts["bindings_membership_rewritten"] = rewritten

    counts["batches"] = (
        session.query(Batch).filter(Batch.id == batch.id)
        .delete(synchronize_session=False)
    )
    session.flush()
    return counts


def delete_batch(
    session: Session, batch_id: int, *, apply: bool,
    pusher=None, reader=None, live_inventory_reader=None,
) -> dict:
    """Delete a batch, or dry-run the exact same path.

    apply=False rolls everything back and expects `pusher`/`reader` to be
    stubs, so the dry run costs no Mana Pool write. apply=True commits,
    but ONLY after the push and the read-back have both succeeded.

    Raises BatchDeleteRefused before touching anything, or
    BatchDeleteFailed after a full rollback.
    """
    batch = session.get(Batch, batch_id)
    if not batch:
        raise BatchDeleteRefused(f"No batch with id {batch_id}.", [])

    blocking = slice_2_refusals(session, batch)
    if blocking:
        logger.info(
            "batch delete refused for batch %s (%s): %s",
            batch.id, batch.batch_code, [r.code for r in blocking],
        )
        raise BatchDeleteRefused(
            f"Batch {batch.batch_code} cannot be deleted: "
            + "; ".join(r.summary for r in blocking),
            blocking,
        )

    preview = build_delete_preview(
        session, batch_id, live_inventory_reader=live_inventory_reader,
    )
    cards = (
        session.query(InventoryCard)
        .filter(InventoryCard.batch_id == batch_id)
        .order_by(InventoryCard.id).all()
    )
    bindings, membership = _affected_bindings(session, cards)
    batch_code = batch.batch_code

    savepoint = session.begin_nested()
    try:
        counts = _delete_local_rows(session, batch, cards, membership)
        # Quantities are recomputed AFTER the flush, so the cards are
        # already gone from the count. Never a blanket zero.
        quantities = push_bindings_strict(session, bindings, pusher=pusher)
        observed = verify_pushed_quantities(quantities, bindings, reader=reader)
    except QuantityPushFailed as exc:
        savepoint.rollback()
        failed = [
            {"binding_id": b.id, "product_id": b.product_id} for b in bindings
        ]
        logger.warning(
            "batch delete ROLLED BACK for batch %s (%s): %s. Nothing local was "
            "deleted, so no listing is left standing without its cards.",
            batch_id, batch_code, exc,
        )
        raise BatchDeleteFailed(str(exc), failed_bindings=failed) from exc
    except Exception as exc:  # noqa: BLE001 -- a rollback must cover everything
        savepoint.rollback()
        logger.warning(
            "batch delete ROLLED BACK for batch %s (%s) by an unexpected error: "
            "%s: %s. Nothing was written.",
            batch_id, batch_code, type(exc).__name__, exc,
        )
        raise

    result = {
        "batch_id": batch_id,
        "batch_code": batch_code,
        "applied": bool(apply),
        "cards_deleted": len(cards),
        "rows": counts,
        "bindings": [
            {
                "binding_id": b.id, "product_id": b.product_id,
                "quantity_written": quantities.get(b.id),
                "quantity_read_back": observed.get(b.id),
            }
            for b in bindings
        ],
        "money": preview["money"],
        "shared_identities": len(preview["shared_identities"]),
        "mana_pool_payload": [
            {
                "product_type": "mtg_single",
                "product_id": b.product_id,
                "price_cents": None,
                "quantity": quantities.get(b.id),
            }
            for b in bindings
        ],
    }

    if apply:
        savepoint.commit()
        logger.info(
            "batch delete APPLIED: batch %s (%s), %s card(s), %s listing(s) "
            "requantified, rows=%s",
            batch_id, batch_code, len(cards), len(bindings), counts,
        )
    else:
        savepoint.rollback()
        logger.info(
            "batch delete DRY RUN: batch %s (%s), %s card(s), %s listing(s) "
            "would be requantified, rows=%s -- rolled back, nothing written.",
            batch_id, batch_code, len(cards), len(bindings), counts,
        )
    return result
