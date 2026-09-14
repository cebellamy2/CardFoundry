"""One-off (operator-approved 2026-09-14): close fulfillment exception #2,
"Expansion Algorithm" on order 37 / Mana Pool 533175-1893490.

WHY IT NEEDED A DECISION. close_stranded_fulfillment_exceptions.py
refused this row on purpose: the card's identity agrees with the order
line on every field, so nothing in the data could distinguish "filed in
error" from "genuinely not fulfilled". That question is answerable only
outside CardFoundry, and Mana Pool returns no per-line fulfillment status
for this order. The operator checked Mana Pool directly and confirmed
that BOTH short lines -- Chromatic Lantern (exception #3) and Expansion
Algorithm (this one) -- were refunded/replaced to the customer. The line
was therefore genuinely not fulfilled, and this closes with the same
outcome #3 already has.

WHY IT COULD NOT JUST USE THE MISSING-CARD RESOLVER. #3's card still
reads removal_reason "fulfillment_missing", so resolve_missing_inventory_
exception reached it in the Ticket C bulk close-out. This card reads
"other": it was initially recorded as missing, then found in a batch not
in the system and retained for personal use, and the removal metadata was
corrected to say so. That correction is what stranded the exception.

removal_reason is deliberately NOT rewritten back to "fulfillment_missing"
to unlock that resolver. The card was not missing -- it was found and
diverted -- and "other" with its correction note is the truthful record.
Rewriting it to reach a convenient code path would put a falsehood in the
inventory record to save writing an accurate note here.

WHAT THIS WRITES is the same set every resolver writes, via that module's
own _event and _projection_audit helpers, so the audit rows match the 32
exceptions already closed. Card status, removal reason and allocation
status are all left exactly as they are.

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
from models import FulfillmentException, InventoryCard

EXCEPTION_ID = 2
CARD_ID = 7032
ORDER_ID = 37
MANA_POOL_LABEL = "533175-1893490"
SIBLING_EXCEPTION_ID = 3

RESOLUTION_NOTE = (
    "Closed as unfulfillable. This line was not shipped: the order went out "
    "short by two units, and the operator confirmed directly against Mana "
    "Pool order 533175-1893490 on 2026-09-14 that both short lines -- "
    "Chromatic Lantern (exception #3) and this one -- were refunded or "
    "replaced to the customer. Same outcome as #3, which closed in the "
    "Ticket C bulk run. Not a false positive: although inventory card #7032 "
    "matches order item #90 on every identity field, the customer never "
    "received it. The card was first recorded as missing, then found in a "
    "batch not present in the system and retained for personal use, so its "
    "removal_reason reads 'other' rather than 'fulfillment_missing' and the "
    "missing-card resolver could not reach it. That reason is left unchanged "
    "because it is the truthful record of what became of the card. This "
    "closes the exception's inventory record only: no card status changed "
    "and no inventory moved."
)


def _check(label: str, actual, wanted) -> bool:
    ok = actual == wanted
    print(f"  {'OK  ' if ok else 'FAIL'} {label:<44} {actual!r}"
          + ("" if ok else f"   (expected {wanted!r})"))
    return ok


def verify(session: Session):
    exception, order, item, allocation, card = _context(session, EXCEPTION_ID)
    sibling = session.get(FulfillmentException, SIBLING_EXCEPTION_ID)
    print("Preconditions, re-read live:")
    checks = [
        _check("exception id", exception.id, EXCEPTION_ID),
        _check("exception type", exception.exception_type, "missing"),
        _check("submission_state", exception.submission_state, "submitted"),
        _check("inventory_resolution_state", exception.inventory_resolution_state, "unresolved"),
        _check("resolution_note still empty", exception.resolution_note, None),
        _check("card id", card.id, CARD_ID),
        _check("card status", card.status, "removed"),
        _check("card removal_reason", card.removal_reason, "other"),
        _check("card projection", card.inventory_exception_state, "exception_unresolved"),
        _check("allocation status", allocation.status, "exception"),
        _check("order id", order.id, ORDER_ID),
        _check("Mana Pool label", order.external_label, MANA_POOL_LABEL),
        _check("order shipped", order.status, "shipped"),
        # The operator's evidence is about BOTH short lines, so the sibling
        # being closed the same way is part of the justification. If #3 were
        # not resolved, the premise of this note would not hold.
        _check("sibling #3 already resolved", sibling.inventory_resolution_state, "resolved"),
    ]
    if not all(checks):
        print("\nAborting: live state does not match the approved plan. Nothing written.")
        sys.exit(1)
    return exception, order, item, allocation, card


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    with Session(engine) as session:
        exception, order, item, allocation, card = verify(session)
        print()
        print("=== WOULD CHANGE ===")
        print(f"  exception #{exception.id} inventory_resolution_state 'unresolved' -> 'resolved'")
        print(f"  exception #{exception.id} inventory_resolved_at      None -> <now>")
        print(f"  exception #{exception.id} resolution_note            None -> <{len(RESOLUTION_NOTE)} chars>")
        print(f"  card #{card.id} inventory_exception_state       'exception_unresolved' -> 'none'")
        print("  + 1 FulfillmentExceptionEvent, + 1 InventoryChangeLog audit row")
        print()
        print("=== WOULD NOT CHANGE ===")
        print(f"  card #{card.id} status          {card.status!r}")
        print(f"  card #{card.id} removal_reason  {card.removal_reason!r}  (truthful; NOT rewritten)")
        print(f"  allocation #{allocation.id} status   {allocation.status!r}")
        print(f"  exception remote_resolution_state {exception.remote_resolution_state!r}")
        print()
        print("=== NOTE ===")
        print(f"  {RESOLUTION_NOTE}")
        print()
        print(f"Mode: {'WRITE (--confirm)' if args.confirm else 'DRY RUN (report only)'}")
        if not args.confirm:
            session.rollback()
            print("\nDRY RUN -- nothing written. Re-run with --confirm to apply.")
            return

        timestamp = datetime.now(timezone.utc)
        exception.inventory_resolution_state = "resolved"
        exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
        exception.resolution_note = RESOLUTION_NOTE
        card.inventory_exception_state = "none"
        _event(
            session, exception, FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
            "unresolved", "resolved", RESOLUTION_NOTE, {
                "exception_type": exception.exception_type,
                "closed_as": "unfulfillable_operator_confirmed_refund_or_replacement",
                "evidence_source": "operator check against Mana Pool order "
                                   f"{MANA_POOL_LABEL} on 2026-09-14",
                "sibling_exception_id": SIBLING_EXCEPTION_ID,
                "card_status_unchanged": card.status,
                "card_removal_reason_unchanged": card.removal_reason,
                "operator_metadata": {
                    "action": "one_off_correction",
                    "script": "close_exception_2_expansion_algorithm.py",
                },
                "inventory_card_id": card.id, "allocation_id": allocation.id,
            }, timestamp,
        )
        _projection_audit(
            session, card, exception, card.status, card.status, RESOLUTION_NOTE, timestamp,
        )
        session.commit()
        print("\nCommitted.")

    with Session(engine) as session:
        exception = session.get(FulfillmentException, EXCEPTION_ID)
        card = session.get(InventoryCard, CARD_ID)
        print("\n=== VERIFY ===")
        _check("inventory_resolution_state", exception.inventory_resolution_state, "resolved")
        _check("resolved_at set", exception.inventory_resolved_at is not None, True)
        _check("card status unchanged", card.status, "removed")
        _check("card removal_reason unchanged", card.removal_reason, "other")
        _check("card projection", card.inventory_exception_state, "none")
        remaining = session.query(FulfillmentException).filter_by(
            inventory_resolution_state="unresolved",
        ).count()
        print(f"  INFO exceptions still unresolved app-wide      {remaining}")


if __name__ == "__main__":
    main()
