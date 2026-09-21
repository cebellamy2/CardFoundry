"""Close out fulfillment exceptions already submitted to Mana Pool.

CF-AUTORESOLVE-001 (2026-09-21). The rule itself now fires on the
submission transition (fulfillment_exception_submission_service), but
exceptions submitted BEFORE the rule existed never got that transition
again and sit unresolved indefinitely. This closes them with the same
logic, so there is one implementation and not two.

Three of the seven currently open (#19, #23, #28) have a terminal Mana
Pool outcome and were already closable through the operator's existing
"close out" button. This script deliberately routes those through
close_out_inventory_after_remote_outcome instead of the new path -- the
button's own reasoning and event type are the more accurate record of
why they closed, and the ticket asked for the existing path where it
already applies.

    PYTHONPATH=. python backfill_auto_resolve_submitted_exceptions.py [--apply]

Dry run by default. Writes nothing without --apply.
"""

import argparse
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from fulfillment_exception_resolution_service import (
    TERMINAL_REMOTE_STATES_FOR_CLOSE_OUT,
    auto_resolve_after_submission,
    close_out_inventory_after_remote_outcome,
)
from models import (
    FulfillmentException,
    InventoryCard,
    OrderItem,
    PickAllocation,
    SalesOrder,
)


def candidates(session: Session):
    """Every exception the rule says should already be closed."""
    return (
        session.query(FulfillmentException)
        .filter(
            FulfillmentException.submission_state == "submitted",
            FulfillmentException.inventory_resolution_state == "unresolved",
        )
        .order_by(FulfillmentException.id)
        .all()
    )


def safety_findings(session: Session, exception) -> list[str]:
    """Reasons this exception should NOT be auto-closed.

    The ticket's specific worry: that closing one exception papers over a
    genuinely-unresolved issue on the same order. So the order is checked
    as a whole, not just this row.
    """
    findings = []
    card = session.get(InventoryCard, exception.inventory_card_id)
    allocation = session.get(PickAllocation, exception.pick_allocation_id)

    if allocation is None or allocation.status != "exception":
        findings.append(
            f"allocation status is {allocation.status if allocation else 'MISSING'!r}, "
            f"not 'exception' -- the resolver will refuse it"
        )
    if card is None:
        findings.append("inventory card is missing")
    elif card.inventory_exception_state != "exception_unresolved":
        findings.append(
            f"card projection is {card.inventory_exception_state!r}, "
            f"expected 'exception_unresolved'"
        )

    # Any OTHER exception on the same order that is not submitted is a
    # real open question, and closing this one would hide it.
    siblings = (
        session.query(FulfillmentException)
        .filter(
            FulfillmentException.sales_order_id == exception.sales_order_id,
            FulfillmentException.id != exception.id,
        )
        .all()
    )
    for sib in siblings:
        if sib.submission_state == "needs_submission":
            findings.append(
                f"sibling exception #{sib.id} on the same order still "
                f"needs submission -- a genuinely open issue"
            )

    # An allocation on this order still mid-flight means the order itself
    # was never finished, so "the operator already dealt with it" does not
    # hold yet.
    open_allocs = (
        session.query(PickAllocation)
        .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
        .filter(
            OrderItem.order_id == exception.sales_order_id,
            PickAllocation.status.in_(("allocated", "picked")),
        )
        .count()
    )
    if open_allocs:
        findings.append(
            f"order still has {open_allocs} allocation(s) in allocated/picked"
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    parser.add_argument("--database-url", default=os.environ.get("CARDFOUNDRY_DATABASE_URL"))
    args = parser.parse_args()

    url = args.database_url or "sqlite:///cardfoundry.db"
    engine = create_engine(url)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode} against {url}\n")

    with Session(engine) as session:
        rows = candidates(session)
        print(f"submitted + inventory-unresolved: {len(rows)}\n")
        if not rows:
            print("nothing to do")
            return 0

        blocked, via_close_out, via_auto = [], [], []
        for exception in rows:
            order = session.get(SalesOrder, exception.sales_order_id)
            card = session.get(InventoryCard, exception.inventory_card_id)
            findings = safety_findings(session, exception)
            terminal = exception.remote_resolution_state in TERMINAL_REMOTE_STATES_FOR_CLOSE_OUT
            route = "close-out (existing button)" if terminal else "auto-resolve (new)"
            print(f"#{exception.id:<4} {exception.exception_type:<19} "
                  f"remote={exception.remote_resolution_state:<19} "
                  f"order #{order.id if order else '?'} "
                  f"({order.status if order else '?'})")
            print(f"       card #{card.id if card else '?'} "
                  f"{(card.name if card else '?')!r} status={card.status if card else '?'} "
                  f"(will be left unchanged)")
            print(f"       route: {route}")
            if findings:
                for f in findings:
                    print(f"       BLOCKED: {f}")
                blocked.append(exception.id)
            elif terminal:
                via_close_out.append(exception.id)
            else:
                via_auto.append(exception.id)
            print()

        print(f"via existing close-out button : {via_close_out}")
        print(f"via new auto-resolve          : {via_auto}")
        print(f"blocked, left alone           : {blocked}")

        if not args.apply:
            print("\ndry run -- nothing written")
            return 0

        done = 0
        for exception_id in via_close_out:
            close_out_inventory_after_remote_outcome(
                session, exception_id,
                operator_metadata={"backfill": "CF-AUTORESOLVE-001"},
            )
            done += 1
        for exception_id in via_auto:
            result = auto_resolve_after_submission(
                session, exception_id,
                operator_metadata={"backfill": "CF-AUTORESOLVE-001"},
            )
            if result is None:
                print(f"  #{exception_id}: no-op")
            else:
                done += 1
        session.commit()
        print(f"\ncommitted: {done} exception(s) closed")

        remaining = candidates(session)
        print(f"verify -- submitted + unresolved remaining: {len(remaining)} "
              f"{[e.id for e in remaining]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
