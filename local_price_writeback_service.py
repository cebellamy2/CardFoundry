"""Store, locally, the price a card is actually selling at.

WHY THIS EXISTS. Until v1.185.0 nothing automatic in CardFoundry had ever
written InventoryCard.current_price. It had exactly two assignment sites,
both a human typing into a form: the CF-SCAN-025 set-price route and the
card edit form. Flow B, the bulk pricing cron and new-listing publish all
computed a price, sent it to Mana Pool, and threw the number away.
Production proof, measured 2026-09-18: `inventory_price_history` held
FIVE rows in the application's entire history, every one of them
`source='manual'`.

The cost of that was 5,883 available cards with no local price at all,
which is not cosmetic: an unpriced card cannot be raised by
inventory_reconciliation_service (`unpriced` is its busiest refusal) and
cannot be returned to sale by manapool_quantity_push_service
(`no_price`). Stock we hold, that Mana Pool prices three times a day,
sat unsellable because we had not written down a number we already knew.

THE PRICE STORED IS THE FLOORED, BUYER-FACING ONE. Operator decision,
2026-09-18, asked and answered directly. Mana Pool stores the raw price
we send -- deliberately unclamped, see bulk_pricing_service's docstring
-- but applies the seller's store minimum at SERVE time, so a listing
whose raw price is $0.15 sells for $0.65. $0.65 is what the buyer pays,
so $0.65 is what the card is worth locally.

    CONSEQUENCE, AND IT IS NOT DRIFT: local current_price and the Mana
    Pool raw listing price now disagree permanently for every sub-floor
    card -- roughly 5,400 of 6,029 in the measured run. That is the
    design. Any future check comparing a local price against a remote
    listing price must compare against max(remote_raw, PRICING_FLOOR_
    CENTS), or it will report thousands of mismatches that are all
    correct -- the same shape of mistake as the membership-vs-identity
    orphan sweep that reported 14 orphans of which zero were real.

NO MANA POOL CALLS, EVER. write_back_bulk_export reads the per-item CSV
the pricing job has already fetched. The whole point is that the number
is free: the job priced 6,029 listings in ~13 seconds and handed us the
result, so storing it costs one local transaction and nothing else.

THE MATCHING KEY IS THE EXPORT'S, NOT THE BINDING'S. Mana Pool's bulk
export carries no product_id and no mtgjson_id; it identifies each
listing by Set Code / Collector Number / Language / Condition / Finish.
So that five-field tuple is the key, taken straight off InventoryCard's
own columns -- no RemoteProductBinding lookup, which also means no
exposure to `local_card_ids_json` going stale.

    Verified against production before this was built (2026-09-18, 8,763
    available cards): every card has all five fields populated, they form
    5,992 distinct tuples, and ZERO tuples span more than one mtgjson_id.
    The export's key is therefore never COARSER than the four-key
    identity rule `_desired_quantity_for_binding` uses, so it cannot
    cross-price two different printings. (The reverse -- one mtgjson_id
    spanning several tuples, 517 of them -- is simply condition and
    finish varying under one printing, which is what the four-key rule
    itself does.)

WHAT IS DELIBERATELY NOT TOUCHED:

  price_usd            The import-time record of what the card was
                       adopted at. It is history, and on legacy stock it
                       is the only surviving evidence of the Mana Pool
                       listing price at import. Overwriting it would
                       destroy that for a number we can recompute any
                       day.
  price_pending_since  An explicit operator "do not list this yet"
                       (CF-SCAN-025). Pricing a held card would quietly
                       overrule the hold, so a held card is skipped and
                       counted, never silently.
  anything remote      This module never writes to Mana Pool. The price
                       is already there; that is where it came from.

AND ONLY REAL CHANGES ARE AUDITED. The bulk job runs three times a day
over ~6,000 listings, and the overwhelming majority of those prices do
not move between runs. Writing an audit row per listing per run would
add ~18,000 rows a day that say nothing happened, and would bury the
handful that mean something. So the comparison is in integer cents (not
floats) and a no-op writes nothing at all -- no history row, no change
log, no UPDATE.
"""

import logging

from sqlalchemy.orm import Session

from models import InventoryCard, InventoryChangeLog, InventoryPriceHistory


logger = logging.getLogger("cardfoundry")


# The owner's absolute floor, and the single definition of it. main.py's
# PRICING_LOCKED_FLOOR_CENTS is this value -- it is imported there rather
# than repeated, so the config panel and the write-back can never drift
# apart into two different "the floor is" answers.
PRICING_FLOOR_CENTS = 65

# InventoryCard columns that reproduce the bulk export's own identity,
# in the order the export presents them.
IDENTITY_FIELDS = (
    "set_code", "collector_number", "language_id", "condition_id", "finish_id",
)

# The export's column headings for those same five fields.
EXPORT_IDENTITY_COLUMNS = (
    "Set Code", "Collector Number", "Language", "Condition", "Finish",
)

