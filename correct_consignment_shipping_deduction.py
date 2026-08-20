"""One-time correction: re-resolve consignment_amount_owed for every still
-unpaid consignment card against the current tier table.

resolve_consignment_payout's tier table just changed: sales over $35.00 now
have a flat $5.50 shipping deduction applied (the operator's real practice
-- a >$35 sale is the one where CardFoundry itself absorbs a real shipping
cost, and that cost is passed through to the consignor's cut rather than
eaten). apply_consignment_payout_if_consigned() freezes the resolved amount
onto the card at ship time specifically so a later tier-table edit never
silently drifts an already-computed amount -- but that also means the
tier-table change just made needs a one-time pass to correct every card
that was resolved under the old table and hasn't been paid out yet. A
card already marked "paid" is left untouched -- that payout is historical
and settled, same rule as every other backfill in this project.

Dry-run by default (report only). Pass --confirm to actually write.
"""

import argparse
import json

from sqlalchemy.orm import Session

from consignment_service import get_consignment_tiers, resolve_consignment_payout
from database import engine
from inventory_sync_service import inventory_sync_lease
from models import Batch, InventoryCard


def plan_correction(session: Session) -> dict:
    tiers = get_consignment_tiers(session)
    rows = (
        session.query(InventoryCard, Batch.batch_code)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.consignment_payout_status == "owed",
            InventoryCard.consignment_amount_owed.isnot(None),
            InventoryCard.sold_price.isnot(None),
        )
        .all()
    )

    corrections = []
    unchanged = 0
    for card, batch_code in rows:
        new_owed = resolve_consignment_payout(tiers, card.sold_price)
        if new_owed == card.consignment_amount_owed:
            unchanged += 1
            continue
        corrections.append({
            "card_id": card.id, "batch": batch_code, "name": card.name,
            "sold_price": card.sold_price,
            "old_owed": card.consignment_amount_owed, "new_owed": new_owed,
        })

    return {
        "checked": len(rows), "unchanged": unchanged,
        "corrections": corrections,
        "total_delta": round(
            sum(c["new_owed"] - c["old_owed"] for c in corrections), 2,
        ),
    }


def apply_correction(session: Session) -> dict:
    plan = plan_correction(session)
    for row in plan["corrections"]:
        card = session.get(InventoryCard, row["card_id"])
        card.consignment_amount_owed = row["new_owed"]
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
                    plan = apply_correction(session)
                print(json.dumps({"mode": "CONFIRMED", **plan}, indent=2, sort_keys=True))
            else:
                plan = plan_correction(session)
                print(json.dumps({"mode": "DRY_RUN", **plan}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
