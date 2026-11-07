"""Inbound Shopify order webhooks: verify, record, then ingest.

Modelled on manapool_webhook_service, deliberately and closely, because
the hard-won shape there is the shape this needs. Where it differs, it is
because Shopify differs, and each difference says so.

★ ORDER OF OPERATIONS, AND IT IS NOT NEGOTIABLE:

    verify -> persist -> answer 2xx -> process

The persist-before-answer half is the same argument the Mana Pool
receiver makes: FastAPI BackgroundTasks run inside the web process, so a
deploy mid-flight kills them silently. A background task is only safe
here because the row is already committed before it starts -- the work
can die and the delivery is still on record and still re-runnable.

★ HOW SHOPIFY DIFFERS FROM MANA POOL, and why each matters:

  SHOPIFY DOES RETRY. Its delivery docs give every request a
  `X-Shopify-Webhook-Id` and expect the receiver to be idempotent, so a
  non-2xx is not necessarily an order lost forever. That makes the
  receiver's job easier, not different: it still records first and
  answers yes, because a 500 from us is a retry we did not need and a
  window in which the order is not pickable.

  THERE IS NO SIGNED TIMESTAMP. Mana Pool signs a timestamp and this app
  rejects stale ones; Shopify's documented scheme is an HMAC over the
  RAW BODY ONLY -- "compute HMAC-SHA256 of the raw request body using
  your app's client secret as the key", compared against the
  base64 `X-Shopify-Hmac-SHA256` header. So there is no stale-timestamp
  status here, and replay protection comes from idempotency on the
  Shopify order id instead, which is where it comes from for duplicate
  deliveries anyway.

  THE SECRET IS THE APP'S CLIENT SECRET, not a webhook-specific one. It
  is already configured as SHOPIFY_CLIENT_SECRET for the Admin API, so
  there is no new secret to set and no bootstrap window in which an
  unverifiable request has to be accepted. A missing secret is simply a
  refusal.

  TWO TOPICS, NOT ONE. `orders/create` and `orders/cancelled`, read from
  the `X-Shopify-Topic` header. Mana Pool has exactly one topic and the
  poll carries everything else; here the cancellation topic is cheap
  enough to take, and it releases a reservation a customer is no longer
  coming for.

★ THE POLL IS STILL THE BACKSTOP. shopify_order_poll_service's
cursor-based pass sees every order again, and its second sighting of a
webhook-ingested order is a no-op by the same idempotency. Push FIRST,
not push instead.

★ NOTHING HAPPENS WHILE INTAKE IS OFF. A delivery arriving with
SHOPIFY_ORDER_INTAKE_ENABLED false is still VERIFIED and RECORDED -- so
the operator can see that Shopify is calling and that the signature
checks out -- and then not processed. Recording a delivery costs nothing
and throwing it away would make the one question worth asking before
turning intake on ("is Shopify actually reaching us?") unanswerable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from inventory_sync_service import InventoryLeaseBusy, inventory_sync_lease
from models import WebhookDelivery
from shopify_order_service import ingest_one, intake_enabled

logger = logging.getLogger("cardfoundry")

SOURCE = "shopify"

ENABLED_ENV = "SHOPIFY_WEBHOOK_ENABLED"

# The documented headers. Names are compared case-insensitively because
# Shopify's own docs say to: "Treat header names as case-insensitive in
# your code, as HTTP/2 often lowercases them."
HMAC_HEADER = "X-Shopify-Hmac-Sha256"
TOPIC_HEADER = "X-Shopify-Topic"
SHOP_DOMAIN_HEADER = "X-Shopify-Shop-Domain"
WEBHOOK_ID_HEADER = "X-Shopify-Webhook-Id"

ORDER_CREATED_TOPIC = "orders/create"
ORDER_CANCELLED_TOPIC = "orders/cancelled"
HANDLED_TOPICS = (ORDER_CREATED_TOPIC, ORDER_CANCELLED_TOPIC)

# Same vocabulary as the Mana Pool receiver, minus stale_timestamp --
# Shopify signs no timestamp, so that status could never be reached and
# a status nothing can produce is a status that misleads whoever reads
# the table.
SIGNATURE_VERIFIED = "verified"
SIGNATURE_INVALID = "invalid_signature"
SIGNATURE_MISSING_SECRET = "missing_secret"
# ★ AN AUTHENTIC DELIVERY FROM A STORE THIS APP IS NOT POINTED AT. Its
# own verdict and not a flavour of invalid_signature, because the two
# say opposite things about the body: invalid_signature means "we cannot
# tell where this came from", wrong_shop means "we know exactly where it
# came from, and it is not our shop". A row that says the second is
# evidence the operator can act on; folding it into the first would
# throw that away.
#
# ★ NO MIGRATION, AND THAT IS CHECKED, NOT HOPED. webhook_deliveries
# .signature_status is a plain indexed VARCHAR with no CHECK constraint
# (models.py), and the Mana Pool receiver already stores the off-
# vocabulary "unverified_bootstrap" in it -- so a new value is a new
# string and nothing else.
SIGNATURE_WRONG_SHOP = "wrong_shop"

RETRY_BACKOFF_SECONDS = (5, 10, 20, 30)
RETRY_BUDGET_SECONDS = 600
SWEEP_BUDGET_SECONDS = 30
SWEEP_MAX_ATTEMPTS = 5
UNFINISHED_STATUSES = ("pending", "stranded", "failed")
SUCCESS_STATUSES = ("processed", "already_known")


def webhook_enabled() -> bool:
    """Whether the receiver accepts deliveries at all.

    An environment variable rather than an app_setting, matching the Mana
    Pool receiver: it gates the HTTP edge, and the edge has to be able to
    answer before a database session exists.
    """
    return str(os.getenv(ENABLED_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on")


def webhook_secret() -> str:
    """The app's client secret, which is what Shopify signs with.

    Read at call time, never cached: a rotated secret has to take effect
    without a deploy, and Shopify warns "it can take up to an hour for
    the HMAC digest to be generated using the new secret", so both values
    may be in play for a while.
    """
    return str(os.getenv("SHOPIFY_CLIENT_SECRET", "") or "").strip()


def header(headers, name: str) -> str:
    """One header, case-insensitively, from a dict or a Starlette
    Headers object."""
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        if value is None:
            # A plain dict with original casing.
            lowered = {str(k).lower(): v for k, v in dict(headers).items()}
            value = lowered.get(name.lower())
        return str(value or "")
    return ""


def expected_signature(secret: str, raw_body: bytes) -> str:
    """Shopify's own recipe: base64 HMAC-SHA256 of the raw body.

    The documented snippet is
    `crypto.createHmac('sha256', appClientSecret).update(req.body)
    .digest('base64')`, and the raw body must be captured BEFORE any JSON
    parser runs -- a re-serialised body does not hash the same.
    """
    digest = hmac.new(secret.encode("utf-8"), raw_body or b"",
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def normalised_shop(raw) -> str:
    """A myshopify host, comparable: lower-cased, no scheme, no slashes.

    The same shapes shopify_service.store_domain tolerates, because the
    value being compared here is the same variable that one reads and a
    pasted "https://shop.myshopify.com/" must not read as a different
    shop from "shop.myshopify.com".
    """
    host = str(raw or "").strip().lower()
    host = host.removeprefix("https://").removeprefix("http://")
    return host.strip("/")


def configured_shop_domain() -> str:
    """The store this app is pointed at, from the environment.

    Read at call time and from os.getenv rather than through
    shopify_service, for the same two reasons webhook_secret is: the
    edge has to be able to answer before anything else is imported, and
    a variable the operator changes in Railway has to take effect on the
    next delivery rather than on the next deploy.
    """
    return normalised_shop(os.getenv("SHOPIFY_STORE_DOMAIN", ""))


def verify_signature(headers, raw_body: bytes) -> dict:
    """Verify a delivery. Returns {status, detail} and never raises.

    Compared with hmac.compare_digest, which is constant-time: a
    signature check that leaks timing is a signature check an attacker
    can grind.

    ★ AND THEN WHICH SHOP SENT IT, IN THAT ORDER AND NOT THE OTHER.
    X-Shopify-Shop-Domain is an unauthenticated header: anyone can type
    one. Checking it FIRST would let a forged header turn a signature
    failure into a tidier-sounding wrong_shop, which is an attacker
    choosing what our audit row says. Checking it only AFTER the HMAC
    has passed means a wrong_shop verdict is only ever recorded for a
    body Shopify really signed with our app's secret -- so the row is
    worth believing, which is the entire point of recording it.

    ★ WHAT IT DEFENDS, AND WHAT IT CANNOT. Two stores with the same app
    installed produce deliveries that BOTH pass the HMAC, and until now
    nothing downstream asked which store an order came from: the
    receiver read X-Shopify-Shop-Domain into a constant and never used
    it (the constant has existed unused since the receiver shipped).
    During the cutover one app is installed on the real shop while the
    dev shop still holds subscriptions, so this is the fence that stops
    a dev-shop order reserving real cards. It is not a security boundary
    -- the HMAC is -- it is a configuration boundary.
    """
    provided = header(headers, HMAC_HEADER).strip()
    secret = webhook_secret()
    if not secret:
        return {"status": SIGNATURE_MISSING_SECRET,
                "detail": "SHOPIFY_CLIENT_SECRET is not configured"}
    if not provided:
        return {"status": SIGNATURE_INVALID,
                "detail": f"no {HMAC_HEADER} header"}
    if not hmac.compare_digest(provided,
                               expected_signature(secret, raw_body)):
        return {"status": SIGNATURE_INVALID,
                "detail": "the signature did not match the body"}
    sent_by = normalised_shop(header(headers, SHOP_DOMAIN_HEADER))
    ours = configured_shop_domain()
    if sent_by and ours and sent_by != ours:
        return {"status": SIGNATURE_WRONG_SHOP,
                "detail": f"{SHOP_DOMAIN_HEADER} is {sent_by}, and this "
                          f"app is configured for {ours}"}
    # ★ AN UNJUDGEABLE DELIVERY IS ACCEPTED, DELIBERATELY, and only
    # because the HMAC has already passed. Two cases reach here: the
    # header is absent (Shopify documents sending it, so this is a
    # Shopify change, not an attack -- and refusing would stop order
    # intake over a header nobody needed yesterday), or
    # SHOPIFY_STORE_DOMAIN is unset, which is every test worktree and
    # which cannot be a refusal: a fence needs two posts, and an
    # unconfigured app has only the one. Neither case is weaker than the
    # behaviour this check replaced, where the shop was never compared
    # at all -- but an unjudged delivery says so in the log rather than
    # passing silently as if it had been checked.
    if not sent_by or not ours:
        logger.info(
            "shopify webhook: the sending shop was not checked (%s), so "
            "the HMAC is the only thing that vouches for this delivery.",
            "no shop-domain header" if not sent_by
            else "SHOPIFY_STORE_DOMAIN is not configured",
        )
    return {"status": SIGNATURE_VERIFIED, "detail": ""}


def _order_id_from_body(raw_body: bytes) -> str | None:
    """The Shopify order id, for the delivery row, read defensively.

    A body that will not parse is still worth recording -- the signature
    verdict is the interesting part of such a row -- so this returns None
    rather than raising.
    """
    try:
        body = json.loads((raw_body or b"").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    raw = body.get("id")
    if raw is None:
        return None
    return str(raw)


def record_delivery(session: Session, *, headers, raw_body: bytes,
                    signature: dict) -> WebhookDelivery:
    """Persist the delivery BEFORE anything is processed. Caller commits.

    The raw body is stored verbatim, which is what makes a delivery
    re-runnable later: re-deriving it from the order we built would be
    re-deriving the evidence from the conclusion.
    """
    row = WebhookDelivery(
        source=SOURCE,
        event=header(headers, TOPIC_HEADER) or None,
        external_order_id=_order_id_from_body(raw_body),
        signature_status=signature["status"],
        raw_body=(raw_body or b"").decode("utf-8", errors="replace"),
        processing_status="pending",
        attempts=0,
    )
    session.add(row)
    return row


def _ingest_from_delivery(session: Session, row: WebhookDelivery) -> dict:
    """Ingest the order this delivery carries. Caller commits."""
    body = json.loads(row.raw_body or "{}")
    outcome = ingest_one(session, body, initiated_by="shopify-webhook")
    return outcome


def process_delivery(session_factory, delivery_id: int) -> dict:
    """Process one recorded delivery. Never raises.

    ★ A BUSY INVENTORY LEASE IS NOT A FAILURE. It is an ordinary local
    condition with nothing to do with Shopify, so it leaves the delivery
    `pending` for the sweep rather than marking it failed -- the same
    judgement the Mana Pool receiver makes, for the same reason.

    ★ AND A DELIVERY THAT ARRIVES WITH INTAKE OFF IS LEFT ALONE, not
    failed: `stranded` says "verified, recorded, deliberately not
    processed", which is exactly true and is what the sweep will pick up
    on the day intake goes on.
    """
    with session_factory() as session:
        row = session.get(WebhookDelivery, delivery_id)
        if row is None:
            return {"outcome": "missing", "delivery_id": delivery_id}
        if row.source != SOURCE:
            return {"outcome": "not_ours", "delivery_id": delivery_id}
        if row.processing_status in SUCCESS_STATUSES:
            return {"outcome": "already_finished",
                    "delivery_id": delivery_id,
                    "status": row.processing_status}
        if row.signature_status != SIGNATURE_VERIFIED:
            _finish(session_factory, delivery_id, "failed",
                    error=f"signature {row.signature_status}")
            return {"outcome": "unverified", "delivery_id": delivery_id}
        topic = (row.event or "").strip()
        if topic not in HANDLED_TOPICS:
            _finish(session_factory, delivery_id, "failed",
                    error=f"unhandled topic {topic!r}")
            return {"outcome": "unhandled_topic", "delivery_id": delivery_id,
                    "topic": topic}
        if not intake_enabled(session):
            _finish(session_factory, delivery_id, "stranded",
                    error="SHOPIFY_ORDER_INTAKE_ENABLED is off")
            return {"outcome": "intake_off", "delivery_id": delivery_id}

    deadline = time.monotonic() + RETRY_BUDGET_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            with session_factory() as session:
                row = session.get(WebhookDelivery, delivery_id)
                # The lease takes no arguments and manages its own
                # session -- the same call the Mana Pool receiver makes.
                with inventory_sync_lease():
                    if (row.event or "") == ORDER_CANCELLED_TOPIC:
                        outcome = _cancel_from_delivery(session, row)
                    else:
                        outcome = _ingest_from_delivery(session, row)
                    session.commit()
        except InventoryLeaseBusy:
            wait = RETRY_BACKOFF_SECONDS[min(attempt - 1,
                                             len(RETRY_BACKOFF_SECONDS) - 1)]
            if time.monotonic() + wait > deadline:
                logger.warning(
                    "shopify webhook delivery %s left pending: the inventory "
                    "lease stayed busy for %ss. The sweep will retry.",
                    delivery_id, RETRY_BUDGET_SECONDS,
                )
                return {"outcome": "lease_busy", "delivery_id": delivery_id}
            time.sleep(wait)
            continue
        except Exception as exc:  # noqa: BLE001 -- a receiver must not crash
            logger.warning(
                "shopify webhook delivery %s failed: %s: %s",
                delivery_id, type(exc).__name__, exc,
            )
            _finish(session_factory, delivery_id, "failed",
                    error=f"{type(exc).__name__}: {exc}")
            return {"outcome": "failed", "delivery_id": delivery_id,
                    "error": str(exc)}
        status = ("already_known"
                  if outcome.get("outcome") == "already_known"
                  else "processed")
        _finish(session_factory, delivery_id, status,
                ingest_result=json.dumps(outcome, sort_keys=True,
                                         default=str)[:4000])
        logger.info("shopify webhook delivery %s -> %s (%s).",
                    delivery_id, status, outcome.get("outcome"))
        # ★ AFTER THE DELIVERY IS RECORDED, AND OUTSIDE THE LEASE. Asking
        # Shopify what the customer chose is one read, and it must not be
        # able to change what this delivery recorded, hold the inventory
        # lease a moment longer, or fail the ingest that already
        # succeeded. So it happens here, last, in its own session.
        _record_delivery_method(session_factory, outcome.get("order_id"))
        _record_order_contact(session_factory, outcome.get("order_id"))
        return {"outcome": outcome.get("outcome"), "delivery_id": delivery_id,
                "result": outcome}


def _record_delivery_method(session_factory, order_id) -> None:
    """Store what this order's customer chose at checkout. Never raises.

    Shared with the poll: shopify_delivery_method_service holds the one
    definition, and both readers call it rather than each asking Shopify
    their own way. Phase 1 is pickup only, so an order that chose
    shipping is the thing this exists to notice.
    """
    if not order_id:
        return
    try:
        from models import SalesOrder
        from shopify_delivery_method_service import record_delivery_methods

        with session_factory() as session:
            order = session.get(SalesOrder, order_id)
            if order is None:
                return
            # store_unclassified=False: see that function's docstring --
            # four seconds after the purchase, "in neither bucket" is
            # more likely to mean "not indexed yet" than an answer.
            record_delivery_methods(session, [order],
                                    store_unclassified=False)
            session.commit()
    except Exception as exc:  # noqa: BLE001 -- the delivery is already done
        logger.warning(
            "shopify webhook: the delivery method for order %s could not be "
            "recorded and stays unknown; the poll will ask again: %s: %s",
            order_id, type(exc).__name__, exc,
        )


def _record_order_contact(session_factory, order_id) -> None:
    """Store who is collecting this order. Never raises, never logs a value.

    Shared with the poll: shopify_order_contact_service holds the one
    definition. refresh=True because this hook runs for an updated
    order as well as a new one, and a contact edited in Shopify should
    catch up here rather than wait for the hourly poll.

    ★ A DENIAL IS EXPECTED ON THE DEV STORE, which has no
    read_customers scope. It is logged once, counted, with no values,
    and the delivery it is part of has already succeeded.
    """
    if not order_id:
        return
    try:
        from models import SalesOrder
        from shopify_order_contact_service import record_order_contacts

        with session_factory() as session:
            order = session.get(SalesOrder, order_id)
            if order is None:
                return
            record_order_contacts(session, [order], refresh=True,
                                  actor="shopify-webhook")
            session.commit()
    except Exception as exc:  # noqa: BLE001 -- the delivery is already done
        logger.warning(
            "shopify webhook: the buyer contact for order %s could not be "
            "recorded and stays unknown; the poll will ask again: %s: %s",
            order_id, type(exc).__name__, exc,
        )


def _cancel_from_delivery(session: Session, row: WebhookDelivery) -> dict:
    """A cancelled Shopify order: release the reservation.

    ★ ONE DEFINITION, SHARED WITH THE POLL.
    shopify_order_service.release_cancelled_order holds what a Shopify
    cancellation releases, what it refuses, and what it logs -- itself
    nothing but a call to the canonical release_order. This receiver and
    the poll's own cancellation pass now cannot drift apart, which they
    could while only this one existed.
    """
    from shopify_order_service import (
        canonical_external_order_id, existing_order, release_cancelled_order,
    )

    body = json.loads(row.raw_body or "{}")
    external_order_id = canonical_external_order_id((body or {}).get("id"))
    order = existing_order(session, external_order_id)
    if order is None:
        return {"outcome": "unknown_order",
                "external_order_id": external_order_id}
    outcome = release_cancelled_order(session, order,
                                      initiated_by="shopify-webhook")
    # `cancelled` is the word this receiver has always returned for a
    # release it made, and process_delivery's status mapping and the
    # stored ingest_result both read it. Kept.
    if outcome["outcome"] == "released":
        outcome = {**outcome, "outcome": "cancelled"}
    return {**outcome, "external_order_id": external_order_id}


LOCK_ERROR_MARKERS = ("database is locked", "database table is locked",
                      "database is busy")
FINISH_LOCK_RETRIES = 4
FINISH_LOCK_WAIT_SECONDS = 2


def is_locked_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in LOCK_ERROR_MARKERS)


def _finish(session_factory, delivery_id: int, status: str, *,
            error: str | None = None, ingest_result: str | None = None
            ) -> bool:
    """Record the delivery's outcome, retrying a locked database.

    ★ THE ONE WRITE THAT MUST NOT BE LOST. If this fails the delivery
    looks unfinished forever and the sweep re-runs work that already
    happened, so a locked database is waited out rather than given up on
    -- the same lesson the Mana Pool receiver's own retry loop records.
    """
    for attempt in range(FINISH_LOCK_RETRIES + 1):
        try:
            with session_factory() as session:
                row = session.get(WebhookDelivery, delivery_id)
                if row is None:
                    return False
                row.processing_status = status
                row.processed_at = datetime.now()
                row.attempts = (row.attempts or 0) + 1
                if error is not None:
                    row.last_error = error[:2000]
                elif status in SUCCESS_STATUSES:
                    row.last_error = None
                if ingest_result is not None:
                    row.ingest_result = ingest_result
                session.commit()
            return True
        except OperationalError as exc:
            if not is_locked_error(exc) or attempt == FINISH_LOCK_RETRIES:
                logger.error(
                    "shopify webhook delivery %s could not be marked %r: "
                    "%s", delivery_id, status, exc,
                )
                return False
            time.sleep(FINISH_LOCK_WAIT_SECONDS)
    return False


def unfinished_deliveries(session: Session, *,
                          older_than_seconds: int = 60) -> list:
    """Verified Shopify deliveries that never reached a finished status.

    Older-than is what keeps the sweep off a delivery the receiver is
    still working on in another process.
    """
    cutoff = datetime.now().timestamp() - older_than_seconds
    rows = (session.query(WebhookDelivery)
            .filter(WebhookDelivery.source == SOURCE,
                    WebhookDelivery.signature_status == SIGNATURE_VERIFIED,
                    WebhookDelivery.processing_status.in_(UNFINISHED_STATUSES))
            .order_by(WebhookDelivery.id)
            .all())
    return [row for row in rows
            if row.received_at.timestamp() <= cutoff
            and (row.attempts or 0) < SWEEP_MAX_ATTEMPTS]


def sweep_unfinished(session_factory, *, older_than_seconds: int = 60,
                     budget_seconds: int = SWEEP_BUDGET_SECONDS) -> dict:
    """Re-run every delivery that never finished, within a time budget.

    Bounded by both a clock and an attempt count: a delivery that cannot
    be processed must not be retried forever, and a sweep must not be the
    thing that holds a request open.
    """
    started = time.monotonic()
    with session_factory() as session:
        pending = [row.id for row in
                   unfinished_deliveries(
                       session, older_than_seconds=older_than_seconds)]
    done = []
    for delivery_id in pending:
        if time.monotonic() - started > budget_seconds:
            logger.info(
                "shopify webhook sweep stopped at its %ss budget with %s "
                "delivery(ies) left for the next run.",
                budget_seconds, len(pending) - len(done),
            )
            break
        done.append(process_delivery(session_factory, delivery_id))
    if pending:
        logger.info("shopify webhook sweep: %s of %s delivery(ies) re-run.",
                    len(done), len(pending))
    return {"found": len(pending), "processed": done}
