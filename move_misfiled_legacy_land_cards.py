"""One-time correction: move the 15 mis-filed modal-DFC cards out of leg_land.

★ APPLIED TO PRODUCTION 2026-10-09 (operator-approved). It wrote
InventoryChangeLog ids 21985-21999 under actor
`script:legacy_land_batch_move`, moving 11 cards to leg_multi, 3 to
leg_red and 1 to leg_g. Kept in the repo as the record of what ran, the
same way recategorize_legacy_batches.py and the correct_*/fix_* scripts
are. Re-running it now is a no-op: the route reports every card as
`unchanged` because it is already in the target batch.

★ IT DEFAULTS TO A DRY RUN. Writing requires --apply. It was originally
written and reviewed under the name dry_run_legacy_land_batch_move.py and
renamed afterwards, because a file whose name says "dry run" but which
can write to production is a trap for the next reader.

v2.28.1 fixed classify_legacy_batch to read the FRONT face when deciding
land vs non-land. The cards imported before that fix are still sitting in
leg_land -- a spell front with a land back ("Sorcery // Land") was filed
as a land. This moves only those 15, by card id.

★ IT GOES THROUGH THE REAL ROUTE. The write path is
main.bulk_move_cards_to_batch -- the same function the operator's
"Confirm Move" button posts to -- not raw SQL and not
recategorize_legacy_batches.py (which rewrites card.batch_id with no
audit row at all, and would re-examine all 6,307 legacy cards). The route
brings the guards with it: target batch must exist and not be archived,
every selected card must be `available`, the move is all-or-nothing, and
each moved card gets an InventoryChangeLog row naming the old and new
batch code.

★ HOW THE WRITE IS SWITCHED OFF. Not by skipping it. The whole thing runs
against a Connection with an open transaction, and both main.engine and
inventory_sync_service.engine are pointed at that connection, so every
session the route opens -- including the sync lease's own -- lands inside
it. The route's session.commit() becomes a SAVEPOINT release, and the
outer rollback at the end discards all of it. The dry run therefore sees
exactly what the apply would see, including the guards firing, and writes
nothing anywhere. Same shape as dry_run_non_english_mirror_fold.py.

UNDO: one bulk move back through the same route -- the same 15 ids with
target batch 8 (leg_land), which writes 15 reciprocal audit rows so the
reversal is itself audited. See the UNDO_IDS/UNDO_TARGET constants below.
"""

import json
import sys

from sqlalchemy.orm import Session

import inventory_sync_service
import main
from actor_context import script_actor, set_actor
from database import engine as real_engine
from models import Batch, InventoryCard, InventoryChangeLog

# Approved by the operator 2026-10-09. Target batch id -> card ids.
MOVES = {
    1: [204, 915, 919, 920, 921, 1497, 2426, 2427, 5547, 6277, 6278],  # leg_multi
    5: [3300, 3301, 3302],                                             # leg_red
    6: [3072],                                                         # leg_g
}
ALL_IDS = sorted(cid for ids in MOVES.values() for cid in ids)
# To reverse the applied move: MOVES = {UNDO_TARGET: UNDO_IDS}.
UNDO_TARGET = 8   # leg_land
UNDO_IDS = ALL_IDS
SOURCE_BATCH_CODE = "leg_land"


def snapshot(session: Session) -> dict:
    rows = (
        session.query(InventoryCard, Batch)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(InventoryCard.id.in_(ALL_IDS))
        .all()
    )
    return {
        card.id: {"name": card.name, "batch": batch.batch_code,
                  "status": card.status}
        for card, batch in rows
    }


def preflight(session: Session) -> list[dict]:
    """Every reason a card should be left out, before the route is asked.

    The route itself refuses the whole move if any card is not available,
    so naming them here is the difference between a report and a 409.
    """
    found = snapshot(session)
    problems = []
    for cid in ALL_IDS:
        row = found.get(cid)
        if row is None:
            problems.append({"id": cid, "problem": "card not found"})
        elif row["status"] != "available":
            problems.append({"id": cid, "problem": f"status is {row['status']!r}, not available"})
        elif row["batch"] != SOURCE_BATCH_CODE:
            problems.append({"id": cid, "problem": f"already in {row['batch']!r}, not {SOURCE_BATCH_CODE}"})
    return problems


def run(apply: bool) -> dict:
    conn = real_engine.connect()
    trans = conn.begin()
    saved = (main.engine, inventory_sync_service.engine)
    # Both: the route opens its session on main.engine, the lease opens
    # its own on inventory_sync_service.engine. Miss the second and the
    # lease row is a real, committed write.
    main.engine, inventory_sync_service.engine = conn, conn
    token = set_actor(script_actor("legacy_land_batch_move"))
    try:
        with Session(conn) as session:
            before = snapshot(session)
            problems = preflight(session)
            high_water = session.query(
                InventoryChangeLog.id).order_by(
                InventoryChangeLog.id.desc()).limit(1).scalar() or 0

        responses = []
        for target_batch_id, card_ids in sorted(MOVES.items()):
            response = main.bulk_move_cards_to_batch(
                card_ids=list(card_ids),
                target_batch_id=str(target_batch_id),
                back_link="/inventory",
            )
            responses.append({
                "target_batch_id": target_batch_id,
                "card_ids": list(card_ids),
                "status_code": response.status_code,
                "body": bytes(response.body).decode("utf-8", "replace"),
            })

        with Session(conn) as session:
            after = snapshot(session)
            audit = [
                {"id": row.id, "card_id": row.inventory_card_id,
                 "actor": row.actor, "summary": row.change_summary}
                for row in session.query(InventoryChangeLog)
                .filter(InventoryChangeLog.id > high_water)
                .order_by(InventoryChangeLog.id).all()
            ]

        result = {"before": before, "after": after, "problems": problems,
                  "responses": responses, "audit": audit, "applied": apply}
        if apply:
            trans.commit()
        else:
            trans.rollback()
        return result
    except BaseException:
        trans.rollback()
        raise
    finally:
        try:
            conn.close()
        finally:
            main.engine, inventory_sync_service.engine = saved
            set_actor(None)


def verify_untouched() -> dict:
    """Re-read on a FRESH connection after the rollback. A dry run that
    only claims it rolled back is not a dry run."""
    with Session(real_engine) as session:
        return snapshot(session)


def main_cli():
    apply = "--apply" in sys.argv
    result = run(apply)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not apply:
        print("=== POST-ROLLBACK STATE (fresh connection) ===")
        print(json.dumps(verify_untouched(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main_cli()
