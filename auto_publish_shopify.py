"""The scheduled Shopify publish, run by hand. Dry by default.

★ ONE SCRIPT, SO THERE IS NO SECOND PATH. This is the same
shopify_auto_publish_service.run_auto_publish the Railway cron drives
over HTTP, and its dry run is this same program with the write refused:
same plan, same filter, same two reads, same shrink-only check, same
binding and audit rows, rolled back. CLAUDE.md: "A dry run runs the SAME
code path as the write and commits nothing internally."

★ NOTHING HERE CAN WRITE TO SHOPIFY WITHOUT BOTH --confirm AND THE GATE.
The refusals are not branches in this file. --confirm reaches
shopify_service.authorize_writes, which needs an explicit confirm and an
allow-listed store; the gate (shopify_auto_publish_enabled, default OFF,
no row = OFF) is checked by the service. A dry run with the gate OFF is
allowed on purpose -- it is exactly what the operator needs in order to
decide whether to turn it on.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python auto_publish_shopify.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python auto_publish_shopify.py \
        --show-setting
    cd /app && PYTHONPATH=/app /opt/venv/bin/python auto_publish_shopify.py \
        --set-gate on
    cd /app && PYTHONPATH=/app /opt/venv/bin/python auto_publish_shopify.py \
        --confirm
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from shopify_auto_publish_service import (
    MAX_NEW_PRODUCTS_PER_RUN,
    SETTING_KEY,
    auto_publish_enabled,
    run_auto_publish,
    set_auto_publish_enabled,
)

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "auto_publish_shopify"

SAMPLE_LIMIT = 10


def _money(cents) -> str:
    return f"${int(cents) / 100:,.2f}"


def samples(outcome, limit=SAMPLE_LIMIT) -> list:
    """Up to `limit` of the identities this run would publish, each
    labelled with whether the PRODUCT is new to Shopify or only the
    variant is.

    The distinction matters more than any other property of a sample
    here: a brand-new product is a create, while an existing product that
    has gained a variant is re-sent WHOLE, so its siblings' price and
    quantity are re-asserted too.
    """
    read = outcome["first_read"]
    unbound = read["unbound_skus_by_product"]
    rows = []
    for product in read["plan"]["products"]:
        missing = set(unbound.get((product["set_code"],
                                   product["collector_number"],
                                   product["language_id"])) or [])
        whole_product_is_new = len(missing) == len(product["variants"])
        for variant in product["variants"]:
            if variant["sku"] not in missing:
                continue
            rows.append({
                "why": ("new product" if whole_product_is_new
                        else "new variant on an existing product"),
                "title": product["title"],
                "handle": product["handle"],
                "sku": variant["sku"],
                "quantity": variant["quantity"],
                "price": variant["price"],
                "siblings_re_sent": [
                    v["sku"] for v in product["variants"]
                    if v["sku"] not in missing
                ],
                "scryfall_id": product["scryfall_id"],
            })
            if len(rows) >= limit:
                return rows
    return rows


def print_report(outcome, rows) -> None:
    confirmed = outcome["confirmed"]
    print("SHOPIFY AUTO-PUBLISH --", "APPLIED" if confirmed else "DRY RUN")
    if not confirmed:
        print("  no mutation was sent, and nothing was committed")
    print()

    print("THE GATE")
    print(f"  {SETTING_KEY:<29}"
          f"{'ON' if outcome['gate_enabled'] else 'OFF'}")
    print(f"  may this run write                 "
          f"{confirmed and outcome['gate_enabled']}")
    report = outcome["report"] or {}
    gate = report.get("gate") or {}
    if gate:
        print(f"  store domain                       {gate['store_domain']}")
        print(f"  on the write allow-list            {gate['allow_listed']}")
        print(f"  SHOPIFY_LOCATION_ID                {gate['location_gid']}")
        print(f"  that location, read back           {gate['location_name']} "
              f"(confirmed={gate['location_confirmed']})")
        print(f"  products created as                {gate['product_status']}")
        print(f"  may write                          {gate['may_write']}")
        for reason in gate.get("refusals") or []:
            print(f"    refusal: {reason}")
    print()

    read = outcome["first_read"]
    print("WHAT IS NOT ON SHOPIFY YET")
    print(f"  publishable products, all          "
          f"{read['products_considered']:,}")
    print(f"  publishable variants, all          "
          f"{read['variants_considered']:,}")
    print(f"  SKUs already bound                 {read['bound_skus']:,}")
    print(f"  products this run would send       "
          f"{read['plan']['product_count']:,}  "
          f"(cap {MAX_NEW_PRODUCTS_PER_RUN:,})")
    print(f"  variants in those products         "
          f"{read['plan']['variant_count']:,}")
    print(f"  of those, NEW to Shopify           {read['new_sku_count']:,}")
    print(f"  physical cards behind them         "
          f"{read['plan']['card_count']:,}")
    print(f"  handle collisions in this plan     "
          f"{len(read['plan']['handle_collisions'])}")
    for handle, dupes in list(read["plan"]["handle_collisions"].items())[:5]:
        print(f"    !! {handle}  {dupes}")
    print()

    print("CARDS THIS RUN WOULD REFUSE, AND WHY")
    print("  (the one eligibility rule, not a second one -- price_pending")
    print("   and no_price are the operator's 'not listed until priced')")
    if not read["excluded_counts"]:
        print("  (none)")
    for reason, count in read["excluded_counts"].items():
        print(f"  {reason:<36} {count:,}")
    print()

    second = outcome["second_read"]
    if second is not None:
        print("THE SECOND READ (what stands in for an approved preview)")
        print(f"  products, read two                 "
              f"{second['plan']['product_count']:,}")
        print(f"  new skus, read two                 {second['new_sku_count']:,}")
        # publish() RAISES on a non-subset rather than returning, so the
        # verdict is on the outcome in that case and in the report in
        # every other -- see run_auto_publish's except clause.
        shrink = report.get("shrink_only") or outcome.get("shrink_only")
        if shrink:
            print(f"  appeared between the reads         "
                  f"{len(shrink['appeared'])}   (blocking)")
            print(f"  quantity rose                      "
                  f"{len(shrink['quantity_grew'])}   (blocking)")
            print(f"  repriced                           "
                  f"{len(shrink['repriced'])}   (blocking)")
            print(f"  dropped out                        "
                  f"{len(shrink['vanished'])}   (allowed)")
            print(f"  quantity fell                      "
                  f"{len(shrink['quantity_shrank'])}   (allowed)")
            print(f"  is a subset                        {shrink['is_subset']}")
        print()

    print(f"SAMPLE ROWS ({len(rows)})")
    if not rows:
        print("  (none -- there is nothing to publish)")
    for row in rows:
        price = row["price"]
        print(f"  [{row['why']}]")
        print(f"     title          {row['title']}")
        print(f"     handle         {row['handle']}")
        print(f"     sku            {row['sku']}")
        print(f"     price          {_money(price['shopify_price_cents'])}"
              f"   (mana pool {_money(price['manapool_price_cents'])}"
              f" -> {price['exact_cents']}c exact"
              f" -> floor {price['floored_cents']}c"
              f"{' -> CLAMPED' if price['clamped_to_floor'] else ''})")
        print(f"     quantity       {row['quantity']}")
        print(f"     scryfall image {row['scryfall_id'] or 'NONE'}")
        if row["siblings_re_sent"]:
            print(f"     ALSO re-sent   {row['siblings_re_sent']}")
            print("                    (productSet replaces a product's "
                  "variant list, so")
            print("                     the whole product goes or its "
                  "siblings are deleted)")
    print()

    preview = report.get("preview")
    if preview:
        print("WHAT THE BULK IMPORT WOULD SEND")
        print(f"  JSONL lines (one per product)      {preview['lines']:,}")
        print(f"  JSONL size                         "
              f"{preview['jsonl_bytes']:,} bytes of "
              f"{preview['max_jsonl_bytes']:,} ({preview['within_file_size_limit']})")
        print(f"  inventory quantities               "
              f"{preview['inventory_quantities']:,}")
        print(f"  products identified by stored gid  "
              f"{preview['identified_by_gid']:,}")
        print(f"  products identified by handle      "
              f"{preview['identified_by_handle']:,}")
        print(f"  GRAPHQL CALLS, AT MOST             "
              f"{preview['api_calls']['total_graphql_at_most']}")
        print()

    results = report.get("results")
    if results:
        print("RESULTS" + ("" if confirmed else " (simulated -- see --confirm)"))
        print(f"  lines sent                         {results['sent_lines']:,}")
        print(f"  variants confirmed                 "
              f"{results['confirmed_variants']:,}")
        print(f"  line errors                        {results['line_errors']:,}")
        print(f"  skus Shopify never mentioned       "
              f"{results['unanswered_skus']:,}")
        print(f"  UNCLASSIFIED result lines          {results['unclassified']:,}")
        for raw in results["unclassified_examples"]:
            print(f"    ?? {raw}")
        for raw in results["error_examples"]:
            print(f"    !! {raw}")
        print()

    bindings = report.get("bindings")
    if bindings:
        print(f"  bindings written                   "
              f"{len(bindings['bindings_written']):,}"
              f"{'' if confirmed else '  (rolled back)'}")
    retire = report.get("retirements") or {}
    for key, skus in retire.items():
        print(f"  {key:<35}{len(skus):,}")
        for sku in skus[:5]:
            print(f"    -- {sku}")
    if retire:
        print()

    verify = report.get("verification")
    if verify:
        print("STOCK READ BACK FROM SHOPIFY")
        print(f"  checked                            {verify['checked']:,}")
        print(f"  matched what we sent               {len(verify['matched']):,}")
        print(f"  DIFFERENT                          "
              f"{len(verify['mismatched']):,}")
        print(f"  absent from Shopify                "
              f"{len(verify['absent_from_shopify']):,}")
        print()
    if report.get("uncertain"):
        print("A WRITE WAS UNCERTAIN -- NOTHING WAS RETRIED")
        for key, value in report["uncertain"].items():
            print(f"  {key:<35}{value}")
        print()

    if outcome["stopped_because"]:
        print(f"STOPPED: {outcome['stopped_because']}")
    if not confirmed:
        print("NOTHING WAS WRITTEN TO SHOPIFY AND NOTHING WAS COMMITTED.")


def show_setting() -> int:
    with Session(engine) as session:
        print(f"{SETTING_KEY} = "
              f"{'ON' if auto_publish_enabled(session) else 'OFF'}")
    return 0


def set_gate(value: str) -> int:
    wanted = value.strip().lower()
    if wanted not in ("on", "off"):
        print(f"--set-gate takes 'on' or 'off', not {value!r}.")
        return 2
    with Session(engine) as session:
        result = set_auto_publish_enabled(session, wanted == "on")
        session.commit()
    print(json.dumps({**result, "setting": SETTING_KEY}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="Actually publish. Without it, nothing is sent "
                             "and nothing is committed.")
    parser.add_argument("--show-setting", action="store_true",
                        help=f"Print {SETTING_KEY} and exit.")
    parser.add_argument("--set-gate", metavar="on|off",
                        help=f"Turn {SETTING_KEY} on or off, audited, and "
                             f"exit. There is no /admin button for this: the "
                             f"job writes to Shopify.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output instead of the report.")
    parser.add_argument("--samples", type=int, default=SAMPLE_LIMIT,
                        help=f"How many sample rows to show "
                             f"(default {SAMPLE_LIMIT}).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    if args.show_setting:
        return show_setting()
    if args.set_gate:
        return set_gate(args.set_gate)

    with Session(engine) as session:
        outcome = run_auto_publish(session, confirm=args.confirm)
        # ★ THE ONLY COMMIT IN THIS SCRIPT, AND IT IS BEHIND --confirm.
        # run_auto_publish never commits; whatever the dry run wrote --
        # binding rows against dry-run:// gids, audit rows -- ends here.
        if args.confirm and not outcome["stopped_because"]:
            session.commit()
        else:
            session.rollback()
        rows = samples(outcome, limit=args.samples)

    if args.json:
        print(json.dumps({"outcome": outcome, "samples": rows},
                         indent=2, sort_keys=True, default=str))
    else:
        print_report(outcome, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