PRICE_SOURCE_BULK = "bulk_market_job"
PRICE_SOURCE_PUBLISH = "new_listing_publish"
PRICE_SOURCE_INVENTORY_SCAN = "seller_inventory_scan"


def floored_cents(raw_cents) -> int | None:
    """The buyer-facing price: never below the owner's floor.

    Returns None for anything that is not a usable price, so a malformed
    export cell can never be mistaken for a free card.
    """
    try:
        cents = int(raw_cents)
    except (TypeError, ValueError):
        return None
    if cents < 1:
        return None
    return max(cents, PRICING_FLOOR_CENTS)


def _normalise(value) -> str:
    return str(value or "").strip().upper()


def card_identity(card) -> tuple:
    return tuple(_normalise(getattr(card, field, None)) for field in IDENTITY_FIELDS)


def export_row_identity(row: dict) -> tuple:
    return tuple(_normalise(row.get(column)) for column in EXPORT_IDENTITY_COLUMNS)


def _export_price_cents(row: dict) -> int | None:
    """The 'New' column, as cents. Mirrors bulk_pricing_service._cents --
    the same '$1,234.56' shape, read here rather than imported so this
    module has no dependency on the HTTP-facing pricing client."""
    text = str(row.get("New") or "").replace("$", "").replace(",", "").strip()
    try:
        return int(round(float(text) * 100))
    except (TypeError, ValueError):
        return None


def _current_cents(card) -> int | None:
    if card.current_price is None:
        return None
    return int(round(float(card.current_price) * 100))


def set_card_price(session: Session, card, target_cents: int, *, source: str, note: str) -> bool:
    """Write one card's price, with the two audit rows a price change
    gets everywhere else. Returns False -- and writes nothing at all --
    when the card already holds this exact price.

    Deliberately the same pair of rows the card edit form writes
    (InventoryPriceHistory + InventoryChangeLog), because this is the
    same event: the card's price changed. The only difference is who
    decided it.
    """
    before_cents = _current_cents(card)
    if before_cents == target_cents:
        return False
    dollars = target_cents / 100
    session.add(InventoryPriceHistory(
        inventory_card_id=card.id,
        old_price=card.current_price,
        new_price=dollars,
        source=source,
    ))
    session.add(InventoryChangeLog(
        inventory_card_id=card.id,
        change_summary=f"current_price: {card.current_price!r} -> {dollars!r}; {note}",
    ))
    card.current_price = dollars
    return True


def seller_inventory_identity(item: dict) -> tuple | None:
    """The same five-field identity, read off a seller-inventory row.

    The bulk export and the seller-inventory scan describe the same
    listings in two different shapes: the export as CSV columns, the scan
    as a nested product/single object. Both carry set, collector number,
    language, condition and finish, so one matching rule serves both --
    this only unwraps the second shape into the first's tuple.

    Returns None when any field is missing, rather than a tuple with a
    blank in it: a blank would match local cards that are themselves
    missing that field, which is a different card, not this one.
    """
    single = (item.get("product") or {}).get("single") or {}
    values = (
        single.get("set"), single.get("number"), single.get("language_id"),
        single.get("condition_id"), single.get("finish_id"),
    )
    identity = tuple(_normalise(value) for value in values)
    return None if "" in identity else identity


def _apply_priced_identities(
    session: Session, pairs, *, source: str, note: str, dry_run: bool = False,
) -> dict:
    """The one implementation both entry points use.

    ``pairs`` is an iterable of (identity_tuple, target_cents). Everything
    that makes this correct -- available-only scope, the price_pending
    hold, cents comparison, the two audit rows, no-op suppression -- lives
    here once, so the bulk export and the inventory scan cannot drift into
    two different answers about what a card's price should be.

    dry_run answers the same questions without writing anything, so the
    preview and the apply can never disagree about what the apply would
    do: it is the same walk, with the write suppressed.
    """
    counts = {
        "rows_considered": 0, "rows_unmatched": 0, "rows_no_price": 0,
        "cards_updated": 0, "cards_unchanged": 0, "cards_skipped_hold": 0,
        "cards_newly_priced": 0, "cards_repriced": 0,
    }
    cards_by_identity: dict[tuple, list] = {}
    for card in session.query(InventoryCard).filter(
        InventoryCard.status == "available",
    ).all():
        cards_by_identity.setdefault(card_identity(card), []).append(card)

    for identity, target_cents in pairs:
        counts["rows_considered"] += 1
        if target_cents is None:
            counts["rows_no_price"] += 1
            continue
        matches = cards_by_identity.get(identity)
        if not matches:
            # Expected and uninteresting: a listing whose local cards are
            # sold, removed, or were never ours.
            counts["rows_unmatched"] += 1
            continue
        for card in matches:
            if card.price_pending_since is not None:
                counts["cards_skipped_hold"] += 1
                continue
            before = _current_cents(card)
            if before == target_cents:
                counts["cards_unchanged"] += 1
                continue
            counts["cards_updated"] += 1
            counts["cards_newly_priced" if before is None else "cards_repriced"] += 1
            if not dry_run:
                set_card_price(session, card, target_cents, source=source, note=note)
    return counts


