"""Preview slice 7: acting on a card found AFTER it was reported to Mana Pool.

WHAT IS NEW. Pressing "Submitted to ManaPool" closes the exception's
inventory record (CF-AUTORESOLVE-001), after which
revert_fulfillment_exception_mark and confirm_substitution both refuse -- so
the operator had nothing to click on a card he then found. slice 7 adds two
outcomes he chooses between:

  back_to_order  Mana Pool has NOT acted: the card goes back on the order and
                 ships. Sellable stock does not move -> NO Mana Pool write.
  back_to_stock  Mana Pool refunded/replaced the line: the card becomes
                 sellable again and the order is left alone. Sellable stock
                 goes up by one -> ONE QUANTITY WRITE.

★ WHY THIS SCRIPT RUNS THE REAL PATH. It calls the real
mark_reported_card_found inside a transaction that is rolled back, so the
guards, the card disposition reversal, the allocation move and the decision
about whether a push is needed are all the production code. The push itself
is NEVER made: the script resolves the binding and computes the quantity the
write WOULD carry, before and after, and prints it.

NO MANA POOL WRITES. Reads only: one seller-inventory walk, to report each
listing's current remote quantity. The transaction is rolled back regardless.

Usage, in the container:
    PYTHONPATH=/app /opt/venv/bin/python dry_run_reported_card_found.py --plan
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "dry_run_reported_card_found"


def _actionable(session):
    """Reported exceptions whose allocation AND card are still exactly where
    mark_fulfillment_exception left them -- the only ones any of this can
    act on."""
    from models import FulfillmentException, InventoryCard, PickAllocation, SalesOrder

    rows = []
    for exception in session.query(FulfillmentException).order_by(
        FulfillmentException.id,
    ).all():
        allocation = session.get(PickAllocation, exception.pick_allocation_id)
        card = session.get(InventoryCard, exception.inventory_card_id)
        order = session.get(SalesOrder, exception.sales_order_id)
        if not allocation or not card or not order:
            continue
        if allocation.status != "exception":
            continue
        if exception.exception_type == "missing":
            ok = card.status == "removed" and card.removal_reason == "fulfillment_missing"
        else:
            ok = (card.status == "unsellable"
                  and card.unsellable_reason == "fulfillment_inventory_mismatch")
        if ok:
            rows.append((exception, order, allocation, card))
    return rows


def _quantity_write(session, card, remote_by_product):
    """The write push_for_cards WOULD make for this card, without making it."""
    from manapool_quantity_push_service import (
        _desired_quantity_for_binding, _resolve_binding_for_card,
    )

    binding = _resolve_binding_for_card(session, card)
    if binding is None:
        return {"binding": None, "note": "card resolves to no validated binding"}
    item = remote_by_product.get(str(binding.product_id)) or {}
    return {
        "binding": binding.id,
        "product_id": binding.product_id,
        "variant": f"{binding.language_id}/{binding.condition_id}/{binding.finish_id}",
        "remote_quantity_now": int(item.get("quantity") or 0) if item else None,
        "desired_quantity_now": _desired_quantity_for_binding(session, binding),
    }


def _describe(label, exception, order, allocation, card):
    print(f"  {label}")
    print(f"     exception {exception.id} ({exception.exception_type}) "
          f"submission={exception.submission_state!r} "
          f"remote={exception.remote_resolution_state!r} "
          f"inventory={exception.inventory_resolution_state!r}")
    print(f"     order {order.id} ({order.external_label or order.external_order_id}) "
          f"status={order.status!r}")
    print(f"     card {card.id} {card.name} status={card.status!r} "
          f"allocation={allocation.status!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", required=True,
                        help="The only mode. Rolls back; sends nothing.")
    args = parser.parse_args()
    assert args.plan

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    from fulfillment_exception_constants import (
        FOUND_OUTCOME_BACK_TO_ORDER, FOUND_OUTCOME_BACK_TO_STOCK,
        REMOTE_STATES_MANA_POOL_HAS_ACTED,
    )
    from fulfillment_exception_resolution_service import (
        FOUND_BACK_TO_ORDER_ORDER_STATUSES, mark_reported_card_found,
        undo_reported_card_found,
    )
    from fulfillment_exception_service import FulfillmentExceptionError
    from manapool_service import get_all_seller_inventory

    remote = get_all_seller_inventory(min_quantity=0)
    remote_by_product = {
        str(i.get("product_id")): i for i in remote if i.get("product_id")
    }
    print(f"seller inventory rows read: {len(remote)}")

    with Session(engine) as session:
        try:
            rows = _actionable(session)
            print(f"actionable reported exceptions: {len(rows)}")
            case_a = [
                r for r in rows
                if r[1].status in FOUND_BACK_TO_ORDER_ORDER_STATUSES
                and r[0].remote_resolution_state not in REMOTE_STATES_MANA_POOL_HAS_ACTED
            ]
            case_b = [
                r for r in rows
                if r[0].remote_resolution_state in REMOTE_STATES_MANA_POOL_HAS_ACTED
            ]
            print(f"  case (a) candidates (order can still ship, Mana Pool silent): {len(case_a)}")
            print(f"  case (b) candidates (Mana Pool has acted):                    {len(case_b)}")
            print()

            # ---------------- case (b), on a real exception ----------------
            print("=== CASE (b) BACK TO STOCK -- REAL REPORTED EXCEPTION ===")
            if not case_b:
                print("  none available")
            else:
                exception, order, allocation, card = case_b[0]
                _describe("before:", exception, order, allocation, card)
                before = _quantity_write(session, card, remote_by_product)
                result = mark_reported_card_found(
                    session, exception.id,
                    outcome=FOUND_OUTCOME_BACK_TO_STOCK,
                    note=f"{SCRIPT_NAME} dry run",
                )
                session.flush()
                _describe("after:", exception, order, allocation, card)
                after = _quantity_write(session, card, remote_by_product)
                print(f"     submission_state untouched: "
                      f"{exception.submission_state!r}")
                print("     THE MANA POOL WRITE THIS WOULD MAKE:")
                print(f"       binding {after['binding']} product {after.get('product_id')} "
                      f"({after.get('variant')})")
                print(f"       remote quantity now {after.get('remote_quantity_now')}; "
                      f"desired {before.get('desired_quantity_now')} -> "
                      f"{after.get('desired_quantity_now')}")
                print(f"       cards_to_push: {[c.id for c in result['cards_to_push']]}")
                undo = undo_reported_card_found(
                    session, exception.id, f"{SCRIPT_NAME} dry run undo",
                )
                session.flush()
                _describe("after undo:", exception, order, allocation, card)
                print(f"       undo cards_to_push: {[c.id for c in undo['cards_to_push']]}")
                print(f"       undo desired would be: "
                      f"{_quantity_write(session, card, remote_by_product).get('desired_quantity_now')}")
            print()

            # ------------- case (a): the real refusal, then a what-if ------
            print("=== CASE (a) BACK TO ORDER -- REAL REPORTED EXCEPTION ===")
            if case_a:
                exception, order, allocation, card = case_a[0]
                _describe("before:", exception, order, allocation, card)
                result = mark_reported_card_found(
                    session, exception.id,
                    outcome=FOUND_OUTCOME_BACK_TO_ORDER,
                    note=f"{SCRIPT_NAME} dry run",
                )
                session.flush()
                _describe("after:", exception, order, allocation, card)
                print(f"     cards_to_push (must be empty): "
                      f"{[c.id for c in result['cards_to_push']]}")
            else:
                print("  NO case (a) candidate exists in production. Every")
                print("  actionable reported exception sits on an order that has")
                print("  already shipped or been cancelled -- case (a) only")
                print("  arises while the wave is still in flight, which is the")
                print("  window slice 7 opens and that has never existed before.")
                print()
                print("  The real refusal, on a real exception:")
                exception, order, allocation, card = rows[0]
                _describe("subject:", exception, order, allocation, card)
                try:
                    mark_reported_card_found(
                        session, exception.id,
                        outcome=FOUND_OUTCOME_BACK_TO_ORDER,
                        note=f"{SCRIPT_NAME} dry run",
                    )
                except FulfillmentExceptionError as exc:
                    print(f"     REFUSED: {exc}")
                else:
                    print("     !! expected a refusal and did not get one")
                print()
                print("  WHAT-IF, clearly synthetic: the same real exception with")
                print("  its order moved back to 'picked' inside this rolled-back")
                print("  transaction, so case (a) can be exercised on real data.")
                was = order.status
                order.status = "picked"
                exception.remote_resolution_state = "awaiting"
                session.flush()
                result = mark_reported_card_found(
                    session, exception.id,
                    outcome=FOUND_OUTCOME_BACK_TO_ORDER,
                    note=f"{SCRIPT_NAME} what-if",
                )
                session.flush()
                _describe("after:", exception, order, allocation, card)
                print(f"     submission_state untouched: {exception.submission_state!r}")
                print(f"     cards_to_push (must be EMPTY -- no quantity write): "
                      f"{[c.id for c in result['cards_to_push']]}")
                undo = undo_reported_card_found(
                    session, exception.id, f"{SCRIPT_NAME} what-if undo",
                )
                session.flush()
                _describe("after undo:", exception, order, allocation, card)
                print(f"     undo cards_to_push (must be EMPTY): "
                      f"{[c.id for c in undo['cards_to_push']]}")
                order.status = was
        finally:
            session.rollback()
            print()
            print("PLAN ONLY -- transaction rolled back, no Mana Pool request made.")


if __name__ == "__main__":
    main()
