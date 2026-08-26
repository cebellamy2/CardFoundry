"""One-time backfill: queue the missed consignor payout for cards sold via
manual disposition before transition_manual_disposition() called
apply_consignment_payout_if_consigned().

Bug: transition_manual_disposition() (the "local sale"/"disposition
(other)" path) set status="sold" and sold_price but never resolved or
froze consignment_amount_owed -- unlike mark_shipped(), the real Mana
Pool sale path, which always has. Any consigned card sold manually before
the fix landed has a real sale price but no payout ever queued.

Targets exactly that gap: a consigned-batch card, status="sold", with a
disposition_type set (proving it went through the manual path, not
mark_shipped) and a sold_price, but consignment_amount_owed still null.
Reuses apply_consignment_payout_if_consigned() directly for the actual
write, matching the price-tiered resolution mark_shipped() and the fixed
transition_manual_disposition() both use -- no separate logic here.

Dry-run by default (report only). Pass --confirm to actually write.
"""

import argparse
import json

from sqlalchemy.orm import Session

from consignment_service import (
    apply_consignment_payout_if_consigned, get_consignment_tiers,
    resolve_consignment_payout,
)
from database import engine
from inventory_sync_service import inventory_sync_lease
from models import Batch, InventoryCard


def plan_backfill(session: Session) -> dict:
    tiers = get_consignment_tiers(session)
    rows = (
        session.query(InventoryCard, Batch.batch_code)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            Batch.is_consignment == True,
            InventoryCard.status == "sold",
            InventoryCard.disposition_type.isnot(None),
            InventoryCard.sold_price.isnot(None),
            InventoryCard.consignment_amount_owed.is_(None),
        )
        .all()
    )

    backfills = [
        {
            "card_id": card.id, "batch": batch_code, "name": card.name,
            "sold_price": card.sold_price,
            "disposition_type": card.disposition_type,
            "disposed_at": str(card.disposed_at),
            "resolved_owed": resolve_consignment_payout(tiers, card.sold_price),
        }
        for card, batch_code in rows
    ]

    return {
        "found": len(backfills),
        "backfills": backfills,
        "total_owed": round(sum(row["resolved_owed"] for row in backfills), 2),
    }


def apply_backfill(session: Session) -> dict:
    plan = plan_backfill(session)
    for row in plan["backfills"]:
        card = session.get(InventoryCard, row["card_id"])
        apply_consignment_payout_if_consigned(session, card)
    return plan


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
                    plan = apply_backfill(session)
                print(json.dumps({"mode": "CONFIRMED", **plan}, indent=2, sort_keys=True))
            else:
                plan = plan_backfill(session)
                print(json.dumps({"mode": "DRY_RUN", **plan}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
