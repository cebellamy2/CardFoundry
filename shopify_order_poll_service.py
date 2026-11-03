"""The polling fallback for Shopify orders: everything the webhook missed.

★ WHY A POLL AT ALL, when Shopify retries its webhooks. Because a
webhook we never received is a webhook we cannot know about. The Mana
Pool side learned this as a latency problem -- median 24 minutes before
its webhook existed -- but the shape that matters here is the one the
Mana Pool receiver's own docstring names: push FIRST, not push instead.
A subscription can be deleted, a secret can rotate, a deploy can land
mid-delivery, and the only thing that notices is something that goes
looking.

★ IT IS IDEMPOTENT BY INHERITANCE, not by anything added here.
shopify_order_service.ingest_one keys on (source="shopify",
external_order_id) and returns "already_known" for an order it has seen,
so the poll seeing every webhook-ingested order again is a no-op. That
property is what lets both paths run without coordinating.

★ THE CURSOR IS A TIMESTAMP, NOT A PAGE CURSOR, and deliberately. A
GraphQL `after` cursor is only meaningful within one connection and one
sort; `updated_at` is meaningful across runs, survives a restart, and
re-reading a few minutes of overlap costs nothing because the ingest is
idempotent. It is stored one minute BEHIND what was seen, for exactly
that overlap.

★ AND IT READS ONLY. The poll ingests locally -- which reserves stock,
the whole point -- but makes no Shopify write of any kind.

Shopify's own constraint, quoted: "Only the last 60 days' worth of
orders from a store are accessible from the `Order` object by default."
So this is a fallback for recent orders, not a history import.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

import shopify_service
from models import AppSetting
from shopify_order_service import ingest_shopify_orders, intake_enabled

logger = logging.getLogger("cardfoundry")

CURSOR_SETTING_KEY = "shopify_order_poll_cursor"

# How far back a first run looks when there is no cursor yet. Short on
# purpose: a first run is for proving the path, not for importing
# history, and Shopify only serves 60 days anyway.
FIRST_RUN_LOOKBACK_HOURS = 24

# Re-read this much either side of the cursor. The ingest is idempotent,
# so overlap is free, and an order updated in the same second the cursor
# was written must not fall down the gap between two runs.
CURSOR_OVERLAP_MINUTES = 1

ORDERS_PAGE_SIZE = 50
LINE_ITEMS_PER_ORDER = 50
MAX_PAGES = 40

# ★ NO sortKey. The documented default is PROCESSED_AT and the
# OrderSortKeys enum's values are not something this app has verified --
# a guessed enum value fails at runtime on the operator's machine rather
# than here. The `updated_at` filter is what bounds the result; the order
# within it does not matter to an idempotent ingest.
ORDERS_QUERY = """
query CardFoundryShopifyOrders($first: Int!, $after: String, $query: String!) {
  orders(first: $first, after: $after, query: $query) {
    edges {
      cursor
      node {
        id
        name
        createdAt
        updatedAt
        cancelledAt
        displayFulfillmentStatus
        displayFinancialStatus
        email
        lineItems(first: %d) {
          edges {
            node {
              id
              sku
              quantity
              name
              discountedUnitPriceSet { shopMoney { amount currencyCode } }
              originalUnitPriceSet { shopMoney { amount currencyCode } }
            }
          }
        }
      }
    }
    pageInfo { hasNextPage }
  }
}
""" % LINE_ITEMS_PER_ORDER


def poll_cursor(session: Session) -> datetime | None:
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == CURSOR_SETTING_KEY).first())
    raw = (setting.value if setting else "") or ""
    if not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        logger.warning(
            "%s holds an unreadable timestamp %r; treating this as a first "
            "run rather than guessing.", CURSOR_SETTING_KEY, raw,
        )
        return None


def set_poll_cursor(session: Session, when: datetime) -> None:
    """Move the cursor. No audit row: it is a position, not a policy --
    the same reasoning the reconciliation's stored result uses."""
    value = when.replace(microsecond=0).isoformat()
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == CURSOR_SETTING_KEY).first())
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=CURSOR_SETTING_KEY, value=value,
                               updated_at=datetime.now()))


