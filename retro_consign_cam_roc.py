"""One-time retroactive correction: attribute batch CON_CAM_ROC to consignor
CameronRochelle.

CON_CAM_ROC was deliberately excluded from backfill_consignor_setup.py's
initial consignor-linking pass (see that script's CONSIGNOR_BATCHES
comment) -- it sat unassigned to any consignor while cards inside it
still sold as regular stock. The operator has now confirmed the whole
batch, sold and unsold cards alike, should be retroactively attributed to
CameronRochelle (an existing consignor -- this script never creates one).

The batch-edit UI that shipped this session (see BATCH_EDIT in
CHANGELOG.md) deliberately locks the consignor field once a batch has any
sold card -- correct for the general case, since a plain field flip would
leave the batch saying "consigned to CameronRochelle" while cards that
already sold under it as regular stock have no payout tracked at all.
This script does both halves of the correction together, the same shape
as backfill_consignor_setup.py:

1. Link the batch to CameronRochelle (is_consignment=True, consignor_id).
2. For every already-sold card in the batch, retroactively compute
   consignment_amount_owed from its real sold_price using the current
   price-tiered payout table -- resolve_consignment_payout already
   applies the $35/$5.50 shipping deduction on the top tier, see
   consignment_service.py's DEFAULT_CONSIGNMENT_TIERS -- and mark it
   owed-unpaid. Nothing has actually been paid to Cameron yet; this only
   makes the amount visible on /consignors/{id}/owed.

Refuses to run (dry or confirmed) if the batch is already linked to a
DIFFERENT consignor, or if any card in the batch already has a non-null
consignment_payout_id or consignment_amount_owed -- either would mean
this batch isn't the clean, untouched case it's assumed to be, and a
silent overwrite could erase or double-count real payout history.

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


CONSIGNOR_NAME = "CameronRochelle"
BATCH_CODE = "CON_CAM_ROC"


def plan_retro_consignment(session: Session) -> dict:
    """Build the full set of changes without writing anything. Raises
    ValueError if a precondition is violated -- callers should let that
    propagate rather than catching it, so a bad precondition stops the
    run instead of silently reporting a partial/wrong plan."""
    consignor = session.query(Consignor).filter(Consignor.name == CONSIGNOR_NAME).first()
    if not consignor:
        raise ValueError(f"Consignor {CONSIGNOR_NAME!r} not found -- refusing to create one.")

    batch = session.query(Batch).filter(Batch.batch_code == BATCH_CODE).first()
    if not batch:
        raise ValueError(f"Batch {BATCH_CODE!r} not found.")

    if batch.is_consignment and batch.consignor_id and batch.consignor_id != consignor.id:
        existing = session.get(Consignor, batch.consignor_id)
        raise ValueError(
            f"Batch {BATCH_CODE} is already linked to a different consignor "
            f"({existing.name if existing else batch.consignor_id!r}) -- refusing to overwrite."
        )

    cards = session.query(InventoryCard).filter(InventoryCard.batch_id == batch.id).all()

    tainted_cards = [
        c for c in cards
        if c.consignment_payout_id is not None or c.consignment_amount_owed is not None
    ]
    if tainted_cards:
        raise ValueError(
            f"{len(tainted_cards)} card(s) in {BATCH_CODE} already carry consignment "
            f"tracking (e.g. card {tainted_cards[0].id} has consignment_payout_id="
            f"{tainted_cards[0].consignment_payout_id!r}, consignment_amount_owed="
            f"{tainted_cards[0].consignment_amount_owed!r}) -- refusing to overwrite; "
            "this batch is not as clean as assumed."
        )

    tiers = get_consignment_tiers(session)

    cards_to_backfill = []
    unsold_cards = []
    for card in cards:
        if card.status != "sold" or card.sold_price is None:
            unsold_cards.append({"card_id": card.id, "name": card.name, "status": card.status})
            continue
        owed = resolve_consignment_payout(tiers, card.sold_price)
        cards_to_backfill.append({
            "card_id": card.id, "name": card.name,
            "sold_price": card.sold_price, "computed_owed": owed,
        })

    return {
        "consignor_id": consignor.id,
        "consignor_name": consignor.name,
        "batch_id": batch.id,
        "batch_code": batch.batch_code,
        "batch_already_linked_to_target": bool(
            batch.is_consignment and batch.consignor_id == consignor.id,
        ),
        "total_cards_in_batch": len(cards),
        "unsold_cards": unsold_cards,
        "cards_to_backfill": cards_to_backfill,
        "cards_to_backfill_total_owed": round(
            sum(row["computed_owed"] for row in cards_to_backfill), 2,
        ),
    }


def apply_retro_consignment(session: Session) -> dict:
    """Execute the plan for real. Caller is responsible for commit()."""
    plan = plan_retro_consignment(session)

    consignor = session.get(Consignor, plan["consignor_id"])
    batch = session.get(Batch, plan["batch_id"])
    batch.is_consignment = True
    batch.consignor_id = consignor.id

    for row in plan["cards_to_backfill"]:
        card = session.get(InventoryCard, row["card_id"])
        card.consignment_amount_owed = row["computed_owed"]
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
                    plan = apply_retro_consignment(session)
                print(json.dumps({"mode": "CONFIRMED", **plan}, indent=2, sort_keys=True))
            else:
                plan = plan_retro_consignment(session)
                print(json.dumps({"mode": "DRY_RUN", **plan}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
