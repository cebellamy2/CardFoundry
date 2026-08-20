"""One-time fix: link CardFoundry's existing CON_* batches to real Consignor
records and backfill payout tracking for cards that already sold before the
link existed.

Every CON_* batch was created with that naming convention manually, well
before the Phase 1-3 consignment payout system existed -- none of them ever
had Consignor.is_consignment set or a consignor_id assigned. That means
apply_consignment_payout_if_consigned() was a silent no-op for every one of
their sales to date: real cards sold, real money owed, zero payout tracking
recorded. This script fixes the flag/link going forward and retroactively
computes consignment_amount_owed for cards that already sold, using each
card's own real sold_price (already on the row -- no external lookup
needed) and CardFoundry's current tier table.

Dry-run by default (prints a report, writes nothing). Pass --confirm to
actually commit.
"""

import argparse
import json

from sqlalchemy.orm import Session

from consignment_service import get_consignment_tiers, resolve_consignment_payout
from database import engine
from inventory_sync_service import inventory_sync_lease
from models import Batch, Consignor, InventoryCard


# (consignor name, batch code) -- CON_CAM_ROC and CON_RAU are deliberately
# excluded per the operator's explicit direction.
CONSIGNOR_BATCHES = [
    ("Connor", "CON_CON"),
    ("Kevin", "CON_KEV1"),
    ("Luca", "CON_LUC"),
    ("Nick", "CON_NIC"),
    ("Patrick", "CON_PAT"),
    ("Ransom", "CON_RAN"),
    ("Richard", "CON_RIC1"),
    ("Cameron", "CON_CAM"),
]


def plan_consignor_setup(session: Session) -> dict:
    """Build the full set of changes without writing anything."""
    consignors_to_create = []
    consignors_existing = []
    batches_to_link = []
    batches_missing = []
    batches_already_linked = []
    cards_to_backfill = []
    cards_already_tracked = []

    tiers = get_consignment_tiers(session)

    for name, batch_code in CONSIGNOR_BATCHES:
        consignor = session.query(Consignor).filter(Consignor.name == name).first()
        if consignor:
            consignors_existing.append(name)
        else:
            consignors_to_create.append(name)

        batch = session.query(Batch).filter(Batch.batch_code == batch_code).first()
        if not batch:
            batches_missing.append(batch_code)
            continue

        if batch.is_consignment and batch.consignor_id:
            batches_already_linked.append(batch_code)
        else:
            batches_to_link.append(batch_code)

        sold_cards = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.batch_id == batch.id,
                InventoryCard.status == "sold",
                InventoryCard.sold_price.isnot(None),
            )
            .all()
        )
        for card in sold_cards:
            if card.consignment_amount_owed is not None:
                cards_already_tracked.append({
                    "batch": batch_code, "card_id": card.id, "name": card.name,
                })
                continue
            owed = resolve_consignment_payout(tiers, card.sold_price)
            cards_to_backfill.append({
                "batch": batch_code, "card_id": card.id, "name": card.name,
                "sold_price": card.sold_price, "computed_owed": owed,
            })

    return {
        "consignors_to_create": consignors_to_create,
        "consignors_existing": consignors_existing,
        "batches_to_link": batches_to_link,
        "batches_already_linked": batches_already_linked,
        "batches_missing": batches_missing,
        "cards_to_backfill": cards_to_backfill,
        "cards_already_tracked": cards_already_tracked,
        "cards_to_backfill_total_owed": round(
            sum(row["computed_owed"] for row in cards_to_backfill), 2,
        ),
    }


def apply_consignor_setup(session: Session) -> dict:
    """Execute the plan for real. Caller is responsible for commit()."""
    plan = plan_consignor_setup(session)
    tiers = get_consignment_tiers(session)

    consignor_by_name = {
        c.name: c for c in session.query(Consignor).filter(
            Consignor.name.in_([name for name, _ in CONSIGNOR_BATCHES]),
        )
    }
    for name in plan["consignors_to_create"]:
        consignor = Consignor(name=name)
        session.add(consignor)
        session.flush()
        consignor_by_name[name] = consignor

    for name, batch_code in CONSIGNOR_BATCHES:
        if batch_code in plan["batches_missing"]:
            continue
        batch = session.query(Batch).filter(Batch.batch_code == batch_code).first()
        batch.is_consignment = True
        batch.consignor_id = consignor_by_name[name].id

    for row in plan["cards_to_backfill"]:
        card = session.get(InventoryCard, row["card_id"])
        card.consignment_amount_owed = resolve_consignment_payout(tiers, card.sold_price)
        card.consignment_payout_status = "owed"

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
                    plan = apply_consignor_setup(session)
                print(json.dumps({"mode": "CONFIRMED", **plan}, indent=2, sort_keys=True))
            else:
                plan = plan_consignor_setup(session)
                print(json.dumps({"mode": "DRY_RUN", **plan}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
