"""Orders Mana Pool has that CardFoundry could not realize locally.

WHY. Every attention category iterates LOCAL rows, so an order with no
local SalesOrder is invisible to all of them -- including v2.15.0's
shipping-deadline alert, which queries SalesOrder. Order 638925-2261040
(local 4303) sat in exactly that state for 4.7 days and shipped ~6 days
late; the deadline alert could not have caught it, because there was no
order to measure. See models.UningestedRemoteOrder.

ZERO EXTRA MANA POOL CALLS. The hourly order-sync already fetches the
whole needs_shipping listing and already computes the OPPOSITE direction
(order_service.orders_missing_from_remote_listing -- locally-open orders
ABSENT from the listing). This is that same comparison reversed, over the
list the caller is already holding, so it costs one local query.

WHAT COUNTS AS WRONG. As of the pass that calls this, an order in the
listing is unresolved when EITHER
  (a) no local SalesOrder exists for it, or
  (b) this pass's ingest reported a failure against it.
(b) matters on its own: an order whose row already exists but whose
re-sync keeps failing is the shape that leaves placed_at NULL forever,
because _apply_placed_at runs before _build_remote_items and a per-order
rollback discards it.

NEVER RAISES INTO THE SYNC. This is a reporting step bolted onto the end
of a run that has already done the real work, so a failure here must not
fail the tick that just ingested orders successfully. Every exception is
caught, logged on the `cardfoundry` logger, and reported in the summary.
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from models import SalesOrder, UningestedRemoteOrder
from order_service import parse_remote_timestamp

logger = logging.getLogger("cardfoundry")

SOURCE = "manapool"


def parse_ingest_failures(failures) -> dict:
    """``["<remote_id>: <error>", ...]`` -> ``{remote_id: error}``.

    ingest_manapool_orders formats each entry as ``f"{remote_id}: {exc}"``.
    The caller's `failed` list also collects entries from OTHER steps in
    the same tick (cancellation reconciliation, short-order retry) whose
    text is not id-prefixed, so this is deliberately tolerant: anything it
    cannot split on the first colon is skipped rather than guessed at. A
    mis-attributed reason would be worse than no reason.
    """
    reasons = {}
    for entry in failures or []:
        text = str(entry)
        remote_id, separator, detail = text.partition(":")
        if not separator:
            continue
        remote_id = remote_id.strip()
        if not remote_id:
            continue
        reasons[remote_id] = detail.strip() or text
    return reasons


def _remote_label(summary: dict) -> str | None:
    value = str((summary or {}).get("label") or "").strip()
    return value or None


def record_uningested_orders(
    session: Session, remote_orders: list[dict], ingest_failures=None,
    *, now: datetime | None = None,
) -> dict:
    """Reconcile the listing against local rows and write what is missing.

    Caller commits. Returns counts for the run's log line.
    """
    now = now or datetime.now()
    reasons = parse_ingest_failures(ingest_failures)
    summary = {"unresolved": 0, "newly_recorded": 0, "resolved": 0}

    listed = {}
    for remote in remote_orders or []:
        remote_id = str((remote or {}).get("id") or "").strip()
        if remote_id:
            listed[remote_id] = remote

    # One query for every id in the listing, not one per order.
    known_locally = set()
    if listed:
        known_locally = {
            row[0] for row in session.query(SalesOrder.external_order_id).filter(
                SalesOrder.source == SOURCE,
                SalesOrder.external_order_id.in_(tuple(listed)),
            ).all()
        }

    existing = {
        row.external_order_id: row
        for row in session.query(UningestedRemoteOrder).filter(
            UningestedRemoteOrder.source == SOURCE,
        ).all()
    }

    for remote_id, remote in listed.items():
        reason = reasons.get(remote_id)
        missing = remote_id not in known_locally
        row = existing.get(remote_id)

        if not missing and not reason:
            # Healed, or never broken. Resolution is automatic by design.
            if row is not None and row.resolved_at is None:
                row.resolved_at = now
                summary["resolved"] += 1
                logger.info(
                    "uningested orders: %s is now present locally and "
                    "failure-free; resolved.", remote_id,
                )
            continue

        summary["unresolved"] += 1
        if row is None:
            row = UningestedRemoteOrder(
                source=SOURCE,
                external_order_id=remote_id,
                first_seen_at=now,
            )
            session.add(row)
            summary["newly_recorded"] += 1
            logger.warning(
                "uningested orders: Mana Pool order %s (%s) is in the "
                "needs_shipping listing but %s. It is invisible to every "
                "order alarm until a local row exists.",
                remote_id, _remote_label(remote) or "no label",
                "ingest failed for it" if reason else "has no local order",
            )
        row.resolved_at = None
        row.last_seen_at = now
        row.external_label = _remote_label(remote) or row.external_label
        row.remote_created_at = (
            parse_remote_timestamp((remote or {}).get("created_at"))
            or row.remote_created_at
        )
        row.failure_reason = (reason or None)

    # An order that has DROPPED OUT of the listing is not automatically
    # fine: if we never ingested it, we have permanently missed it, and
    # the listing can no longer tell us anything about it. So resolve it
    # only on the evidence that actually settles the question -- a local
    # row now exists. Otherwise it stays unresolved and keeps asking.
    absent = [
        row for row in existing.values()
        if row.resolved_at is None and row.external_order_id not in listed
    ]
    for row in absent:
        exists = session.query(SalesOrder.id).filter(
            SalesOrder.source == SOURCE,
            SalesOrder.external_order_id == row.external_order_id,
        ).first()
        if exists:
            row.resolved_at = now
            summary["resolved"] += 1
        else:
            summary["unresolved"] += 1

    logger.info(
        "uningested orders: unresolved=%s newly_recorded=%s resolved=%s",
        summary["unresolved"], summary["newly_recorded"], summary["resolved"],
    )
    return summary


def unresolved_orders(session: Session) -> list[UningestedRemoteOrder]:
    """Oldest order date first -- the longest-missing order is the most
    urgent, and a NULL remote date sorts last rather than first so a row
    with no date cannot crowd out one with a known deadline."""
    return (
        session.query(UningestedRemoteOrder)
        .filter(
            UningestedRemoteOrder.source == SOURCE,
            UningestedRemoteOrder.resolved_at.is_(None),
        )
        .order_by(
            UningestedRemoteOrder.remote_created_at.is_(None),
            UningestedRemoteOrder.remote_created_at,
            UningestedRemoteOrder.id,
        )
        .all()
    )
