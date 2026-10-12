"""One-time correction: point 7 pick allocations at the batch their card is in.

★ THE BUG IT REPAIRS (v2.39.0, found by post-delete verification of E4
2026-10-12). `PickAllocation` carries its OWN `batch_id` column, written
once when the allocation is created and never kept in sync with the card.
v2.39.0's reserved move updated `InventoryCard.batch_id` and left the
allocation pointing at batch 81 (E4), which the same delete then removed.

Three INNER joins read that column -- `pick_wave_service.get_wave_picklist`,
`order_service.get_picklist` and `main._order_line_batch_codes` -- so the
rows were not shown wrong, they were DROPPED. Orders 4526, 4534, 4542,
4543, 4550, 4577 and 4607 were each missing a line, and 4526 (a one-line
order) had an entirely empty pick list. The cards themselves were fine:
present, `reserved`, and correctly in A1.

★ IT DEFAULTS TO A DRY RUN. Writing requires --apply, undoing requires
--undo. A file that can write to production must not be able to do it by
being run.

★ HOW THE WRITE IS SWITCHED OFF. Not by skipping it. The dry run calls the
SAME function the apply calls, against a Session bound to a Connection
with an open transaction that is rolled back at the end. So the guards
fire, the audit rows are built, and nothing is written -- the same shape
as move_misfiled_legacy_land_cards.py and dry_run_non_english_mirror_fold.py.

★ EVERY GUARD IS RE-CHECKED INSIDE THE WRITE. The allocation must still be
`allocated`, its card must still sit in the target batch, and the order
must still be unshipped and non-terminal. Anything that moved on is
skipped and named rather than forced -- an allocation that has since been
picked or shipped is somebody else's work, not a stale pointer.

UNDO: `--undo` reads the audit rows this script wrote and restores each
allocation's previous batch_id from the value recorded in them, writing a
reciprocal audit row so the reversal is itself audited. It does not need
batch 81 to exist, because the old value is in the record rather than
re-derived.
"""

import json
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ACTOR = "script:allocation_batch_id_repair"
ACTION = "pick_allocation_batch_id_repair"
UNDO_ACTION = "pick_allocation_batch_id_repair_undo"

# allocation id -> the batch its card was moved into
APPROVED = {
    1999: 26, 2013: 26, 2041: 26, 2043: 26, 2056: 26, 2126: 26, 2215: 26,
}

TERMINAL_ORDER_STATUSES = {
    "cancelled", "completed", "fulfilled", "refunded", "shipped", "closed",
}
REPAIRABLE_ALLOCATION_STATUS = "allocated"


def repair(session: Session, approved: dict) -> dict:
    """Re-point each approved allocation at its card's batch.

    Writes one InventoryChangeLog row per change, keyed on the card, with
    the previous value recorded so --undo needs nothing else.
    """
    from models import (
        Batch, InventoryCard, InventoryChangeLog, OrderItem, PickAllocation,
        SalesOrder,
    )

    changed, skipped = [], []
    now = datetime.now(timezone.utc)

    for allocation_id, target_batch_id in sorted(approved.items()):
        allocation = session.get(
            PickAllocation, allocation_id, with_for_update=True,
        )
        if allocation is None:
            skipped.append({"allocation_id": allocation_id,
                            "reason": "the allocation no longer exists"})
            continue
        if allocation.status != REPAIRABLE_ALLOCATION_STATUS:
            skipped.append({
                "allocation_id": allocation_id,
                "reason": f"its status is {allocation.status!r}, not "
                          f"{REPAIRABLE_ALLOCATION_STATUS!r}",
            })
            continue
        card = session.get(InventoryCard, allocation.inventory_card_id)
        if card is None:
            skipped.append({"allocation_id": allocation_id,
                            "reason": "its card no longer exists"})
            continue
        if card.batch_id != target_batch_id:
            skipped.append({
                "allocation_id": allocation_id,
                "reason": f"its card is in batch {card.batch_id}, not the "
                          f"approved {target_batch_id}",
            })
            continue
        target = session.get(Batch, target_batch_id)
        if target is None or target.is_archived:
            skipped.append({
                "allocation_id": allocation_id,
                "reason": f"batch {target_batch_id} is missing or archived",
            })
            continue
        item = session.get(OrderItem, allocation.order_item_id)
        order = session.get(SalesOrder, item.order_id) if item else None
        if order is None:
            skipped.append({"allocation_id": allocation_id,
                            "reason": "its order could not be found"})
            continue
        if order.shipped_at is not None or str(order.status or "") in TERMINAL_ORDER_STATUSES:
            skipped.append({
                "allocation_id": allocation_id,
                "reason": f"its order is {order.status!r} "
                          f"(shipped_at={order.shipped_at})",
            })
            continue
        if allocation.batch_id == target_batch_id:
            skipped.append({"allocation_id": allocation_id,
                            "reason": "already correct -- nothing to do"})
            continue

        previous = allocation.batch_id
        allocation.batch_id = target_batch_id
        session.add(InventoryChangeLog(
            actor=ACTOR,
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": ACTION,
                "pick_allocation_id": allocation.id,
                "previous_batch_id": previous,
                "new_batch_id": target_batch_id,
                "new_batch_code": target.batch_code,
                "inventory_card_id": card.id,
                "card_name": card.name,
                "sales_order_id": order.id,
                "order_status": order.status,
                "allocation_status": allocation.status,
                "reason": (
                    "v2.39.0's reserved move left this allocation pointing at "
                    "the deleted batch, so three inner joins dropped the row "
                    "and its order was missing a pick-list line"
                ),
                "timestamp": now.isoformat(),
            }, sort_keys=True),
        ))
        changed.append({
            "allocation_id": allocation.id, "card_id": card.id,
            "card_name": card.name, "order_id": order.id,
            "previous_batch_id": previous, "new_batch_id": target_batch_id,
            "new_batch_code": target.batch_code,
        })

    session.flush()
    return {"changed": changed, "skipped": skipped}


