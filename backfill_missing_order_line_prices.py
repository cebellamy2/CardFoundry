"""One-off (operator-approved 2026-09-15): recover the handful of order
lines that have no stored price_cents.

WHY IT MATTERS NOW. price_cents is what the Orders list, Order Detail and
the packing slip all render, and it is what the consignor payout math
ultimately trusts (sold_price is written from price_cents / 100 at ship
time). A line with no price makes its order's total UNKNOWABLE -- every
surface deliberately shows an em dash rather than a confident understated
figure. Recovering the price removes the gap at the source instead of
teaching the pages to paper over it.

SCOPE. Measured 2026-09-15: 5 lines across 4 orders (#3, #8, #32, #37),
all from 14-15 August, all tied to the earliest fulfillment exceptions,
NONE on a consigned card, so no payout was ever computed from a missing
value. One paced Mana Pool call per order.

MATCHING is by the identity fields the app already treats as canonical --
mtgjson_id, language_id, condition_id, finish_id, set_code and collector
number -- the same tuple _line_signature uses to decide whether a remote
line is the same line. A remote line that does not match exactly is left
alone and reported; guessing which line a price belongs to is how the
wrong card gets the wrong money.

It writes price_cents and an InventoryChangeLog entry per affected line's
card. It deliberately does NOT touch sold_price: these five cards never
sold (they went to fulfillment exceptions), so there is nothing to
correct, and inventing one would create a sale that did not happen.

Dry-run by default. Pass --confirm to write.
"""

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from competitor_pricing_service import _RequestPacer
from database import engine
from manapool_service import get_seller_order
from models import InventoryCard, InventoryChangeLog, OrderItem, PickAllocation, SalesOrder
from order_service import ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS


def _identity(source) -> tuple:
    return (
        (getattr(source, "mtgjson_id", None) or "").lower(),
        (getattr(source, "language_id", None) or "").upper(),
        (getattr(source, "condition_id", None) or "").upper(),
        (getattr(source, "finish_id", None) or "").upper(),
        (getattr(source, "set_code", None) or "").upper(),
        (getattr(source, "collector_number", None) or "").upper(),
    )


def _remote_identity(remote_item: dict) -> tuple:
    single = ((remote_item.get("product") or {}).get("single")) or {}
    return (
        str(single.get("mtgjson_id") or "").lower(),
        str(single.get("language_id") or "").upper(),
        str(single.get("condition_id") or "").upper(),
        str(single.get("finish_id") or "").upper(),
        str(single.get("set") or "").upper(),
        str(single.get("number") or "").upper(),
    )


def find_gaps(session: Session) -> list:
    return (
        session.query(OrderItem)
        .filter(OrderItem.price_cents.is_(None))
        .order_by(OrderItem.id)
        .all()
    )


def run(confirm: bool) -> dict:
    pacer = _RequestPacer(ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS)
    result = {"found": 0, "matched": 0, "written": 0, "unmatched": [], "calls": 0}

    with Session(engine) as session:
        gaps = find_gaps(session)
        result["found"] = len(gaps)
        by_order = {}
        for item in gaps:
            by_order.setdefault(item.order_id, []).append(item)

        print(f"Order lines with no stored price: {len(gaps)} across {len(by_order)} orders")
        print(f"Mode: {'WRITE (--confirm)' if confirm else 'DRY RUN (report only)'}")
        print()

        for order_id, items in sorted(by_order.items()):
            order = session.get(SalesOrder, order_id)
            pacer.wait()
            try:
                detail = (get_seller_order(order.external_order_id).get("order") or {})
                result["calls"] += 1
            except Exception as exc:
                for item in items:
                    result["unmatched"].append(f"item {item.id}: fetch failed: {exc}")
                print(f"  order #{order_id} {order.external_label}: FETCH FAILED {exc}")
                continue

            remote_by_identity = {}
            for remote_item in detail.get("items") or []:
                remote_by_identity.setdefault(_remote_identity(remote_item), remote_item)

            print(f"  order #{order_id} {order.external_label}")
            for item in items:
                remote = remote_by_identity.get(_identity(item))
                if not remote or remote.get("price_cents") is None:
                    result["unmatched"].append(
                        f"item {item.id} ({item.name}): no exact identity match remotely"
                    )
                    print(f"    item {item.id:<6} {item.name[:28]:<28} NO MATCH -- left alone")
                    continue
                price = int(remote["price_cents"])
                result["matched"] += 1
                print(f"    item {item.id:<6} {item.name[:28]:<28} "
                      f"price_cents None -> {price}  (${price / 100:.2f})")
                if not confirm:
                    continue

                item.price_cents = price
                allocation = (
                    session.query(PickAllocation)
                    .filter(PickAllocation.order_item_id == item.id)
                    .first()
                )
                card = session.get(InventoryCard, allocation.inventory_card_id) if allocation else None
                if card:
                    session.add(InventoryChangeLog(
                        inventory_card_id=card.id,
                        change_summary=json.dumps({
                            "action_type": "order_line_price_backfill",
                            "order_item_id": item.id,
                            "sales_order_id": order_id,
                            "previous_price_cents": None,
                            "new_price_cents": price,
                            "source": "mana pool order detail",
                            "note": (
                                "Recovered a line price that was never captured at "
                                "ingest. sold_price deliberately untouched: this card "
                                "never sold."
                            ),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }, sort_keys=True),
                    ))
                result["written"] += 1

        if confirm:
            session.commit()

    print()
    print(f"found={result['found']} matched={result['matched']} "
          f"written={result['written']} calls={result['calls']}")
    for problem in result["unmatched"]:
        print(f"  UNMATCHED {problem}")
    if not confirm:
        print("\nDRY RUN -- nothing written. Re-run with --confirm to apply.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true")
    run(parser.parse_args().confirm)


if __name__ == "__main__":
    main()
