"""One list of everything currently waiting on the operator.

WHY THIS EXISTS. The things needing attention were spread across five
sections of one page, an ambient banner, and two other screens, with no
single number saying how much was outstanding. An operator had to visit a
page to find out whether there was anything to visit it for.

THE COUNT AND THE DISMISS ARE ONE FEATURE, NOT TWO. A standing count of
everything is exactly what the 2026-09-14 Ticket B decision refused for
the ambient banner, in its own words: folding a long-lived backlog into
an always-visible signal "is exactly how a useful alert becomes
wallpaper". That decision named its own expiry -- "worth revisiting once
Ticket C drains the backlog" -- and the backlog is drained. The reason a
count is safe NOW is that an item the operator has consciously judged can
be dismissed, so the number reflects what is actually undecided rather
than what merely exists. Remove the dismiss and the count becomes
wallpaper again; that is the failure mode to watch for.

A DISMISS IS NOT A MUTE. Every item carries a condition_hash -- a
snapshot of the state that made it appear. A dismissal only silences THAT
state. If the condition changes, the hash changes and the item comes
back, still carrying its old dismissal for the record. So "I have decided
about the 3 drift rows" does not silently swallow a 4th, and an order
that gets worse does not stay hidden because someone looked at it once
when it was merely bad.

Nothing here is ever deleted or edited on the underlying record. A
dismissal is a row in its own table naming the category, the item, and
the operator's reason in their own words; un-dismissing writes a second
timestamp rather than removing anything. The record of what was decided,
and when, survives either way.

NO MANA POOL CALLS, EVER. This runs on every page load via the nav badge.
Everything here reads local tables only -- the listing-integrity check
reads the cached result of the last sync's scan, never a live one. A
badge that costs a 19,000-row pagination would be a badge nobody can
afford to render.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from models import (
    DismissedAttentionItem, FulfillmentException, InventoryCard,
    InventoryPriceHistory, InventorySyncJob, PricingJob, SalesOrder,
    WebhookDelivery,
)


logger = logging.getLogger("cardfoundry")


# Categories, in the order the page shows them. Urgency is the operator's
# own ordering: something Mana Pool is currently wrong about, or an order
# that cannot ship, outranks a bookkeeping mismatch.
CATEGORY_MANAPOOL_SYNC = "manapool_sync"
CATEGORY_SHORT_ORDER = "short_order"
CATEGORY_FULFILLMENT_EXCEPTION = "fulfillment_exception"
CATEGORY_WEBHOOK_DELIVERY = "webhook_delivery"
CATEGORY_LISTING_DRIFT = "listing_drift"
CATEGORY_PRICING_FRESHNESS = "pricing_freshness"
CATEGORY_PRICE_JUMP = "price_jump"

HIGH, MEDIUM = "high", "medium"

CATEGORY_URGENCY = {
    CATEGORY_MANAPOOL_SYNC: HIGH,
    CATEGORY_SHORT_ORDER: HIGH,
    CATEGORY_FULFILLMENT_EXCEPTION: MEDIUM,
    CATEGORY_WEBHOOK_DELIVERY: MEDIUM,
    CATEGORY_LISTING_DRIFT: MEDIUM,
    CATEGORY_PRICING_FRESHNESS: HIGH,
    CATEGORY_PRICE_JUMP: MEDIUM,
}

# The pricing cron runs three times a day, so one missed tick is ~8 hours.
# 12 hours is a missed tick plus slack; 24 hours is two consecutive misses
# and cannot be a blip.
PRICING_STALE_WARN_HOURS = 12
PRICING_STALE_ALARM_HOURS = 24

# A run that completes having priced almost nothing is not a healthy tick,
# whatever its status says. Seen live on 2026-09-20: a bulk apply reported
# "completed" having priced 1 listing of 5,975. The status field describes
# whether the job ran, not whether it did anything.
PRICING_EMPTY_RUN_FRACTION = 0.01

# Flat dollars, not a percentage: the median automatic price move is
# $0.33 while the ratio median is 1.67x, so a ratio threshold is
# meaningless here and a dollar one is not. $10 flagged 13 rows across the
# whole history at the time this shipped -- roughly one a week.
PRICE_JUMP_MIN_DOLLARS = 10.0

# A card's FIRST price is never a jump: the operator deliberately
# overprices hard-to-price new imports and lets the cron correct them
# down, sometimes by a lot. Rows with no old_price are excluded outright;
# this age floor additionally excludes the early corrections that follow.
PRICE_JUMP_MIN_CARD_AGE_DAYS = 30

# Only recent moves. Two reasons, and both matter:
#
#   It is what the flag MEANS. A jump from three months ago is not news,
#   and leaving it on the list forever made "yes, I know" a manual
#   dismiss for something that should age out on its own.
#
#   It bounds the cost. This is the one attention query that joins two
#   growing tables (inventory_price_history x inventory_cards) and
#   filters on abs(new - old), which no index supports. Unwindowed it
#   scanned all history on EVERY PAGE LOAD via the nav badge and was
#   measured at 36 ms of the badge's total. Price history grows by
#   thousands of rows a week now that the write-back runs three times a
#   day, so unwindowed it would only get slower.
PRICE_JUMP_WINDOW_DAYS = 30


@dataclass
class AttentionItem:
    """One thing waiting on the operator, in one category."""
    category: str
    item_key: str
    condition_hash: str
    summary: str
    detail: str = ""
    href: str | None = None
    payload: dict = field(default_factory=dict)

    @property
    def urgency(self) -> str:
        return CATEGORY_URGENCY.get(self.category, MEDIUM)


def condition_hash(value) -> str:
    """A short, stable digest of whatever made an item appear.

    Deliberately hashes a CANONICAL json dump: a dict that means the same
    thing must produce the same hash regardless of key order, or items
    would re-surface at random and the dismiss would look broken.
    """
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]


# --- the collectors -----------------------------------------------------
#
# Each returns AttentionItems for its category and reads ONLY local
# tables. A collector that needed a remote call would make the nav badge
# unaffordable, so there is no such collector.

def _manapool_sync_items(session: Session) -> list[AttentionItem]:
    """Local status changes that have not reached Mana Pool.

    The same two queries the ambient banner counts. The banner stays as it
    is -- its value is that it is rare and red -- and this is the itemised
    view of the same condition.
    """
    items = []
    stuck = (
        session.query(SalesOrder)
        .filter(
            SalesOrder.source == "manapool",
            SalesOrder.mana_pool_shipment_released_at.is_(None),
            SalesOrder.status.in_(("shipped", "picked")),
        ).all()
    )
    for order in stuck:
        if order.status == "shipped" and order.mana_pool_shipment_synced_at is not None:
            continue
        if order.status == "picked" and order.mana_pool_processing_synced_at is not None:
            continue
        items.append(AttentionItem(
            category=CATEGORY_MANAPOOL_SYNC,
            item_key=f"order:{order.id}",
            # The failure detail is part of the condition: a DIFFERENT
            # failure on the same order is a new thing to look at.
            condition_hash=condition_hash({
                "status": order.status,
                "shipment_failure": order.mana_pool_shipment_failure_detail,
                "processing_failure": order.mana_pool_processing_failure_detail,
            }),
            summary=f"Order {order.external_label or order.id} is {order.status} locally but Mana Pool has not been told",
            detail=(order.mana_pool_shipment_failure_detail
                    or order.mana_pool_processing_failure_detail or ""),
            href=f"/orders/{order.id}",
        ))
    return items


def _short_order_items(session: Session) -> list[AttentionItem]:
    items = []
    for order in session.query(SalesOrder).filter(
        SalesOrder.status.in_(("short", "needs_review")),
    ).all():
        items.append(AttentionItem(
            category=CATEGORY_SHORT_ORDER,
            item_key=f"order:{order.id}",
            condition_hash=condition_hash({
                "status": order.status, "review_detail": order.review_detail,
            }),
            summary=f"Order {order.external_label or order.id} is {order.status} and cannot ship as it stands",
            detail=order.review_detail or "",
            href=f"/orders/{order.id}",
        ))
    return items


def _fulfillment_exception_items(session: Session) -> list[AttentionItem]:
    items = []
    for exc in session.query(FulfillmentException).filter(
        FulfillmentException.inventory_resolution_state == "unresolved",
    ).all():
        items.append(AttentionItem(
            category=CATEGORY_FULFILLMENT_EXCEPTION,
            item_key=f"exception:{exc.id}",
            condition_hash=condition_hash({
                "remote": exc.remote_resolution_state,
                "inventory": exc.inventory_resolution_state,
                "submission": exc.submission_state,
            }),
            summary=f"Fulfillment exception #{exc.id} is still open on the inventory side",
            detail=f"Mana Pool: {exc.remote_resolution_state}",
            href=f"/orders/{exc.sales_order_id}",
        ))
    return items


def _webhook_delivery_items(session: Session) -> list[AttentionItem]:
    """Only VERIFIED deliveries, matching the existing section: a rejected
    delivery is a security observation, not an order waiting on anyone."""
    items = []
    for row in session.query(WebhookDelivery).filter(
        WebhookDelivery.source == "manapool",
        WebhookDelivery.signature_status == "verified",
        WebhookDelivery.processing_status.in_(("pending", "stranded", "failed")),
    ).all():
        items.append(AttentionItem(
            category=CATEGORY_WEBHOOK_DELIVERY,
            item_key=f"delivery:{row.id}",
            # attempts is part of the condition on purpose: a delivery
            # that has since been retried and failed again is new news.
            condition_hash=condition_hash({
                "processing": row.processing_status, "attempts": row.attempts,
            }),
            summary=f"Webhook delivery {row.id} for order {row.external_order_id or '(unknown)'} has not been ingested",
            detail=(row.last_error or "")[:200],
            href="/orders/needs-attention",
        ))
    return items


def _listing_drift_items(session: Session, drift_rows: list) -> list[AttentionItem]:
    """Caller supplies the rows -- this never runs a scan of its own.

    identity_drift_rows() is local-only, but the OVER-listed half of the
    same check needs a live seller-inventory read, so the page passes what
    it already has rather than letting this function decide to fetch.
    """
    items = []
    for row in drift_rows or []:
        items.append(AttentionItem(
            category=CATEGORY_LISTING_DRIFT,
            item_key=f"card:{row.get('card_id')}",
            condition_hash=condition_hash({
                "differs_on": sorted(row.get("differs_on") or []),
                "binding_id": row.get("binding_id"),
            }),
            summary=f"Card {row.get('card_id')} {row.get('name') or ''} is attached to a listing for a different printing",
            detail=", ".join(row.get("differs_on") or []),
            href=f"/inventory/{row.get('card_id')}",
        ))
    return items


def pricing_freshness(session: Session, *, now: datetime | None = None) -> dict:
    """Has the pricing cron actually done its job recently?

    TWO DIFFERENT FAILURES, and only one of them is visible in a status
    column. The obvious one is that no run completed. The other, seen live
    on 2026-09-20, is a run that completed having priced ONE listing out
    of 5,975 -- "completed" describes whether the job ran, not whether it
    achieved anything, so a status check alone would call that healthy.
    """
    now = now or datetime.now()
    latest = (
        session.query(PricingJob)
        .filter(PricingJob.action.like("%apply%"), PricingJob.status == "completed")
        .order_by(PricingJob.created_at.desc())
        .first()
    )
    if latest is None:
        return {"state": "alarm", "reason": "No completed pricing run has ever been recorded.",
                "last_run_at": None, "hours": None, "priced": None, "total": None}

    hours = (now - latest.created_at).total_seconds() / 3600.0
    priced = total = None
    try:
        summary = (json.loads(latest.response_json or "{}") or {}).get("summary") or {}
        priced = summary.get("successful_items")
        total = summary.get("total_items")
    except Exception as exc:  # noqa: BLE001 -- a malformed blob must not hide staleness
        logger.warning("pricing freshness: could not read job %s summary: %s: %s",
                       latest.id, type(exc).__name__, exc)

    result = {"last_run_at": latest.created_at, "hours": hours,
              "priced": priced, "total": total, "state": "fresh", "reason": ""}
    if hours >= PRICING_STALE_ALARM_HOURS:
        result["state"] = "alarm"
        result["reason"] = f"No pricing run has completed in {hours:.0f} hours."
    elif hours >= PRICING_STALE_WARN_HOURS:
        result["state"] = "warn"
        result["reason"] = f"The last pricing run completed {hours:.0f} hours ago."
    elif isinstance(priced, int) and isinstance(total, int) and total > 0 and (
        priced / total
    ) <= PRICING_EMPTY_RUN_FRACTION:
        # Deliberately a separate branch from staleness: this run is
        # RECENT. It is flagged because it did nothing, which a status
        # column cannot say.
        result["state"] = "warn"
        result["reason"] = (
            f"The last pricing run completed but priced only {priced} of "
            f"{total} listings, which is not a healthy tick."
        )
    return result


def _pricing_freshness_items(session: Session, *, now: datetime | None = None) -> list[AttentionItem]:
    status = pricing_freshness(session, now=now)
    if status["state"] == "fresh":
        return []
    return [AttentionItem(
        category=CATEGORY_PRICING_FRESHNESS,
        item_key="pricing_freshness",
        # Bucketed, not exact: hashing the precise hour would re-surface
        # this every single hour it stayed stale, which is nagging, not
        # re-surfacing. It comes back when it gets WORSE.
        condition_hash=condition_hash({"state": status["state"]}),
        summary="Pricing may not be running",
        detail=status["reason"],
        href="/pricing",
        payload=status,
    )]


def _price_jump_items(session: Session, *, now: datetime | None = None) -> list[AttentionItem]:
    """Large moves on cards whose price was already settled.

    old_price IS NOT NULL does most of the work: it excludes every
    first-ever price, which is precisely the operator's deliberate
    "overprice a hard-to-price import and let the cron bring it down"
    workflow. The card-age floor excludes the early corrections that
    follow one.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=PRICE_JUMP_MIN_CARD_AGE_DAYS)
    since = now - timedelta(days=PRICE_JUMP_WINDOW_DAYS)
    items = []
    rows = (
        session.query(InventoryPriceHistory, InventoryCard)
        .join(InventoryCard, InventoryCard.id == InventoryPriceHistory.inventory_card_id)
        .filter(
            InventoryPriceHistory.old_price.isnot(None),
            InventoryPriceHistory.new_price.isnot(None),
            InventoryPriceHistory.changed_at >= since,
            InventoryCard.imported_at <= cutoff,
        ).all()
    )
    for row, card in rows:
        move = abs(float(row.new_price) - float(row.old_price))
        if move < PRICE_JUMP_MIN_DOLLARS:
            continue
        items.append(AttentionItem(
            category=CATEGORY_PRICE_JUMP,
            item_key=f"price_history:{row.id}",
            # A history row never changes, so this hash is stable for
            # good: dismissing one jump dismisses that jump only, and the
            # next big move on the same card is a new item.
            condition_hash=condition_hash({
                "old": row.old_price, "new": row.new_price, "row": row.id,
            }),
            summary=(f"{card.name} moved ${row.old_price:.2f} to ${row.new_price:.2f} "
                     f"(${move:.2f})"),
            detail=f"source: {row.source}",
            href=f"/inventory/{card.id}",
            payload={"move": move, "changed_at": row.changed_at},
        ))
    return items


