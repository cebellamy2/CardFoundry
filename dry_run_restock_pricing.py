"""Preview the restock-pricing fix: nothing is sent to Mana Pool.

WHAT IS BEING PREVIEWED. Two changes that only work together:

  (A) local_price_writeback_service.write_back_seller_inventory no longer
      reads a price off a quantity-0 listing. The pricing cron filters on
      `minQuantity: 1`, so a dormant listing's price is one automated
      pricing has never seen and cannot maintain.
  (B) inventory_reconciliation_service's increase direction sends our own
      price WITH the quantity, in the same documented write, so a listing
      coming back off an empty shelf never goes live at the price it was
      left at.

★ WHY THERE ARE TWO MODES, AND WHY NEITHER COMPUTES ITS OWN ANSWER.

--replay reconstructs the two sales that happened, from the audit rows
production actually holds, and runs the REAL write_back_seller_inventory
and the REAL restock_price_cents over them. The local price at the moment
of each failure is not inferred: it is `inventory_price_history.old_price`
on the row that overwrote it, and the dormant listing's price is that same
row's `new_price`, which is what the scan copied off Mana Pool. So the
replay needs no Mana Pool call at all, and no figure in it is derived.

--plan runs the real perform-sync chain on live data -- the additive
MTGJSON backfill, then create_inventory_sync_preview, then
build_reconciliation_preview, then the real apply_reconciliation_preview
-- and reports what the write WOULD have carried. A preview that
reimplements the apply cannot predict it; that is how v2.12.0 published 59
listings when 8 were approved.

THE ONE DIFFERENCE FROM THE WRITE, STATED PLAINLY: product_writer is a
RECORDER. It captures the payload Mana Pool would have received and makes
no request. Everything upstream of that boundary is the real code.

★ RUN --plan AGAINST A COPY. create_inventory_sync_preview commits
internally (listing status, order ingestion, the price write-back), and so
does run_additive_mtgjson_backfill's caller, so a rollback here cannot
undo everything this touches. A dry run through code that commits is not a
dry run. Make the copy and point DATABASE_URL at it:

    cd /app \
      && /opt/venv/bin/python -c "import sqlite3; \
           sqlite3.connect('file:/data/cardfoundry.db?mode=ro', uri=True) \
             .execute(\"VACUUM INTO '/tmp/restock_dryrun.db'\")" \
      && DATABASE_URL=sqlite:////tmp/restock_dryrun.db PYTHONPATH=/app \
         /opt/venv/bin/python dry_run_restock_pricing.py --plan

VACUUM INTO is safe on the live file: it reads through SQLite's own locking
and writes a fresh database elsewhere, touching nothing in /data.

--replay only reads, so it is safe against production directly:

    cd /app && PYTHONPATH=/app /opt/venv/bin/python dry_run_restock_pricing.py --replay

NO MANA POOL WRITES IN EITHER MODE. --replay makes no request at all.
--plan reads only: the order listing, order details (capped by
ORDER_SYNC_MAX_ORDERS_PER_RUN), the seller-inventory walk, and the
backfill's catalog reads.
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import DATABASE_URL, engine

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "dry_run_restock_pricing"

# inventory_sync_workflow's own key for the same setting.
GO_LIVE_SETTING_KEY = "manapool_go_live_at"

# The two sales this change exists to prevent. Everything else about them
# is read from the database; these are only the keys to find them by.
REPLAY_CASES = (
    {"inventory_card_id": 16023, "order_label": "730352-2572548",
     "sold_cents": 313, "note": "Nicol Bolas, Dragon-God -- the reported sale"},
    {"inventory_card_id": 12799, "order_label": "684607-2413591",
     "sold_cents": 219, "note": "Chatterfang, Squirrel General -- the same failure, 17 days earlier"},
)


class RecordingProductWriter:
    """Captures what would have been written. Makes no request.

    Mirrors the real writer's return shape -- a LIST of one response per
    chunk, not a single dict -- because the caller stores whatever comes
    back and a wrong shape here would itself be a difference between the
    preview and the write.
    """

    def __init__(self):
        self.updates = []

    def __call__(self, updates):
        self.updates.extend(updates)
        logger.info(
            "%s: RECORDED (not sent) %s inventory update(s).",
            SCRIPT_NAME, len(updates),
        )
        return [{"recorded": len(updates), "sent": False}]


class _DetachedCard:
    """Just enough of an InventoryCard for restock_price_cents, built from
    values read out of production. Deliberately not a real ORM object: the
    replay must not be able to write anything, even by accident."""

    def __init__(self, card_id, current_price):
        self.id = card_id
        self.current_price = current_price


def replay(session) -> int:
    """The two real failures, through the real functions. Reads only."""
    from models import InventoryCard, InventoryPriceHistory
    from local_price_writeback_service import (
        PRICING_FLOOR_CENTS, listing_holds_stock,
    )
    from inventory_reconciliation_service import restock_price_cents

    print("=" * 72)
    print("REPLAY -- the two sales, through the real code. No Mana Pool call.")
    print("=" * 72)
    problems = 0
    for case in REPLAY_CASES:
        card = session.get(InventoryCard, case["inventory_card_id"])
        if card is None:
            print(f"\nUNCLASSIFIED: inventory card {case['inventory_card_id']} is not "
                  "in this database -- nothing replayed for it.")
            problems += 1
            continue
        scan_row = (
            session.query(InventoryPriceHistory)
            .filter(
                InventoryPriceHistory.inventory_card_id == card.id,
                InventoryPriceHistory.source == "seller_inventory_scan",
            )
            .order_by(InventoryPriceHistory.changed_at)
            .first()
        )
        if scan_row is None or scan_row.old_price is None or scan_row.new_price is None:
            print(f"\nUNCLASSIFIED: card {card.id} ({card.name}) has no "
                  "seller_inventory_scan row carrying both prices -- the replay "
                  "has no source for what the local price was.")
            problems += 1
            continue

        # Source-verified, both of them: the price the card held before the
        # scan, and the price the scan copied off the dormant listing.
        local_price_before = float(scan_row.old_price)
        dormant_cents = int(round(float(scan_row.new_price) * 100))

        print(f"\n--- card {card.id}: {card.name} "
              f"[{card.set_code} {card.collector_number} {card.language_id} "
              f"{card.condition_id} {card.finish_id}]")
        print(f"    {case['note']}")
        print(f"    order {case['order_label']} sold at "
              f"${case['sold_cents'] / 100:,.2f} (production order_items)")
        print(f"    audit row {scan_row.id} @ {scan_row.changed_at}: "
              f"${local_price_before:,.2f} -> ${dormant_cents / 100:,.2f} "
              f"(source {scan_row.source})")

        # (A) The dormant listing, in the shape the scan reads.
        listing = {
            "product_id": "(replayed)",
            "price_cents": dormant_cents,
            "quantity": 0,
            "product": {"single": {
                "set": card.set_code, "number": card.collector_number,
                "language_id": card.language_id,
                "condition_id": card.condition_id,
                "finish_id": card.finish_id,
            }},
        }
        holds_stock = listing_holds_stock(listing)
        print(f"    (A) write-back: listing_holds_stock -> {holds_stock}; "
              f"{'PRICED' if holds_stock else 'SKIPPED as dormant'} "
              f"-- local price stays ${local_price_before:,.2f}")
        if holds_stock:
            print("        !! the write-back would still copy the dormant price")
            problems += 1

        # (B) The restock, with the local price fix (A) preserved.
        sent, refusal = restock_price_cents(
            [_DetachedCard(card.id, local_price_before)], listing,
        )
        if refusal:
            print(f"    (B) restock: REFUSED -- {refusal}")
            problems += 1
        elif sent is None:
            print("    (B) restock: no price sent (listing already at our price)")
            problems += 1
        else:
            print(f"    (B) restock: would send price_cents={sent} "
                  f"(${sent / 100:,.2f}) WITH the quantity, in one write, "
                  f"replacing Mana Pool's {dormant_cents}")
            if sent <= case["sold_cents"]:
                print("        !! that is no better than the price it sold at")
                problems += 1
        print(f"    floor in force: {PRICING_FLOOR_CENTS} cents")
    return problems


def _bucket(update: dict, audit_by_product: dict) -> str:
    product_id = str(update.get("product_id") or "")
    audit = audit_by_product.get(product_id)
    if audit is None:
        if update.get("price_cents") is None:
            return "no price sent (decrease, or already at our price)"
        return "UNCLASSIFIED: a price was sent with no audit entry"
    before = audit.get("remote_price_cents_before")
    sent = audit.get("price_cents_sent")
    if before is None or sent is None:
        return "UNCLASSIFIED: audit entry is missing a price"
    try:
        before = int(before)
        sent = int(sent)
    except (TypeError, ValueError):
        return "UNCLASSIFIED: audit price is not a whole number of cents"
    if before <= 0:
        return "UNCLASSIFIED: Mana Pool reported no previous price"
    ratio = sent / before
    if ratio >= 4:
        return "raised 4x or more (a dormant price being corrected)"
    if ratio > 1.25:
        return "raised (1.25x - 4x)"
    if ratio >= 0.8:
        return "about the same (0.8x - 1.25x)"
    return "LOWERED below 0.8x -- explain before shipping"


def plan(session) -> int:
    """The real perform-sync chain, stopped at the write boundary."""
    from manapool_service import (get_all_seller_inventory, get_seller_order,
                                  get_seller_orders,
                                  get_single_catalog_by_product_ids)
    from mtgjson_backfill_service import run_additive_mtgjson_backfill
    from inventory_sync_workflow import create_inventory_sync_preview
    from inventory_reconciliation_service import (
        NOTHING_TO_APPLY, apply_reconciliation_preview,
        build_reconciliation_preview,
    )
    from models import AppSetting

    # Read straight from app_settings rather than importing main. The
    # setting is one row with one key, and pulling 33,000 lines of web
    # application into a diagnostic script to read it would be the wrong
    # trade -- inventory_sync_workflow does the same thing for the same
    # reason (see its _setting helper).
    go_live_row = (
        session.query(AppSetting)
        .filter(AppSetting.key == GO_LIVE_SETTING_KEY)
        .first()
    )
    go_live_at = go_live_row.value if go_live_row else None
    if not go_live_at:
        raise SystemExit(
            "The Mana Pool go-live timestamp is not configured in this "
            "database, so the order re-ingest the apply starts with cannot "
            "run. Nothing was done."
        )

    print("=" * 72)
    print("PLAN -- the real chain on live data. The writer is a recorder.")
    print("=" * 72)

    logger.info("%s: run_additive_mtgjson_backfill (Perform Sync's own first "
                "step) -- a NULL-mtgjson card is invisible to the mirror "
                "without it.", SCRIPT_NAME)
    backfill = run_additive_mtgjson_backfill(
        session, get_all_seller_inventory, get_single_catalog_by_product_ids,
        operator_note=f"{SCRIPT_NAME} dry run",
    )
    session.commit()
    print(f"backfill updated cards:        {backfill['updated_inventory_cards']}")

    mirror_preview = create_inventory_sync_preview(
        fail_closed_on_unresolved=False, acquire_lease=True,
    )
    write_back = (mirror_preview or {}).get("local_price_write_back") or {}
    print()
    print("(A) the seller-inventory price write-back, as it now behaves:")
    for key in ("rows_considered", "listings_skipped_dormant",
                "listings_quantity_unknown", "cards_newly_priced",
                "cards_repriced", "cards_unchanged", "cards_skipped_hold",
                "rows_unmatched", "rows_no_price", "error"):
        if key in write_back:
            print(f"    {key:28s} {write_back[key]}")
    if "error" in write_back:
        print("    !! the write-back errored; its counts above are absent by design")

    reconciliation_preview = build_reconciliation_preview(session, mirror_preview)
    summary = reconciliation_preview["summary"]
    print()
    print("(B) reconciliation candidates:")
    print(f"    candidates                   {summary['candidates']}")
    print(f"    increase (restocks)          {summary['increase']}")
    print(f"    decrease                     {summary['decrease']}")
    if summary["candidates"] == 0:
        print()
        print("Nothing to reconcile on this run, so nothing would be restocked "
              "and no price would be sent. That is an ordinary outcome, not a "
              "failure -- run again after the next intake.")
        return 0

    writer = RecordingProductWriter()
    result = apply_reconciliation_preview(
        session, reconciliation_preview,
        get_seller_orders, get_seller_order, go_live_at,
        get_all_seller_inventory, writer,
    )
    print()
    print(f"apply outcome:                 {result.get('outcome')}")
    if result.get("outcome") == NOTHING_TO_APPLY:
        print(f"    {result.get('message')}")
        return 0

    audit_by_product = {
        str(row.get("product_id")): row for row in result.get("price_writes") or []
    }
    updates = result.get("updates") or []
    increases = [u for u in updates if u.get("price_cents") is not None]
    print(f"updates that would be written:  {len(updates)}")
    print(f"  carrying a price:             {len(increases)}")
    print(f"  price left unchanged (null):  {len(updates) - len(increases)}")
    print(f"  excluded rows:                {len(result.get('excluded') or [])}")

    buckets = {}
    for update in updates:
        key = _bucket(update, audit_by_product)
        buckets[key] = buckets.get(key, 0) + 1
    print()
    print("every update classified -- no row is dropped from this table:")
    for key, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"    {count:5d}  {key}")

    if audit_by_product:
        print()
        print("each priced restock, old Mana Pool price vs the price we would send:")
        rows = sorted(
            audit_by_product.values(),
            key=lambda r: -(int(r.get("price_cents_sent") or 0)
                            - int(r.get("remote_price_cents_before") or 0)),
        )
        for row in rows:
            before = row.get("remote_price_cents_before")
            sent = row.get("price_cents_sent")
            before_text = "unknown" if before is None else f"${int(before) / 100:,.2f}"
            print(f"    {row.get('name')}  [{row.get('product_id')}]")
            print(f"        Mana Pool held {before_text}  ->  we would send "
                  f"${int(sent) / 100:,.2f}   (remote qty before "
                  f"{row.get('remote_quantity_before')})")
            print(f"        deciding card prices: {row.get('deciding_card_prices')}")

    excluded_reasons = {}
    for row in result.get("excluded") or []:
        key = str(row.get("exclusion_reason") or "UNCLASSIFIED: no reason recorded")
        excluded_reasons[key] = excluded_reasons.get(key, 0) + 1
    if excluded_reasons:
        print()
        print("excluded rows, by reason:")
        for key, count in sorted(excluded_reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {count:5d}  {key}")

    for update in updates:
        extra = set(update) - {"product_type", "product_id", "price_cents", "quantity"}
        if extra:
            print()
            print(f"!! the payload carries fields Mana Pool does not document: {extra}")
            return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", action="store_true",
                      help="The two historical sales, through the real code. Reads only.")
    mode.add_argument("--plan", action="store_true",
                      help="The real chain on live data; the writer is a recorder. "
                           "Run against a COPY -- see this module's docstring.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    print(f"database: {DATABASE_URL}")
    if args.plan and "/data/cardfoundry.db" in DATABASE_URL:
        raise SystemExit(
            "--plan commits internally (create_inventory_sync_preview and the "
            "additive backfill both do). Point DATABASE_URL at a VACUUM INTO "
            "copy instead -- the command is in this module's docstring. "
            "Nothing was run."
        )
    print()

    with Session(engine) as session:
        problems = replay(session) if args.replay else plan(session)
        # Nothing is written by this script, whatever happened above. On
        # --plan the chain's own commits have already landed in the COPY,
        # which is exactly why --plan refuses to run against production.
        session.rollback()

    print()
    if args.replay:
        print("REPLAY ONLY -- no database write, no Mana Pool request.")
    else:
        print("PLAN ONLY -- no Mana Pool request was made; the writer recorded.")
    if problems:
        print(f"!! {problems} thing(s) above need explaining before anything ships.")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
