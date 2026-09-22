"""Inbound Mana Pool `order_created` webhook: verify, record, then ingest.

WHY THIS EXISTS. A new Mana Pool order reached CardFoundry only when the
hourly poll next ran. Measured on production 2026-09-18 over the 97
orders since v1.143.0: median 24 minutes, p90 53 minutes, worst case 124
minutes. Nothing was ever lost -- zero orders failed to arrive -- they
were simply late, and an order nobody can pick is an order that is not
being fulfilled. This path makes the same order appear in seconds.

THE POLL STAYS, UNCHANGED, AT HOURLY. Mana Pool has exactly ONE webhook
topic, `order_created`. There is no updated, refunded, shipped or
cancelled topic -- the enum has a single value in all four places it
appears in the spec. So every other thing that can happen to an order
still reaches CardFoundry only through the poll, which remains the only
path for cancellations, refunds, replacements and reconciliation. This
is push-FIRST, not push-instead.

ORDER OF OPERATIONS, AND IT IS NOT NEGOTIABLE:

    verify -> persist -> answer 2xx -> process

Mana Pool's spec documents the signature scheme and the payload in
complete detail and says NOTHING about delivery guarantees: no retries,
no redelivery, no ordering, no duplicate policy, no response deadline
beyond the 10 seconds the registration probe allows. The only safe
reading of that silence is that a non-2xx may lose the order forever.
So the receiver must never answer with a failure it could avoid -- in
particular it must NEVER return 409 because the inventory lease was
busy, which is an ordinary, expected, entirely local condition that has
nothing to do with Mana Pool. It takes the delivery, commits it, says
yes, and sorts the lease out on its own time.

The persist-before-answer half matters just as much. FastAPI
BackgroundTasks run inside the web process, so a deploy mid-flight kills
them silently -- main.py documents the case that proved it, a pricing
job frozen at 121/306 batches for 9+ hours across five deploys. A
background task is only safe here because the row is already committed
before it starts: the work can die, and the delivery is still on record
and still re-runnable.

WHY IT CALLS THE BATCH INGEST WITH ONE ORDER. `ingest_manapool_orders`
with a one-item list, not `_sync_one_manapool_order` directly, because
the batch function is where `validate_inventory_invariants`, the
per-order commit, and the per-order failure isolation live. A path that
can now fire at any moment of the day is the last place to skip the
invariant check. The `detail_loader` hands back the body we already
have, so this costs ZERO Mana Pool API calls, and `min_request_interval`
is 0 because there is no request to pace.

Allocation is not a separate step: `allocate_order` runs inside the
per-order core, so ingesting an order here also makes it pickable. That
is the whole point of processing immediately rather than recording and
waiting.

IDEMPOTENCY IS INHERITED, NOT ADDED. `_sync_one_manapool_order` keys on
(source="manapool", external_order_id) and returns "already_known" for
an order it has seen. The poll will see every webhook-ingested order
again within the hour and that second sighting is a no-op today, with no
change needed here. Duplicate deliveries are handled by the same
property.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime

from sqlalchemy.orm import Session

from inventory_sync_service import InventoryLeaseBusy, inventory_sync_lease
from models import WebhookDelivery
from order_service import ingest_manapool_orders


logger = logging.getLogger("cardfoundry")


ENABLED_ENV = "MANAPOOL_WEBHOOK_ENABLED"
SECRET_ENV = "MANAPOOL_WEBHOOK_SECRET"

# Operator decision 2026-09-18. Mana Pool's own guidance is "reject
# requests that are too old for your needs" with no number attached, so
# this is ours: five minutes either side of our clock.
TIMESTAMP_TOLERANCE_SECONDS = 300

# Operator decision 2026-09-18: keep trying for ten minutes, then give up
# and SAY SO. The backoff is short at the start because a lease is
# usually held for seconds, and the ceiling exists because the hourly
# poll will ingest the order anyway -- past ten minutes the retry has
# stopped being the thing that saves the order and has become a
# background loop nobody is watching.
RETRY_BACKOFF_SECONDS = (5, 10, 20, 30)
RETRY_BUDGET_SECONDS = 600

VERIFICATION_EVENT = "verification"
ORDER_CREATED_EVENT = "order_created"

# processing_status values that mean "this delivery has not landed yet"
# -- what the attention page shows and what the sweep retries.
UNFINISHED_STATUSES = ("pending", "stranded", "failed")


def webhook_enabled() -> bool:
    return str(os.environ.get(ENABLED_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def webhook_secret() -> str:
    return str(os.environ.get(SECRET_ENV, "") or "").strip()


def _parse_signature_header(value: str) -> dict:
    """`t=<unix>,v1=<hex>` into a dict. Unknown keys are kept and ignored.

    The spec calls the header "comma-delimited key/value pairs (currently
    t and v1)" -- "currently" is an explicit promise of more later, so
    this parses the shape rather than the two names it happens to carry
    today.
    """
    parts = {}
    for chunk in str(value or "").split(","):
        key, _, val = chunk.partition("=")
        key = key.strip()
        if key:
            parts[key] = val.strip()
    return parts


def expected_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """The v1 digest, over the EXACT bytes received.

    Signing payload is `v1:{timestamp}:{rawBody}` per the spec. raw_body
    stays bytes throughout: re-serializing the JSON would reorder keys or
    change spacing and produce a different, wrong digest -- the most
    likely way to build a verifier that rejects every genuine delivery.
    """
    payload = f"v1:{timestamp}:".encode("utf-8") + raw_body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_signature(
    *, secret: str, signature_header: str, timestamp_header: str, raw_body: bytes,
    now: float | None = None,
) -> str:
    """One of: verified | missing_secret | stale_timestamp | invalid_signature.

    Returns a status rather than raising, because every outcome here --
    including rejection -- is recorded, and the caller needs the reason
    to record.

    The header's own `t` must equal X-ManaPool-Timestamp. They are two
    copies of one value, so disagreement means the request was
    assembled by something that did not sign it, and there is no reading
    where trusting either copy is right.
    """
    if not secret:
        return "missing_secret"
    parts = _parse_signature_header(signature_header)
    supplied = parts.get("v1") or ""
    signed_ts = parts.get("t") or ""
    if not supplied or not signed_ts:
        return "invalid_signature"
    header_ts = str(timestamp_header or "").strip()
    if not header_ts or signed_ts != header_ts:
        return "invalid_signature"
    try:
        ts = int(header_ts)
    except (TypeError, ValueError):
        return "invalid_signature"
    current = time.time() if now is None else now
    if abs(current - ts) > TIMESTAMP_TOLERANCE_SECONDS:
        return "stale_timestamp"
    # Constant-time: a byte-by-byte comparison leaks how much of a forged
    # digest was right, which is enough to build the rest of it.
    if not hmac.compare_digest(expected_signature(secret, header_ts, raw_body), supplied):
        return "invalid_signature"
    return "verified"


def _order_id_from_body(raw_body: bytes) -> str | None:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        # Only SIGNATURE-VERIFIED bodies reach here, so an unparseable one
        # means Mana Pool sent something this code does not understand --
        # rare, and worth knowing about. The delivery row is still
        # recorded; only the order id is lost.
        logger.warning(
            "webhook: verified delivery body is not parseable JSON, order "
            "id unavailable: %s: %s", type(exc).__name__, exc,
        )
        return None
    order = (payload or {}).get("order") or {}
    value = str(order.get("id") or "").strip()
    return value or None


def record_delivery(
    session: Session, *, event: str, raw_body: bytes, signature_status: str,
    timestamp_header: str,
) -> int:
    """Write the delivery down and COMMIT, before anything else happens.

    Returns the row id. Committing here rather than at the end of the
    request is the whole design: the 2xx that follows is a promise that
    this delivery is durably ours, and the promise has to be true at the
    moment it is made.
    """
    try:
        ts = int(str(timestamp_header or "").strip())
    except (TypeError, ValueError):
        ts = None
    row = WebhookDelivery(
        source="manapool",
        event=event or None,
        external_order_id=_order_id_from_body(raw_body),
        received_at=datetime.now(),
        timestamp_header=ts,
        signature_status=signature_status,
        raw_body=raw_body.decode("utf-8", errors="replace"),
        processing_status="pending",
        attempts=0,
    )
    session.add(row)
    session.commit()
    return row.id


def _ingest_one(session: Session, order: dict) -> dict:
    """The existing batch ingest, handed exactly one order and no network.

    detail_loader returns the body we were delivered, so ingest makes no
    Mana Pool request at all; min_request_interval=0 because pacing
    exists to space out requests that here do not happen.
    """
    return ingest_manapool_orders(
        session, [order], lambda _id: {"order": order},
        min_request_interval=0,
    )


def process_delivery(
    delivery_id: int, *, session_factory, sleep=time.sleep, now=time.monotonic,
    budget_seconds: int = RETRY_BUDGET_SECONDS,
) -> str:
    """Ingest one recorded delivery, retrying only a busy inventory lease.

    Returns the final processing_status.

    A busy lease is not an error and is never reported as one: another
    inventory operation is simply running, which on a bench in use is
    most of the time. It waits, and if the budget runs out it marks the
    delivery `stranded` -- which means "visible on Orders Needing
    Attention", not "lost". The hourly poll ingests the order regardless;
    the status exists so the page can be honest about what this path did
    and did not manage.
    """
    deadline = now() + budget_seconds
    attempt = 0
    while True:
        with session_factory() as session:
            row = session.get(WebhookDelivery, delivery_id)
            if row is None:
                logger.warning("webhook processing: delivery %s vanished", delivery_id)
                return "missing"
            if row.processing_status in ("processed", "already_known"):
                return row.processing_status
            raw = (row.raw_body or "").encode("utf-8")
            row.attempts = (row.attempts or 0) + 1
            attempt = row.attempts
            session.commit()

        try:
            payload = json.loads(raw.decode("utf-8"))
            order = (payload or {}).get("order")
            if not isinstance(order, dict) or not str(order.get("id") or "").strip():
                raise ValueError("delivery body has no usable order object")
        except Exception as exc:
            logger.warning(
                "webhook processing: delivery %s has an unusable body: %s: %s",
                delivery_id, type(exc).__name__, exc,
            )
            return _finish(session_factory, delivery_id, "failed", error=str(exc))

        try:
            with inventory_sync_lease():
                with session_factory() as session:
                    result = _ingest_one(session, order)
        except InventoryLeaseBusy:
            remaining = deadline - now()
            if remaining <= 0:
                logger.warning(
                    "webhook processing: delivery %s stranded after %s attempts -- "
                    "the inventory lease stayed busy for the whole %ss budget. The "
                    "hourly poll will still ingest this order.",
                    delivery_id, attempt, budget_seconds,
                )
                return _finish(session_factory, delivery_id, "stranded",
                               error="inventory lease busy for the whole retry budget")
            wait = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            sleep(min(wait, max(remaining, 0)))
            continue
        except Exception as exc:
            logger.warning(
                "webhook processing: delivery %s failed to ingest: %s: %s",
                delivery_id, type(exc).__name__, exc,
            )
            return _finish(session_factory, delivery_id, "failed",
                           error=f"{type(exc).__name__}: {exc}")

        failed = result.get("failed") or []
        if failed:
            detail = json.dumps(failed, default=str)[:2000]
            logger.warning(
                "webhook processing: delivery %s -- ingest reported a per-order "
                "failure: %s", delivery_id, detail,
            )
            return _finish(session_factory, delivery_id, "failed",
                           error=detail, ingest_result=json.dumps(result, default=str)[:2000])
        status = "already_known" if result.get("already_known") else "processed"
        logger.info(
            "webhook processing: delivery %s -> %s (imported=%s already_known=%s)",
            delivery_id, status, result.get("imported"), result.get("already_known"),
        )
        return _finish(session_factory, delivery_id, status,
                       ingest_result=json.dumps(result, default=str)[:2000])


def _finish(session_factory, delivery_id: int, status: str, *,
            error: str | None = None, ingest_result: str | None = None) -> str:
    with session_factory() as session:
        row = session.get(WebhookDelivery, delivery_id)
        if row is not None:
            row.processing_status = status
            row.processed_at = datetime.now()
            if error is not None:
                row.last_error = error[:4000]
            if ingest_result is not None:
                row.ingest_result = ingest_result
            session.commit()
    return status


def unfinished_deliveries(session: Session) -> list:
    """Rows the attention page shows: recorded, verified, not yet landed.

    Deliveries we REJECTED are deliberately excluded. An invalid
    signature is a security observation, not an order waiting on an
    operator, and putting it in a queue of orders to chase would be
    telling the operator to go looking for an order that may not exist.
    It stays on the row and in the log.
    """
    return (
        session.query(WebhookDelivery)
        .filter(
            WebhookDelivery.source == "manapool",
            WebhookDelivery.signature_status == "verified",
            WebhookDelivery.processing_status.in_(UNFINISHED_STATUSES),
        )
        .order_by(WebhookDelivery.received_at.desc())
        .all()
    )


# A sweep retry is a backstop, not the fast path: the poll has usually
# ingested the order by now. A short budget keeps a sweep of several rows
# from running for an hour.
SWEEP_BUDGET_SECONDS = 30


def sweep_unfinished(session_factory, *, older_than_seconds: int = 60,
                     process=None, limit: int = 20) -> dict:
    """Give stalled deliveries one more attempt.

    Runs at startup and once per hourly poll tick. A delivery is only
    picked up once it has sat for a minute, so this never races the
    in-flight background task that is already working on it.

    By the time this runs the poll has usually ingested the order
    already, so the retry's real job is to move the row to
    already_known and take it off the attention page -- the section
    exists to show what needs attention, and a row that resolved itself
    must not keep asking for it.
    """
    process = process or (
        lambda did: process_delivery(
            did, session_factory=session_factory,
            budget_seconds=SWEEP_BUDGET_SECONDS,
        )
    )
    cutoff = datetime.now().timestamp() - older_than_seconds
    picked = []
    with session_factory() as session:
        for row in (
            session.query(WebhookDelivery)
            .filter(
                WebhookDelivery.source == "manapool",
                WebhookDelivery.signature_status == "verified",
                WebhookDelivery.processing_status.in_(("pending", "stranded")),
            )
            .order_by(WebhookDelivery.received_at.asc())
            .limit(limit)
            .all()
        ):
            if row.received_at and row.received_at.timestamp() > cutoff:
                continue
            picked.append(row.id)
    outcomes = {}
    for delivery_id in picked:
        try:
            outcomes[delivery_id] = process(delivery_id)
        except Exception as exc:
            logger.warning(
                "webhook sweep: delivery %s raised during retry: %s: %s",
                delivery_id, type(exc).__name__, exc,
            )
            outcomes[delivery_id] = "failed"
    if picked:
        logger.info("webhook sweep: retried %s stalled deliveries: %s", len(picked), outcomes)
    return {"picked": picked, "outcomes": outcomes}
