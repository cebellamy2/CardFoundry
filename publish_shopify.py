"""Publish local stock to Shopify. Dry run by default; --confirm to write.

★ WHAT THIS REPLACES. dry_run_shopify_publish.py (v2.86.0) previewed the
MAPPING and deliberately did not import shopify_service, so it could not
be the dry run of a publish -- it was a dry run of the arithmetic. This
script is the publish, and its dry run is the same program with the write
refused: same gate, same JSONL, same binding and audit writes, rolled
back. One script, so there is no second path to drift.

★ NOTHING HERE CAN WRITE TO SHOPIFY WITHOUT --confirm. The refusal is
not a branch in this file; it is shopify_service.authorize_writes, which
needs an explicit confirm AND an allow-listed store. Without --confirm
this script reaches that refusal and reports it.

★ AND --confirm ALONE IS NOT ENOUGH. A real publish also needs the
approved preview it is applying (--approved), and the live plan has to be
a SUBSET of it: CLAUDE.md's shrink-only rule, because between a preview
and an apply the shop is still trading. New stock, a grown quantity or a
moved price stops the run.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python publish_shopify.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python publish_shopify.py \
        --approve-to /tmp/shopify-approved.json
    cd /app && PYTHONPATH=/app /opt/venv/bin/python publish_shopify.py \
        --confirm --approved /tmp/shopify-approved.json
    cd /app && PYTHONPATH=/app /opt/venv/bin/python publish_shopify.py \
        --undo shopify-20261102T120000 [--confirm]
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from shopify_binding_service import undo_publish_run
from shopify_mapping_service import (
    MAX_PRODUCT_OPTIONS,
    MAX_PRODUCT_VARIANTS,
    build_publish_plan,
    price_report,
)
from shopify_publish_service import approved_set, publish

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "publish_shopify"

# One sample per shape the operator asked to see, in the order asked.
SAMPLE_WANTED = (
    ("non-English printing", lambda p, v: p["language_id"] != "EN"),
    ("foil", lambda p, v: v["finish_id"] == "FO"),
    ("etched foil", lambda p, v: v["finish_id"] == "EF"),
    ("suffixed collector number",
     lambda p, v: not p["collector_number"].isdigit()),
    ("consigned card", lambda p, v: v.get("is_consignment")),
    ("a 65c card (at the floor)",
     lambda p, v: v["price"]["manapool_price_cents"] == 65),
    ("clamped up to the floor", lambda p, v: v["price"]["clamped_to_floor"]),
    # Deliberately NOT just "discounted": a 67c card is discounted AND
    # clamped, and showing one as the discount example would make the
    # sample set look like the floor is the only rule there is.
    ("a discounted card (the 3% survives)",
     lambda p, v: v["price"]["discounted"]
     and not v["price"]["clamped_to_floor"]),
    ("deepest stock", lambda p, v: v["quantity"] > 1),
    ("most variants on one product", lambda p, v: len(p["variants"]) > 1),
)


def load_rows(session):
    """Cards, their batches and every allocation -- the same three inputs
    build_inventory_mirror_preview takes, so the eligibility rule sees
    exactly what it sees there."""
    from models import Batch, InventoryCard, PickAllocation

    cards = session.query(InventoryCard).all()
    batches_by_id = {batch.id: batch for batch in session.query(Batch).all()}
    allocations = session.query(PickAllocation).all()
    return cards, batches_by_id, allocations


def annotate_consignment(plan, cards, batches_by_id):
    """Mark each variant that holds at least one consigned card.

    Consignment is a property of the BATCH (Batch.is_consignment), not the
    card, and it changes nothing about how the card is published -- the
    operator's decision was that every card goes to Shopify, consigned
    included. It is annotated only so the preview can show one.
    """
    consigned_card_ids = {
        card.id for card in cards
        if (batches_by_id.get(card.batch_id) is not None
            and batches_by_id[card.batch_id].is_consignment)
    }
    for product in plan["products"]:
        for variant in product["variants"]:
            overlap = [
                card_id for card_id in variant["inventory_card_ids"]
                if card_id in consigned_card_ids
            ]
            variant["is_consignment"] = bool(overlap)
            variant["consigned_card_ids"] = overlap
    return plan


def pick_samples(plan, limit=10):
    """One row per interesting shape, up to `limit`, each labelled with
    why it is in the list. A sample nobody can tell the point of is just
    a row."""
    samples = []
    seen = set()
    for label, predicate in SAMPLE_WANTED:
        if len(samples) >= limit:
            break
        for product in plan["products"]:
            hit = None
            for variant in product["variants"]:
                try:
                    if predicate(product, variant):
                        hit = variant
                        break
                except (KeyError, TypeError):
                    continue
            if hit is None or hit["sku"] in seen:
                continue
            seen.add(hit["sku"])
            samples.append({"why": label, "product": product, "variant": hit})
            break
    # Backfill with ordinary rows if the interesting shapes did not fill
    # the quota -- an under-filled sample looks like a bug in the report.
    for product in plan["products"]:
        if len(samples) >= limit:
            break
        for variant in product["variants"]:
            if variant["sku"] in seen:
                continue
            seen.add(variant["sku"])
            samples.append({"why": "ordinary", "product": product,
                            "variant": variant})
            break
    return samples[:limit]


def _money(cents) -> str:
    return f"${int(cents) / 100:,.2f}"


def print_report(plan, prices, samples, report) -> None:
    confirmed = report["confirmed"]
    print("SHOPIFY PUBLISH --", "APPLIED" if confirmed else "DRY RUN")
    if not confirmed:
        print("  no mutation was sent, and nothing was committed")
    print()

    gate = report["gate"]
    print("THE GATE")
    print(f"  store domain                  {gate['store_domain']}")
    print(f"  on the write allow-list       {gate['allow_listed']} "
          f"{gate['allowed_write_domains']}")
    print(f"  SHOPIFY_LOCATION_ID           {gate['location_gid']}")
    print(f"  that location, read back      {gate['location_name']} "
          f"(confirmed={gate['location_confirmed']})")
    print(f"  API version                   {gate['api_version']}")
    print(f"  products created as           {gate['product_status']}")
    print(f"  may write                     {gate['may_write']}")
    for reason in gate["refusals"]:
        print(f"    refusal: {reason}")
    print()

    print("WHAT WOULD BE CREATED")
    print(f"  products                      {plan['product_count']:,}")
    print(f"  variants                      {plan['variant_count']:,}")
    print(f"  physical cards behind them    {plan['card_count']:,}")
    print(f"  max variants on one product   {plan['max_variants_per_product']:,}")
    limits = plan["limits"]
    print(f"  options used                  {limits['options_used']} of "
          f"{MAX_PRODUCT_OPTIONS}   within limit: "
          f"{limits['within_option_limit']}")
    print(f"  variants per product          "
          f"{plan['max_variants_per_product']} of {MAX_PRODUCT_VARIANTS}   "
          f"within limit: {limits['within_variant_limit']}")
    print(f"  handles claimed twice         {len(plan['handle_collisions'])}")
    for handle, rows in list(plan["handle_collisions"].items())[:5]:
        print(f"    !! {handle}  {rows}")
    print()

    print("CARDS NOT PUBLISHED, AND WHY")
    if not plan["excluded_counts"]:
        print("  (none)")
    for reason, count in plan["excluded_counts"].items():
        print(f"  {reason:<36} {count:,}")
    print()

    print("PRICE: THE ONE RULE (x0.97, round DOWN, then clamp up to the floor)")
    print(f"  the shop's floor                      {prices['price_floor_cents']}c")
    print(f"  variants priced                       {prices['variants_total']:,}")
    print(f"  physical cards priced                 {prices['units_total']:,}")
    print(f"  variants that GET the 3%              {prices['variants_discounted']:,}"
          f"  ({prices['units_discounted']:,} cards)")
    print(f"  variants CLAMPED to the floor         {prices['variants_clamped_to_floor']:,}"
          f"  ({prices['units_clamped_to_floor']:,} cards)")
    print(f"  ... of those, already AT 65c          {prices['variants_at_the_floor_price']:,}")
    print(f"  variants landing on a fraction        {prices['variants_fractional']:,}"
          f"  ({prices['units_fractional']:,} cards)")
    print(f"  variants whose cards DISAGREE on price {prices['variants_with_a_price_spread']:,}")
    print()
    print("  DRAFT -- totals computed from the local database, not verified")
    print("  against Mana Pool or Shopify:")
    print(f"    Mana Pool value of this stock       {_money(prices['manapool_total_cents'])}  DRAFT")
    print(f"    Shopify value of this stock         {_money(prices['shopify_total_cents'])}  DRAFT")
    print(f"    difference (the discount)           {_money(prices['discount_total_cents'])}  DRAFT")
    print(f"    given up by rounding down           {prices['rounding_gave_up_cents']}c  DRAFT")
    print(f"    put back by the floor               {_money(prices['floor_added_back_cents'])}  DRAFT")
    print()

    preview = report["preview"]
    print("WHAT THE BULK IMPORT WOULD SEND")
    print(f"  JSONL lines (one per product)         {preview['lines']:,}")
    print(f"  JSONL size                            {preview['jsonl_bytes']:,} bytes "
          f"of {preview['max_jsonl_bytes']:,} allowed "
          f"({preview['within_file_size_limit']})")
    print(f"  inventory quantities                  {preview['inventory_quantities']:,} "
          f"of {preview['max_inventory_quantities']:,} allowed "
          f"({preview['within_inventory_quantity_limit']})")
    print(f"  products identified by stored gid     {preview['identified_by_gid']:,}")
    print(f"  products identified by handle         {preview['identified_by_handle']:,}")
    print(f"  productType, on create only           "
          f"{preview['product_type_on_create']!r}")
    print()
    print("ESTIMATED SHOPIFY API COST AND TIME FOR THE FULL LOAD")
    calls = preview["api_calls"]
    print(f"  stagedUploadsCreate (mutation)        {calls['stagedUploadsCreate']}")
    print(f"  staged upload POST (not the API)      {calls['staged_upload_post']}")
    print(f"  bulkOperationRunMutation              {calls['bulkOperationRunMutation']}"
          f"   -- documented as NOT rate limited")
    print(f"  status polls, at most                 {calls['bulk_operation_polls_at_most']}"
          f"   (every {preview['poll_ceiling_seconds'] / calls['bulk_operation_polls_at_most']:.0f}s "
          f"up to {preview['poll_ceiling_seconds']:.0f}s)")
    print(f"  results download                      {calls['results_download']}")
    print(f"  stock read-backs                      {calls['verification_reads']}"
          f"   ({preview['variants']:,} variants, 25 per read)")
    print(f"  GRAPHQL CALLS, AT MOST                {calls['total_graphql_at_most']}")
    print("  wall clock: unknown from here -- the import runs on Shopify's")
    print("  side and the only documented numbers are ceilings (24h to")
    print("  complete; this client stops watching after 30 min and says so).")
    print()

    print(f"SAMPLE ROWS ({len(samples)})")
    for sample in samples:
        product, variant = sample["product"], sample["variant"]
        price = variant["price"]
        print(f"  [{sample['why']}]")
        print(f"     title          {product['title']}")
        print(f"     handle         {product['handle']}")
        print(f"     sku            {variant['sku']}")
        print(f"     price          {_money(price['shopify_price_cents'])}"
              f"   (mana pool {_money(price['manapool_price_cents'])}"
              f" -> {price['exact_cents']}c exact"
              f" -> floor {price['floored_cents']}c"
              f"{' -> CLAMPED' if price['clamped_to_floor'] else ''})")
        print(f"     quantity       {variant['quantity']} "
              f"(cards {variant['inventory_card_ids'][:6]}"
              f"{' ...' if len(variant['inventory_card_ids']) > 6 else ''})")
        print(f"     options        "
              f"{product['option_names'][0]}="
              f"{variant['option_values'][product['option_names'][0]]}  "
              f"{product['option_names'][1]}="
              f"{variant['option_values'][product['option_names'][1]]}")
        print(f"     consigned      {variant.get('is_consignment', False)}")
        print(f"     scryfall image {product['scryfall_id'] or 'NONE'}")
    print()

    if report["shrink_only"] is not None:
        shrink = report["shrink_only"]
        print("SHRINK-ONLY CHECK AGAINST THE APPROVED PREVIEW")
        print(f"  approved variants             {shrink['approved_variant_count']:,}")
        print(f"  live variants                 {shrink['live_variant_count']:,}")
        print(f"  dropped out (allowed)         {len(shrink['vanished']):,}")
        print(f"  quantity fell (allowed)       {len(shrink['quantity_shrank']):,}")
        print(f"  NEW since approval            {len(shrink['appeared']):,}")
        print(f"  quantity ROSE                 {len(shrink['quantity_grew']):,}")
        print(f"  REPRICED                      {len(shrink['repriced']):,}")
        print(f"  is a subset                   {shrink['is_subset']}")
        print()

    results = report["results"]
    if results:
        print("RESULTS" + ("" if confirmed else " (simulated -- see --confirm)"))
        print(f"  lines sent                    {results['sent_lines']:,}")
        print(f"  result lines                  {results['result_lines']:,}")
        print(f"  variants confirmed            {results['confirmed_variants']:,}")
        print(f"  line errors                   {results['line_errors']:,}")
        print(f"  skus Shopify never mentioned  {results['unanswered_skus']:,}")
        print(f"  UNCLASSIFIED result lines     {results['unclassified']:,}")
        for row in results["unclassified_examples"]:
            print(f"    ?? {row}")
        for row in results["error_examples"]:
            print(f"    !! {row}")
        print()

    bindings = report["bindings"]
    if bindings:
        print("LOCAL ROWS")
        print(f"  bindings written              {len(bindings['bindings_written']):,}"
              f"{'' if confirmed else '  (rolled back)'}")
    retire = report["retirements"] or {}
    for key, rows in retire.items():
        print(f"  {key:<29}{len(rows):,}")
    print()

    if report["bulk_operation"]:
        print("BULK OPERATION")
        for key, value in report["bulk_operation"].items():
            print(f"  {key:<29}{value}")
        print()
    if report["verification"]:
        verify = report["verification"]
        print("STOCK READ BACK FROM SHOPIFY")
        print(f"  checked                       {verify['checked']:,}")
        print(f"  matched what we sent          {len(verify['matched']):,}")
        print(f"  DIFFERENT                     {len(verify['mismatched']):,}")
        print(f"  absent from Shopify           {len(verify['absent_from_shopify']):,}")
        for row in verify["mismatched"][:10]:
            print(f"    !! {row}")
        print()
    if report["uncertain"]:
        print("A WRITE WAS UNCERTAIN -- NOTHING WAS RETRIED")
        for key, value in report["uncertain"].items():
            print(f"  {key:<29}{value}")
        print()

    if report["stopped_because"]:
        print(f"STOPPED: {report['stopped_because']}")
    if not confirmed:
        print("NOTHING WAS WRITTEN TO SHOPIFY AND NOTHING WAS COMMITTED.")


def run_undo(args) -> int:
    with Session(engine) as session:
        result = undo_publish_run(session, args.undo, confirm=args.confirm)
        if args.confirm:
            session.commit()
        else:
            session.rollback()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if not args.confirm:
        print("\nDRY RUN -- nothing was committed. Re-run with --confirm.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="Actually write. Without it, nothing is sent "
                             "and nothing is committed.")
    parser.add_argument("--approved", metavar="FILE",
                        help="The approved preview this apply is applying. "
                             "Required with --confirm.")
    parser.add_argument("--approve-to", metavar="FILE",
                        help="Write this dry run's approved set to FILE, for "
                             "a later --confirm to be checked against.")
    parser.add_argument("--undo", metavar="RUN_ID",
                        help="Undo a publish run's LOCAL rows. Dry by "
                             "default; add --confirm to apply.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output instead of the report.")
    parser.add_argument("--samples", type=int, default=10,
                        help="How many sample rows to show (default 10).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    if args.undo:
        return run_undo(args)

    approved = None
    if args.approved:
        with open(args.approved, encoding="utf-8") as handle:
            approved = json.load(handle)

    with Session(engine) as session:
        cards, batches_by_id, allocations = load_rows(session)
        logger.info("%s: read %s card(s), %s batch(es), %s allocation(s).",
                    SCRIPT_NAME, len(cards), len(batches_by_id),
                    len(allocations))
        plan = build_publish_plan(cards, batches_by_id, allocations)
        plan = annotate_consignment(plan, cards, batches_by_id)
        prices = price_report(plan)
        samples = pick_samples(plan, limit=args.samples)
        report = publish(session, plan, confirm=args.confirm,
                         approved=approved)
        # ★ THE ONLY COMMIT IN THIS SCRIPT, AND IT IS BEHIND --confirm.
        # publish() never commits; whatever the dry run wrote -- binding
        # rows against dry-run:// gids, audit rows -- ends here.
        if args.confirm and not report["stopped_because"]:
            session.commit()
        else:
            session.rollback()
        if args.approve_to:
            with open(args.approve_to, "w", encoding="utf-8") as handle:
                json.dump(approved_set(plan), handle, indent=2,
                          sort_keys=True)
            logger.info("%s: approved set written to %s.",
                        SCRIPT_NAME, args.approve_to)

    if args.json:
        print(json.dumps(
            {"plan": plan, "prices": prices, "samples": samples,
             "report": report},
            indent=2, sort_keys=True, default=str,
        ))
    else:
        print_report(plan, prices, samples, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
