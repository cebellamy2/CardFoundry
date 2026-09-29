"""Plan an approved new-listing run, then publish exactly what was approved.

THE WORKFLOW THIS EXISTS FOR (after 2026-09-29, when an approved run published
59 listings instead of 8):

  1. PLAN -- writes nothing, anywhere:
         cd /app && PYTHONPATH=/app /opt/venv/bin/python \\
             dry_run_new_listings.py --plan --out /tmp/approved.json

     It runs the REAL MTGJSON backfill inside a transaction that is always
     rolled back, so it sees the same candidate set the apply will -- the
     thing the old dry run could not do. It prints every candidate and writes
     the approved set to --out.

  2. The operator reviews that file. It IS the approval.

  3. PUBLISH -- refuses if anything drifted:
         cd /app && PYTHONPATH=/app /opt/venv/bin/python \\
             dry_run_new_listings.py --apply --approved-set /tmp/approved.json

     The apply re-plans, compares against the approved file, and aborts BEFORE
     any Mana Pool write if the sets differ in any way -- an extra candidate, a
     missing one, or a changed quantity, price or identity.

--apply publishes new listings ONLY. It does not run reconciliation and does
not change quantities on existing listings; Perform Sync still owns that.
"""
import argparse
import json
import logging
import sys

from sqlalchemy.orm import Session

from competitor_pricing_service import SELLER_EXCLUSION_ID
from database import engine
from inventory_sync_service import inventory_sync_lease
from manapool_service import (create_or_update_inventory_by_scryfall_id,
                              get_all_seller_inventory,
                              get_inventory_listings_by_ids,
                              get_single_catalog_by_product_ids,
                              get_single_catalog_by_scryfall_ids,
                              optimize_exact_variant_batch_with_conflicts,
                              update_inventory_prices_by_product)
from new_listing_dry_run import plan_new_listing_run
from new_listing_upload_service import (NewListingUploadError,
                                        apply_new_listing_preview)

logger = logging.getLogger("cardfoundry")


def _manual_overrides(session):
    import main
    return main._active_manual_price_overrides(session)


def build_plan(session_for_overrides=None) -> dict:
    with Session(engine) as session:
        overrides = _manual_overrides(session)
    return plan_new_listing_run(
        engine,
        seller_loader=get_all_seller_inventory,
        catalog_product_loader=get_single_catalog_by_product_ids,
        catalog_scryfall_loader=get_single_catalog_by_scryfall_ids,
        optimizer_call=optimize_exact_variant_batch_with_conflicts,
        listings_call=get_inventory_listings_by_ids,
        seller_id=SELLER_EXCLUSION_ID,
        manual_overrides=overrides,
    )


def print_plan(plan: dict) -> None:
    print("mirror categories :", json.dumps(plan["mirror_summary"], sort_keys=True))
    print("backfill would set:", json.dumps(plan["backfill"], sort_keys=True))
    rows = plan["preview"].get("rows") or []
    print("candidates        :", len(plan["approved_set"]), "priced of", len(rows), "rows")
    print()
    for entry in plan["approved_set"]:
        print("  %-30s %-5s #%-8s %s/%s/%s qty=%-3s $%s" % (
            (entry.get("name") or "")[:30], entry.get("set_code"),
            entry.get("collector_number"), entry.get("language_id"),
            entry.get("condition_id"), entry.get("finish_id"),
            entry.get("quantity"), "%.2f" % ((entry.get("price_cents") or 0) / 100),
        ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", help="Dry run. Writes nothing.")
    parser.add_argument("--apply", action="store_true",
                        help="Publish, guarded by --approved-set.")
    parser.add_argument("--out", help="Where --plan writes the approved set.")
    parser.add_argument("--approved-set", help="The reviewed file --apply must match.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)

    if args.plan == args.apply:
        parser.error("choose exactly one of --plan or --apply")

    if args.plan:
        plan = build_plan()
        print_plan(plan)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(plan["approved_set"], handle, indent=2, sort_keys=True)
            print()
            print("approved set written to", args.out)
        print()
        print("NOTHING WAS WRITTEN. The backfill was rolled back.")
        return 0

    if not args.approved_set:
        parser.error("--apply requires --approved-set")
    with open(args.approved_set, encoding="utf-8") as handle:
        approved = json.load(handle)
    print("approved set:", len(approved), "candidate(s)")

    # Re-plan immediately before writing, so the comparison is against the
    # world as it is now -- not as it was when the operator reviewed it.
    plan = build_plan()
    print_plan(plan)

    with inventory_sync_lease():
        with Session(engine) as session:
            try:
                result = apply_new_listing_preview(
                    session, plan["preview"],
                    get_all_seller_inventory,
                    create_or_update_inventory_by_scryfall_id,
                    update_inventory_prices_by_product,
                    optimize_exact_variant_batch_with_conflicts,
                    get_inventory_listings_by_ids,
                    SELLER_EXCLUSION_ID,
                    get_single_catalog_by_scryfall_ids,
                    market_catalog_product_call=get_single_catalog_by_product_ids,
                    manual_overrides=_manual_overrides(session),
                    approved_candidates=approved,
                )
            except NewListingUploadError as exc:
                session.rollback()
                print()
                print("REFUSED -- nothing was published:")
                print(" ", exc)
                return 1
            session.commit()
    print()
    print("published:", json.dumps({
        k: v for k, v in result.items()
        if k in ("published_card_ids", "excluded", "repriced")
    }, default=str)[:900])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