# --- dismissal ----------------------------------------------------------

def active_dismissals(session: Session) -> dict:
    """(category, item_key) -> condition_hash, for dismissals still in force.

    Still in force means never un-dismissed. Whether it actually silences
    an item is decided per item by comparing hashes, not here.
    """
    return {
        (row.category, row.item_key): row.condition_hash
        for row in session.query(DismissedAttentionItem).filter(
            DismissedAttentionItem.undismissed_at.is_(None),
        ).all()
    }


def is_dismissed(item: AttentionItem, dismissals: dict) -> bool:
    """A dismissal silences the exact condition it was made against.

    Same condition -> stays hidden. Changed condition -> comes back, with
    the old dismissal left untouched as the record of what was decided
    about the previous state.
    """
    return dismissals.get((item.category, item.item_key)) == item.condition_hash


def collect(session: Session, *, drift_rows=None, now: datetime | None = None) -> list[AttentionItem]:
    """Every attention item, dismissed or not, in urgency order.

    Collectors are individually isolated: one category failing must not
    blank the whole page, and it must say so rather than quietly showing
    a short list that looks like good news.
    """
    collectors = (
        (CATEGORY_MANAPOOL_SYNC, lambda: _manapool_sync_items(session)),
        (CATEGORY_SHORT_ORDER, lambda: _short_order_items(session)),
        (CATEGORY_PRICING_FRESHNESS, lambda: _pricing_freshness_items(session, now=now)),
        (CATEGORY_FULFILLMENT_EXCEPTION, lambda: _fulfillment_exception_items(session)),
        (CATEGORY_WEBHOOK_DELIVERY, lambda: _webhook_delivery_items(session)),
        (CATEGORY_LISTING_DRIFT, lambda: _listing_drift_items(session, drift_rows or [])),
        (CATEGORY_PRICE_JUMP, lambda: _price_jump_items(session, now=now)),
    )
    items = []
    for category, run in collectors:
        try:
            items.extend(run())
        except Exception as exc:  # noqa: BLE001 -- see docstring
            logger.warning(
                "attention: the %s collector failed and its items are missing "
                "from this view: %s: %s", category, type(exc).__name__, exc,
            )
    return items


