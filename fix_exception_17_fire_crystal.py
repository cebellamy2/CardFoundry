"""One-off data correction (operator-approved 2026-09-15): close
fulfillment exception #17, "The Fire Crystal" on order 3877, which no
resolver in the app can reach.

WHY IT IS STUCK. The exception was correctly filed on 2026-08-26: order
item 10367 requests the ENGLISH printing and inventory card #6688 is
genuinely the JAPANESE one. Over the following hours the operator
corrected the card's printing to JA and returned it to sellable
inventory by hand, through the ordinary inventory screens. Those screens
clear unsellable_reason -- the exact field resolve_inventory_mismatch_
exception requires -- so the type-correct resolver was locked out from
that moment on. Every other path is closed too:

  * resolve_inventory_mismatch_exception needs card.status "unsellable"
    with unsellable_reason "fulfillment_inventory_mismatch". The card is
    "available" with no reason. It would also re-apply a printing
    correction that has already been applied.
  * close_out_inventory_after_remote_outcome needs a terminal remote
    state. This exception's is "awaiting".
  * bulk-accept-missing (Ticket C) needs exception_type "missing".
  * revert_fulfillment_exception_mark -- the path that closed the
    comparable exception #37 -- needs submission_state
    "needs_submission". This one was submitted on 2026-08-26.
  * The Resolve route is not blocked but does not help: dry-run against
    live Mana Pool returns {"resolved": 0, "review_required": 2} and
    moves BOTH #17 and #18 from "awaiting" to "review_required", which
    is still not terminal. Mana Pool reports the replacement at order
    level with no per-line status, so the matcher declines to guess.

NOT the same case as #37. That one was a true false positive: its order
line wanted JA and its card was already JA, an exact match, so reverting
the mark was right. Here the two genuinely differ, so this closes as
UNFULFILLABLE rather than as filed-in-error.

WHAT THIS WRITES is exactly what every resolver in
fulfillment_exception_resolution_service writes, and nothing else. It
imports that module's own _event and _projection_audit helpers rather
than hand-building the JSON, so the audit rows cannot drift in shape
from the 27 closed by Ticket C.

  exception.inventory_resolution_state  unresolved -> resolved
  exception.inventory_resolved_at       set
  exception.resolution_note             set
  card.inventory_exception_state        exception_unresolved -> none
  + one FulfillmentExceptionEvent, + one InventoryChangeLog audit row

DELIBERATELY LEFT ALONE:
  * card.status stays "available". The Japanese card is real, correctly
    identified, consigned to Cameron and currently listed on Mana Pool.
    Nothing about closing a stale record should pull live stock.
  * allocation 445 stays "exception". No resolver in this app has ever
    changed allocation status, and all 27 exceptions Ticket C closed
    still sit at "exception". Matching them keeps one convention.

Dry-run by default. Pass --confirm to write.
"""

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import engine
from fulfillment_exception_constants import (
    FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
)
from fulfillment_exception_resolution_service import _context, _event, _projection_audit
from models import InventoryCard, OrderItem, SalesOrder

EXCEPTION_ID = 17
CARD_ID = 6688
ALLOCATION_ID = 445
ORDER_ID = 3877

RESOLUTION_NOTE = (
    "Closed as unfulfillable, not as a false positive. Order item 10367 "
    "requested the English printing (language_id EN, scryfall "
    "58306c68-5de8-48f4-9ce5-ea69792af58c) and inventory card #6688 is "
    "genuinely the Japanese printing (language_id JA, scryfall "
    "ae8738dd-7361-4c10-a61c-88c6de94817f), so the allocation could not be "
    "fulfilled as originally specified and Mana Pool replaced the order. "
    "The printing correction to JA and the return to sellable inventory "
    "were applied by hand on 2026-08-26 and 2026-08-28, which cleared the "
    "unsellable_reason that resolve_inventory_mismatch_exception requires "
    "and left this record unreachable by every resolver. This closes the "
    "exception's inventory record only: the card stays available, "
    "correctly identified and listed, and no inventory moved."
)


def _expected(label: str, actual, wanted) -> bool:
    ok = actual == wanted
    print(f"  {'OK  ' if ok else 'FAIL'} {label:<46} {actual!r}"
          + ("" if ok else f"   (expected {wanted!r})"))
    return ok


def verify_preconditions(session: Session) -> tuple:
    """Re-check at run time, never from the investigation's snapshot."""
    exception, order, item, allocation, card = _context(session, EXCEPTION_ID)
    print("Preconditions, re-read live:")
    checks = [
        _expected("exception id", exception.id, EXCEPTION_ID),
        _expected("exception type", exception.exception_type, "inventory_mismatch"),
        _expected("submission_state", exception.submission_state, "submitted"),
        _expected("remote_resolution_state", exception.remote_resolution_state, "awaiting"),
        _expected("inventory_resolution_state", exception.inventory_resolution_state, "unresolved"),
        _expected("resolution_note is still empty", exception.resolution_note, None),
        _expected("card id", card.id, CARD_ID),
        _expected("card status", card.status, "available"),
        _expected("card projection", card.inventory_exception_state, "exception_unresolved"),
        _expected("card language", card.language_id, "JA"),
        _expected("card unsellable_reason", card.unsellable_reason, None),
        _expected("card removal_reason", card.removal_reason, None),
        _expected("order item language", item.language_id, "EN"),
        _expected("allocation id", allocation.id, ALLOCATION_ID),
        _expected("allocation status", allocation.status, "exception"),
        _expected("order id", order.id, ORDER_ID),
        _expected("order remote_fulfillment_status", order.remote_fulfillment_status, "replaced"),
    ]
    # The justification for closing it this way is that the two identities
    # genuinely differ. If they ever matched, this would be #37's case and
    # the right action would be a revert, not an unfulfillable close-out.
    mismatch_is_real = (card.language_id or "") != (item.language_id or "")
    checks.append(_expected("card/order language genuinely differ", mismatch_is_real, True))
    if not all(checks):
        print("\nAborting: live state does not match the approved plan. Nothing written.")
        sys.exit(1)
    return exception, order, item, allocation, card


