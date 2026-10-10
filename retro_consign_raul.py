"""One-time retroactive correction: attribute batch CON_RAU to consignor Raul.

CON_RAU was named like every other consignor batch but was never linked:
is_consignment=0, consignor_id NULL. Two of its cards had already sold as
regular stock with no payout tracked. Found by the Delete Batch slice-1
report (v2.33.0), which refuses a CON_-coded batch whose consignment flag
is false precisely because this state is a data inconsistency rather than
permission to delete.

★ WHY THIS IS A SCRIPT AND NOT THE BATCH-EDIT UI. That form deliberately
LOCKS consignment status and consignor once any card in the batch has
sold (main.py, batch_update: `if not has_sold_cards`), and silently
ignores whatever was submitted for those two fields. Correct in general:
a bare field flip would leave the batch saying "consigned to Raul" while
the two cards that already sold under it have no payout tracked at all.
This script does both halves together, exactly as
retro_consign_cam_roc.py did for CON_CAM_ROC.

★ IT NEVER CREATES THE CONSIGNOR. Raul already exists (id 16, created
2026-10-10 through create_consignor_with_log, with its own
ConsignorChangeLog row and no portal credentials). Creating a second
"Raul" would be a duplicate, so this refuses if the name is missing
rather than inventing one -- the same stance as the CON_CAM_ROC script.

Refuses to run (dry or confirmed) if the batch is already linked to a
DIFFERENT consignor, or if any card already carries consignment tracking
-- either would mean this is not the clean, untouched case assumed here,
and a silent overwrite could erase or double-count real payout history.

Dry-run by default (prints a report, writes nothing). Pass --confirm to
actually commit.
"""

import argparse
import json

from sqlalchemy.orm import Session

from actor_context import script_actor
from consignment_service import get_consignment_tiers, resolve_consignment_payout
from database import engine
from inventory_sync_service import inventory_sync_lease
from models import (
    Batch, Consignor, ConsignorChangeLog, InventoryCard, InventoryChangeLog,
)

CONSIGNOR_NAME = "Raul"
BATCH_CODE = "CON_RAU"

# ★ WHERE THE AUDIT LANDS (operator addition, 2026-10-10). There is NO
# batch-level audit table in this schema -- the only change logs are
# Consignor*, InventoryChangeLog, FulfillmentExceptionEvent and
# PickWaveEvent -- so the two halves are recorded where each one belongs:
#
#   the per-card amounts -> InventoryChangeLog, which already carries
#   arbitrary change_summary JSON for exactly this (see
#   consignment_service.CONSIGNMENT_AMOUNT_CORRECTION_ACTION) and needs no
#   schema change. This is also how bulk_move_cards_to_batch records a
#   batch change: one card row per card.
#
#   the batch link -> ConsignorChangeLog, keyed to the consignor whose
#   holdings actually changed, so it shows on /consignors/16/history.
#   revert_consignor_change only accepts "consignor_updated", so this
#   entry renders in the history and is correctly refused as
#   non-revertible rather than being half-undoable.
BATCH_LINK_ACTION = "consignment_batch_linked"
CARD_BACKFILL_ACTION = "consignment_retro_backfill"
ACTOR = script_actor("retro_consign_raul")


def plan_retro_consignment(session: Session) -> dict:
    """Build the full set of changes without writing anything.

    Raises ValueError on a violated precondition -- callers let it
    propagate, so a bad precondition stops the run rather than reporting a
    partial or wrong plan.
    """
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
            f"tracking (e.g. card {tainted_cards[0].id}) -- refusing to overwrite; "
            "this batch is not as clean as assumed."
        )

    tiers = get_consignment_tiers(session)

    cards_to_backfill = []
    unsold_cards = []
    for card in cards:
        if card.status != "sold" or card.sold_price is None:
            unsold_cards.append({
                "card_id": card.id, "name": card.name, "status": card.status,
            })
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
        "batch_is_consignment_before": bool(batch.is_consignment),
        "batch_consignor_id_before": batch.consignor_id,
        "tiers": tiers,
        "total_cards_in_batch": len(cards),
        "unsold_cards": unsold_cards,
        "cards_to_backfill": cards_to_backfill,
        "cards_to_backfill_total_owed": round(
            sum(row["computed_owed"] for row in cards_to_backfill), 2,
        ),
    }


def apply_retro_consignment(session: Session) -> dict:
    """Execute the plan for real, audit rows included. Caller commits.

    Every write below -- the batch link, both card amounts and all three
    audit rows -- lands in the caller's single transaction, so a failure
    anywhere leaves nothing behind.
    """
    plan = plan_retro_consignment(session)

    consignor = session.get(Consignor, plan["consignor_id"])
    batch = session.get(Batch, plan["batch_id"])

    before = {
        "is_consignment": bool(batch.is_consignment),
        "consignor_id": batch.consignor_id,
    }
    batch.is_consignment = True
    batch.consignor_id = consignor.id
    session.add(ConsignorChangeLog(
        consignor_id=consignor.id,
        change_summary=json.dumps({
            "action_type": BATCH_LINK_ACTION,
            "actor": ACTOR,
            "batch_id": batch.id,
            "batch_code": batch.batch_code,
            "before": before,
            "after": {"is_consignment": True, "consignor_id": consignor.id},
            "reason": (
                "CON_RAU was named like a consignment batch but was never "
                "linked: is_consignment=0, consignor_id NULL. Two of its "
                "cards had already sold with no payout tracked."
            ),
            "cards_in_batch": plan["total_cards_in_batch"],
            "cards_backfilled": [r["card_id"] for r in plan["cards_to_backfill"]],
            "total_owed": plan["cards_to_backfill_total_owed"],
        }, sort_keys=True),
    ))
    audit_ids = {"consignor_change_log": None, "inventory_change_logs": []}

    for row in plan["cards_to_backfill"]:
        card = session.get(InventoryCard, row["card_id"])
        card_before = {
            "consignment_amount_owed": card.consignment_amount_owed,
            "consignment_payout_status": card.consignment_payout_status,
        }
        card.consignment_amount_owed = row["computed_owed"]
        card.consignment_payout_status = "owed"
        session.add(InventoryChangeLog(
            actor=ACTOR,
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": CARD_BACKFILL_ACTION,
                "batch_id": batch.id,
                "batch_code": batch.batch_code,
                "consignor_id": consignor.id,
                "consignor_name": consignor.name,
                "sold_price": row["sold_price"],
                "before": card_before,
                "after": {
                    "consignment_amount_owed": row["computed_owed"],
                    "consignment_payout_status": "owed",
                },
                "tiers_at_the_time": plan["tiers"],
                "reason": (
                    "Retroactive consignment attribution: this card sold as "
                    "regular stock before its batch was linked to its "
                    "consignor, so no payout was tracked at the time."
                ),
            }, sort_keys=True),
        ))

    session.flush()
    # Reported so the operator can find the exact rows afterwards.
    logged = (
        session.query(ConsignorChangeLog)
        .filter(ConsignorChangeLog.consignor_id == consignor.id)
        .order_by(ConsignorChangeLog.id.desc()).first()
    )
    audit_ids["consignor_change_log"] = logged.id if logged else None
    audit_ids["inventory_change_logs"] = [
        row_id for (row_id,) in session.query(InventoryChangeLog.id)
        .filter(
            InventoryChangeLog.inventory_card_id.in_(
                [r["card_id"] for r in plan["cards_to_backfill"]] or [-1]),
            InventoryChangeLog.actor == ACTOR,
        ).order_by(InventoryChangeLog.id).all()
    ]
    plan["audit_ids"] = audit_ids
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