def _since(session: Session, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cursor = poll_cursor(session)
    if cursor is None:
        return now - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS)
    return cursor - timedelta(minutes=CURSOR_OVERLAP_MINUTES)


def read_orders_since(since: datetime, *, client=None,
                      page_size: int = ORDERS_PAGE_SIZE,
                      max_pages: int = MAX_PAGES) -> dict:
    """Every Shopify order updated at or after `since`. Reads only.

    `updated_at` rather than `created_at`: a cancellation updates an
    order without creating one, and the cancelled topic can be missed
    the same way a create can. The filter syntax is Shopify's own, from
    its examples: `updated_at:>'2020-10-21T23:39:20Z'`.

    Reports whether the walk FINISHED. A truncated page walk read as a
    complete one would move the cursor past orders nobody ingested.
    """
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f"updated_at:>='{stamp}'"
    orders = []
    cursor = None
    pages = 0
    complete = False
    for pages in range(1, max_pages + 1):
        data = shopify_service.execute(
            ORDERS_QUERY,
            {"first": page_size, "after": cursor, "query": query},
            client=client,
        )
        connection = (data or {}).get("orders") or {}
        edges = connection.get("edges") or []
        orders.extend((edge or {}).get("node") or {} for edge in edges)
        if not (connection.get("pageInfo") or {}).get("hasNextPage"):
            complete = True
            break
        cursor = edges[-1].get("cursor") if edges else None
        if not cursor:
            logger.warning(
                "Shopify order poll: hasNextPage with no cursor after %s "
                "page(s); stopping.", pages,
            )
            break
    else:
        logger.warning(
            "Shopify order poll hit the %s-page ceiling at %s order(s); the "
            "cursor is NOT moved.", max_pages, len(orders),
        )
    logger.info(
        "Shopify order poll: %s order(s) updated since %s, read in %s "
        "page(s) (complete=%s).", len(orders), stamp, pages, complete,
    )
    return {"orders": orders, "complete": complete, "pages": pages,
            "since": stamp}


def poll_shopify_orders(session: Session, *, client=None,
                        now: datetime | None = None,
                        allocate: bool = True) -> dict:
    """Read recent Shopify orders and ingest the ones we have not seen.

    ★ THE CURSOR MOVES ONLY ON A COMPLETE WALK, and only to the newest
    `updatedAt` actually read. A partial walk that moved the cursor would
    skip every order it did not reach -- which, for a feature whose whole
    job is that no order is missed, is the one bug that matters.

    Caller commits (ingest_shopify_orders commits per order itself; this
    commits the cursor).
    """
    if not intake_enabled(session):
        return {"skipped": "SHOPIFY_ORDER_INTAKE_ENABLED is off",
                "read": 0, "ingested": 0}
    since = _since(session, now=now)
    read = read_orders_since(since, client=client)
    result = ingest_shopify_orders(session, read["orders"], allocate=allocate)
    newest = None
    for order in read["orders"]:
        raw = order.get("updatedAt")
        if not raw:
            continue
        try:
            seen = datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")
            ).astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        if newest is None or seen > newest:
            newest = seen
    moved = False
    if read["complete"] and newest is not None:
        set_poll_cursor(session, newest)
        moved = True
    elif read["complete"]:
        # A complete walk that found nothing still means "nothing changed
        # up to now", so the cursor may advance to the window's own edge.
        set_poll_cursor(session, since)
        moved = True
    logger.info(
        "Shopify order poll: %s read, %s ingested, %s already known, %s "
        "refused, %s failed, %s collision(s); cursor %s.",
        len(read["orders"]), len(result["ingested"]),
        len(result["already_known"]), len(result["refused"]),
        len(result["failed"]), result["collisions"],
        "moved" if moved else "LEFT WHERE IT WAS",
    )
    return {
        "skipped": None,
        "since": read["since"],
        "complete": read["complete"],
        "read": len(read["orders"]),
        "ingested": len(result["ingested"]),
        "already_known": len(result["already_known"]),
        "refused": len(result["refused"]),
        "failed": len(result["failed"]),
        "collisions": result["collisions"],
        "cursor_moved": moved,
        "detail": result,
    }
