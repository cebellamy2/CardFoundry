"""Convert AVAILABLE NM cards to LP, in chunks, with the listings moved.

★ THIS IS THE OPERATION THAT CAUSED AN INCIDENT. On 2026-09-07 a bulk
condition backfill (database._correct_condition_id_mapping) moved 2,966
cards' condition_id and told Mana Pool nothing. ~1,900 listings were left
live at identities with no cards behind them, and SIX REAL ORDERS arrived
against them. identity_change_service exists because of that, and this
script goes through it rather than around it.

WHAT IT DOES, PER CARD:
  1. read the bindings backing the card BEFORE touching it
     (bindings_to_retire -- after the change, a lookup would find the NEW
     binding and push the wrong listing)
  2. change condition_id/condition locally
  3. recompute and push the OLD listings' quantities in BULK
     (push_bindings_strict), which is a recompute from local state and
     therefore NEVER a blanket zero -- a listing shared with cards that
     stay NM keeps exactly the quantity those cards justify
  4. read every pushed quantity back, and treat a disagreement as failure
  5. commit the chunk only if 3 and 4 both succeeded

★ SCOPE IS AVAILABLE ONLY (operator, 2026-10-11). Never reserved, never
sold, never removed, never unsellable: a reserved card is promised to a
buyer at the identity they bought, and a sold card's record is what the
sale actually was. Rewriting either rewrites a fact.

★ IT REFUSES, IT DOES NOT SKIP. A card that has gained an allocation or
an exception since the dry run stops its chunk, so the counts the
operator approved stay the counts that execute.

★ THE LP LISTINGS ARE PUBLISHED BY THE REAL PUBLISH PATH, not here.
identity_change_service deliberately does not list the new identity --
publishing carries a pricing decision it has no business making. The
operator asked for a forced sync so the LP listings do not wait for the
next scheduled Perform Sync, so each chunk hands off to the canonical
new-listing path afterwards. The DRY RUN of that half goes through
new_listing_dry_run.plan_new_listing_run, which runs the REAL
run_additive_mtgjson_backfill inside a rolled-back transaction -- a dry
run that skips it cannot predict the real run, and one that did
over-published 59 listings instead of 8 on 2026-09-29.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from actor_context import script_actor
from identity_change_service import clear_listing_status, identity_snapshot
from import_service import normalized_condition_id
from manapool_quantity_push_service import (
    QuantityPushFailed, push_bindings_strict, verify_pushed_quantities,
)
from models import (
    Batch, FulfillmentException, InventoryCard, InventoryChangeLog,
    PickAllocation, RemoteProductBinding,
)

logger = logging.getLogger("cardfoundry")

FROM_CONDITION = "NM"
TO_CONDITION = "LP"
ELIGIBLE_STATUS = "available"
CONVERSION_ACTION = "condition_bulk_converted"
ACTOR = script_actor("nm_to_lp_conversion")
# Small enough that one chunk's write transaction is short, large enough
# that 5,230 cards is a few dozen chunks rather than hundreds. The push
# itself is bulked, so a chunk is ~2 Mana Pool calls whatever its size.
DEFAULT_CHUNK_SIZE = 250


class ConversionRefused(RuntimeError):
    def __init__(self, message, *, card_ids=None):
        super().__init__(message)
        self.card_ids = card_ids or []


class ConversionFailed(RuntimeError):
    def __init__(self, message, *, failed_bindings=None, chunk=None):
        super().__init__(message)
        self.failed_bindings = failed_bindings or []
        self.chunk = chunk


def eligible_cards(session: Session) -> list:
    """Available NM cards, in a stable order so chunks are reproducible.

    An archived batch is excluded: its cards contribute nothing to any
    listing, so converting them would be a local-only relabel with no
    remote meaning, and it would inflate every count the operator reads.
    """
    return (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.status == ELIGIBLE_STATUS,
            func.upper(InventoryCard.condition_id) == FROM_CONDITION,
            Batch.is_archived == False,  # noqa: E712 -- SQLAlchemy needs ==
        )
        .order_by(InventoryCard.id)
        .all()
    )


def refusals_for(session: Session, cards: list) -> list:
    """Why this set must not be converted. Empty means proceed."""
    found = []
    ids = [card.id for card in cards]
    if not ids:
        return found

    wrong_status = [c.id for c in cards if c.status != ELIGIBLE_STATUS]
    if wrong_status:
        found.append({
            "code": "not_available",
            "summary": f"{len(wrong_status)} card(s) are no longer available.",
            "card_ids": wrong_status,
        })
    wrong_condition = [
        c.id for c in cards
        if str(c.condition_id or "").upper() != FROM_CONDITION
    ]
    if wrong_condition:
        found.append({
            "code": "not_nm",
            "summary": f"{len(wrong_condition)} card(s) are no longer NM.",
            "card_ids": wrong_condition,
        })

    allocated = [
        row[0] for row in session.query(PickAllocation.inventory_card_id)
        .filter(PickAllocation.inventory_card_id.in_(ids)).distinct().all()
    ]
    if allocated:
        found.append({
            "code": "has_allocation",
            "summary": (
                f"{len(allocated)} card(s) have gained a pick allocation. "
                "A card promised to an order must keep the identity it was "
                "bought at."
            ),
            "card_ids": allocated,
        })

    excepted = [
        row[0] for row in session.query(FulfillmentException.inventory_card_id)
        .filter(FulfillmentException.inventory_card_id.in_(ids)).distinct().all()
    ]
    if excepted:
        found.append({
            "code": "has_exception",
            "summary": f"{len(excepted)} card(s) have a fulfillment exception.",
            "card_ids": excepted,
        })
    return found


def identity_key(card) -> tuple:
    """The four-key identity a Mana Pool listing is made of, minus the
    condition that is about to change."""
    return (
        str(card.mtgjson_id or "").upper(),
        str(card.language_id or "").upper(),
        str(card.finish_id or "").upper(),
    )


def chunks(cards: list, size: int = DEFAULT_CHUNK_SIZE) -> list:
    """Group by identity first, then pack whole groups into chunks.

    ★ AN IDENTITY MUST NEVER SPAN TWO CHUNKS. The first dry run chunked
    by a flat slice, and 83 NM listings came back NON-ZERO -- not because
    anything shared them, but because chunk N still saw chunk N+1's cards
    as available NM. Every chunk was individually correct and the set of
    them was misleading: the preview's numbers did not match what a
    cumulative apply would produce, which is the one thing a preview must
    never do. Keeping an identity whole makes each chunk's recomputed
    quantity FINAL, so preview == apply.

    A single identity larger than `size` gets its own oversized chunk
    rather than being split: correctness beats the size target, and the
    push is bulked so a large chunk is still ~2 Mana Pool calls.
    """
    groups = {}
    for card in cards:
        groups.setdefault(identity_key(card), []).append(card)
    packed, current = [], []
    for key in sorted(groups):
        group = groups[key]
        if current and len(current) + len(group) > size:
            packed.append(current)
            current = []
        current.extend(group)
    if current:
        packed.append(current)
    return packed


def binding_map(session: Session, cards: list) -> dict:
    """card_id -> [bindings backing it], built in ONE pass.

    ★ WHY THIS EXISTS. bindings_backing_card scans every binding and
    JSON-parses local_card_ids_json for EACH card it is handed. With
    8,362 bindings in production that is ~2.1 million parses per 250-card
    chunk, and it made the first dry run take 20.4 minutes. This walks
    the bindings once, indexes membership as it goes, and resolves
    identity matches with one query per distinct identity rather than per
    card.

    Same two sources bindings_backing_card uses -- explicit membership
    and identity match -- because the two can disagree, and a card listed
    under one and attached to the other would otherwise have half its
    listing left standing.
    """
    from manapool_quantity_push_service import _resolve_binding_for_card

    by_card = {card.id: [] for card in cards}
    wanted = set(by_card)
    seen = {card.id: set() for card in cards}

    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
    ).all():
        try:
            held = json.loads(binding.local_card_ids_json or "[]")
        except (TypeError, ValueError):
            logger.warning(
                "nm->lp: binding %s has unreadable local_card_ids_json; "
                "treating it as holding no cards.", binding.id,
            )
            continue
        for card_id in wanted.intersection(held):
            if binding.id not in seen[card_id]:
                by_card[card_id].append(binding)
                seen[card_id].add(binding.id)

    # One identity lookup per distinct identity, not per card.
    by_identity = {}
    for card in cards:
        key = identity_key(card) + (str(card.condition_id or "").upper(),)
        if key not in by_identity:
            by_identity[key] = _resolve_binding_for_card(session, card)
        binding = by_identity[key]
        if binding is not None and binding.id not in seen[card.id]:
            by_card[card.id].append(binding)
            seen[card.id].add(binding.id)
    return by_card


def _convert_one(session: Session, card: InventoryCard, bindings: list) -> dict:
    """Change one card's condition locally, given its old bindings.

    `bindings` must have been resolved BEFORE the card changed -- after
    the change an identity lookup finds the NEW binding, and pushing that
    would write the new listing's quantity while leaving the old listing
    exactly as it was. That orphan is what identity_change_service exists
    to prevent.

    Does NOT push: the caller pushes every chunk's bindings together, so
    the chunk costs ~2 Mana Pool calls rather than two per card.
    """
    before = identity_snapshot(card)
    card.condition_id = TO_CONDITION
    # `condition` is the human label shown on pick lists and packing
    # slips. Derived from the code via the app's own mapping rather than
    # hardcoded, so the two cannot disagree.
    card.condition = _label_for(TO_CONDITION) or card.condition
    clear_listing_status(session, card.id)
    session.add(InventoryChangeLog(
        actor=ACTOR,
        inventory_card_id=card.id,
        change_summary=json.dumps({
            "action_type": CONVERSION_ACTION,
            "before": before,
            "after": identity_snapshot(card),
            "old_binding_ids": [b.id for b in bindings],
            "old_product_ids": [b.product_id for b in bindings],
            "reason": (
                "Shop-wide condition policy: cards graded NM on intake are "
                "relabelled LP so Mana Pool lists them as LP."
            ),
            "at": datetime.now(timezone.utc).isoformat(),
        }, sort_keys=True),
    ))
    return {"card_id": card.id, "bindings": bindings}


def _label_for(code: str) -> str:
    """The human label for a condition code, from the app's own INTAKE
    vocabulary.

    ★ NOT CONDITION_LABELS. That map is for display and says "Lightly
    Played" for LP -- which normalized_condition_id reads back as
    LIGHTLY_PLAYED, not LP. Writing it into card.condition would put a
    string on the card that this app's own importer would misread. The
    add-card dropdown's "Light Play" does round-trip, so that is the one
    used, and the round-trip is asserted rather than assumed.

    The list comes from intake_defaults_service now; it used to be
    imported from main INSIDE this function purely to dodge the import
    cycle, which is not a reason for a word list to live in the web app.
    """
    from intake_defaults_service import (
        INTAKE_CONDITION_LABELS, condition_label_for,
    )

    label = condition_label_for(code, INTAKE_CONDITION_LABELS)
    if label and normalized_condition_id(label) == code:
        return label
    logger.warning(
        "nm->lp: no intake label round-trips to condition %r; leaving the "
        "card's label unchanged rather than writing one this app would "
        "misread.", code,
    )
    return ""


def convert_chunk(
    session: Session, cards: list, *, apply: bool,
    pusher=None, reader=None, listed_product_ids=None,
    live_inventory_reader=None,
) -> dict:
    """One chunk, all-or-nothing. Caller commits when apply is True."""
    blocking = refusals_for(session, cards)
    if blocking:
        raise ConversionRefused(
            "; ".join(r["summary"] for r in blocking),
            card_ids=sorted({cid for r in blocking for cid in r["card_ids"]}),
        )

    savepoint = session.begin_nested()
    try:
        # Resolved BEFORE any card changes, in one pass.
        backing = binding_map(session, cards)
        bindings = {}
        for card in cards:
            for binding in backing.get(card.id, []):
                bindings[binding.id] = binding
            _convert_one(session, card, backing.get(card.id, []))
        session.flush()
        # Recompute AFTER the flush, so the converted cards are already
        # excluded. Never a blanket zero.
        deferred = []
        quantities = push_bindings_strict(
            session, list(bindings.values()), pusher=pusher, deferred=deferred,
            listed_product_ids=listed_product_ids,
        )
        # Only PUSHED bindings are verified: a binding with no live listing
        # was never written, so there is nothing to read back.
        pushed = [b for b in bindings.values() if b.id in quantities]
        observed = verify_pushed_quantities(quantities, pushed, reader=reader)
    except QuantityPushFailed as exc:
        savepoint.rollback()
        logger.warning(
            "nm->lp: chunk of %s card(s) ROLLED BACK; nothing was converted and "
            "no listing is left without its cards: %s", len(cards), exc,
        )
        raise ConversionFailed(
            str(exc),
            failed_bindings=[
                {"binding_id": b.id, "product_id": b.product_id}
                for b in bindings.values()
            ],
            chunk=[c.id for c in cards],
        ) from exc
    except Exception as exc:  # noqa: BLE001 -- the rollback must cover everything
        savepoint.rollback()
        logger.warning(
            "nm->lp: chunk of %s card(s) ROLLED BACK by an unexpected error: "
            "%s: %s. Nothing was written.", len(cards), type(exc).__name__, exc,
        )
        raise

    result = {
        "cards": len(cards),
        "card_ids": [c.id for c in cards],
        "old_bindings": len(bindings),
        "deferred_to_publish": deferred,
        "quantities_written": {b.product_id: quantities.get(b.id)
                               for b in bindings.values()},
        "read_back_ok": all(
            quantities.get(bid) == observed.get(bid) for bid in quantities
        ),
        "applied": bool(apply),
    }
    if apply:
        savepoint.commit()
        logger.info(
            "nm->lp: chunk APPLIED -- %s card(s) NM->LP, %s old listing(s) "
            "requantified and verified", len(cards), len(bindings),
        )
    else:
        savepoint.rollback()
        logger.info(
            "nm->lp: chunk DRY RUN -- %s card(s) would go NM->LP, %s old "
            "listing(s) would be requantified. Rolled back; nothing written.",
            len(cards), len(bindings),
        )
    return result


# --- the forced sync ----------------------------------------------------
#
# ★ WHY THE PUBLISH PLAN RUNS ON THE CALLER'S SESSION. Until the cards are
# LP there are no LP identities to publish, so a publish plan computed
# after a rolled-back conversion sees the world unchanged and reports
# nothing. That is precisely the "two views of the world disagreed"
# failure that published 59 listings instead of the approved 8 on
# 2026-09-29. So this replicates plan_new_listing_run's steps on the
# session that already holds the conversion, uncommitted.
#
# ★ AND IT RUNS THE REAL BACKFILL. Perform Sync's FIRST step is
# run_additive_mtgjson_backfill, and a card with a NULL mtgjson_id forms
# no local group and is INVISIBLE to a mirror preview. A dry run that
# skips it cannot predict the real run -- that is the 59-listing incident
# in one sentence.

PUBLISH_DRY_RUN_NOTE = "NM->LP DRY RUN -- rolled back, never committed"


def plan_publish_on_session(
    session: Session, *, seller_loader, catalog_product_loader,
    catalog_scryfall_loader, optimizer_call, listings_call, seller_id,
    manual_overrides=(),
) -> dict:
    """What the forced sync would publish, computed on THIS session.

    Writes nothing and commits nothing: every local row it touches is the
    caller's to roll back, and every Mana Pool call it makes is a read.
    """
    from inventory_sync_workflow import _build_mirror_preview_from_snapshot
    from mtgjson_backfill_service import run_additive_mtgjson_backfill
    from new_listing_upload_service import (
        approved_set_from_preview, build_new_listing_preview,
    )

    # One seller read, shared by the backfill and the mirror, so the two
    # see the same remote snapshot -- and so this costs one call, not two.
    remote_inventory = seller_loader(min_quantity=0)

    def shared_seller_loader(min_quantity=0):
        return remote_inventory

    backfill = run_additive_mtgjson_backfill(
        session, shared_seller_loader, catalog_product_loader,
        operator_note=PUBLISH_DRY_RUN_NOTE,
    )
    session.flush()

    cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
    mirror = _build_mirror_preview_from_snapshot(
        session, cards, remote_inventory, False,
    )
    preview = build_new_listing_preview(
        session, mirror, optimizer_call, listings_call, seller_id,
        catalog_scryfall_loader,
        market_catalog_product_call=catalog_product_loader,
        manual_overrides=manual_overrides,
    )
    summary = {}
    for row in mirror.get("rows") or []:
        category = row.get("category")
        summary[category] = summary.get(category, 0) + 1
    logger.info(
        "nm->lp forced sync DRY RUN: backfill would set %s card(s); %s "
        "candidate(s) would publish.",
        backfill.get("updated_inventory_cards"),
        len(approved_set_from_preview(preview)),
    )
    return {
        "preview": preview,
        "approved_set": approved_set_from_preview(preview),
        "mirror_summary": summary,
        "backfill": {
            "updated_inventory_cards": backfill.get("updated_inventory_cards"),
            "updated_bindings": backfill.get("updated_bindings"),
            "skipped": len(backfill.get("skipped") or []),
        },
    }


# --- one chunk, end to end ----------------------------------------------

class BackfillScopeExceeded(RuntimeError):
    """The MTGJSON backfill wanted to change something.

    ★ WHY THAT STOPS THE RUN. plan_publish_on_session runs the REAL
    run_additive_mtgjson_backfill, because a publish plan that skips it
    cannot predict the real run (the 59-listing over-publish of
    2026-09-29). But it mutates the session we are about to COMMIT, and
    it works on the WHOLE inventory, not this chunk. The operator
    approved a condition conversion, not an identity backfill, so if the
    backfill wants to write anything the run stops and says so rather
    than quietly committing a second, unapproved change. Dry run v2
    measured updated_inventory_cards = 0, so in practice this is a
    no-op -- which is exactly why a surprise here matters.
    """


def run_chunk(
    session: Session, cards: list, *, apply: bool,
    pusher=None, reader=None, publish=None, publish_verifier=None,
) -> dict:
    """Convert a chunk and publish its LP listings, all or nothing.

    Order, matching the operator's requirement: convert -> NM listings
    recompute (never a blanket zero) -> read back -> publish this chunk's
    LP identities through the canonical path -> read back -> commit.

    `publish` is called with the session once the chunk's cards are LP and
    flushed; it returns {"published": n, "approved": n, "backfill": {...}}.
    Any exception rolls the whole chunk back, including the conversion.
    """
    outer = session.begin_nested()
    stage = "conversion"
    try:
        conversion = convert_chunk(
            session, cards, apply=True, pusher=pusher, reader=reader,
        )
        stage = "publish"
        published = {}
        if publish is not None:
            published = publish(session, cards) or {}
            backfill = published.get("backfill") or {}
            touched = (
                int(backfill.get("updated_inventory_cards") or 0)
                + int(backfill.get("updated_bindings") or 0)
            )
            if touched:
                raise BackfillScopeExceeded(
                    f"The MTGJSON backfill wanted to change {touched} row(s) "
                    "while publishing this chunk. That is outside the approved "
                    "conversion, so the chunk was rolled back."
                )
            if publish_verifier is not None:
                published["verified"] = publish_verifier(session, published)
    except Exception as exc:  # noqa: BLE001 -- the rollback must cover everything
        outer.rollback()
        logger.warning(
            "nm->lp: chunk of %s card(s) ROLLED BACK at the %s stage: %s: %s. "
            "Nothing in this chunk was converted or published.",
            len(cards), stage, type(exc).__name__, exc,
        )
        raise

    result = {**conversion, "publish": published}
    if apply:
        outer.commit()
        logger.info(
            "nm->lp: chunk COMMITTED -- %s card(s) NM->LP, %s old listing(s) "
            "requantified, %s LP listing(s) published.",
            len(cards), conversion["old_bindings"],
            published.get("published", 0),
        )
    else:
        outer.rollback()
        logger.info(
            "nm->lp: chunk DRY RUN complete -- rolled back, nothing written.",
        )
    return result
