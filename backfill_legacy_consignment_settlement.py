"""One-time: stamp the legacy sheet-import provenance, and settle every
consigned card owed exactly $0.00.

TWO JOBS, both additive to the record and both undoable:

1. PROVENANCE. Stamp consignment_import_source on the cards that
   import_consignment_sheets.py created, found by the note it wrote.
   From now on that importer stamps the column itself; this fills in the
   cards it created before the column existed.

   ★ WHY A COLUMN AND NOT THE NOTE. The overdue rule keys on this stamp,
   and consignment_note is an operator-editable free-text field on the
   card edit form -- clearing a note would otherwise silently change
   whether money counts as overdue. Provenance has to be a fact the UI
   cannot rewrite by accident.

2. ZERO SETTLEMENT. Mark every card owed exactly $0.00 as paid, through
   consignment_service.settle_zero_owed_cards -- the same function the
   live paths call, not a copy of it.

DRY RUN BY DEFAULT, and the dry run runs the REAL functions inside a
transaction that is then rolled back, so it sees exactly what the apply
would see. Nothing here commits on its own.

UNDOABLE. Pass --undo to reverse the settlement (see
consignment_service.undo_zero_owed_settlement); the provenance stamp is
reversed by --undo-provenance, which clears only the stamps this script
set.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_legacy_consignment_settlement.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_legacy_consignment_settlement.py --confirm
    ... --undo --confirm
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from consignment_service import (
    LEGACY_SHEET_IMPORT_SOURCE,
    settle_zero_owed_cards,
    undo_zero_owed_settlement,
)
from database import engine
from models import Batch, Consignor, InventoryCard, InventoryChangeLog

logger = logging.getLogger("cardfoundry")

# The note import_consignment_sheets.py writes on every card it creates.
IMPORTER_NOTE_PREFIX = "Backfilled from Google Sheets import"
PROVENANCE_ACTION = "consignment_import_source_backfill"
PROVENANCE_UNDO_ACTION = "consignment_import_source_backfill_undo"
REASON = "legacy sheet-import cards settle at $0.00 owed; operator decision 2026-10-17"


def provenance_candidates(session: Session) -> list[InventoryCard]:
    """Cards the sheet importer created that are not yet stamped."""
    return (
        session.query(InventoryCard)
        .filter(
            InventoryCard.consignment_note.like(IMPORTER_NOTE_PREFIX + "%"),
            InventoryCard.consignment_import_source.is_(None),
        )
        .order_by(InventoryCard.id)
        .all()
    )


def stamp_provenance(session: Session, *, dry_run: bool) -> dict:
    cards = provenance_candidates(session)
    report = {"action": PROVENANCE_ACTION, "count": len(cards),
              "dry_run": bool(dry_run),
              "cards": [{"inventory_card_id": c.id, "name": c.name} for c in cards]}
    if dry_run or not cards:
        return report
    for card in cards:
        card.consignment_import_source = LEGACY_SHEET_IMPORT_SOURCE
        session.add(InventoryChangeLog(
            actor="backfill_legacy_consignment_settlement",
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": PROVENANCE_ACTION,
                "before": {"consignment_import_source": None},
                "after": {"consignment_import_source": LEGACY_SHEET_IMPORT_SOURCE},
                "operator_authorised": True,
            }, sort_keys=True),
        ))
    session.flush()
    logger.info("legacy provenance stamped on %s card(s)", len(cards))
    return report


def undo_provenance(session: Session, *, dry_run: bool) -> dict:
    """Clear only the stamps this script set, read back from its own
    audit rows -- never a stamp the importer itself wrote."""
    stamped = set()
    for row in session.query(InventoryChangeLog).order_by(InventoryChangeLog.id.asc()).all():
        try:
            summary = json.loads(row.change_summary or "{}")
        except ValueError:
            logger.warning("provenance undo: unparseable audit row %s, skipped", row.id)
            continue
        if summary.get("action_type") == PROVENANCE_ACTION:
            stamped.add(row.inventory_card_id)
        elif summary.get("action_type") == PROVENANCE_UNDO_ACTION:
            stamped.discard(row.inventory_card_id)
    cards = [c for c in (session.get(InventoryCard, i) for i in sorted(stamped)) if c]
    report = {"action": PROVENANCE_UNDO_ACTION, "count": len(cards),
              "dry_run": bool(dry_run)}
    if dry_run or not cards:
        return report
    for card in cards:
        card.consignment_import_source = None
        session.add(InventoryChangeLog(
            actor="backfill_legacy_consignment_settlement",
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": PROVENANCE_UNDO_ACTION,
                "before": {"consignment_import_source": LEGACY_SHEET_IMPORT_SOURCE},
                "after": {"consignment_import_source": None},
                "operator_authorised": True,
                "undoes_action": PROVENANCE_ACTION,
            }, sort_keys=True),
        ))
    session.flush()
    logger.info("legacy provenance stamp UNDONE on %s card(s)", len(cards))
    return report


def owed_snapshot(session: Session) -> dict:
    """What's Owed, as the report itself computes it, per consignor."""
    out = {}
    rows = (
        session.query(Consignor.name, InventoryCard.consignment_amount_owed)
        .join(Batch, Batch.consignor_id == Consignor.id)
        .join(InventoryCard, InventoryCard.batch_id == Batch.id)
        .filter(InventoryCard.consignment_payout_status == "owed")
        .all()
    )
    for name, owed in rows:
        entry = out.setdefault(name, {"cards": 0, "owed": 0.0})
        entry["cards"] += 1
        entry["owed"] = round(entry["owed"] + (owed or 0), 2)
    return out


