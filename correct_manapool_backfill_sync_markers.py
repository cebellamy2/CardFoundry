"""One-time correction: stamp mana_pool_shipment_synced_at on every order
backfill_manapool_order_history.py already inserted into production before
it was fixed to do this itself.

Real production bug: every one of the 3,673 historical orders that script
inserted with status="shipped" left mana_pool_shipment_synced_at null.
main.py's shipment-sync-stuck banner (_shipment_sync_stuck_query,
main.py:8577-8583) treats that as "CardFoundry marked this shipped but
never confirmed pushing the status to Mana Pool -- needs operator retry."
For these orders that's meaningless: they were fulfilled directly on Mana
Pool's own site, months before this backfill ever ran, never through
CardFoundry's own pack/ship flow -- there is no outbound push to retry.
The banner showed "3649 orders failed to sync to Mana Pool" in production
(3,673 imported minus the ones mapped to "cancelled", which the stuck
query's status=="shipped" filter never counts anyway).

Identification is exact, not a heuristic: only backfill_manapool_order_
history.py ever creates a SalesOrder with status=="shipped" and both
picked_at and packed_at still null (a real CardFoundry-fulfilled order
always has picked_at set by the time it reaches "shipped" -- confirmed via
a full call-site audit; order.status is only ever set to "shipped" from
order_service.mark_shipped or this backfill script, nowhere else).
Verified against production before writing this: all 3,649 currently
-flagged stuck orders match this signature; zero have picked_at or
packed_at set.

Dry-run by default (report only). Pass --confirm to actually write.
"""

import argparse
import json
from datetime import datetime

from sqlalchemy.orm import Session

from database import engine
from inventory_sync_service import inventory_sync_lease
from models import SalesOrder


def plan_correction(session: Session) -> dict:
    rows = (
        session.query(SalesOrder)
        .filter(
            SalesOrder.source == "manapool",
            SalesOrder.status == "shipped",
            SalesOrder.mana_pool_shipment_synced_at.is_(None),
            SalesOrder.mana_pool_shipment_released_at.is_(None),
            SalesOrder.picked_at.is_(None),
            SalesOrder.packed_at.is_(None),
        )
        .all()
    )
    return {
        "to_correct": [
            {"order_id": order.id, "external_order_id": order.external_order_id}
            for order in rows
        ],
    }


def apply_correction(session: Session) -> dict:
    plan = plan_correction(session)
    now = datetime.now()
    for row in plan["to_correct"]:
        order = session.get(SalesOrder, row["order_id"])
        order.mana_pool_shipment_synced_at = now
    return {"corrected": len(plan["to_correct"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually write changes. Default is a dry run (report only).",
    )
    args = parser.parse_args()

    with inventory_sync_lease():
        with Session(engine) as session:
            if args.confirm:
                with session.begin():
                    outcome = apply_correction(session)
                print(json.dumps({"mode": "CONFIRMED", **outcome}, indent=2, sort_keys=True))
            else:
                plan = plan_correction(session)
                print(json.dumps(
                    {"mode": "DRY_RUN", "to_correct_count": len(plan["to_correct"])},
                    indent=2, sort_keys=True,
                ))


if __name__ == "__main__":
    main()