def outstanding(session: Session, *, drift_rows=None, now: datetime | None = None) -> list[AttentionItem]:
    dismissals = active_dismissals(session)
    return [i for i in collect(session, drift_rows=drift_rows, now=now)
            if not is_dismissed(i, dismissals)]


# How many candidate items each category has, as an aggregate COUNT --
# never as loaded rows. See badge_count for why that distinction matters.
def _candidate_counts(session: Session, *, now: datetime | None = None) -> dict:
    counts = {
        CATEGORY_MANAPOOL_SYNC: (
            session.query(func.count(SalesOrder.id)).filter(
                SalesOrder.source == "manapool",
                SalesOrder.mana_pool_shipment_released_at.is_(None),
                or_(
                    and_(SalesOrder.status == "shipped",
                         SalesOrder.mana_pool_shipment_synced_at.is_(None)),
                    and_(SalesOrder.status == "picked",
                         SalesOrder.mana_pool_processing_synced_at.is_(None)),
                ),
            ).scalar() or 0
        ),
        CATEGORY_SHORT_ORDER: (
            session.query(func.count(SalesOrder.id)).filter(
                SalesOrder.status.in_(("short", "needs_review")),
            ).scalar() or 0
        ),
        CATEGORY_FULFILLMENT_EXCEPTION: (
            session.query(func.count(FulfillmentException.id)).filter(
                FulfillmentException.inventory_resolution_state == "unresolved",
            ).scalar() or 0
        ),
        CATEGORY_WEBHOOK_DELIVERY: (
            session.query(func.count(WebhookDelivery.id)).filter(
                WebhookDelivery.source == "manapool",
                WebhookDelivery.signature_status == "verified",
                WebhookDelivery.processing_status.in_(("pending", "stranded", "failed")),
            ).scalar() or 0
        ),
        CATEGORY_PRICE_JUMP: (
            session.query(func.count(InventoryPriceHistory.id))
            .join(InventoryCard, InventoryCard.id == InventoryPriceHistory.inventory_card_id)
            .filter(
                InventoryPriceHistory.old_price.isnot(None),
                InventoryPriceHistory.new_price.isnot(None),
                InventoryCard.imported_at <= (now or datetime.now()) - timedelta(
                    days=PRICE_JUMP_MIN_CARD_AGE_DAYS),
                InventoryPriceHistory.changed_at >= (now or datetime.now()) - timedelta(
                    days=PRICE_JUMP_WINDOW_DAYS),
                func.abs(InventoryPriceHistory.new_price
                         - InventoryPriceHistory.old_price) >= PRICE_JUMP_MIN_DOLLARS,
            ).scalar() or 0
        ),
    }
    counts[CATEGORY_PRICING_FRESHNESS] = (
        0 if pricing_freshness(session, now=now)["state"] == "fresh" else 1
    )
    return counts


