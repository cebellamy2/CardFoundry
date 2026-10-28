"""Day-to-day quantity reconciliation between CardFoundry and Mana Pool.

Scoped to identities Mana Pool already lists -- the increase_quantity,
decrease_quantity, and zero_candidate categories from
inventory_mirror_service.build_inventory_mirror_preview(). A brand-new,
never-listed identity is new_listing_upload_service's job instead; this
module never creates a listing, only adjusts one that already exists.

increase_quantity is auto-applied when the gap is explained by cards
that are genuinely sellable right now (see _raisable_gap). The write is
computed as a delta -- fresh remote quantity + fresh new units, clamped
to fresh local desired quantity -- rather than a blind re-assertion of a
stale absolute number, so a concurrent sale is always reflected (Mana
Pool decrements its own quantity immediately on a sale, independent of
whether CardFoundry has ingested that order yet) instead of silently
overwritten.

THE TIMESTAMP GATE IS GONE, DELIBERATELY. Until 2026-09-17 the gate also
required every gap-explaining card to have been imported AFTER the remote
listing's own effective_as_of. That was a reasonable proxy for "new stock
Mana Pool has not seen yet" while listings were touched rarely. It is not
any more: since v1.174.0 the bulk pricing job rewrites every listing three
times a day, so effective_as_of is always hours old while a real card's
imported_at is weeks old, and the gate excluded 138 of 138 genuine
under-listings -- $300.39 of stock Mana Pool was not offering, permanently.

Removing it costs nothing, because the safety property was never actually
carried by that comparison: apply_reconciliation_preview re-reads Mana
Pool's quantity fresh immediately before writing and clamps to the fresh
local desired count. The stale-number risk is handled downstream, at the
moment of the write, by data read seconds earlier. Do not reintroduce a
timestamp check here; fix the downstream re-read instead if it is ever
found wanting.

decrease_quantity/zero_candidate need no such gate: writing a
(possibly slightly stale) lower number is self-correcting, never an
oversell. It does rely on local state being fresh, though -- a Mana
Pool sale reducing true local availability only shows up here once
CardFoundry has ingested that order, which is why apply always
re-ingests orders first.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from inventory_mirror_service import MTGJSON_OVERRIDE_KEY_PREFIX, SELLABLE_STATUS
# The owner's floor has one definition and this is it -- imported rather
# than repeated so the restock price comparison and the local write-back
# can never drift into two different answers about what the floor is.
from local_price_writeback_service import PRICING_FLOOR_CENTS
from manapool_quantity_push_service import _desired_quantity_for_binding
from models import (
    Batch, FulfillmentException, InventoryCard, PickAllocation,
    RemoteProductBinding,
)
from physical_identity import is_english
import order_service
from order_service import ingest_manapool_orders


logger = logging.getLogger("cardfoundry")

# What an apply actually did. "nothing_to_apply" is a clean no-op, NOT a
# failure: every eligible row was independently skipped with its own
# reason and no Mana Pool write was made. It is deliberately NOT a
# success signal for the scheduled-job alarm either -- that keys on a
# completed new_listing_preview, which is the step this no-op now lets
# the chain reach.
# An identity whose local stock cannot be counted: no validated binding
# to resolve it and no contributing cards named by the mirror. A DECREASE
# for such a row is refused rather than written, because the 0 it would
# otherwise produce is a quiet delist. The increase direction cannot
# reach this state -- see the comment at its exclusion branch.
UNCOUNTABLE_IDENTITY_REASON = (
    "Cannot count this identity's local stock -- no validated binding and no "
    "traceable cards; Mana Pool's quantity is left as it is"
)

APPLIED = "applied"
NOTHING_TO_APPLY = "nothing_to_apply"
# The write went out and we never heard back in time. The prices and
# quantities may be live; they may not. Never a blind retry -- the apply
# reads the affected products back and says, per product, which it is.
WRITE_UNCONFIRMED = "write_unconfirmed"

# Why a restock could not carry a price of its own.
PRICE_REFUSALS = {
    "unpriced_at_apply": (
        "A card backing this restock lost its price between preview and apply"
    ),
}


class InventoryReconciliationError(ValueError):
    pass


# Why an increase was refused. Surfaced per row so an excluded gap is
# explicable rather than silently absent -- the previous gate computed a
# reason and showed it nowhere, which is how 138 rows sat unreconciled
# without anyone being able to see why.
GAP_REFUSALS = {
    "no_gap": "Mana Pool already lists at least the local quantity",
    "not_enough_cards": "Fewer sellable cards locally than the gap claims",
    "unavailable": "A contributing card is no longer available",
    "unpriced": "A contributing card has no price -- it must not be listed",
    "open_exception": "A contributing card is under an unresolved fulfillment exception",
}


def _raisable_gap(session: Session, row: dict) -> tuple[list | None, str | None]:
    """The cards that justify raising this listing, or (None, reason).

    Every guard here is about the cards being genuinely sellable RIGHT
    NOW. What is deliberately NOT here is any comparison against the
    listing's effective_as_of -- see this module's docstring for why that
    check became unpassable and why the downstream fresh re-read is the
    real safety.

    An unpriced card is the guard that does the most work in practice:
    126 of the 138 stuck rows have one, and raising them would put a card
    on sale with no price.
    """
    gap = int(row.get("desired_quantity") or 0) - int(row.get("current_remote_quantity") or 0)
    if gap <= 0:
        return None, "no_gap"
    card_ids = row.get("local_contributing_card_ids") or []
    cards = [
        card for card in (session.get(InventoryCard, card_id) for card_id in card_ids)
        if card is not None
    ]
    if any(card.status != "available" for card in cards):
        return None, "unavailable"
    if len(cards) < gap:
        return None, "not_enough_cards"
    if any(card.current_price is None for card in cards):
        return None, "unpriced"
    if _cards_under_open_exception(session, [card.id for card in cards]):
        return None, "open_exception"
    # Newest first: if more cards back this identity than the gap needs,
    # the most recently acquired are the ones Mana Pool is least likely
    # to already be counting.
    cards.sort(key=lambda card: card.imported_at, reverse=True)
    return [card.id for card in cards[:gap]], None


def restock_price_cents(cards: list, remote_item: dict) -> tuple[int | None, str | None]:
    """The price to send alongside a restock, or (None, reason).

    WHY A RESTOCK CARRIES A PRICE AT ALL. Raising the quantity on an
    existing listing re-exposes whatever price that listing was last left
    at. For a listing that has been empty a while, that price is frozen:
    the pricing cron filters on `minQuantity: 1`, so it has never seen it
    and cannot have maintained it. Live cost, 2026-10-27: Nicol Bolas,
    Dragon-God (RVR 205 EN/NM/NF) went back on sale at a price set no
    later than 2026-02-17 and sold for $3.13 inside two hours, against a
    $36.30 lowest competitor. The next pricing tick was 1h54m too late,
    and there is nothing special about that gap -- perform-sync restocks
    at :30 past 2, 10 and 18 UTC while pricing runs at :25 past 1, 6, 11,
    16 and 21, so every restock is exposed for up to ~3h55m.

    So the price goes out WITH the quantity, in the same write. Mana
    Pool's own documentation for POST /seller/inventory/product is "Bulk
    set quantity and/or price ... To update just one field per item, pass
    null for the other" (OpenAPI v0.53.0), which is why this is one
    atomic write and not a price call followed by a quantity call. There
    is no window in between because there is no in between.

    (None, None) means "leave the listing's price alone" and is the
    ordinary answer for a healthy listing -- see the floor comparison
    below. (None, reason) means refused.

    WHICH LOCAL PRICE. InventoryCard.current_price, the one price
    CardFoundry maintains for a card, and the MAX across the cards this
    restock is adding.

    ★ THE HIGHEST IS THE OPERATOR'S RULE (operator decision, 2026-10-28),
    not an implementation convenience, so it does not get traded away for
    a mean or a first-card-wins later. A listing carries one price for
    every unit, so when interchangeable copies disagree -- which only
    happens transiently, for a card entered at its own asking price next
    to stock the last scan already priced -- the higher one is sent.
    Undershooting is the failure this whole change exists to stop, and it
    is the irreversible one: a card sold at $3.13 against a $36.30
    competitor cannot be unsold. Overshooting costs a few hours of no
    sale and self-corrects at the next pricing tick.

    price_usd is deliberately not consulted: it is the import-time
    record, not a maintained price.

    THE FLOOR COMPARISON IS NOT OPTIONAL. Mana Pool stores the raw price
    we send and applies the store minimum at serve time, so thousands of
    our listings hold a deliberately sub-floor raw price while
    current_price holds the floored, buyer-facing one (see
    local_price_writeback_service's docstring). Comparing the two
    directly would call every one of those a mismatch and rewrite it for
    no change in what a buyer pays. So the comparison is against
    max(remote_raw, PRICING_FLOOR_CENTS), and an agreement there sends
    nothing.

    ``cards`` must be non-empty -- its only caller has already excluded
    the row when nothing is still available, and a price decided by no
    card at all would be a price nobody chose. Stated as a precondition
    rather than guarded into a refusal the caller cannot produce.
    """
    assert cards, "restock_price_cents needs at least one card to price from"
    if any(card.current_price is None for card in cards):
        return None, "unpriced_at_apply"
    local_cents = max(
        int(round(float(card.current_price) * 100)) for card in cards
    )
    if local_cents < 1:
        # Mana Pool's price_cents has `minimum: 1`; a zero or negative
        # price is not a price it will accept, and inventing one is worse.
        return None, "unpriced_at_apply"
    try:
        remote_raw = int(remote_item.get("price_cents") or 0)
    except (TypeError, ValueError):
        remote_raw = 0
    if local_cents == max(remote_raw, PRICING_FLOOR_CENTS):
        return None, None
    return local_cents, None


def _cards_under_open_exception(session: Session, card_ids: list) -> list:
    """Card ids sitting under an unresolved fulfillment exception.

    Such a card is physically in question -- the operator has said it is
    missing or wrong -- so offering another unit of it is the phantom
    stock the exception exists to prevent.
    """
    if not card_ids:
        return []
    return [
        allocation.inventory_card_id
        for allocation, _exception in (
            session.query(PickAllocation, FulfillmentException)
            .join(FulfillmentException,
                  FulfillmentException.pick_allocation_id == PickAllocation.id)
            .filter(PickAllocation.inventory_card_id.in_(card_ids),
                    FulfillmentException.inventory_resolution_state == "unresolved")
            .all()
        )
    ]


def extract_reconciliation_candidates(session: Session, mirror_preview: dict) -> tuple[list[dict], list[dict]]:
    """Build reconciliation candidates from a mirror preview's
    increase_quantity/decrease_quantity/zero_candidate rows.

    Returns (candidates, excluded). An increase candidate carries
    ``batch_codes``/``gap_card_ids`` for the apply-time re-check (the gap
    can span several batches -- batch_codes is informational display
    only, never read by apply). BOTH directions carry
    ``local_contributing_card_ids``: it is what lets
    _fresh_desired_quantity count an identity it cannot otherwise
    resolve, and on a decrease row that count is the floor below which
    the apply will not write. Neither direction takes its write quantity
    from the preview -- that is always recomputed fresh at apply time.
    """
    candidates = []
    excluded = []
    for row in mirror_preview.get("rows") or []:
        category = row.get("category")
        if category not in ("increase_quantity", "decrease_quantity", "zero_candidate"):
            continue
        base = {
            "canonical_identity": row.get("canonical_identity") or {},
            "name": row.get("name") or "",
            "product_id": row.get("remote_product_id"),
            "reviewed_desired_quantity": row.get("desired_quantity"),
            "reviewed_remote_quantity": row.get("current_remote_quantity"),
        }
        if category == "increase_quantity":
            gap_card_ids, refusal = _raisable_gap(session, row)
            if not gap_card_ids:
                excluded.append({
                    **base, "direction": "increase",
                    "refusal": refusal,
                    "reason": GAP_REFUSALS.get(refusal, "Increase refused"),
                })
                continue
            gap_cards = [session.get(InventoryCard, card_id) for card_id in gap_card_ids]
            batch_ids = sorted({card.batch_id for card in gap_cards if card is not None})
            batches = [session.get(Batch, batch_id) for batch_id in batch_ids]
            batch_codes = sorted(batch.batch_code for batch in batches if batch is not None)
            candidates.append({
                **base, "direction": "increase",
                "batch_codes": batch_codes,
                "gap_card_ids": gap_card_ids,
                "gap": len(gap_card_ids),
                # ★ FIX (a), restored 2026-10-14 together with the
                # decrease floor the operator required as its condition.
                # _fresh_desired_quantity has a fallback for an identity
                # it cannot resolve on its own -- count the cards the
                # mirror attributed to the row -- and the apply already
                # passes row["local_contributing_card_ids"] to reach it.
                # This dict never carried that key, so the argument was
                # ALWAYS None and the fallback was unreachable from the
                # cron path. A non-English row with no validated binding
                # therefore recomputed desired = 0, which excluded it,
                # which emptied `updates`, which (before v2.41.1) raised
                # and stopped the whole perform-sync chain.
                #
                # Measured on production 2026-10-14: the ONLY eligible
                # candidate in the live mirror is The Ozolith JA/LP/NF
                # (product 0256cd6d, cards 11598/11599), whose desired
                # quantity reads 0 without this key and 2 with it.
                #
                # It is the MIRROR's list, not gap_card_ids. The gap is
                # desired MINUS remote, so passing the gap would
                # under-count the desired total whenever remote > 0 -- it
                # only looked equivalent for the Ozolith because its
                # remote is 0.
                "local_contributing_card_ids": (
                    row.get("local_contributing_card_ids") or []
                ),
            })
        else:
            desired = base["reviewed_desired_quantity"] or 0
            remote = base["reviewed_remote_quantity"] or 0
            if desired >= remote:
                # zero_candidate in particular can re-flag the same
                # already-zeroed listing forever: build_inventory_mirror_
                # preview's bound-orphan branch (a bound product_id with
                # no local inventory of any status) categorizes it
                # zero_candidate purely on that shape, with no check that
                # remote is still above 0. Once a prior run has already
                # written it down to 0 (or it was never above the desired
                # count), there's nothing left to decrease -- apply-time
                # re-verification would exclude it anyway (write_quantity
                # >= fresh_remote_quantity), but only after burning the
                # whole batch: confirmed live (2026-09-09), a run whose
                # only eligible candidates were already-zeroed orphans
                # left zero real updates and raised "None of the reviewed
                # rows are still valid to reconcile", crashing the entire
                # perform-sync chain even though nothing needed fixing.
                excluded.append({
                    **base, "direction": "decrease",
                    "reason": "Already at or below desired quantity -- nothing to reconcile",
                })
                continue
            # ★ THE DECREASE FLOOR'S INPUT (2026-10-14). The parked
            # version of (a) withheld this key from decrease rows,
            # reasoning that handing them the fallback "would let an
            # unresolvable identity write a 0 over a live listing". That
            # reasoning was backwards, and _fresh_desired_quantity's own
            # docstring says so: WITHOUT these ids an unresolvable
            # identity already returns 0, and in this direction a 0 IS
            # written. Carrying them is what raises that 0 to the real
            # count of traceable available cards -- the floor. Measured:
            # for the Ozolith after one of its two cards sells, the
            # fallback returns 1 with these ids and 0 without them.
            #
            # The remaining hazard is an unresolvable identity that names
            # NO cards at all; apply_reconciliation_preview refuses to
            # write that row rather than write a 0 it cannot justify.
            candidates.append({
                **base, "direction": "decrease",
                "local_contributing_card_ids": (
                    row.get("local_contributing_card_ids") or []
                ),
            })
    return candidates, excluded


def build_reconciliation_preview(session: Session, mirror_preview: dict) -> dict:
    """Identify reconciliation candidates. No writes, no pricing -- these
    are quantity-only updates; price_cents is never touched.
    """
    candidates, excluded = extract_reconciliation_candidates(session, mirror_preview)
    rows = (
        [{**candidate, "status": "eligible"} for candidate in candidates]
        + [{**row, "status": "excluded"} for row in excluded]
    )
    return {
        "preview_only": True,
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_local_snapshot_hash": mirror_preview.get("local_snapshot_hash"),
        "source_remote_snapshot_hash": mirror_preview.get("remote_snapshot_hash"),
        "rows": rows,
        "summary": {
            "candidates": len(candidates),
            "increase": sum(row["direction"] == "increase" for row in candidates),
            "decrease": sum(row["direction"] == "decrease" for row in candidates),
            "excluded": len(excluded),
        },
    }


# The mirror substitutes a SYNTHETIC key into the mtgjson_id slot when a
# card has no real MTGJSON id -- "__mtgjson_override__:<product_id>" for a
# card whose binding is override-confirmed, "__scryfall__:<scryfall_id>"
# for one with no binding at all (see inventory_mirror_service). The shape
# matches CANONICAL_FIELDS so no grouping logic has to know the difference
# -- but it means the value in that slot is NOT an MTGJSON id, and
# comparing it against InventoryCard.mtgjson_id can only ever match
# nothing.
_SYNTHETIC_MTGJSON_PREFIXES = (MTGJSON_OVERRIDE_KEY_PREFIX, "__scryfall__:")


def _is_synthetic_mtgjson_key(value) -> bool:
    text = str(value or "").strip().lower()
    return any(text.startswith(p) for p in _SYNTHETIC_MTGJSON_PREFIXES)


def _traceable_available_count(session: Session, contributing_card_ids) -> int:
    """How many of the cards the mirror attributes to a row are still
    locally sellable.

    THE ONE definition of "traceable available", shared by
    _fresh_desired_quantity's fallback and the decrease floor below, so
    the number the floor protects is by construction the number the
    fallback computed. Two copies of this query would be two chances to
    disagree about a quantity that gets written to a live listing.
    """
    ids = [int(card_id) for card_id in (contributing_card_ids or [])]
    if not ids:
        return 0
    return (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.id.in_(ids),
            InventoryCard.status == SELLABLE_STATUS,
            Batch.is_archived == False,
        )
        .count()
    )


def _identity_is_unresolvable(
    session: Session, identity: dict, product_id: str | None,
) -> bool:
    """True when _fresh_desired_quantity cannot trust its four-key query
    for this row and must fall back to the mirror's contributing cards.

    Exactly the condition _fresh_desired_quantity branches on, named once
    so the decrease floor can ask the same question instead of restating
    it. An ENGLISH row with a real MTGJSON id is never unresolvable: its
    id is what the mirror matched the local cards on, so the four-key
    query is authoritative and the floor must keep its hands off it.
    """
    if not product_id:
        return False
    synthetic = _is_synthetic_mtgjson_key(identity.get("mtgjson_id"))
    if not (synthetic or not is_english(identity.get("language_id"))):
        return False
    binding = (
        session.query(RemoteProductBinding)
        .filter(
            RemoteProductBinding.provider == "manapool",
            RemoteProductBinding.binding_status == "validated",
            RemoteProductBinding.product_id == str(product_id),
        )
        .first()
    )
    return binding is None


def _fresh_desired_quantity(
    session: Session, identity: dict, product_id: str | None = None,
    contributing_card_ids=None,
) -> int:
    """Fresh count of locally-sellable cards for the row about to be written.

    ★ A SYNTHETIC KEY CANNOT BE MATCHED AS AN MTGJSON ID. When the mirror
    has substituted a sentinel into the mtgjson_id slot, the four-key query
    below compares that sentinel against InventoryCard.mtgjson_id and
    necessarily returns 0 -- for a printing that may hold real available
    stock. Measured live 2026-10-04: one such row (JA Spell Pierce, binding
    6742, override-confirmed since 2026-09-17) with one card available.

    What that 0 does depends on direction, and only one is destructive:

      decrease / zero_candidate -- write_quantity IS this number, and the
          row is excluded only when write >= fresh_remote. A wrong 0
          against a remote quantity of 1+ is therefore WRITTEN, zeroing a
          listing that should have held stock.
      increase -- write_quantity is min(fresh_remote + traceable, this),
          so a 0 makes write <= fresh_remote and the row is EXCLUDED. That
          direction fails safe: the listing simply never goes up.

    For those rows the quantity is taken from the canonical reader of the
    one identity rule, manapool_quantity_push_service.
    _desired_quantity_for_binding -- which applies physical_identity's
    non-English rule (same language, meld-aware name, set code, exact
    collector number, condition, finish) rather than a fourth private copy
    of a matching rule.

    ★ SCOPED TO SYNTHETIC *AND* NON-ENGLISH ROWS (widened in v2.20.0), and
    the English scoping is the whole point. When an ENGLISH row carries a
    real MTGJSON id, that id is what the mirror actually matched the local
    cards on, and a binding sitting on the same product_id may legitimately
    DISAGREE with it (a conflicting binding is a real, tested state -- see
    test_apply_reports_conflict_without_writing_a_second_binding), so
    deferring to the binding there would silently change English answers.
    The four-key query below stays in charge of every English row, exactly
    as before.

    WHY NON-ENGLISH HAD TO JOIN THE SYNTHETIC CASE. That premise -- "the
    row's mtgjson_id is what the local cards were matched on" -- stopped
    being true for non-English rows the moment inventory_mirror_service
    learned the physical-identity rule. A folded row's canonical_identity
    carries the LISTING's mtgjson_id while its cards carry a different one,
    by construction, so the four-key query would return 0 for a row holding
    real stock. In the decrease direction that 0 is WRITTEN, which means
    fixing the mirror alone would have ARMED this function to zero a
    non-English listing it had just correctly matched. The mirror fix and
    this widening are one change in two files, not two changes.
    """
    synthetic = _is_synthetic_mtgjson_key(identity.get("mtgjson_id"))
    non_english = not is_english(identity.get("language_id"))
    if product_id and (synthetic or non_english):
        binding = (
            session.query(RemoteProductBinding)
            .filter(
                RemoteProductBinding.provider == "manapool",
                RemoteProductBinding.binding_status == "validated",
                RemoteProductBinding.product_id == str(product_id),
            )
            .first()
        )
        if binding is not None:
            return _desired_quantity_for_binding(session, binding)
        # No binding to ask. The four-key query below would compare an
        # mtgjson_id that this row may not have been matched on at all, and
        # a wrong 0 IS WRITTEN in the decrease direction, so fall back to the
        # cards the mirror itself says contribute to this row rather than to
        # a count that is known to be unreliable here.
        ids = [int(card_id) for card_id in (contributing_card_ids or [])]
        if ids:
            counted = _traceable_available_count(session, ids)
            logger.warning(
                "reconciliation: row for product %s has no validated binding "
                "to resolve its %s identity; counted %s of %s card(s) the "
                "mirror attributes to it instead.", product_id,
                "synthetic" if synthetic else "non-English",
                counted, len(ids),
            )
            return counted
        logger.warning(
            "reconciliation: row for product %s carries a %s identity key, "
            "has no validated binding to resolve it and names no contributing "
            "cards; its desired quantity cannot be counted and is reported "
            "as 0.", product_id, "synthetic" if synthetic else "non-English",
        )
        return 0
    return (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.status == SELLABLE_STATUS,
            Batch.is_archived == False,
            func.upper(InventoryCard.mtgjson_id) == str(identity.get("mtgjson_id") or "").upper(),
            func.upper(InventoryCard.language_id) == str(identity.get("language_id") or "").upper(),
            func.upper(InventoryCard.condition_id) == str(identity.get("condition_id") or "").upper(),
            func.upper(InventoryCard.finish_id) == str(identity.get("finish_id") or "").upper(),
        )
        .count()
    )


def _ensure_binding_for_increase(session: Session, row: dict, product_id: str, remote_item: dict) -> str | None:
    """Create a validated RemoteProductBinding for this identity if none
    exists yet -- an increase write is CardFoundry asserting real
    sellable stock against this exact product_id, the same fact a new-
    listing publish or the v1.109 backfill would record as a binding.
    Without it, manapool_quantity_push_service (the immediate per-
    transition push) has no product_id to resolve for this identity and
    silently lands in UnresolvedQuantityPush the next time local stock
    changes -- confirmed live: this exact gap (increase to a listing
    with no binding, then a later removal with no push target) is what
    let a Mana Pool order arrive for stock CardFoundry no longer had
    (order 4050, Blood Money, 2026-09-06/07).

    Never raises and never overwrites an existing binding -- a
    conflicting binding already on this product_id is left alone and
    reported, matching every other binding-creation site's guard
    (backfill_remote_product_bindings.py, production_import_service.py).
    Returns "created", "existing", or "conflict" for the caller to
    report; caller commits.
    """
    identity = row.get("canonical_identity") or {}
    mtgjson_id = str(identity.get("mtgjson_id") or "")
    language_id = str(identity.get("language_id") or "")
    condition_id = str(identity.get("condition_id") or "")
    finish_id = str(identity.get("finish_id") or "")
    if not mtgjson_id:
        return None
    existing = session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
        RemoteProductBinding.binding_status == "validated",
        func.upper(RemoteProductBinding.mtgjson_id) == mtgjson_id.upper(),
        func.upper(RemoteProductBinding.language_id) == language_id.upper(),
        func.upper(RemoteProductBinding.condition_id) == condition_id.upper(),
        func.upper(RemoteProductBinding.finish_id) == finish_id.upper(),
    ).first()
    if existing:
        return "existing"
    conflict = session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
        RemoteProductBinding.product_type == "mtg_single",
        RemoteProductBinding.product_id == product_id,
    ).first()
    if conflict:
        return "conflict"
    single = (remote_item.get("product") or {}).get("single") or {}
    requested_identity = {
        "name": row.get("name") or single.get("name") or "",
        "set_code": single.get("set") or "",
        "collector_number": single.get("number") or "",
        "scryfall_id": single.get("scryfall_id") or "",
        "mtgjson_id": mtgjson_id,
        "language_id": language_id,
        "condition_id": condition_id,
        "finish_id": finish_id,
    }
    evidence = {
        "source": "inventory_reconciliation_service.apply_reconciliation_preview",
        "matched_via": "reconciliation_increase",
        "requested_identity": requested_identity,
        "product_type": "mtg_single",
        "product_id": product_id,
    }
    evidence_hash = hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    now = datetime.now(timezone.utc)
    session.add(RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product_id,
        local_card_ids_json=json.dumps(sorted(row.get("gap_card_ids") or [])),
        requested_identity_json=json.dumps(requested_identity, sort_keys=True),
        scryfall_id=requested_identity["scryfall_id"], mtgjson_id=mtgjson_id,
        language_id=language_id, condition_id=condition_id, finish_id=finish_id,
        set_code=requested_identity["set_code"], collector_number=requested_identity["collector_number"],
        binding_status="validated", validated_at=now,
        catalog_as_of=remote_item.get("effective_as_of"), evidence_hash=evidence_hash,
        evidence_json=json.dumps(evidence, sort_keys=True),
        remote_inventory_id=str(remote_item.get("id") or "") or None,
    ))
    return "created"


def _read_back_after_timeout(updates: list[dict], seller_loader, exc) -> dict:
    """Did a timed-out inventory write land? Read, never retry.

    One ordinary seller-inventory read -- the same documented call this
    apply already made minutes earlier, through the same injected loader,
    so this adds no new Mana Pool dependency and no write of any kind. A
    product counts as confirmed only when BOTH the quantity and (where one
    was sent) the price match what went out; `price_cents: None` means we
    asked for no price change, so any price is a match for that field.

    A read-back that itself fails is reported as such rather than guessed
    at: "we could not look" and "we looked and it did not land" are
    different facts, and only the second one means the write was lost.
    """
    sent_by_product = {str(row.get("product_id")): row for row in updates}
    try:
        fresh = seller_loader(min_quantity=0)
    except Exception as read_exc:  # noqa: BLE001 -- reported, not swallowed
        logger.error(
            "reconciliation: read-back after the write timeout (%s: %s) could "
            "not itself be completed (%s: %s). The write's outcome is unknown "
            "for all %s product(s); nothing was retried.",
            type(exc).__name__, exc, type(read_exc).__name__, read_exc,
            len(updates),
        )
        return {
            "all_confirmed": False,
            "read_error": f"{type(read_exc).__name__}: {read_exc}",
            "confirmed": [],
            "mismatched": [],
            "unreadable": list(sent_by_product.values()),
        }

    fresh_by_product = {
        str(item.get("product_id")): item for item in fresh or [] if item.get("product_id")
    }
    confirmed, mismatched, unreadable = [], [], []
    for product_id, sent in sent_by_product.items():
        found = fresh_by_product.get(product_id)
        if found is None:
            unreadable.append(sent)
            continue
        try:
            live_quantity = int(found.get("quantity"))
        except (TypeError, ValueError):
            unreadable.append(sent)
            continue
        quantity_matches = live_quantity == int(sent.get("quantity"))
        price_matches = True
        if sent.get("price_cents") is not None:
            try:
                price_matches = int(found.get("price_cents")) == int(sent["price_cents"])
            except (TypeError, ValueError):
                price_matches = False
        row = {
            **sent,
            "live_quantity": live_quantity,
            "live_price_cents": found.get("price_cents"),
        }
        (confirmed if quantity_matches and price_matches else mismatched).append(row)
    return {
        "all_confirmed": bool(confirmed) and not mismatched and not unreadable,
        "read_error": None,
        "confirmed": confirmed,
        "mismatched": mismatched,
        "unreadable": unreadable,
    }


def apply_reconciliation_preview(
    session: Session,
    preview: dict,
    orders_loader,
    detail_loader,
    go_live_at: str,
    seller_loader,
    product_writer,
) -> dict:
    """Re-verify every eligible row immediately before writing, then write.

    Re-ingests Mana Pool orders first (same as the maintenance preview
    does), so any sale that reduced true local availability is reflected
    in the fresh desired-quantity recompute below -- this is what keeps
    the decrease direction correct, since that direction has no other
    signal for a Mana Pool-side sale on this identity.

    Increase rows: recompute the traced batch's still-available card
    count fresh, re-read Mana Pool's current quantity fresh, and write
    fresh_remote + fresh_traceable_units (clamped to fresh local desired
    quantity) -- never the stale reviewed absolute number. An increase
    also sends the card's own price in the SAME write, so a listing
    coming back off an empty shelf never goes live at the price it was
    left at months ago; see restock_price_cents.

    Decrease/zero_candidate rows: recompute local desired quantity fresh
    and write it directly.

    Each row is judged independently -- one row going stale does not
    block its siblings, matching the batch-isolation principle used for
    order-status sync and new-listing publishing.
    """
    eligible_rows = [row for row in preview.get("rows") or [] if row.get("status") == "eligible"]
    if not eligible_rows:
        raise InventoryReconciliationError("This preview has no eligible rows to reconcile.")

    response = orders_loader(since=go_live_at)
    ingest_manapool_orders(
        session, response.get("orders") or [], detail_loader,
        max_orders=order_service.ORDER_SYNC_MAX_ORDERS_PER_RUN,
    )
    session.flush()

    remote_inventory = seller_loader(min_quantity=0)
    remote_by_product = {
        str(item.get("product_id") or ""): item
        for item in remote_inventory if item.get("product_id")
    }

    updates = []
    excluded = []
    binding_outcomes = []
    # One entry per restock that carries a price, for the audit. Kept out
    # of `updates` on purpose: that list IS the request body, and Mana
    # Pool's bulk inventory item schema names exactly five fields -- an
    # extra key of ours has no business being sent. Everything needed to
    # put a price back the way it was (the product, the price it held, the
    # price we sent, the cards that decided it) lives here instead, and
    # the caller stores it in the reconciliation_apply job snapshot.
    price_writes = []
    for row in eligible_rows:
        product_id = row.get("product_id")
        remote_item = remote_by_product.get(product_id)
        if not remote_item:
            excluded.append({**row, "exclusion_reason": "Mana Pool no longer lists this product"})
            continue
        fresh_remote_quantity = int(remote_item.get("quantity") or 0)
        fresh_desired_quantity = _fresh_desired_quantity(
            session, row["canonical_identity"], product_id,
            row.get("local_contributing_card_ids"),
        )

        if row["direction"] == "increase":
            fresh_gap_cards = [
                card for card in (
                    session.get(InventoryCard, card_id) for card_id in row.get("gap_card_ids") or []
                )
                if card is not None and card.status == "available"
            ]
            traceable_units = len(fresh_gap_cards)
            if traceable_units == 0:
                excluded.append({**row, "exclusion_reason": "Traced cards are no longer locally available"})
                continue
            write_quantity = min(fresh_remote_quantity + traceable_units, fresh_desired_quantity)
            if write_quantity <= fresh_remote_quantity:
                # ★ THIS MESSAGE IS NOW ALWAYS TRUE, and fix (a) is what
                # made it true rather than any change here. It used to
                # cover two unrelated situations: a listing that really
                # did already hold enough, and a row whose desired
                # quantity came back as an uncountable 0 -- which is what
                # an operator actually saw for the Ozolith on jobs 530 and
                # 543, saying the opposite of what had happened.
                #
                # With the contributing ids carried, that second case
                # cannot arise in this direction: _raisable_gap draws
                # gap_card_ids FROM local_contributing_card_ids and
                # requires every one of them to be available, so whenever
                # traceable_units > 0 the fresh desired quantity counts at
                # least those same cards and is >= 1. Reaching this line
                # therefore means Mana Pool's freshly re-read quantity has
                # genuinely caught up since the preview. A guard for the
                # uncountable case was written here and deleted: it was
                # unreachable, and an unwired guard reads as protection
                # that does not exist.
                excluded.append({**row, "exclusion_reason": "Mana Pool quantity already reflects this increase"})
                continue
            # The price goes out in THIS write, with the quantity, so the
            # listing never spends a moment live at its old one. See
            # restock_price_cents.
            write_price_cents, price_refusal = restock_price_cents(
                fresh_gap_cards, remote_item,
            )
            if price_refusal:
                # Fail closed. _raisable_gap already refuses an unpriced
                # row at preview time, so this is the gap between preview
                # and apply -- narrow, but the alternative is putting
                # stock on sale at a price nobody chose.
                reason = PRICE_REFUSALS[price_refusal]
                logger.warning(
                    "reconciliation: refusing to restock product %s (%s) -- %s. "
                    "Mana Pool keeps its current quantity of %s.",
                    product_id, row.get("name"), reason, fresh_remote_quantity,
                )
                excluded.append({**row, "exclusion_reason": reason})
                continue
            outcome = _ensure_binding_for_increase(session, row, product_id, remote_item)
            if outcome:
                binding_outcomes.append({"product_id": product_id, "name": row.get("name"), "outcome": outcome})
            if write_price_cents is not None:
                remote_price_before = remote_item.get("price_cents")
                card_prices = {
                    card.id: card.current_price for card in fresh_gap_cards
                }
                price_writes.append({
                    "product_id": product_id,
                    "name": row.get("name"),
                    "remote_price_cents_before": remote_price_before,
                    "price_cents_sent": write_price_cents,
                    "deciding_card_prices": card_prices,
                    "remote_quantity_before": fresh_remote_quantity,
                })
                if len(set(card_prices.values())) > 1:
                    logger.info(
                        "reconciliation: restock of product %s is backed by cards at "
                        "differing local prices %s -- sending the highest, %s cents.",
                        product_id, sorted(card_prices.values()), write_price_cents,
                    )
                logger.info(
                    "reconciliation: restocking product %s (%s) at %s cents, "
                    "replacing Mana Pool's %s -- quantity %s -> %s in one write.",
                    product_id, row.get("name"), write_price_cents,
                    remote_price_before, fresh_remote_quantity, write_quantity,
                )
        else:
            write_price_cents = None
            write_quantity = fresh_desired_quantity
            # ★ THE DECREASE FLOOR (operator's condition for restoring
            # fix (a), 2026-10-14). In this direction write_quantity IS
            # the recomputed desired quantity and the row is excluded only
            # when write >= fresh_remote, so a wrong LOW number is written
            # straight onto a live listing. For an identity the four-key
            # query cannot resolve, the only number we can defend is the
            # count of cards the mirror attributes to this row that are
            # still locally sellable -- and the write must never fall
            # below it.
            #
            # English rows with a real MTGJSON id are deliberately
            # untouched: their four-key count is authoritative, so a
            # genuine decrease (a card sold or removed) still writes the
            # correct lower number, including 0.
            if _identity_is_unresolvable(session, row["canonical_identity"], product_id):
                traceable_ids = row.get("local_contributing_card_ids") or []
                if not traceable_ids:
                    # Nothing to count and nothing to trust. Writing the 0
                    # this would otherwise produce is the quiet delist the
                    # never-zero rule exists to prevent.
                    logger.warning(
                        "reconciliation: refusing to decrease product %s -- its "
                        "identity has no validated binding and the mirror names "
                        "no contributing cards, so no defensible quantity can be "
                        "computed. Mana Pool keeps its current quantity of %s.",
                        product_id, fresh_remote_quantity,
                    )
                    excluded.append({**row, "exclusion_reason": UNCOUNTABLE_IDENTITY_REASON})
                    continue
                # The floor IS the write for this direction, so it is
                # assigned, not compared. With the contributing ids now
                # carried, _fresh_desired_quantity's fallback computes
                # this same count -- a `if write_quantity < floor` guard
                # here could never fire, and a guard that cannot fire
                # reads as protection that does not exist. Computing it
                # from the shared helper instead keeps the floor true on
                # its own terms rather than by coincidence with another
                # function's internals.
                floor = _traceable_available_count(session, traceable_ids)
                if floor != write_quantity:
                    logger.warning(
                        "reconciliation: decrease floor overrode product %s, %s "
                        "-> %s. The recomputed desired quantity disagreed with "
                        "the traceable available count; the floor wins.",
                        product_id, write_quantity, floor,
                    )
                logger.info(
                    "reconciliation: decrease floor for product %s is %s -- "
                    "%s of %s card(s) the mirror attributes to this unresolvable "
                    "identity are still locally sellable (Mana Pool holds %s).",
                    product_id, floor, floor, len(traceable_ids),
                    fresh_remote_quantity,
                )
                write_quantity = floor
            if write_quantity >= fresh_remote_quantity:
                excluded.append({**row, "exclusion_reason": "No longer a decrease as of this apply"})
                continue

        updates.append({
            "product_type": "mtg_single",
            "product_id": product_id,
            # Null leaves Mana Pool's existing price unchanged, which is
            # what every decrease row and every already-current restock
            # wants. A restock onto a stale price sends the real number.
            "price_cents": write_price_cents,
            "quantity": write_quantity,
        })

    if not updates:
        # ★ NOTHING TO APPLY IS NOT A FAILURE (operator, 2026-10-13).
        # This used to raise, which failed the whole perform-sync route
        # closed -- so the new-listing preview was never built and NOTHING
        # published. It fired 10 times; on 2026-10-12 it left 882 newly
        # imported cards unlisted for about six hours. The comment in
        # extract_reconciliation_candidates records the same chain crash
        # from 2026-09-09, patched at one source rather than here.
        #
        # Every row is already judged independently with its own
        # exclusion reason -- this function's own docstring says "one row
        # going stale does not block its siblings". An empty result is
        # that principle reaching its limit, not a new kind of problem.
        # A genuine failure (Mana Pool erroring on the order read, the
        # seller read or the write) still propagates from the callers
        # above and still records a failed attempt, so the v2.41.0 alarm
        # is unaffected.
        reasons = {}
        for row in excluded:
            key = str(row.get("exclusion_reason") or "unknown")
            reasons[key] = reasons.get(key, 0) + 1
        logger.info(
            "reconciliation: nothing to apply -- %s eligible row(s) reviewed, "
            "all %s skipped (%s). The chain continues; no Mana Pool write was "
            "made.",
            len(eligible_rows), len(excluded),
            "; ".join(f"{count} x {reason}" for reason, count in sorted(reasons.items())),
        )
        return {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "outcome": NOTHING_TO_APPLY,
            # ★ (c) THE MESSAGE. The old one said "None of the reviewed
            # rows are still valid", which reads as a mass invalidation --
            # it cost real diagnosis time when the truth was "the single
            # row was skipped". This says how many there were, how many
            # were skipped, and why.
            "message": (
                f"Nothing to reconcile: {len(eligible_rows)} row(s) were "
                f"reviewed and all {len(excluded)} were skipped "
                + "("
                + "; ".join(
                    f"{count} x {reason}" for reason, count in sorted(reasons.items())
                )
                + "). No Mana Pool write was made."
            ),
            "reviewed": len(eligible_rows),
            "skipped": len(excluded),
            "skipped_reasons": reasons,
            "updates": [],
            "responses": [],
            "excluded": excluded,
            "binding_outcomes": binding_outcomes,
            "price_writes": [],
            "remote_skipped": [],
        }

    session.flush()
    try:
        responses = product_writer(updates)
    except httpx.TimeoutException as exc:
        # ★ A CLIENT TIMEOUT IS NOT A SERVER FAILURE, and since this write
        # now carries prices as well as quantities, both are uncertain
        # until something looks. So: read the affected products back once,
        # report per product whether the write landed, and NEVER retry --
        # a blind retry on a write that did land would re-apply it against
        # a quantity Mana Pool has already moved.
        readback = _read_back_after_timeout(updates, seller_loader, exc)
        if readback["all_confirmed"]:
            logger.warning(
                "reconciliation: the inventory write timed out (%s: %s) but a "
                "read-back confirms all %s product(s) hold exactly what was "
                "sent. Treating the write as applied; nothing was retried.",
                type(exc).__name__, exc, len(updates),
            )
            return {
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "outcome": APPLIED,
                "updates": updates,
                "responses": [],
                "excluded": excluded,
                "binding_outcomes": binding_outcomes,
                "price_writes": price_writes,
                # Never seen: the response we would have read it from
                # never arrived. Present so every outcome of this
                # function has the same shape.
                "remote_skipped": [],
                "write_timeout_readback": readback,
            }
        logger.error(
            "reconciliation: the inventory write timed out (%s: %s) and a "
            "read-back does NOT match for %s of %s product(s). Nothing was "
            "retried. Mismatching products: %s",
            type(exc).__name__, exc, len(readback["mismatched"]), len(updates),
            ", ".join(str(row.get("product_id")) for row in readback["mismatched"]),
        )
        return {
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "outcome": WRITE_UNCONFIRMED,
            "message": (
                f"The Mana Pool inventory write timed out. A read-back confirms "
                f"{len(readback['confirmed'])} of {len(updates)} product(s); "
                f"{len(readback['mismatched'])} do not match what was sent and "
                f"{len(readback['unreadable'])} could not be read. Nothing was "
                "retried -- check these products on Mana Pool before the next run."
            ),
            "updates": updates,
            "responses": [],
            "excluded": excluded,
            "binding_outcomes": binding_outcomes,
            "price_writes": price_writes,
            "remote_skipped": [],
            "write_timeout_readback": readback,
        }

    # Mana Pool answers a bulk inventory write with {inventory, skipped},
    # one response per 2,000-item chunk -- a LIST, not a dict. A skipped
    # item is one the write did NOT apply, which for a restock means the
    # stock is not on sale and the price we sent never landed. Reported
    # rather than discarded: a write that half-happened and said nothing
    # is the shape of failure this whole change exists to remove.
    skipped = [
        item
        for chunk in (responses if isinstance(responses, list) else [responses])
        for item in ((chunk or {}).get("skipped") or [])
    ]
    if skipped:
        logger.warning(
            "reconciliation: Mana Pool SKIPPED %s of %s inventory update(s) -- "
            "that stock is not on sale and any price sent with it did not land: %s",
            len(skipped), len(updates),
            "; ".join(
                f"{item.get('product_id')} ({item.get('reason')})" for item in skipped
            ),
        )

    return {
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "outcome": APPLIED,
        "updates": updates,
        "responses": responses,
        "excluded": excluded,
        "binding_outcomes": binding_outcomes,
        "price_writes": price_writes,
        "remote_skipped": skipped,
    }
