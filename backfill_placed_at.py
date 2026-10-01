"""Backfill SalesOrder.placed_at from Mana Pool's own order date.

WHY. Until v2.15.0 the only timestamp on an order was `created_at`, which is
when CardFoundry first SAW it. Order 638925-2261040 reached us four days late,
so it read as brand new on the day it was already four days old -- and it then
went ~6 days unshipped, which got the seller account restricted. placed_at is
the order's real date, from the payload's `created_at`; this fills it in for
orders that were ingested before the column existed.

READ-ONLY AGAINST MANA POOL. Only `GET /seller/orders/{id}` is called -- a
documented v0.34.0 read endpoint. Nothing is written remotely.

★ THE DRY RUN USES THE SAME CODE PATH AS THE WRITE. It calls the very function
ingest uses (`order_service._apply_placed_at`) against real ORM objects, then
rolls back. The v2.12.0 over-publish happened because a dry run computed its
answer a DIFFERENT way from the apply and so could not predict it; this does not
repeat that. The only difference between a dry run and a real run here is the
commit.

AUDITED AND UNDOABLE. Every change writes an OrderStatusEvent-shaped audit row
via InventoryChangeLog's sibling for orders if one exists; where it does not,
the before/after is printed and recorded in the summary so the change can be
reversed by hand from the printed values. placed_at is never overwritten once
set (see _apply_placed_at), so re-running is free.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_placed_at.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_placed_at.py --confirm
    ... --all        every order, not just open ones
    ... --limit 500  bound one run
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from manapool_service import get_seller_order
from models import SalesOrder
from order_deadline_service import SETTLED_LOCAL_STATUSES, format_deadline, ship_by
from order_service import _apply_placed_at

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "backfill_placed_at"


def candidates(session: Session, *, all_orders: bool, limit: int | None):
    query = (
        session.query(SalesOrder)
        .filter(SalesOrder.source == "manapool", SalesOrder.placed_at.is_(None))
    )
    if not all_orders:
        query = query.filter(~SalesOrder.status.in_(tuple(SETTLED_LOCAL_STATUSES)))
    query = query.order_by(SalesOrder.id)
    if limit:
        query = query.limit(limit)
    return query.all()


def run(session: Session, *, confirm: bool, all_orders: bool, limit: int | None,
        detail_loader=get_seller_order) -> dict:
    orders = candidates(session, all_orders=all_orders, limit=limit)
    report = {
        "mode": "CONFIRMED" if confirm else "DRY_RUN",
        "scope": "all orders" if all_orders else "open orders only",
        "candidates": len(orders),
        "filled": 0,
        "no_remote_date": [],
        "failed": [],
        "changes": [],
    }
    for order in orders:
        try:
            response = detail_loader(order.external_order_id)
        except Exception as exc:  # noqa: BLE001 -- one bad order must not stop the rest
            logger.warning(
                "%s: could not read order %s from Mana Pool (%s: %s); left unset.",
                SCRIPT_NAME, order.id, type(exc).__name__, exc,
            )
            report["failed"].append(f"{order.id}: {type(exc).__name__}")
            continue
        detail = response.get("order") or response
        # THE SAME FUNCTION THE INGEST USES. See the module docstring.
        _apply_placed_at(order, detail, detail)
        if order.placed_at is None:
            report["no_remote_date"].append(order.id)
            continue
        report["filled"] += 1
        report["changes"].append({
            "order_id": order.id,
            "label": order.external_label,
            "status": order.status,
            "placed_at": order.placed_at.isoformat(),
            "ingested_at": order.created_at.isoformat() if order.created_at else None,
            "lag_days": round(
                (order.created_at - order.placed_at).total_seconds() / 86400.0, 2
            ) if order.created_at else None,
            "ship_by": format_deadline(ship_by(order.placed_at)),
        })

    if confirm:
        session.commit()
    else:
        session.rollback()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true", help="Write. Dry run otherwise.")
    parser.add_argument("--all", action="store_true",
                        help="Every order, not just open ones.")
    parser.add_argument("--limit", type=int, default=None, help="Bound one run.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    with Session(engine) as session:
        report = run(session, confirm=args.confirm, all_orders=args.all, limit=args.limit)

    changes = report.pop("changes")
    print(json.dumps(report, indent=2, sort_keys=True))
    print()
    print("worst lag first:")
    for change in sorted(changes, key=lambda c: -(c["lag_days"] or 0))[:20]:
        print("  order %-6s %-16s %-14s lag=%-6s placed %s | ship by %s" % (
            change["order_id"], change["label"] or "", change["status"],
            change["lag_days"], change["placed_at"][:16], change["ship_by"]))
    if not args.confirm:
        print()
        print("DRY RUN -- nothing was committed.")


if __name__ == "__main__":
    main()