def badge_count(session: Session, *, now: datetime | None = None) -> int:
    """What the nav badge shows. Aggregate COUNTs only, never loaded rows.

    THIS RUNS ON EVERY PAGE LOAD, so how it is written matters more than
    what it returns. Built first as `len(outstanding(...))`, it loaded
    full ORM objects for every candidate in seven categories and took one
    page from 9 SQL statements to 30 -- caught by an existing N+1 test,
    which is exactly the regression that test exists to catch. It is now
    six aggregate counts plus one freshness lookup, with a bounded extra
    query per ACTIVE DISMISSAL (a handful, not a population).

    Accuracy costs that per-dismissal query: a dismissal only silences
    the condition it was made against, so knowing whether it still
    applies means recomputing that one item's hash. Only dismissed items
    need it, which is what keeps this bounded.

    Drift is deliberately excluded -- its rows come from the last sync's
    cached scan, which the attention page holds and a page header does
    not. The badge can therefore read 1-2 lower than the page when drift
    is present. Accepted trade (operator, 2026-09-20).
    """
    total = sum(_candidate_counts(session, now=now).values())
    dismissals = active_dismissals(session)
    if not dismissals:
        return total
    # Only the dismissed items are re-derived, and only in their own
    # categories -- never the whole list.
    silenced = 0
    for item in collect_categories(
        session, categories={c for c, _ in dismissals}, now=now,
    ):
        if is_dismissed(item, dismissals):
            silenced += 1
    return max(total - silenced, 0)