def print_snapshot(label, snap):
    print(f"  {label}")
    total_cards = total_owed = 0
    for name in sorted(snap, key=lambda k: -snap[k]["owed"]):
        s = snap[name]
        print("    %-20s cards=%-5d $%.2f" % (name, s["cards"], s["owed"]))
        total_cards += s["cards"]; total_owed = round(total_owed + s["owed"], 2)
    print("    %-20s cards=%-5d $%.2f" % ("TOTAL", total_cards, total_owed))


def run(session: Session, *, dry_run: bool, undo: bool, undo_prov: bool) -> dict:
    before = owed_snapshot(session)
    print_snapshot("OWED BEFORE:", before)
    print()

    if undo:
        report = undo_zero_owed_settlement(
            session, reason="reversing the zero settlement", dry_run=dry_run)
        print("  UNDO zero settlement: %s card(s)" % report["count"])
    elif undo_prov:
        report = undo_provenance(session, dry_run=dry_run)
        print("  UNDO provenance stamp: %s card(s)" % report["count"])
    else:
        prov = stamp_provenance(session, dry_run=dry_run)
        print("  provenance stamp: %s card(s)" % prov["count"])
        settle = settle_zero_owed_cards(session, reason=REASON, dry_run=dry_run)
        print("  zero settlement : %s card(s) would be marked paid" % settle["count"])
        by_consignor = {}
        for plan in settle["cards"]:
            card = session.get(InventoryCard, plan["inventory_card_id"])
            batch = session.get(Batch, card.batch_id) if card else None
            consignor = session.get(Consignor, batch.consignor_id) if batch and batch.consignor_id else None
            name = consignor.name if consignor else "(no consignor)"
            by_consignor.setdefault(name, []).append(plan)
        print()
        print("  $0.00 CARDS TO BE MARKED PAID, by consignor:")
        for name in sorted(by_consignor, key=lambda k: -len(by_consignor[k])):
            plans = by_consignor[name]
            print("    %-20s %d card(s)" % (name, len(plans)))
            for plan in plans[:3]:
                print("        e.g. card %-6s %-32s owed $%.2f"
                      % (plan["inventory_card_id"], (plan["name"] or "")[:32],
                         plan["amount_owed"] or 0))
        report = {"provenance": prov, "settlement": settle}

    print()
    after = owed_snapshot(session)
    print_snapshot("OWED AFTER:", after)
    print()
    print("  AMOUNT OWED UNCHANGED PER CONSIGNOR:",
          all(abs(before.get(n, {"owed": 0})["owed"] - after.get(n, {"owed": 0})["owed"]) < 0.005
              for n in set(before) | set(after)))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", help="actually write")
    parser.add_argument("--undo", action="store_true", help="reverse the settlement")
    parser.add_argument("--undo-provenance", action="store_true")
    args = parser.parse_args()
    dry_run = not args.confirm
    set_script_actor("backfill_legacy_consignment_settlement")
    print("=== legacy consignment settlement ===")
    print("MODE:", "DRY RUN (nothing written)" if dry_run else "APPLY")
    print()
    with Session(engine) as session:
        run(session, dry_run=dry_run, undo=args.undo, undo_prov=args.undo_provenance)
        if dry_run:
            # The real functions ran; discard whatever they staged so a
            # dry run sees what the apply sees and still writes nothing.
            session.rollback()
            print()
            print("DRY RUN -- rolled back, nothing written.")
        else:
            session.commit()
            print()
            print("APPLIED and committed.")


if __name__ == "__main__":
    main()