def report(exception, order, item, allocation, card) -> None:
    print()
    print("=== WOULD CHANGE ===")
    print(f"  exception #{exception.id} inventory_resolution_state  "
          f"{exception.inventory_resolution_state!r} -> 'resolved'")
    print(f"  exception #{exception.id} inventory_resolved_at       None -> <now>")
    print(f"  exception #{exception.id} resolution_note             None -> <{len(RESOLUTION_NOTE)} chars>")
    print(f"  card #{card.id} inventory_exception_state          "
          f"{card.inventory_exception_state!r} -> 'none'")
    print("  + 1 FulfillmentExceptionEvent (unresolved -> resolved)")
    print("  + 1 InventoryChangeLog projection audit row")
    print()
    print("=== WOULD NOT CHANGE ===")
    print(f"  card #{card.id} status                  {card.status!r}  (live, listed, consigned stock)")
    print(f"  card #{card.id} language_id             {card.language_id!r}")
    print(f"  allocation #{allocation.id} status           {allocation.status!r}  "
          f"(matches all 27 closed by Ticket C)")
    print(f"  order #{order.id} status                 {order.status!r}")
    print(f"  order #{order.id} remote_fulfillment     {order.remote_fulfillment_status!r}")
    print(f"  exception remote_resolution_state    {exception.remote_resolution_state!r}")
    print()
    print("=== RESOLUTION NOTE TO BE WRITTEN ===")
    print(f"  {RESOLUTION_NOTE}")


def apply_fix(session: Session, exception, allocation, card) -> None:
    timestamp = datetime.now(timezone.utc)
    exception.inventory_resolution_state = "resolved"
    exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
    exception.resolution_note = RESOLUTION_NOTE
    card.inventory_exception_state = "none"
    _event(
        session, exception, FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
        "unresolved", "resolved", RESOLUTION_NOTE, {
            "exception_type": "inventory_mismatch",
            "closed_as": "unfulfillable_printing_mismatch",
            "operator_metadata": {
                "action": "one_off_correction",
                "script": "fix_exception_17_fire_crystal.py",
                "approved": "2026-09-15",
            },
            "card_status_unchanged": card.status,
            "card_language_id": card.language_id,
            "order_item_language_id": "EN",
            "inventory_card_id": card.id,
            "allocation_id": allocation.id,
        }, timestamp,
    )
    # previous and new status are both the card's current status: this
    # correction deliberately moves no card state.
    _projection_audit(
        session, card, exception, card.status, card.status, RESOLUTION_NOTE, timestamp,
    )
    session.flush()


def verify_after(session: Session) -> None:
    from models import FulfillmentException, FulfillmentExceptionEvent, InventoryChangeLog, PickAllocation
    exception = session.get(FulfillmentException, EXCEPTION_ID)
    card = session.get(InventoryCard, CARD_ID)
    allocation = session.get(PickAllocation, ALLOCATION_ID)
    order = session.get(SalesOrder, ORDER_ID)
    print()
    print("=== VERIFY (re-read after commit) ===")
    _expected("inventory_resolution_state", exception.inventory_resolution_state, "resolved")
    _expected("inventory_resolved_at set", exception.inventory_resolved_at is not None, True)
    _expected("resolution_note set", (exception.resolution_note or "").startswith("Closed as unfulfillable"), True)
    _expected("card projection", card.inventory_exception_state, "none")
    _expected("card status UNCHANGED", card.status, "available")
    _expected("card language UNCHANGED", card.language_id, "JA")
    _expected("allocation UNCHANGED", allocation.status, "exception")
    _expected("order remote status UNCHANGED", order.remote_fulfillment_status, "replaced")
    resolved_events = [
        e for e in session.query(FulfillmentExceptionEvent)
        .filter_by(fulfillment_exception_id=EXCEPTION_ID).all()
        if e.new_state == "resolved"
    ]
    _expected("resolution events written", len(resolved_events), 1)
    audits = [
        l for l in session.query(InventoryChangeLog).filter_by(inventory_card_id=CARD_ID).all()
        if "fulfillment_exception_inventory_resolved" in (l.change_summary or "")
    ]
    _expected("projection audit rows written", len(audits), 1)
    still_open = session.query(FulfillmentException).filter_by(
        inventory_resolution_state="unresolved",
    ).count()
    print(f"  INFO exceptions still unresolved app-wide      {still_open}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually write (default: dry run)")
    args = parser.parse_args()

    with Session(engine) as session:
        exception, order, item, allocation, card = verify_preconditions(session)
        report(exception, order, item, allocation, card)
        print()
        print(f"Mode: {'WRITE (--confirm)' if args.confirm else 'DRY RUN (report only)'}")
        if not args.confirm:
            session.rollback()
            print("\nDRY RUN -- nothing written. Re-run with --confirm to apply.")
            return
        apply_fix(session, exception, allocation, card)
        session.commit()
        print("\nCommitted.")

    with Session(engine) as session:
        verify_after(session)


if __name__ == "__main__":
    main()