def collect_categories(session: Session, *, categories: set,
                       now: datetime | None = None) -> list:
    """Collect only the named categories. Used by the badge so a single
    dismissal does not force every collector to run."""
    return [i for i in collect(session, drift_rows=[], now=now)
            if i.category in categories]


def dismiss(session: Session, *, category: str, item_key: str, reason: str,
            condition_hash_value: str) -> DismissedAttentionItem:
    """Record a decision about one item. Nothing else is touched.

    The reason is required, in the operator's own words, because the
    context is the whole point: "left the 3 drift rows on purpose" is the
    difference between a considered decision and a lost one.
    """
    text = str(reason or "").strip()
    if not text:
        raise ValueError("A reason is required to dismiss an attention item.")
    existing = (
        session.query(DismissedAttentionItem)
        .filter(
            DismissedAttentionItem.category == category,
            DismissedAttentionItem.item_key == item_key,
            DismissedAttentionItem.undismissed_at.is_(None),
        ).first()
    )
    if existing is not None:
        # Re-dismissing after the condition moved: keep the history, point
        # the live dismissal at the new condition.
        existing.condition_hash = condition_hash_value
        existing.reason = text
        existing.dismissed_at = datetime.now()
        session.flush()
        logger.info("attention: re-dismissed %s %s", category, item_key)
        return existing
    row = DismissedAttentionItem(
        category=category, item_key=item_key, reason=text,
        condition_hash=condition_hash_value, dismissed_at=datetime.now(),
    )
    session.add(row)
    session.flush()
    logger.info("attention: dismissed %s %s", category, item_key)
    return row


def undismiss(session: Session, dismissal_id: int) -> DismissedAttentionItem | None:
    """Bring an item back deliberately. The row is stamped, never deleted."""
    row = session.get(DismissedAttentionItem, dismissal_id)
    if row is None or row.undismissed_at is not None:
        return row
    row.undismissed_at = datetime.now()
    session.flush()
    logger.info("attention: un-dismissed %s %s", row.category, row.item_key)
    return row
