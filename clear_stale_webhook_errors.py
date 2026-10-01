"""Drop failure notes from webhook deliveries that have since succeeded.

WHY. Until this release `_finish` only ever WROTE `last_error`; it wrote
nothing when `error` was None. So a delivery that failed once and
succeeded on retry kept its failure text forever. Order 4303's delivery
62 is the live example: status `already_known`, with a pre-v2.8.0
UNIQUE-constraint error still attached. `_finish` now clears it going
forward, and this fixes the rows that predate that.

★ THE DRY RUN GOES THROUGH THE SAME CODE AS THE WRITE. It calls
`manapool_webhook_service.clear_stale_error` -- the very function `_finish`
now calls -- against real ORM rows, then rolls back. The only difference
between a dry run and a real run is the commit. A repair that computed its
own answer separately would only be a guess about what the write would do.

READ-ONLY AGAINST MANA POOL: no request of any kind is made. This touches
one nullable local column on rows that are already terminal and already
successful; it changes no status and no order.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python clear_stale_webhook_errors.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python clear_stale_webhook_errors.py --confirm
    ... --delivery-id 62   just that one row
"""
import argparse
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from manapool_webhook_service import SUCCESS_STATUSES, clear_stale_error
from models import WebhookDelivery

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "clear_stale_webhook_errors"


def candidates(session: Session, *, delivery_id: int | None = None):
    query = session.query(WebhookDelivery).filter(
        WebhookDelivery.processing_status.in_(SUCCESS_STATUSES),
        WebhookDelivery.last_error.isnot(None),
        WebhookDelivery.last_error != "",
    )
    if delivery_id is not None:
        query = query.filter(WebhookDelivery.id == delivery_id)
    return query.order_by(WebhookDelivery.id).all()


def run(session: Session, *, confirm: bool, delivery_id: int | None = None) -> dict:
    rows = candidates(session, delivery_id=delivery_id)
    report = {
        "mode": "CONFIRMED" if confirm else "DRY_RUN",
        "candidates": len(rows),
        "cleared": 0,
        "changes": [],
    }
    for row in rows:
        before = row.last_error
        # THE SAME FUNCTION _finish USES. See the module docstring.
        changed = clear_stale_error(row)
        if not changed:
            continue
        report["cleared"] += 1
        report["changes"].append({
            "delivery_id": row.id,
            "status": row.processing_status,
            "attempts": row.attempts,
            "external_order_id": row.external_order_id,
            # Truncated so a long SQL error does not bury the summary. The
            # full text is still in the log line this script's caller sees.
            "last_error_before": (before or "")[:160],
            "last_error_after": row.last_error,
        })
        logger.info(
            "%s: delivery %s is %s and succeeded; cleared its stale error.",
            SCRIPT_NAME, row.id, row.processing_status,
        )

    if confirm:
        session.commit()
    else:
        session.rollback()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="Write. Dry run otherwise.")
    parser.add_argument("--delivery-id", type=int, default=None,
                        help="Just this one delivery.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    set_script_actor(SCRIPT_NAME)

    with Session(engine) as session:
        report = run(session, confirm=args.confirm, delivery_id=args.delivery_id)

    print("mode:       %s" % report["mode"])
    print("candidates: %s" % report["candidates"])
    print("cleared:    %s" % report["cleared"])
    for change in report["changes"]:
        print()
        print("  delivery %s (%s, attempts=%s) order=%s" % (
            change["delivery_id"], change["status"], change["attempts"],
            change["external_order_id"]))
        print("    before: %r" % change["last_error_before"])
        print("    after:  %r" % change["last_error_after"])
    if not args.confirm:
        print()
        print("DRY RUN -- nothing was committed.")


if __name__ == "__main__":
    main()
