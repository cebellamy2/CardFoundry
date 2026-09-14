"""Close the fulfillment exceptions stranded by the manual-edit gap that
v1.159.0 now prevents.

These are exceptions no resolver can reach, because a manual inventory
edit cleared the status/reason pair the resolvers require:
  * resolve_inventory_mismatch_exception wants "unsellable" +
    "fulfillment_inventory_mismatch"
  * resolve_missing_inventory_exception wants "removed" +
    "fulfillment_missing"
  * revert_fulfillment_exception_mark wants one of those two pairs AND an
    unsubmitted exception
  * close_out_inventory_after_remote_outcome wants a terminal remote state
Once an operator marked the card Not For Sale, returned it to sellable,
removed it for another reason, or rewrote its removal metadata, none of
those hold and the exception is stuck forever. Exception #17 was the
first found and was closed by hand in v1.158.1; this generalises that
one-off rather than repeating it four more times.

CLASSIFICATION IS THE WHOLE POINT, and it is deliberately narrow. This
closes an exception as UNFULFILLABLE only when the card's identity
genuinely DIFFERS from what the order line asked for. If the two agree,
the exception was filed in error -- exception #37's case -- and the
truthful close is a revert, not an "could not be fulfilled" note. Writing
the wrong one puts a permanent falsehood in the audit trail, so an
agreeing identity is a hard refusal here, not a warning.

Identity is compared on finish_id, condition_id, language_id AND
scryfall_id together. scryfall_id alone is NOT enough: for #5, #18 and
#33 the scryfall_id matches exactly and only the FINISH differs (the
customer ordered non-foil, the shelf copy was etched or foil). An
identity check that stopped at scryfall_id would have misread all three
as false positives and closed them the wrong way.

Card status and allocation status are never touched, matching every
resolver in the app and the 28 exceptions already closed.

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
from models import FulfillmentException

DEFAULT_EXCEPTION_IDS = (5, 18, 33)
IDENTITY_FIELDS = ("scryfall_id", "finish_id", "condition_id", "language_id")


def identity_difference(card, item) -> dict:
    """Which identity fields disagree. Empty dict means they agree."""
    return {
        field: (getattr(item, field, None), getattr(card, field, None))
        for field in IDENTITY_FIELDS
        if str(getattr(item, field, None)) != str(getattr(card, field, None))
    }


def classify(session: Session, exception_id: int) -> dict:
    """Decide whether this exception can be closed truthfully, and why."""
    try:
        exception, order, item, allocation, card = _context(session, exception_id)
    except Exception as exc:
        return {"id": exception_id, "ok": False, "why": f"linkage refused: {exc}"}

    if exception.inventory_resolution_state != "unresolved":
        return {"id": exception_id, "ok": False,
                "why": f"already {exception.inventory_resolution_state}"}
    if exception.submission_state != "submitted":
        return {"id": exception_id, "ok": False,
                "why": f"submission_state is {exception.submission_state!r}, "
                       "so Mana Pool was never told"}

    differences = identity_difference(card, item)
    record = {
        "id": exception_id, "exception": exception, "order": order, "item": item,
        "allocation": allocation, "card": card, "differences": differences,
    }
    if not differences:
        # #37's shape, and #2's. The mark was filed against a card that
        # matches the order line, so "could not be fulfilled" would be a
        # lie. Whatever the right answer is, a script cannot know it.
        record.update(ok=False, why=(
            "card identity AGREES with the order line on every field, so this "
            "is not an unfulfillable mismatch. Needs an operator decision: it "
            "is either a false positive (revert) or something else entirely."
        ))
        return record

    record.update(ok=True, why="identity genuinely differs")
    return record


def note_for(record: dict) -> str:
    card, item, order = record["card"], record["item"], record["order"]
    parts = ", ".join(
        f"{field} requested {wanted!r} but the card is {actual!r}"
        for field, (wanted, actual) in sorted(record["differences"].items())
    )
    return (
        f"Closed as unfulfillable, not as a false positive. Order item "
        f"#{item.id} and inventory card #{card.id} genuinely differ: {parts}. "
        f"The allocation could not be fulfilled as specified, and Mana Pool "
        f"reports the order as {order.remote_fulfillment_status or '(unknown)'}. "
        f"The card was subsequently edited by hand (it is now {card.status!r}"
        + (f", removal_reason {card.removal_reason!r}" if card.removal_reason else "")
        + "), which cleared the status and reason pair every resolver requires "
        f"and left this exception unreachable. That gap is closed in v1.159.0. "
        f"This closes the exception's inventory record only: no card status "
        f"changed and no inventory moved."
    )


def apply_close(session: Session, record: dict) -> None:
    exception, card, allocation = record["exception"], record["card"], record["allocation"]
    note = note_for(record)
    timestamp = datetime.now(timezone.utc)
    exception.inventory_resolution_state = "resolved"
    exception.inventory_resolved_at = timestamp.replace(tzinfo=None)
    exception.resolution_note = note
    card.inventory_exception_state = "none"
    _event(
        session, exception, FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT,
        "unresolved", "resolved", note, {
            "exception_type": exception.exception_type,
            "closed_as": "unfulfillable_identity_mismatch",
            "identity_differences": {
                field: {"order_line": wanted, "card": actual}
                for field, (wanted, actual) in record["differences"].items()
            },
            "card_status_unchanged": card.status,
            "operator_metadata": {
                "action": "stranded_exception_close_out",
                "script": "close_stranded_fulfillment_exceptions.py",
            },
            "inventory_card_id": card.id, "allocation_id": allocation.id,
        }, timestamp,
    )
    _projection_audit(session, card, exception, card.status, card.status, note, timestamp)
    session.flush()


def describe(record: dict) -> None:
    if "exception" not in record:
        print(f"  #{record['id']}: REFUSED -- {record['why']}")
        return
    card, item, order = record["card"], record["item"], record["order"]
    verdict = "WILL CLOSE" if record["ok"] else "REFUSED"
    print(f"  #{record['id']:<3} {verdict}")
    print(f"      order   #{order.id} {order.external_label} "
          f"status={order.status} mana_pool={order.remote_fulfillment_status}")
    print(f"      wanted  {item.name!r} {item.set_code}#{item.collector_number} "
          f"finish={item.finish_id} cond={item.condition_id} lang={item.language_id}")
    print(f"      have    card #{card.id} finish={card.finish_id} "
          f"cond={card.condition_id} lang={card.language_id} "
          f"status={card.status} removal_reason={card.removal_reason}")
    if record["differences"]:
        for field, (wanted, actual) in sorted(record["differences"].items()):
            print(f"      DIFFERS {field}: order wants {wanted!r}, card is {actual!r}")
    else:
        print("      identity AGREES on every field")
    if not record["ok"]:
        print(f"      why: {record['why']}")
    else:
        print(f"      note: {note_for(record)[:150]}...")
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="actually write")
    parser.add_argument("--ids", default=",".join(str(i) for i in DEFAULT_EXCEPTION_IDS))
    args = parser.parse_args()
    ids = [int(part) for part in args.ids.split(",") if part.strip()]

    with Session(engine) as session:
        records = [classify(session, exception_id) for exception_id in ids]
        print(f"=== CLASSIFYING {len(records)} EXCEPTIONS ===")
        for record in records:
            describe(record)

        closeable = [r for r in records if r.get("ok")]
        refused = [r for r in records if not r.get("ok")]
        print(f"Closeable: {len(closeable)}   Refused: {len(refused)}")
        if refused:
            print("Refused, needing an operator decision:")
            for record in refused:
                print(f"  #{record['id']}: {record['why']}")
        print()
        print(f"Mode: {'WRITE (--confirm)' if args.confirm else 'DRY RUN (report only)'}")
        if not args.confirm:
            session.rollback()
            print("\nDRY RUN -- nothing written. Re-run with --confirm to apply.")
            return
        if not closeable:
            print("Nothing closeable; nothing written.")
            return
        for record in closeable:
            apply_close(session, record)
        session.commit()
        print(f"\nCommitted: closed {len(closeable)} exceptions.")

    with Session(engine) as session:
        print("\n=== VERIFY ===")
        for record in closeable:
            exception = session.get(FulfillmentException, record["id"])
            print(f"  #{exception.id} inventory_resolution_state="
                  f"{exception.inventory_resolution_state} "
                  f"resolved_at={exception.inventory_resolved_at}")
        remaining = session.query(FulfillmentException).filter_by(
            inventory_resolution_state="unresolved",
        ).count()
        print(f"  exceptions still unresolved app-wide: {remaining}")


if __name__ == "__main__":
    main()
