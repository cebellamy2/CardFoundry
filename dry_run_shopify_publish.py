"""Preview what a Shopify publish would create. Writes nothing, anywhere.

★ WHAT THIS IS. Phase 1 ticket 2's hard stop. It reads production,
computes the products, variants, SKUs and prices a publish would create,
and prints them. Nothing is written to Shopify, to Mana Pool or to the
local database, and no Shopify request of any kind is made -- this script
does not import shopify_service at all, so there is no HTTP client here
to misuse.

★ WHY THERE IS NO --confirm FLAG. CLAUDE.md's rule is that a script is
dry-run by default and takes --confirm to write. This one has nothing to
confirm: it is a report, like the dry_run_* scripts beside it, and the
publish that WILL need a --confirm is a later ticket with an operator
approval in front of it. A flag that did nothing would imply otherwise.

★ WHY IT STILL OPENS A TRANSACTION AND ROLLS IT BACK. Reading through a
Session can emit writes nobody intended (an autoflush of a lazily
mutated row). The explicit rollback at the end is the guarantee, not the
intention.

★ THE ELIGIBILITY RULE IS NOT THIS SCRIPT'S. It is
build_inventory_mirror_preview's, read through
shopify_mapping_service.eligibility -- one rule for both channels, so a
card for sale on Mana Pool is a card for sale on Shopify, and the
difference between the two reports can only be a Shopify-specific
reason (no price, no set code / collector number).

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python dry_run_shopify_publish.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python dry_run_shopify_publish.py --json
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from local_price_writeback_service import PRICING_FLOOR_CENTS
from shopify_mapping_service import (
    MAX_PRODUCT_OPTIONS,
    MAX_PRODUCT_VARIANTS,
    build_publish_plan,
    fractional_price_report,
)

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "dry_run_shopify_publish"

SAMPLE_WANTED = (
    ("non-English printing", lambda p, v: p["language_id"] != "EN"),
    ("foil", lambda p, v: v["finish_id"] == "FO"),
    ("etched foil", lambda p, v: v["finish_id"] == "EF"),
    ("suffixed collector number",
     lambda p, v: not p["collector_number"].isdigit()),
    ("consigned card", lambda p, v: v.get("is_consignment")),
    ("price below the floor after 3%",
     lambda p, v: v["price"]["half_up_cents"] < PRICING_FLOOR_CENTS),
    ("fractional cent", lambda p, v: v["price"]["is_fractional"]),
    ("rounding rules disagree", lambda p, v: v["price"]["rules_disagree"]),
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
    included. It is annotated only so the preview can show one, which is
    what was asked for.
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


def print_report(plan, prices, samples) -> None:
    print("SHOPIFY PUBLISH PREVIEW -- nothing written, nothing sent")
    print()
    print("WHAT WOULD BE CREATED")
    print(f"  products                      {plan['product_count']:,}")
    print(f"  variants                      {plan['variant_count']:,}")
    print(f"  physical cards behind them    {plan['card_count']:,}")
    print(f"  max variants on one product   {plan['max_variants_per_product']:,}")
    print()
    limits = plan["limits"]
    print("AGAINST THE STORE'S OWN LIMITS (read live 2026-11-01)")
    print(f"  options used                  {limits['options_used']} "
          f"of {MAX_PRODUCT_OPTIONS}   within limit: "
          f"{limits['within_option_limit']}")
    print(f"  variants per product          {plan['max_variants_per_product']} "
          f"of {MAX_PRODUCT_VARIANTS}   within limit: "
          f"{limits['within_variant_limit']}")
    print()
    print("CARDS NOT PUBLISHED, AND WHY")
    if not plan["excluded_counts"]:
        print("  (none)")
    for reason, count in plan["excluded_counts"].items():
        print(f"  {reason:<36} {count:,}")
    print()
    print("PRICE: THE 3% DISCOUNT, EXACTLY")
    print(f"  variants priced                       {prices['variants_total']:,}")
    print(f"  landing on a FRACTION of a cent       {prices['variants_fractional']:,}"
          f"  ({prices['units_fractional']:,} physical cards)")
    print(f"  where half-up and down DISAGREE       {prices['variants_where_rules_disagree']:,}")
    print(f"  exact total (fractional cents)        {prices['exact_total_cents']}c")
    print(f"  total if ROUND HALF UP                {_money(prices['half_up_total_cents'])}")
    print(f"  total if ROUND DOWN                   {_money(prices['down_total_cents'])}")
    print(f"  difference between the two rules      {_money(prices['half_up_minus_down_cents'])}")
    print()
    print(f"  variants whose cards DISAGREE on price {prices['variants_with_a_price_spread']:,}")
    print()
    print(f"  the shop's price floor                {prices['price_floor_cents']}c")
    print(f"  variants priced AT the floor          {prices['variants_at_the_floor']:,}")
    print(f"  variants BELOW it after the 3%        {prices['variants_below_floor_after_discount']:,}")
    print(f"  physical cards below it               {prices['units_below_floor_after_discount']:,}")
    if prices["variants_below_floor_after_discount"]:
        print("  !! 97% of a floor-priced card is under the floor by")
        print("     construction. Needs the operator's rule before any publish.")
    print()
    print(f"SAMPLE ROWS ({len(samples)})")
    for sample in samples:
        product, variant = sample["product"], sample["variant"]
        print(f"  [{sample['why']}]")
        print(f"     {product['title']}  {product['set_code_printed']} "
              f"{product['collector_number_printed']} {product['language_id']}")
        print(f"     sku            {variant['sku']}")
        print(f"     options        {product['option_names'][0]}="
              f"{variant['option_values'][product['option_names'][0]]}  "
              f"{product['option_names'][1]}="
              f"{variant['option_values'][product['option_names'][1]]}")
        print(f"     quantity       {variant['quantity']} "
              f"(cards {variant['inventory_card_ids'][:6]}"
              f"{' ...' if len(variant['inventory_card_ids']) > 6 else ''})")
        price = variant["price"]
        print(f"     mana pool      {_money(price['price_cents'])}")
        print(f"     shopify x0.97  {price['exact_cents']}c exact  ->  "
              f"half-up {_money(price['half_up_cents'])} / "
              f"down {_money(price['down_cents'])}")
        print(f"     consigned      {variant.get('is_consignment', False)}")
        print(f"     scryfall image {product['scryfall_id'] or 'NONE'}")
        print(f"     variants here  {len(product['variants'])}")
    print()
    print("NOTHING WAS WRITTEN. No Shopify request was made.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable plan instead of the report.")
    parser.add_argument("--samples", type=int, default=10,
                        help="How many sample rows to show (default 10).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    with Session(engine) as session:
        cards, batches_by_id, allocations = load_rows(session)
        logger.info("%s: read %s card(s), %s batch(es), %s allocation(s).",
                    SCRIPT_NAME, len(cards), len(batches_by_id), len(allocations))
        plan = build_publish_plan(cards, batches_by_id, allocations)
        plan = annotate_consignment(plan, cards, batches_by_id)
        prices = fractional_price_report(plan)
        samples = pick_samples(plan, limit=args.samples)
        # Whatever the reads did, this transaction ends having changed
        # nothing.
        session.rollback()

    if args.json:
        print(json.dumps(
            {"plan": plan, "prices": prices, "samples": samples},
            indent=2, sort_keys=True, default=str,
        ))
    else:
        print_report(plan, prices, samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
