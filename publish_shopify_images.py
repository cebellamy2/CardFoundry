"""Attach one Scryfall image to each published Shopify product.

Dry run by default; --confirm to write. The same program either way: the
dry run builds the same JSONL, drives the same media and audit writes,
and stops where shopify_service.authorize_writes refuses.

★ WHY A SEPARATE SCRIPT FROM publish_shopify.py. They are different runs
with different failure modes and different undo units. A publish creates
products and variants and is gated on an approved, shrink-only preview;
an image run touches nothing a shopper can buy, has nothing to shrink,
and is safe to re-run precisely because it skips anything that already
has media. One script that did both would need two approval shapes and
two run ids, which is two programs wearing one name.

★ IT IS SAFE TO RUN AFTER EVERY PUBLISH, and that is how future creates
get their pictures: a product that already has media is skipped, so a
second run only ever fills in what the last publish added.

★ THE CANARY IS PART OF THE PROCEDURE, NOT A DEBUG AID. --canary picks
five products that between them cover the shapes most likely to be
wrong -- a non-English printing, a product with a foil variant, a
suffixed collector number, a double-faced card (where "which image" is a
real question and the answer is the front face), and one ordinary card --
and sends only those. Read them back before the other six thousand.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python publish_shopify_images.py
    ... publish_shopify_images.py --canary
    ... publish_shopify_images.py --canary --confirm
    ... publish_shopify_images.py --confirm
    ... publish_shopify_images.py --verify-sample 1000
    ... publish_shopify_images.py --undo shopify-images-20261102T200000
    ... publish_shopify_images.py --resume gid://shopify/BulkOperation/123
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from publish_shopify import load_rows
from shopify_binding_service import live_product_gids
from shopify_mapping_service import build_publish_plan
from shopify_media_service import (
    SCRYFALL_IMAGE_SIZE,
    attach_images,
    build_image_plan,
    media_counts,
    resume_image_run,
    scryfall_image_urls,
    undo_image_run,
    verify_images,
)

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "publish_shopify_images"

CANARY_SIZE = 5

# One canary per shape, in the order they are tried. The label is printed,
# because a canary nobody can tell the point of proves nothing.
CANARY_WANTED = (
    ("non-English printing", lambda row: row["language_id"] != "EN"),
    ("double-faced card (front face)", lambda row: row["double_faced"]),
    ("suffixed collector number",
     lambda row: not row["collector_number"].isdigit()),
    ("has a foil variant", lambda row: row["has_foil"]),
    ("ordinary card", lambda row: True),
)


def pick_canary(rows, limit=CANARY_SIZE):
    """Up to `limit` rows covering the awkward shapes, each labelled."""
    picked = []
    seen = set()
    for label, predicate in CANARY_WANTED:
        if len(picked) >= limit:
            break
        for row in rows:
            if row["handle"] in seen:
                continue
            try:
                if not predicate(row):
                    continue
            except (KeyError, TypeError):
                continue
            seen.add(row["handle"])
            picked.append({**row, "why": label})
            break
    # Backfill so a canary is never short of its five just because the
    # catalogue happens to lack one shape.
    for row in rows:
        if len(picked) >= limit:
            break
        if row["handle"] in seen:
            continue
        seen.add(row["handle"])
        picked.append({**row, "why": "ordinary card"})
    return picked[:limit]


def build(session, *, only_handles=None, limit=None, read_media=True):
    """The image plan, from the live store and the live database.

    `read_media=False` skips the store walk, for the one case where it is
    pointless: an --undo, which touches nothing remote.
    """
    cards, batches_by_id, allocations = load_rows(session)
    logger.info("%s: read %s card(s), %s batch(es), %s allocation(s).",
                SCRIPT_NAME, len(cards), len(batches_by_id), len(allocations))
    plan = build_publish_plan(cards, batches_by_id, allocations)
    skus = [v["sku"] for p in plan["products"] for v in p["variants"]]
    product_gids = live_product_gids(session, skus)
    media = (media_counts() if read_media
             else {"counts": {}, "handles": {}, "pages": 0, "complete": True})
    scryfall_ids = sorted({p["scryfall_id"] for p in plan["products"]
                           if p.get("scryfall_id")})
    images = scryfall_image_urls(scryfall_ids)
    image_plan = build_image_plan(
        session, plan,
        media_counts=media["counts"],
        product_gids=product_gids,
        image_urls=images["urls"],
        scryfall_not_found=images["not_found"],
        scryfall_no_image=images["no_image"],
        scryfall_double_faced=images["double_faced"],
        only_handles=only_handles,
        limit=limit,
    )
    return plan, image_plan, media, images


def print_report(report, image_plan, media, images, *, canary=None) -> None:
    confirmed = report["confirmed"]
    print("SHOPIFY IMAGES --", "APPLIED" if confirmed else "DRY RUN")
    if not confirmed:
        print("  no mutation was sent, and nothing was committed")
    print(f"  image run id                  {report['image_run_id']}")
    print()

    gate = report.get("gate") or {}
    if gate:
        print("THE GATE")
        print(f"  store domain                  {gate['store_domain']}")
        print(f"  on the write allow-list       {gate['allow_listed']}")
        print(f"  may write                     {gate['may_write']}")
        for reason in gate["refusals"]:
            print(f"    refusal: {reason}")
        print()

    print("WHAT SHOPIFY ALREADY HOLDS")
    print(f"  products read                 {len(media['counts']):,} "
          f"in {media['pages']} page(s)  complete={media['complete']}")
    print(f"  already holding media         "
          f"{sum(1 for v in media['counts'].values() if v > 0):,}")
    print()

    print("SCRYFALL")
    print(f"  image size used               {SCRYFALL_IMAGE_SIZE}")
    print(f"  printings resolved            {len(images['urls']):,}")
    print(f"  not found by Scryfall         {len(images['not_found']):,}")
    print(f"  no image at that size         {len(images['no_image']):,}")
    print(f"  double-faced (front face)     {len(images['double_faced']):,}")
    print()

    print("WHAT WOULD GET AN IMAGE")
    print(f"  products to send              {len(image_plan['sendable']):,}")
    for reason, count in image_plan["skipped_counts"].items():
        print(f"  skipped: {reason:<28}{count:,}")
    if image_plan["truncated_by_limit"]:
        print(f"  held back by the limit        "
              f"{image_plan['truncated_by_limit']:,}")
    print()

    preview = report.get("preview") or {}
    if preview:
        print("WHAT THE BULK IMPORT WOULD SEND")
        print(f"  JSONL lines                   {preview['lines']:,}")
        print(f"  JSONL size                    {preview['jsonl_bytes']:,} "
              f"bytes of {preview['max_jsonl_bytes']:,} "
              f"({preview['within_file_size_limit']})")
        calls = preview["api_calls"]
        print(f"  stagedUploadsCreate           {calls['stagedUploadsCreate']}")
        print(f"  bulkOperationRunMutation      {calls['bulkOperationRunMutation']}")
        print(f"  status polls, at most         {calls['bulk_operation_polls_at_most']}")
        print(f"  verification reads            {calls['verification_reads']}")
        print(f"  GRAPHQL CALLS, AT MOST        {calls['total_graphql_at_most']}")
        print()

    if canary:
        print(f"CANARY ({len(canary)})")
        for row in canary:
            print(f"  [{row['why']}]")
            print(f"     title        {row['title']}")
            print(f"     handle       {row['handle']}")
            print(f"     product      {row['product_gid']}")
            print(f"     scryfall     {row['scryfall_id']}"
                  f"{'  DOUBLE-FACED -> front face' if row['double_faced'] else ''}")
            print(f"     image        {row['image_url']}")
            print(f"     alt          {row['alt']}")
        print()

    results = report.get("results")
    if results:
        print("RESULTS" + ("" if confirmed else " (simulated -- see --confirm)"))
        print(f"  lines sent                    {results['sent_lines']:,}")
        print(f"  result lines                  {results['result_lines']:,}")
        print(f"  products confirmed            {results['confirmed_products']:,}")
        print(f"  line errors                   {results['line_errors']:,}")
        print(f"  products never mentioned      {results['unanswered_product_gids']:,}")
        print(f"  MORE THAN ONE media returned  {results['more_than_one_media']:,}")
        print(f"  UNCLASSIFIED result lines     {results['unclassified']:,}")
        for row in results["error_examples"]:
            print(f"    !! {row}")
        for row in results["multiple_examples"]:
            print(f"    !! {row}")
        for row in results["unclassified_examples"]:
            print(f"    ?? {row}")
        print()

    if report.get("media"):
        print("LOCAL ROWS")
        print(f"  media rows written            "
              f"{len(report['media']['media_written']):,}"
              f"{'' if confirmed else '  (rolled back)'}")
        print()

    if report.get("bulk_operation"):
        print("BULK OPERATION")
        for key, value in report["bulk_operation"].items():
            if key == "url":
                value = "(present)" if value else "(none)"
            print(f"  {key:<29}{value}")
        print()

    verification = report.get("verification")
    if verification:
        print_verification(verification)

    if report.get("uncertain"):
        print("A WRITE WAS UNCERTAIN -- NOTHING WAS RETRIED")
        for key, value in report["uncertain"].items():
            print(f"  {key:<29}{value}")
        print()

    if report.get("stopped_because"):
        print(f"STOPPED: {report['stopped_because']}")
    if not confirmed:
        print("NOTHING WAS WRITTEN TO SHOPIFY AND NOTHING WAS COMMITTED.")


def print_verification(verification) -> None:
    print("IMAGES READ BACK FROM SHOPIFY")
    print(f"  products checked              {verification['checked']:,}")
    print(f"  with EXACTLY ONE image        {len(verification['exactly_one']):,}")
    print(f"  with NONE                     {len(verification['none_at_all']):,}")
    print(f"  with MORE THAN ONE            {len(verification['more_than_one']):,}")
    print(f"  absent from Shopify           {len(verification['absent_from_shopify']):,}")
    print(f"  media READY                   {verification['media_ready']:,}")
    print(f"  media still processing        {verification['media_pending']:,}")
    print(f"  media FAILED                  {verification['media_failed']:,}")
    for row in verification["more_than_one"][:10]:
        print(f"    !! {row}")
    for gid in verification["none_at_all"][:10]:
        print(f"    !! no image: {gid}")
    print()


def run_undo(args) -> int:
    with Session(engine) as session:
        result = undo_image_run(session, args.undo, confirm=args.confirm)
        if args.confirm:
            session.commit()
        else:
            session.rollback()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if not args.confirm:
        print("\nDRY RUN -- nothing was committed. Re-run with --confirm.")
    return 0


def run_verify(args) -> int:
    """Read a sample of the products this app has attached images to, and
    say whether each still holds exactly one. Reads only."""
    from models import ShopifyProductMedia

    with Session(engine) as session:
        rows = (session.query(ShopifyProductMedia)
                .filter(ShopifyProductMedia.media_state == "attached")
                .order_by(ShopifyProductMedia.id).all())
        gids = [row.shopify_product_gid for row in rows]
        session.rollback()
    step = max(1, len(gids) // args.verify_sample) if args.verify_sample else 1
    sample = gids[::step][:args.verify_sample]
    verification = verify_images(sample)
    print(f"SHOPIFY IMAGES -- READ-BACK ONLY ({len(gids):,} attached locally)")
    print()
    print_verification(verification)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="Actually write. Without it, nothing is sent "
                             "and nothing is committed.")
    parser.add_argument("--canary", action="store_true",
                        help=f"Send only {CANARY_SIZE} products, chosen to "
                             f"cover the awkward shapes.")
    parser.add_argument("--limit", type=int,
                        help="Send at most this many products.")
    parser.add_argument("--handles",
                        help="Comma-separated handles; send only these.")
    parser.add_argument("--verify-sample", type=int, metavar="N",
                        help="Read N attached products back and report "
                             "whether each holds exactly one image. "
                             "Reads only; writes nothing.")
    parser.add_argument("--undo", metavar="RUN_ID",
                        help="Give up an image run's LOCAL claims. Dry by "
                             "default; add --confirm to apply.")
    parser.add_argument("--resume", metavar="BULK_OPERATION_ID",
                        help="Record a FINISHED bulk operation's results "
                             "without re-sending anything.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output instead of the report.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    if args.undo:
        return run_undo(args)
    if args.verify_sample:
        return run_verify(args)

    only_handles = None
    if args.handles:
        only_handles = {h.strip() for h in args.handles.split(",") if h.strip()}

    with Session(engine) as session:
        plan, image_plan, media, images = build(
            session, only_handles=only_handles, limit=args.limit)
        canary = None
        if args.canary:
            canary = pick_canary(image_plan["sendable"])
            chosen = {row["handle"] for row in canary}
            image_plan["sendable"] = [row for row in image_plan["sendable"]
                                      if row["handle"] in chosen]
        if args.resume:
            report = resume_image_run(session, image_plan, args.resume,
                                      confirm=args.confirm)
        else:
            report = attach_images(session, image_plan, confirm=args.confirm)
        # ★ THE ONLY COMMIT, AND IT IS BEHIND --confirm.
        if args.confirm and not report["stopped_because"]:
            session.commit()
        else:
            session.rollback()

    if args.json:
        print(json.dumps({"report": report, "image_plan": image_plan,
                          "canary": canary},
                         indent=2, sort_keys=True, default=str))
    else:
        print_report(report, image_plan, media, images, canary=canary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