def undo(session: Session) -> dict:
    """Restore each allocation's previous batch_id from this script's own
    audit rows. Needs nothing re-derived and no vanished batch to exist."""
    from models import InventoryChangeLog, PickAllocation

    restored, skipped = [], []
    now = datetime.now(timezone.utc)
    rows = (
        session.query(InventoryChangeLog)
        .filter(InventoryChangeLog.actor == ACTOR)
        .order_by(InventoryChangeLog.id)
        .all()
    )
    for row in rows:
        try:
            entry = json.loads(row.change_summary or "{}")
        except ValueError:
            skipped.append({"log_id": row.id, "reason": "unreadable summary"})
            continue
        if entry.get("action_type") != ACTION:
            continue
        allocation = session.get(
            PickAllocation, entry["pick_allocation_id"], with_for_update=True,
        )
        if allocation is None:
            skipped.append({"log_id": row.id,
                            "reason": "the allocation no longer exists"})
            continue
        if allocation.batch_id != entry["new_batch_id"]:
            skipped.append({
                "log_id": row.id,
                "reason": f"allocation {allocation.id} is now in batch "
                          f"{allocation.batch_id}, not the "
                          f"{entry['new_batch_id']} this wrote -- leaving it",
            })
            continue
        allocation.batch_id = entry["previous_batch_id"]
        session.add(InventoryChangeLog(
            actor=ACTOR,
            inventory_card_id=entry["inventory_card_id"],
            change_summary=json.dumps({
                "action_type": UNDO_ACTION,
                "pick_allocation_id": allocation.id,
                "restored_batch_id": entry["previous_batch_id"],
                "undid_batch_id": entry["new_batch_id"],
                "undoes_log_id": row.id,
                "timestamp": now.isoformat(),
            }, sort_keys=True),
        ))
        restored.append({"allocation_id": allocation.id,
                         "batch_id": entry["previous_batch_id"]})
    session.flush()
    return {"restored": restored, "skipped": skipped}


def _report(result: dict) -> None:
    for row in result.get("changed", []):
        print(f"  alloc {row['allocation_id']:5} card {row['card_id']:6} "
              f"{str(row['card_name'])[:28]:28} order {row['order_id']}  "
              f"batch {row['previous_batch_id']} -> {row['new_batch_id']} "
              f"({row['new_batch_code']})")
    for row in result.get("restored", []):
        print(f"  alloc {row['allocation_id']:5} restored to batch {row['batch_id']}")
    for row in result.get("skipped", []):
        print(f"  SKIPPED {row}")


def main() -> int:
    apply = "--apply" in sys.argv
    undoing = "--undo" in sys.argv
    db = "/data/cardfoundry.db"
    for arg in sys.argv[1:]:
        if arg.startswith("--db="):
            db = arg.split("=", 1)[1]

    engine = create_engine(f"sqlite:///{db}")
    import database
    import inventory_sync_service
    database.engine = engine
    inventory_sync_service.engine = engine

    label = "UNDO" if undoing else "REPAIR"
    print(f"{label} {'APPLY' if apply else 'DRY RUN'} against {db}")
    print("-" * 68)

    # ★ ONE TRANSACTION, ONE DECIDING LINE. Both modes run the same
    # function against the same Session bound to the same open
    # transaction; `repair`/`undo` flush, so the changes are on the
    # connection either way. The ONLY difference is commit versus
    # rollback at the end. Deliberately no session.commit() as well --
    # committing the Session and then the outer transaction makes it
    # ambiguous which one actually persisted, and SQLAlchemy warns that
    # the transaction was already deassociated. A repair whose write path
    # I cannot reason about precisely is not one worth running.
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection) as session:
            result = undo(session) if undoing else repair(session, APPROVED)
            _report(result)
        if apply:
            transaction.commit()
            print("\nAPPLIED and committed.")
        else:
            transaction.rollback()
            print("\nDRY RUN -- rolled back, nothing written.")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
