"""Ticket A, step 1 (operator-approved 2026-09-12): refresh
SalesOrder.remote_fulfillment_status for the orders whose stored value is
not trustworthy, so the exception-resolution classification that follows
runs on real data rather than stale data.

Two populations, treated identically because neither stored value can be
believed until re-read from Mana Pool:

  * remote_fulfillment_status IS NULL -- never populated at all, so the
    reconciliation logic has nothing to classify.
  * remote_fulfillment_status == "processing" -- the operator checked
    Mana Pool directly on 2026-09-12 and confirmed ZERO orders are
    actually in "processing" there, while CardFoundry itself already has
    these same orders at status="shipped". That is stale local data, not
    a live divergence.

Scoped deliberately to orders that carry at least one UNRESOLVED
fulfillment exception -- those are the only ones whose status blocks
Ticket A. This is not a general re-sync of every order in the database.

Uses the EXISTING per-order mechanism (manapool_service.get_seller_order,
the same call the Resolve route already makes) -- no new sync path. Calls
are paced with the same shared _RequestPacer and interval order ingestion
uses, so 33 sequential reads stay well clear of the account-wide Mana Pool
rate limit that bit this app on 2026-09-10.

Writes nothing but remote_fulfillment_status and last_synced_at, and only
for orders where Mana Pool actually returned a status. Mirrors the Resolve
route's own `or order.remote_fulfillment_status` fallback: a missing
status leaves the stored value alone rather than blanking it.

Dry-run by default (report only). Pass --confirm to actually write.
"""

import argparse
from collections import Counter
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from competitor_pricing_service import _RequestPacer
from database import engine
from manapool_service import get_seller_order
from models import FulfillmentException, SalesOrder
from order_service import ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS

STALE_STATUS = "processing"


def orders_needing_resync(session: Session) -> list[SalesOrder]:
    """Orders carrying >=1 unresolved exception whose stored remote status
    is either NULL or the stale "processing" value."""
    return (
        session.query(SalesOrder)
        .join(FulfillmentException, FulfillmentException.sales_order_id == SalesOrder.id)
        .filter(
            FulfillmentException.inventory_resolution_state == "unresolved",
            (SalesOrder.remote_fulfillment_status.is_(None))
            | (SalesOrder.remote_fulfillment_status == STALE_STATUS),
        )
        .distinct()
        .order_by(SalesOrder.id)
        .all()
    )


def resync(confirm: bool) -> dict:
    pacer = _RequestPacer(ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS)
    transitions: Counter = Counter()
    failures: list[str] = []
    unchanged = 0
    changed = 0

    with Session(engine) as session:
        orders = orders_needing_resync(session)
        print(f"Orders needing resync: {len(orders)}")
        print(f"  stored NULL:         {sum(1 for o in orders if o.remote_fulfillment_status is None)}")
        print(f"  stored 'processing': {sum(1 for o in orders if o.remote_fulfillment_status == STALE_STATUS)}")
        print(f"Mode: {'WRITE (--confirm)' if confirm else 'DRY RUN (report only)'}")
        print()

        for order in orders:
            previous = order.remote_fulfillment_status
            pacer.wait()
            try:
                response = get_seller_order(order.external_order_id)
            except (httpx.HTTPError, RuntimeError) as exc:
                failures.append(f"order {order.id} ({order.external_order_id}): {exc}")
                print(f"  order {order.id}: FETCH FAILED -- {type(exc).__name__}: {exc}")
                continue
            detail = response.get("order") or response
            fetched = detail.get("latest_fulfillment_status")
            resolved = fetched or previous
            transitions[(previous or "(null)", resolved or "(null)")] += 1
            if resolved != previous:
                changed += 1
            else:
                unchanged += 1
            print(
                f"  order {order.id}: {previous or '(null)'} -> {resolved or '(null)'}"
                + ("" if fetched else "   [Mana Pool returned no status; stored value kept]")
            )
            if confirm and fetched:
                order.remote_fulfillment_status = fetched
                order.last_synced_at = datetime.now()

        if confirm:
            session.commit()

    print()
    print("=== transitions (stored -> actual) ===")
    for (before, after), count in sorted(transitions.items()):
        print(f"  {before:<14} -> {after:<14} {count}")
    print()
    print(f"changed: {changed}   unchanged: {unchanged}   fetch failures: {len(failures)}")
    for failure in failures:
        print(f"  FAILED {failure}")
    if not confirm:
        print("\nDRY RUN -- nothing was written. Re-run with --confirm to apply.")
    return {"changed": changed, "unchanged": unchanged, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually write (default is dry run)")
    args = parser.parse_args()
    resync(args.confirm)


if __name__ == "__main__":
    main()
