"""See exactly what an approved new-listing run would publish -- writing
nothing anywhere.

WHY THIS EXISTS. On 2026-09-29 an approved run published 59 listings instead
of the approved 8. The dry run beforehand was built with the ordinary mirror
path, which does NOT run the MTGJSON backfill. Perform Sync's FIRST step is
``run_additive_mtgjson_backfill``, and a card whose ``mtgjson_id`` is NULL has
no canonical key, so it forms no local group and is INVISIBLE in a mirror
preview. The backfill filled 54 batch-D4 cards' identities, which made them
``local_only_requires_listing``, and the apply published them. The dry run had
correctly reported 8 -- it was simply looking at a different world from the one
the apply would act on.

So a dry run that skips the backfill cannot predict the real run. This one
performs the real backfill, using the real function, and throws the result away.

THE MECHANISM, AND WHY THIS ONE. Three options were considered:

  1. Re-implement what the backfill WOULD set, read-only. Rejected: a second
     implementation of identity resolution is exactly the kind of copy that
     drifts from the original, and the bug being fixed is already a
     "two views of the world disagreed" bug.
  2. Run the real backfill and undo it afterwards. Rejected: "undo" is a write,
     and a crash between do and undo leaves production altered.
  3. CHOSEN -- run the real backfill inside a transaction that is ALWAYS rolled
     back. ``run_additive_mtgjson_backfill`` and ``build_new_listing_preview``
     both mutate only the SQLAlchemy session and neither one commits (their own
     docstrings say the caller commits), so a rollback is total and needs no
     compensating write. The session is owned and closed here, in a ``finally``,
     so no caller can forget.

WHAT IT DELIBERATELY DOES NOT DO:
  * It does not commit. Nothing it touches survives the call.
  * It does not write to Mana Pool. Every Mana Pool call it makes is a read.
  * It does not ingest orders, and it does not persist prices or listing
    status -- so it is NOT ``create_inventory_sync_preview``, which does all
    three. THE CONSEQUENCE, STATED PLAINLY: an order that arrives between the
    dry run and the apply is not reflected here, so a card could sell in
    between. That is not a gap this can close (ingesting orders is a write);
    it is caught instead by ``apply_new_listing_preview``'s own re-validation,
    which re-checks local availability immediately before writing.
"""
import logging

from sqlalchemy.orm import Session

from inventory_sync_workflow import _build_mirror_preview_from_snapshot
from models import InventoryCard
from mtgjson_backfill_service import run_additive_mtgjson_backfill
from new_listing_upload_service import (approved_set_from_preview,
                                        build_new_listing_preview)

logger = logging.getLogger("cardfoundry")


def plan_new_listing_run(
    engine,
    *,
    seller_loader,
    catalog_product_loader,
    catalog_scryfall_loader,
    optimizer_call,
    listings_call,
    seller_id,
    manual_overrides=(),
) -> dict:
    """What the next approved run would publish, computed and thrown away.

    Returns ``{"preview", "approved_set", "mirror_summary", "backfill"}``.
    ``approved_set`` is ready to hand straight back to
    ``apply_new_listing_preview(approved_candidates=...)``.
    """
    # One seller read, shared by the backfill and the mirror, so the two see
    # the same remote snapshot -- and so this costs one call, not two.
    remote_inventory = seller_loader(min_quantity=0)

    def shared_seller_loader(min_quantity=0):
        return remote_inventory

    session = Session(engine)
    try:
        backfill = run_additive_mtgjson_backfill(
            session, shared_seller_loader, catalog_product_loader,
            operator_note="DRY RUN -- rolled back, never committed",
        )
        # Make the backfilled identities visible to the mirror's own queries
        # without committing them.
        session.flush()

        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        mirror = _build_mirror_preview_from_snapshot(
            session, cards, remote_inventory, False,
        )
        preview = build_new_listing_preview(
            session, mirror,
            optimizer_call, listings_call, seller_id,
            catalog_scryfall_loader,
            market_catalog_product_call=catalog_product_loader,
            manual_overrides=manual_overrides,
        )

        summary = {}
        for row in mirror.get("rows") or []:
            category = row.get("category")
            summary[category] = summary.get(category, 0) + 1

        result = {
            "preview": preview,
            "approved_set": approved_set_from_preview(preview),
            "mirror_summary": summary,
            "backfill": {
                "updated_inventory_cards": backfill.get("updated_inventory_cards"),
                "updated_bindings": backfill.get("updated_bindings"),
                "skipped": len(backfill.get("skipped") or []),
                "auto_overridden_bindings": len(
                    backfill.get("auto_overridden_bindings") or []
                ),
            },
        }
        logger.info(
            "New-listing dry run: backfill would set %s card(s); %d candidate(s) "
            "would publish. Nothing was committed.",
            backfill.get("updated_inventory_cards"),
            len(result["approved_set"]),
        )
        return result
    finally:
        # ALWAYS. This is what makes running the real backfill safe: every
        # local row it touched is discarded, on success and on failure alike.
        session.rollback()
        session.close()
