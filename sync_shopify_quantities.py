"""Reconcile and push Shopify quantities. Dry run by default.

★ WHAT THIS IS FOR. The live push (behind SHOPIFY_QTY_SYNC_ENABLED) keeps
Shopify in step with local stock from the moment a card sells. This
script is the two things that flag cannot do: the READ-ONLY report that
says whether the channels currently agree, and the one-time CATCH-UP
that fixes the drift that accumulated before the flag was ever on.

★ BOTH GO THROUGH THE SAME PUSH CODE as a sale does --
shopify_quantity_push_service.push_bindings -- so a catch-up cannot
succeed in a way a sale would not, and a dry run cannot pass in a way the
apply would not.

Usage, in the container:
    ... sync_shopify_quantities.py --reconcile
    ... sync_shopify_quantities.py                      # dry-run catch-up
    ... sync_shopify_quantities.py --canary
    ... sync_shopify_quantities.py --canary --confirm
    ... sync_shopify_quantities.py --confirm
    ... sync_shopify_quantities.py --show-setting
    ... sync_shopify_quantities.py --set-sync on --confirm
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from shopify_publish_service import publish_gate
from shopify_quantity_push_service import (
    SETTING_KEY,
    live_bindings,
    push_bindings,
    reconcile,
    set_sync_enabled,
    sync_enabled,
)

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "sync_shopify_quantities"
CANARY_SIZE = 5


def pick_canary(report, limit=CANARY_SIZE):
    """Five variants that would actually change, worst drift first.

    ★ WORST FIRST, NOT FIRST FIVE. The dangerous direction is Shopify
    advertising MORE than the shop holds -- that is the oversell -- so
    the canary leads with the biggest of those. A canary of five rows
    that happened to be alphabetically early would prove the mechanism
    and miss the risk.
    """
    rows = list(report["would_change"])
    rows.sort(key=lambda row: (row["local_says"] - row["shopify_has"],
                               row["sku"]))
    return rows[:limit]


def print_reconcile(result) -> None:
    if result.get("stopped_because"):
        print(f"STOPPED: {result['stopped_because']}")
        return
    print("SHOPIFY QUANTITY RECONCILIATION -- READ-ONLY")
    print(f"  location                      {result['location_gid']}")
    print(f"  bound variants                {result['bindings']:,}")
    print(f"  MATCHED local state           {len(result['matched']):,}")
    print(f"  DIFFERENT                     {len(result['different']):,}")
    print(f"  ... advertising MORE than we hold "
          f"{len(result['advertising_more_than_we_hold']):,}   <- the oversell")
    print(f"  absent from Shopify           {len(result['absent_from_shopify']):,}")
    print(f"  unbound (no inventory item)   {len(result['unbound']):,}")
    print()
    if result["advertising_more_than_we_hold"]:
        print("  WORST OFFENDERS (Shopify has more than local says)")
        worst = sorted(result["advertising_more_than_we_hold"],
                       key=lambda r: r["shopify_has"] - r["local_says"],
                       reverse=True)
        for row in worst[:10]:
            print(f"    {row['sku']:<34} shopify {row['shopify_has']} "
                  f"vs local {row['local_says']}  ({row['product_status']})")
        print()
    under = [row for row in result["different"]
             if row["local_says"] > row["shopify_has"]]
    if under:
        print(f"  under-advertised (we hold more than Shopify shows) {len(under):,}")
        for row in under[:5]:
            print(f"    {row['sku']:<34} shopify {row['shopify_has']} "
                  f"vs local {row['local_says']}")
        print()


def print_push(report, *, canary=None) -> None:
    confirmed = report["confirmed"]
    print("SHOPIFY QUANTITY PUSH --", "APPLIED" if confirmed else "DRY RUN")
    if not confirmed:
        print("  no mutation was sent, and nothing was committed")
    print(f"  run id                        {report['run_id']}")
    print(f"  bound variants considered     {report['bindings']:,}")
    print(f"  already correct               {len(report['already_correct']):,}")
    print(f"  WOULD CHANGE" if not confirmed else "  CHANGED",
          f"                {len(report['would_change']):,}")
    print(f"  absent from Shopify           {len(report['absent_from_shopify']):,}")
    print(f"  unbound (no inventory item)   {len(report['unbound']):,}")
    print(f"  inventorySetQuantities calls  {report['calls']:,}")
    print(f"  errors                        {len(report['errors']):,}")
    print(f"  still stale after a re-read   {len(report['stale_after_retry']):,}")
    print()
    if canary:
        print(f"CANARY ({len(canary)}) -- worst drift first")
        for row in canary:
            print(f"  {row['sku']:<34} shopify {row['shopify_has']} "
                  f"-> {row['local_says']}")
        print()
    shown = report["would_change"][:15]
    if shown and not canary:
        print("SAMPLE OF THE CHANGES")
        for row in shown:
            print(f"  {row['sku']:<34} shopify {row['shopify_has']} "
                  f"-> {row['local_says']}")
        print()
    for row in report["errors"][:10]:
        print(f"  !! {row}")
    if report["stale_after_retry"]:
        print("  !! still stale after a re-read -- something else is writing")
        print(f"     these quantities: {report['stale_after_retry'][:10]}")
    if report["stopped_because"]:
        print(f"STOPPED: {report['stopped_because']}")
    if not confirmed:
        print("NOTHING WAS WRITTEN TO SHOPIFY AND NOTHING WAS COMMITTED.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="Actually write. Without it, nothing is sent "
                             "and nothing is committed.")
    parser.add_argument("--reconcile", action="store_true",
                        help="Read-only: compare every bound variant's "
                             "Shopify quantity to local state.")
    parser.add_argument("--canary", action="store_true",
                        help=f"Push only {CANARY_SIZE} variants, worst "
                             f"drift first.")
    parser.add_argument("--limit", type=int,
                        help="Push at most this many variants.")
    parser.add_argument("--show-setting", action="store_true",
                        help=f"Print whether {SETTING_KEY} is on.")
    parser.add_argument("--set-sync", choices=("on", "off"),
                        help=f"Turn {SETTING_KEY} on or off (audited). "
                             f"Needs --confirm.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output instead of the report.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    if args.show_setting:
        with Session(engine) as session:
            print(f"{SETTING_KEY} = "
                  f"{'ON' if sync_enabled(session) else 'OFF'}")
            session.rollback()
        return 0

    if args.set_sync:
        with Session(engine) as session:
            result = set_sync_enabled(session, args.set_sync == "on")
            if args.confirm:
                session.commit()
            else:
                session.rollback()
        print(json.dumps(result, indent=2, sort_keys=True))
        if not args.confirm:
            print("\nDRY RUN -- nothing was committed. Re-run with --confirm.")
        return 0

    gate = publish_gate(read_back=True)
    print("THE GATE")
    print(f"  store domain                  {gate['store_domain']}")
    print(f"  on the write allow-list       {gate['allow_listed']}")
    print(f"  location                      {gate['location_gid']}  "
          f"({gate['location_name']})")
    print(f"  may write                     {gate['may_write']}")
    for reason in gate["refusals"]:
        print(f"    refusal: {reason}")
    print()
    with Session(engine) as session:
        print(f"  {SETTING_KEY} = "
              f"{'ON' if sync_enabled(session) else 'OFF'}")
        print()
        if args.reconcile:
            result = reconcile(session)
            session.rollback()
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True,
                                 default=str))
            else:
                print_reconcile(result)
            return 0

        bindings = live_bindings(session)
        if args.limit:
            bindings = bindings[:args.limit]
        # ★ THE CANARY GOES THROUGH A DRY RUN FIRST to learn which
        # variants would change at all -- there is no point sending five
        # that already match.
        canary = None
        if args.canary:
            preview = push_bindings(session, bindings, confirm=False)
            session.rollback()
            canary = pick_canary(preview)
            chosen = {row["sku"] for row in canary}
            bindings = [b for b in bindings if b.sku in chosen]
        report = push_bindings(session, bindings, confirm=args.confirm)
        # ★ THE ONLY COMMIT, AND IT IS BEHIND --confirm. push_bindings
        # writes no local row today, so this is belt-and-braces -- and
        # the brace is what keeps it true if it ever does.
        if args.confirm and not report["stopped_because"]:
            session.commit()
        else:
            session.rollback()

    if args.json:
        print(json.dumps({"report": report, "canary": canary}, indent=2,
                         sort_keys=True, default=str))
    else:
        print_push(report, canary=canary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
