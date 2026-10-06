"""Drop a stale membership left behind by an identity correction.

WHAT WENT WRONG. identity_change_service.retire_old_listings reduces the
OLD Mana Pool listing's quantity when a card's condition/finish/printing
/language is corrected -- the part a buyer can see, and it works. But
nothing then removed the card from that old binding's
local_card_ids_json, so the binding kept CLAIMING a card it no longer
describes, and listing_integrity_service logged LISTING_IDENTITY_DRIFT on
every sync tick afterwards.

Confirmed live 2026-10-06: card 10997 (Hunting Grounds, JUD 138) was
corrected NM -> MP by the operator at 02:02; the guard zeroed binding
7089's listing in the same action (audit row 18531, quantity_written 0);
the 02:35 tick published it properly as binding 7596. Only the stale
membership was left.

★ WHAT "RETIRED" MEANS, AND WHAT IT DOES NOT. The card id is removed from
the stale binding's local_card_ids_json. Nothing else. No row is deleted,
binding_status is NOT changed, and NOTHING is written to Mana Pool. See
identity_change_service.retire_membership for why the status is left
alone -- in short, production has exactly one value in that column and 28
call sites read it, and changing it would demote the listing from a
managed zero_candidate to remote_only_unmanaged, which nothing acts on.

★ THE SUPERSEDING BINDING IS REQUIRED. A stale membership with no
replacement binding is reported and LEFT ALONE: dropping it would leave
the card claimed by nothing, which is worse than a stale claim.

★ REFUSES IF THE DESIRED QUANTITY WOULD MOVE. Measured before and after
on the real binding, because an override binding with no mtgjson_id
counts by membership and removing a card there WOULD change the number
the next push sends. Nothing is guessed about which case applies.

AUDITED AND UNDOABLE. Each change writes an InventoryChangeLog row
carrying the membership before and after, so the undo is
identity_change_service.restore_membership with the recorded list.

★ THE DRY RUN IS THE REAL WRITE, ROLLED BACK. It calls the same
retire_membership against real ORM objects and rolls the transaction
back; the only difference between --plan and --confirm is the commit.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python retire_superseded_bindings.py --plan
    cd /app && PYTHONPATH=/app /opt/venv/bin/python retire_superseded_bindings.py --confirm
"""
import argparse
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from identity_change_service import (
    retire_membership_audited,
    superseded_memberships,
)

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "retire_superseded_bindings"


def run(session: Session, *, confirm: bool) -> dict:
    candidates = superseded_memberships(session)
    report = {
        "mode": "CONFIRMED" if confirm else "PLAN",
        "candidates": len(candidates),
        "retired": 0,
        "refused": [],
        "changes": [],
    }
    for row in candidates:
        try:
            # THE SAME audited write the live correction path uses, so the
            # mutation, the quantity guard, the audit shape and the undo
            # cannot drift apart. Only the CANDIDATE TEST differs -- see
            # identity_change_service.supersession_rows_for_correction.
            outcome = retire_membership_audited(session, row, script=SCRIPT_NAME)
        except Exception as exc:  # noqa: BLE001 -- one bad row must not stop the rest
            logger.warning(
                "%s: card %s / binding %s could not be retired (%s: %s); left alone.",
                SCRIPT_NAME, row["card_id"], row["stale_binding_id"],
                type(exc).__name__, exc,
            )
            report["refused"].append({**row, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if not outcome.get("changed"):
            report["refused"].append({**row, "reason": outcome.get("reason")})
            continue

        report["retired"] += 1
        report["changes"].append(outcome)
        logger.info(
            "%s: card %s retired from binding %s (membership %s -> %s); desired "
            "quantity unchanged at %s.",
            SCRIPT_NAME, row["card_id"], outcome["binding_id"],
            outcome["membership_before"], outcome["membership_after"],
            outcome["desired_quantity"],
        )

    if confirm:
        session.commit()
    else:
        session.rollback()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="Dry run; rolls back.")
    mode.add_argument("--confirm", action="store_true", help="Write.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    with Session(engine) as session:
        report = run(session, confirm=args.confirm)

    changes = report.pop("changes")
    print(json.dumps(report, indent=2, sort_keys=True))
    if changes:
        print()
        print("row by row:")
        for change in changes:
            print(f"  card {change['card_id']} -> binding {change['binding_id']}: "
                  f"membership {change['membership_before']} -> "
                  f"{change['membership_after']}, desired quantity stays "
                  f"{change['desired_quantity']} (no Mana Pool write)")
    if not args.confirm:
        print()
        print("PLAN ONLY -- rolled back, nothing committed, no Mana Pool request made.")


if __name__ == "__main__":
    main()
