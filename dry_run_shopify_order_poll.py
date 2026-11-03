"""What one Shopify order-poll tick would do, through the REAL poll.

★ WHY IT RUNS AGAINST A COPY AND NOT IN A ROLLBACK.
shopify_order_service.ingest_shopify_orders commits each order itself --
that per-order isolation is the point of it -- so a rollback around the
poll would roll back nothing that mattered and the "dry run" would be an
apply with a reassuring name. CLAUDE.md's rule for exactly this shape:
run it against a copy. So this script runs the real
poll_shopify_orders, commits to whatever database it is pointed at, and
refuses to run until the caller says that database is disposable.

★ WHAT IT PROVES, AND IT IS THE WHOLE REASON FOR THE SCRIPT. Ticket 8
changed two things a cron WRITES: where the poll's cursor lands, and
whether a second sighting of a cancelled order releases it. Both are the
operator's call under CLAUDE.md, so the before/after of both is printed
here rather than described.

★ IT MAKES NO SHOPIFY WRITE, and checks rather than claims. The one path
from a poll tick to a Shopify write is the quantity push that an
allocation makes, which is gated on shopify_qty_sync_enabled. This
script READS that gate in the target database first and refuses to run
while it is ON -- a copy with the gate on would lower the live store's
quantities for orders this run ingested into a throwaway database.

Shopify READS are real and deliberate: the point is what Shopify
actually answers today, not what a fixture says it would.

Usage:

    DATABASE_URL=sqlite:////tmp/copy.db \\
        .venv/bin/python dry_run_shopify_order_poll.py --copy

    # and, to see the window without touching the copy at all:
    DATABASE_URL=sqlite:////tmp/copy.db \\
        .venv/bin/python dry_run_shopify_order_poll.py --window-only
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

import shopify_quantity_push_service
from actor_context import set_script_actor
from database import DATABASE_URL, engine
from models import SalesOrder
from shopify_order_poll_service import (
    _since, poll_cursor, poll_shopify_orders, read_orders_since,
)
from shopify_order_service import SOURCE, intake_enabled

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "dry_run_shopify_order_poll"


def _shopify_orders(session) -> list[SalesOrder]:
    return (session.query(SalesOrder)
            .filter(SalesOrder.source == SOURCE)
            .order_by(SalesOrder.id)
            .all())


def _describe(order: SalesOrder) -> dict:
    return {
        "id": order.id,
        "external_order_id": order.external_order_id,
        "label": order.external_label,
        "status": order.status,
        "shipping_cents": order.shipping_cents,
        "delivery_method": order.shopify_delivery_method,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--copy", action="store_true",
        help="I confirm DATABASE_URL points at a disposable COPY. The real "
             "poll runs and commits to it.")
    parser.add_argument(
        "--window-only", action="store_true",
        help="Read the cursor and the window and stop. Writes nothing, "
             "ingests nothing, and makes one Shopify read.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    set_script_actor(SCRIPT_NAME)

    print(f"database: {DATABASE_URL}")
    with Session(engine) as session:
        gate = shopify_quantity_push_service.sync_enabled(session)
        print(f"shopify_qty_sync_enabled (in THIS database): "
              f"{'ON' if gate else 'OFF'}")
        print(f"shopify_order_intake_enabled: "
              f"{'ON' if intake_enabled(session) else 'OFF'}")
        before = poll_cursor(session)
        since = _since(session)
        print("cursor before: "
              + (before.isoformat() if before else "NONE (first run)"))
        print(f"window starts: {since.isoformat()} "
              f"(the cursor minus the overlap, subtracted ONCE)")
        print("\nShopify orders in this database before the tick:")
        for row in _shopify_orders(session):
            print("   ", json.dumps(_describe(row), sort_keys=True))

        if args.window_only:
            read = read_orders_since(since)
            print(f"\nShopify would return {len(read['orders'])} order(s) "
                  f"updated since {read['since']} "
                  f"(complete={read['complete']}):")
            for node in read["orders"]:
                print("   ", json.dumps({
                    "id": node.get("id"), "name": node.get("name"),
                    "updatedAt": node.get("updatedAt"),
                    "cancelledAt": node.get("cancelledAt"),
                }, sort_keys=True))
            print("\nNothing was written. --window-only.")
            return 0

        if gate:
            print("\nREFUSED: shopify_qty_sync_enabled is ON in this "
                  "database. An order ingested here would push a quantity "
                  "to the LIVE store. Turn it off in the copy and re-run.")
            return 2
        if not args.copy:
            print("\nREFUSED: the real poll commits per order, so this only "
                  "runs against a copy. Pass --copy once DATABASE_URL points "
                  "at one.")
            return 2

        outcome = poll_shopify_orders(session)
        session.commit()

    print("\nwhat the tick did:")
    for key in ("skipped", "since", "complete", "read", "ingested",
                "already_known", "released", "refused", "failed",
                "collisions", "cursor_moved"):
        print(f"    {key}: {outcome.get(key)}")
    for key in ("cursor_before", "cursor_after"):
        value = outcome.get(key)
        print(f"    {key}: {value.isoformat() if value else None}")
    print(f"    delivery_methods: "
          f"{json.dumps(outcome.get('delivery_methods'), sort_keys=True)}")
    released = (outcome.get("detail") or {}).get("released") or []
    print(f"\nreleases this tick made: {len(released)}")
    for row in released:
        print("   ", json.dumps(row, sort_keys=True, default=str))

    with Session(engine) as session:
        print("\nShopify orders in this database AFTER the tick:")
        for row in _shopify_orders(session):
            print("   ", json.dumps(_describe(row), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
