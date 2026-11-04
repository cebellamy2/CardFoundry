"""What the Shopify quantity RAISE would push, without pushing it.

★ WHY THIS SCRIPT EXISTS. v2.103.0 made a release raise Shopify's
quantity, and a raise is the one direction that can advertise a card the
shop does not have. CLAUDE.md puts anything that changes what a cron
writes behind a preview at a hard stop, so this is the preview: it runs
the REAL push -- the real desired_quantities, the real binding lookup,
the real compare-and-swap payload against a real Shopify read -- and
replaces exactly one thing, the HTTP call that would send it.

★ IT IS A DRY RUN OF THE WRITE, NOT A DIFFERENT CODE PATH.
push_bindings(confirm=False) already stops at authorize_writes, but that
is not what the release path calls: the flush calls it with confirm=True,
and the thing worth previewing is what THAT builds. So the write
function itself is replaced by a recorder, which means authorize_writes
runs, the idempotency key is built, the chunking happens, and the exact
payload Shopify would receive is printed. Nothing reaches the store.

★ SHOPIFY READS ARE REAL, deliberately. The whole question is what
Shopify says it holds TODAY against what committed local state says, and
a fixture cannot answer that.

★ NOTHING LOCAL IS WRITTEN EITHER, and that is a property of the push
rather than of this script: the Shopify quantity push stamps nothing on
a binding and adds no row, unlike the Mana Pool one. Its session is
read-only and never committed.

Usage:

    # one identity, by SKU
    .venv/bin/python dry_run_shopify_quantity_raise.py \\
        --sku CF-MIR-157-EN-LP-NF

    # every identity a local order touched, by local order id
    .venv/bin/python dry_run_shopify_quantity_raise.py --order 5093

    # everything the reconciliation says we are UNDER-advertising
    .venv/bin/python dry_run_shopify_quantity_raise.py --under-advertised
"""
import argparse
import contextlib
import json
import logging
import sys

from sqlalchemy.orm import Session

import shopify_quantity_push_service as qty
import shopify_service
from database import engine
from models import InventoryCard, OrderItem, PickAllocation

SCRIPT_NAME = "dry_run_shopify_quantity_raise"


@contextlib.contextmanager
def capture_shopify_writes():
    """Replace the one outbound quantity write with a recorder.

    Yields the list it records into. Everything upstream of the HTTP call
    runs for real, which is the point: a preview that skipped
    authorize_writes would not be previewing the path that writes.
    """
    sent = []
    real = shopify_service.set_inventory_quantities

    def record(quantities, **kwargs):
        sent.append({
            "quantities": [dict(row) for row in quantities],
            "reference_document_uri": kwargs.get("reference_document_uri"),
            "idempotency_key": kwargs.get("idempotency_key"),
        })
        # The shape push_bindings expects from a clean write, so the
        # caller's own bookkeeping runs exactly as it would.
        return {"adjustment_group": {"createdAt": "dry-run"},
                "user_errors": [], "stale": []}

    shopify_service.set_inventory_quantities = record
    try:
        yield sent
    finally:
        shopify_service.set_inventory_quantities = real


def _skus_for_order(session: Session, order_id: int) -> list[str]:
    """Every Shopify identity this local order's cards belong to."""
    cards = (session.query(InventoryCard)
             .join(PickAllocation,
                   PickAllocation.inventory_card_id == InventoryCard.id)
             .join(OrderItem, OrderItem.id == PickAllocation.order_item_id)
             .filter(OrderItem.sales_order_id == order_id)
             .all())
    return qty.skus_for_cards(cards)


def _under_advertised_skus(session: Session) -> list[str]:
    """What the last reconciliation said Shopify advertises too LITTLE of.

    Read from the stored result rather than recomputed, so this names the
    rows the operator has already seen reported.
    """
    stored = qty.last_reconcile_result(session) or {}
    return sorted({row["sku"] for row in (stored.get("under") or [])
                   if row.get("sku")})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sku", action="append", default=[],
                        help="a Shopify SKU; repeatable")
    parser.add_argument("--order", type=int,
                        help="a LOCAL sales order id, whose cards' "
                             "identities are previewed")
    parser.add_argument("--under-advertised", action="store_true",
                        help="every SKU the last reconciliation reported as "
                             "under-advertised")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    with Session(engine) as session:
        gate = qty.sync_enabled(session)
        print(f"{SCRIPT_NAME}: shopify_qty_sync_enabled = {gate}")
        if not gate:
            print("\nThe push is OFF, so the real raise would do nothing at "
                  "all. This preview still runs, to show what it WOULD send "
                  "once the gate is on.")

        skus = list(args.sku)
        if args.order:
            skus.extend(_skus_for_order(session, args.order))
        if args.under_advertised:
            skus.extend(_under_advertised_skus(session))
        skus = sorted({sku for sku in skus if sku})
        if not skus:
            print("\nNothing to preview. Pass --sku, --order or "
                  "--under-advertised.")
            return 2

        print(f"\nidentities asked about: {len(skus)}")
        bindings = qty.bindings_for_skus(session, skus)
        by_sku = {b.sku: b for b in bindings}
        for sku in skus:
            binding = by_sku.get(sku)
            print(f"    {sku}: "
                  + ("no live Shopify binding" if binding is None else
                     f"binding {binding.id}, item "
                     f"{binding.shopify_inventory_item_gid}"))

        wanted = qty.desired_quantities(session, bindings)
        print("\nwhat COMMITTED local state says:")
        for sku in sorted(wanted):
            print(f"    {sku}: {wanted[sku]}")

    # ★ THE GATE IS FORCED ON FOR THE PREVIEW ONLY, in this process, and
    # only because the write it would reach is already a recorder. Without
    # this, a preview run while the gate is off would print nothing at all
    # and look like "no raise needed", which is the opposite of the truth.
    real_sync_enabled = qty.sync_enabled
    qty.sync_enabled = lambda session: True
    try:
        with capture_shopify_writes() as sent:
            result = qty.push_committed_for_skus(
                engine, skus, origin=f"{SCRIPT_NAME}")
    finally:
        qty.sync_enabled = real_sync_enabled

    print("\nwhat the raise WOULD send to Shopify:")
    if not sent:
        print("    nothing -- Shopify already agrees with committed local "
              "state, or no identity resolved to a binding.")
    for call in sent:
        print("   ", json.dumps(call, sort_keys=True))

    raises = result.get("raised") or []
    print(f"\nidentities it would RAISE: {len(raises)}")
    for row in raises:
        print(f"    {row['sku']}: {row['shopify_has']} -> "
              f"{row['local_says']}")
    if result.get("skipped"):
        print(f"\nskipped: {result['skipped']}")
    if result.get("failed"):
        print(f"\nFAILED: {result['failed']}")
    report = result.get("report") or {}
    for key in ("already_correct", "absent_from_shopify", "unbound",
                "errors", "stale_after_retry"):
        if report.get(key):
            print(f"    {key}: {report[key]}")
    print("\nNothing was sent to Shopify and nothing local was committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