def write_back_seller_inventory(
    session: Session, inventory: list[dict], *, dry_run: bool = False,
) -> dict:
    """Store every listed identity's floored price, from Perform Sync's
    own full seller-inventory read.

    WHY THIS EXISTS ALONGSIDE THE BULK ONE. The bulk pricing job's export
    contains only the listings that job CHANGED -- a few hundred on a
    normal tick, and twice on 2026-09-18 it contained nothing at all,
    because "price already at target" is a skip. So a card whose market
    price never moves would never appear in an export and would never get
    a local price, which left 5,883 cards unpriced and therefore unable
    to be raised or returned to sale.

    The seller-inventory scan has no such gap: it lists every listing,
    changed or not. Perform Sync already fetches it (18,904 rows, one
    paginated call) to build the mirror preview, so reading the price out
    of rows already in memory costs NOTHING -- no extra call, no extra
    pagination.

    Same matching rule, same floor, same audit rows, same no-op
    suppression as the bulk path; see _apply_priced_identities.
    """
    pairs = (
        (identity, floored_cents(item.get("price_cents")))
        for item, identity in (
            (item, seller_inventory_identity(item)) for item in inventory or []
        )
        if identity is not None
    )
    counts = _apply_priced_identities(
        session, pairs,
        source=PRICE_SOURCE_INVENTORY_SCAN,
        note="priced from the Mana Pool seller-inventory scan",
        dry_run=dry_run,
    )
    logger.info(
        "seller-inventory price write-back%s: cards newly priced=%s repriced=%s "
        "unchanged=%s skipped_hold=%s; listings considered=%s unmatched=%s no_price=%s",
        " (preview)" if dry_run else "",
        counts["cards_newly_priced"], counts["cards_repriced"],
        counts["cards_unchanged"], counts["cards_skipped_hold"],
        counts["rows_considered"], counts["rows_unmatched"], counts["rows_no_price"],
    )
    return counts


def write_back_bulk_export(session: Session, rows: list[dict]) -> dict:
    """Store each priced listing's floored price, from the bulk job's own
    per-item export. No Mana Pool calls -- ``rows`` is the export the job
    already downloaded (bulk_pricing_service.parse_export).

    NOTE THE LIMIT, measured 2026-09-18: this export contains only the
    listings the job CHANGED that run. "Already at target" is a skip, and
    skipped rows are omitted entirely -- two consecutive ticks exported
    nothing at all. So this path alone can never reach a card whose price
    does not move; write_back_seller_inventory is what covers those.

    Scope, as everywhere here: every AVAILABLE card matching the
    identity, not only the ones Mana Pool currently counts as listed. An
    available card of a listed identity is stock the next reconciliation
    raises into that same listing, at that same price.
    """
    pairs = (
        (export_row_identity(row), floored_cents(_export_price_cents(row)))
        for row in rows or []
        # A skipped or failed row's price did not move on Mana Pool, so
        # there is nothing of it to record here.
        if str(row.get("Status") or "").strip() == "success"
    )
    counts = _apply_priced_identities(
        session, pairs,
        source=PRICE_SOURCE_BULK,
        note="priced by the Mana Pool bulk market job",
    )
    logger.info(
        "bulk price write-back: cards updated=%s unchanged=%s skipped_hold=%s; "
        "export rows considered=%s unmatched=%s no_price=%s",
        counts["cards_updated"], counts["cards_unchanged"], counts["cards_skipped_hold"],
        counts["rows_considered"], counts["rows_unmatched"], counts["rows_no_price"],
    )
    return counts


def write_back_published_prices(session: Session, published_rows: list[dict]) -> dict:
    """Store the price a first listing was just published at.

    Without this a newly published card is listed on Mana Pool at a real
    computed price and remains locally unpriced -- immediately unable to
    be raised or returned to sale by its own price guard. The price is
    already floored by every tier of new_listing_pricing_service; it is
    put through floored_cents anyway so there is exactly one answer to
    "what is stored" in this codebase.
    """
    counts = {"cards_updated": 0, "cards_unchanged": 0, "cards_skipped_hold": 0}
    for row in published_rows or []:
        target_cents = floored_cents(row.get("target_price_cents"))
        if target_cents is None:
            continue
        for card_id in row.get("reconfirmed_card_ids") or []:
            card = session.get(InventoryCard, card_id)
            if card is None:
                continue
            if card.price_pending_since is not None:
                counts["cards_skipped_hold"] += 1
                continue
            changed = set_card_price(
                session, card, target_cents,
                source=PRICE_SOURCE_PUBLISH,
                note="priced when first listed on Mana Pool",
            )
            counts["cards_updated" if changed else "cards_unchanged"] += 1
    if counts["cards_updated"] or counts["cards_skipped_hold"]:
        logger.info(
            "new-listing publish write-back: cards updated=%s unchanged=%s skipped_hold=%s",
            counts["cards_updated"], counts["cards_unchanged"], counts["cards_skipped_hold"],
        )
    return counts
