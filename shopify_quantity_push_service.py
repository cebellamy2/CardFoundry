"""Shopify quantity push: the mirror of manapool_quantity_push_service.

★ WHY THIS EXISTS, in the operator's own terms: NO OVERSELLING. A card
that sells on Mana Pool has to stop being for sale on Shopify within
minutes, not at the next manual sync. The catalogue drifted 6,199 ->
6,197 in a single afternoon of trading while Shopify went on advertising
both, and that gap is the whole shape of the Orcish Bowmasters incident
that the Mana Pool push was built to close -- a real order arriving for
a card sold two weeks earlier.

★ IT IS DELIBERATELY THE SAME SHAPE AS THE MANA POOL PUSH, and differs
in exactly three places, each for a reason:

  1. QUANTITY IS RECOMPUTED AT PUSH TIME from local state, never cached
     and never derived from the number being replaced -- same as Mana
     Pool. What counts is "the cards a publish would publish", through
     shopify_mapping_service.publishable, so the quantity on a product
     and the existence of that product can never be decided by two
     different rules.
  2. IT READS BEFORE IT WRITES. Mana Pool's push deliberately does not:
     a lower number is self-correcting there. Here the write is
     `inventorySetQuantities`, whose `changeFromQuantity` is
     "mandatory: you must explicitly pass in a value", and passing the
     value just read turns the write into a compare-and-swap. A
     concurrent change then FAILS loudly (CHANGE_FROM_QUANTITY_STALE)
     instead of being silently overwritten. For a no-overselling
     requirement that is worth one read.
  3. IT IS OFF BY DEFAULT. SHOPIFY_QTY_SYNC_ENABLED ships false, so
     wiring it into every sale path changes nothing until the operator
     turns it on.

★ AND IT CAN NEVER HURT MANA POOL. The hook lives at the END of
manapool_quantity_push_service.push_for_cards, after that function's own
work is done, wrapped so that nothing it raises escapes. Mana Pool's
behaviour is unchanged whether Shopify is off, broken, or unreachable.

★ WHAT THIS DOES NOT DO, AND WHY IT MATTERS BEFORE ANYTHING GOES ACTIVE.
CardFoundry has no Shopify ORDER ingestion. So a sale on Shopify is
invisible here, and this push -- which writes the local count -- would
raise the quantity back up and advertise a card that just sold. Today
that is harmless because every product is DRAFT and nothing on Shopify
can be bought. It stops being harmless the moment anything goes ACTIVE.
The compare-and-swap is what would catch it (the write would go stale
rather than clobber), but catching it is not the same as handling it.
See the ticket 5 report.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

import scheduled_job_runs
import shopify_publish_service
import shopify_service
from actor_context import current_actor
from inventory_mirror_service import ACTIVE_ALLOCATION_STATUSES, SELLABLE_STATUS
from models import (
    AppSetting, AppSettingChangeLog, Batch, InventoryCard, PickAllocation,
    ShopifyBinding,
)
from shopify_binding_service import BINDING_STATUS_RETIRED
from shopify_mapping_service import publishable, variant_key, variant_sku

logger = logging.getLogger("cardfoundry")

SETTING_KEY = "shopify_qty_sync_enabled"
SETTING_CHANGE_ACTION = "shopify_qty_sync_setting"

# ★ SHIPPED OFF. The wiring is live in every sale path from the moment
# this release deploys; this flag is what makes that a no-op. Turning it
# on is the operator's decision and it is a one-row change with an audit
# row, not a release.
SETTING_DEFAULT = False

# How many variants go in one inventorySetQuantities call. The reference
# documents no maximum, so this is chosen rather than derived: small
# enough that one failure explains itself and one idempotency key covers
# a reviewable set of numbers, large enough that a 6,800-variant
# catch-up is ~28 calls instead of 6,800.
QUANTITIES_PER_CALL = 250

# A compare-and-swap that went stale is re-read ONCE and re-attempted
# with the fresh value. Never more: a second staleness means something
# else is writing these quantities continuously, and that is a thing to
# report, not to race.
STALE_RETRIES = 1

REFERENCE_DOCUMENT_PREFIX = "gid://cardfoundry/ShopifyQuantityPush/"

# Above this many bindings, the desired quantities are computed in ONE
# pass over the inventory instead of one scoped query per binding. The
# sale path hands over one to a handful of bindings and wants the cheap
# scoped query inside a web request; a reconciliation hands over 6,800
# and would otherwise run ~20,000 queries. Both go through the same
# counting rule -- see _count_publishable.
ONE_PASS_THRESHOLD = 25


# --- the setting ----------------------------------------------------------

def sync_enabled(session: Session) -> bool:
    """Whether a local stock change should push to Shopify. Default OFF.

    Reads as false for anything that is not an explicit truthy string, so
    a corrupted value fails CLOSED -- the failure mode of "we did not
    push" is visible drift that reconciliation reports, while the reverse
    would be writes nobody asked for.
    """
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == SETTING_KEY).first())
    stored = str((setting.value if setting else "") or "").strip().lower()
    if not stored:
        return SETTING_DEFAULT
    if stored in ("1", "true", "yes", "on"):
        return True
    if stored in ("0", "false", "no", "off"):
        return False
    logger.warning(
        "%s: stored value %r is not a boolean; treating Shopify quantity "
        "sync as OFF.", SETTING_KEY, stored,
    )
    return False


def set_sync_enabled(session: Session, enabled: bool) -> dict:
    """Turn the Shopify quantity sync on or off, audited. Caller commits."""
    before = sync_enabled(session)
    value = "true" if enabled else "false"
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == SETTING_KEY).first())
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=SETTING_KEY, value=value,
                               updated_at=datetime.now()))
    if before != bool(enabled):
        session.add(AppSettingChangeLog(
            key=SETTING_KEY,
            actor=current_actor(),
            changed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            change_summary=json.dumps({
                "action_type": SETTING_CHANGE_ACTION,
                "setting": SETTING_KEY,
                "before": before, "after": bool(enabled),
            }, sort_keys=True),
        ))
        logger.warning(
            "Shopify quantity sync turned %s (was %s).",
            "ON" if enabled else "OFF", "ON" if before else "OFF",
        )
    return {"before": before, "after": bool(enabled),
            "changed": before != bool(enabled)}


# --- what the quantity should be -----------------------------------------

def _binding_key(binding: ShopifyBinding) -> tuple:
    """A binding's identity in the same shape variant_key gives a card."""
    return (binding.set_code.upper(), binding.collector_number.upper(),
            binding.language_id.upper(), binding.condition_id.upper(),
            binding.finish_id.upper())


def _count_publishable(session: Session, cards) -> dict[tuple, int]:
    """Group candidate cards by variant identity and count the ones a
    publish would publish. THE counting rule, used by both scopes.

    ★ THE SAME RULE AS THE PUBLISH, through the same function. Everything
    beyond "status is available" -- an unresolved inventory exception, an
    archived batch, a price hold, an active allocation holding a card
    that still reads available -- is decided by
    shopify_mapping_service.publishable. A card that would not be
    published does not count toward what Shopify advertises, or the
    quantity on a product and the existence of that product would be
    decided by two different rules and drift apart.
    """
    if not cards:
        return {}
    batch_ids = sorted({card.batch_id for card in cards})
    batches_by_id = {
        batch.id: batch for batch in
        session.query(Batch).filter(Batch.id.in_(batch_ids)).all()
    }
    card_ids = [card.id for card in cards]
    allocations = []
    for start in range(0, len(card_ids), 500):
        allocations.extend(
            session.query(PickAllocation)
            .filter(PickAllocation.inventory_card_id
                    .in_(card_ids[start:start + 500]),
                    PickAllocation.status.in_(ACTIVE_ALLOCATION_STATUSES))
            .all()
        )
    counts: dict[tuple, int] = {}
    for card in publishable(cards, batches_by_id, allocations):
        key = variant_key(card)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def desired_quantity(session: Session, binding: ShopifyBinding) -> int:
    """Fresh count for ONE binding, scoped to its own identity.

    Recomputed at push time, never cached -- so a card that changed
    between the binding being resolved and the write being built is
    still reflected. For the sale path, which asks about a handful of
    identities inside a web request.
    """
    candidates = (
        session.query(InventoryCard)
        .filter(
            InventoryCard.status == SELLABLE_STATUS,
            func.upper(InventoryCard.set_code) == binding.set_code.upper(),
            func.upper(InventoryCard.collector_number)
            == binding.collector_number.upper(),
            func.upper(InventoryCard.language_id) == binding.language_id.upper(),
            func.upper(InventoryCard.condition_id)
            == binding.condition_id.upper(),
            func.upper(InventoryCard.finish_id) == binding.finish_id.upper(),
        )
        .all()
    )
    return _count_publishable(session, candidates).get(_binding_key(binding), 0)


def desired_quantities(session: Session, bindings) -> dict[str, int]:
    """sku -> fresh count, for many bindings, in ONE pass.

    One query for every available card and one per 500 for allocations,
    rather than three queries per binding -- a 6,800-binding
    reconciliation was ~20,000 queries before this existed. The counting
    rule is _count_publishable either way, so the two scopes cannot
    disagree.
    """
    rows = list(bindings)
    if not rows:
        return {}
    if len(rows) <= ONE_PASS_THRESHOLD:
        return {b.sku: desired_quantity(session, b) for b in rows}
    candidates = (session.query(InventoryCard)
                  .filter(InventoryCard.status == SELLABLE_STATUS).all())
    counts = _count_publishable(session, candidates)
    return {b.sku: counts.get(_binding_key(b), 0) for b in rows}


def bindings_for_cards(session: Session, cards) -> list[ShopifyBinding]:
    """Every live Shopify binding covering any of these cards' identities.

    ★ RESOLVED BY COMPUTING THE SKU, not by scanning the bindings table.
    The SKU IS the identity (see ShopifyBinding), and variant_sku is the
    one function that builds it -- so a card's binding is one indexed
    lookup on a value derived locally, and a bulk route's hundreds of
    cards collapse to one `IN` query. The alternative, reading every
    binding and filtering in Python, would pull 6,800 rows into a web
    request on every sale.
    """
    skus = set()
    for card in cards:
        key = variant_key(card)
        if not key:
            continue
        set_code, collector_number, language_id, condition_id, finish_id = key
        skus.add(variant_sku(
            set_code=set_code, collector_number=collector_number,
            language_id=language_id, condition_id=condition_id,
            finish_id=finish_id,
        ))
    return bindings_for_skus(session, skus)


def bindings_for_skus(session: Session, skus) -> list[ShopifyBinding]:
    """Every live binding for these SKUs, one indexed query per 500."""
    skus = {str(sku) for sku in (skus or []) if str(sku or "").strip()}
    if not skus:
        return []
    found = []
    ordered = sorted(skus)
    for start in range(0, len(ordered), 500):
        found.extend(
            session.query(ShopifyBinding)
            .filter(ShopifyBinding.sku.in_(ordered[start:start + 500]),
                    ShopifyBinding.binding_status != BINDING_STATUS_RETIRED)
            .all()
        )
    return found


def live_bindings(session: Session) -> list[ShopifyBinding]:
    """Every binding this app still claims, ordered so a run is stable."""
    return (session.query(ShopifyBinding)
            .filter(ShopifyBinding.binding_status != BINDING_STATUS_RETIRED)
            .order_by(ShopifyBinding.sku).all())


# --- the write ------------------------------------------------------------

def _idempotency_key(run_id: str, rows) -> str:
    """A fresh UUID per distinct set of numbers.

    Shopify "tracks idempotency keys for 24 hours" and warns that
    "changing any value--even a single quantity or ID--requires a new
    idempotency key". A v5 UUID over the exact payload gives both
    halves: the same numbers retried reuse the key (so a retry after an
    uncertain timeout cannot double-apply), and any different number
    produces a different key.
    """
    material = run_id + "|" + json.dumps(
        [[row["inventoryItemId"], row["locationId"], row["quantity"],
          row["changeFromQuantity"]] for row in rows], sort_keys=True)
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          REFERENCE_DOCUMENT_PREFIX + material))


def push_bindings(session: Session, bindings, *, confirm: bool = False,
                  run_id: str | None = None, client=None,
                  location_gid: str | None = None) -> dict:
    """Set Shopify's quantity for these bindings from local state.

    Dry by default, and the dry run takes the same path: it reads the
    same current quantities, computes the same desired ones and builds
    the same payload, then stops at shopify_service.authorize_writes.
    Never commits -- the caller owns the transaction.
    """
    run = run_id or f"shopify-qty-{datetime.now():%Y%m%dT%H%M%S}"
    report = {
        "run_id": run,
        "confirmed": bool(confirm),
        "bindings": len(bindings),
        "unbound": [],
        "absent_from_shopify": [],
        "already_correct": [],
        "would_change": [],
        "changed": [],
        "errors": [],
        "stale_after_retry": [],
        "calls": 0,
        "stopped_because": None,
    }
    if not bindings:
        report["stopped_because"] = "no binding to push"
        return report
    try:
        location = location_gid or shopify_service.location_gid()
    except shopify_service.ShopifyError as exc:
        report["stopped_because"] = str(exc)
        return report

    by_sku = {}
    for binding in bindings:
        if not binding.shopify_inventory_item_gid:
            # Nothing to write to. Reported, never skipped silently --
            # this is a binding whose publish never came back with an
            # inventory item, and it needs a republish, not a retry.
            report["unbound"].append(binding.sku)
            continue
        by_sku[binding.sku] = binding
    if not by_sku:
        report["stopped_because"] = (
            "no binding carries a Shopify inventory item id")
        return report

    current = shopify_publish_service.read_variant_stock(
        sorted(by_sku), location_gid=location, client=client)
    wanted_by_sku = desired_quantities(session, list(by_sku.values()))
    pending = []
    for sku, binding in sorted(by_sku.items()):
        live = current.get(sku)
        if live is None or live.get("available_at_location") is None:
            report["absent_from_shopify"].append(sku)
            continue
        wanted = wanted_by_sku.get(sku, 0)
        have = int(live["available_at_location"])
        if have == wanted:
            report["already_correct"].append(sku)
            continue
        pending.append({
            "sku": sku,
            "inventoryItemId": binding.shopify_inventory_item_gid,
            "locationId": location,
            "quantity": wanted,
            "changeFromQuantity": have,
        })
        report["would_change"].append(
            {"sku": sku, "shopify_has": have, "local_says": wanted})

    if not pending:
        # ★ ONE LINE PER PUSH, not per card. Without it an ordinary sale
        # leaves no trace that the hook fired at all, and the operator's
        # only evidence is a reconciliation hours later. Deliberately not
        # a line per variant: this path runs on every sale.
        logger.info(
            "Shopify quantity push %s: %s bound variant(s) already match "
            "local; nothing to write.", run, len(report["already_correct"]),
        )
        report["stopped_because"] = "every bound variant already matches local"
        return report

    try:
        authorization = shopify_service.authorize_writes(
            confirm=bool(confirm), reason=f"shopify quantity push {run}")
    except shopify_service.ShopifyWriteRefused as exc:
        report["stopped_because"] = str(exc)
        return report

    for start in range(0, len(pending), QUANTITIES_PER_CALL):
        chunk = pending[start:start + QUANTITIES_PER_CALL]
        report["calls"] += 1
        outcome = _set_chunk(chunk, authorization=authorization, run_id=run,
                             location=location, client=client, report=report)
        report["changed"].extend(outcome["changed"])
    logger.warning(
        "Shopify quantity push %s: %s binding(s), %s changed, %s already "
        "correct, %s absent, %s unbound, %s error(s), %s still stale.",
        run, len(bindings), len(report["changed"]),
        len(report["already_correct"]), len(report["absent_from_shopify"]),
        len(report["unbound"]), len(report["errors"]),
        len(report["stale_after_retry"]),
    )
    return report


def _set_chunk(chunk, *, authorization, run_id, location, client, report
               ) -> dict:
    """One inventorySetQuantities call, with ONE re-read if it went stale.

    ★ A STALE COMPARE IS NOT A FAILURE AND NOT A RETRY. It means someone
    else changed the quantity between our read and our write, so the
    right answer is to read again and set from the fresh value -- which
    is reconciling, not retrying the same numbers blindly. Bounded at
    STALE_RETRIES, because a second staleness means something is writing
    continuously and that is a thing to report.
    """
    attempt = 0
    changed = []
    while True:
        payload = [{k: v for k, v in row.items() if k != "sku"}
                   for row in chunk]
        try:
            result = shopify_service.set_inventory_quantities(
                payload, authorization=authorization,
                reference_document_uri=REFERENCE_DOCUMENT_PREFIX + run_id,
                idempotency_key=_idempotency_key(run_id, payload),
                client=client,
            )
        except shopify_service.ShopifyWriteUncertain as exc:
            # ★ UNCERTAIN, NOT FAILED. The write may have landed, so the
            # only safe move is to read back -- which the caller's
            # reconciliation does, and which this records rather than
            # guessing at.
            logger.error(
                "Shopify quantity push %s: a write timed out on %s item(s); "
                "NOT retried. Read back before anything else. %s",
                run_id, len(chunk), exc,
            )
            report["errors"].append({
                "skus": [row["sku"] for row in chunk],
                "uncertain": True,
                "detail": str(exc),
            })
            return {"changed": changed}
        except shopify_service.ShopifyError as exc:
            logger.warning(
                "Shopify quantity push %s: %s item(s) failed: %s",
                run_id, len(chunk), exc,
            )
            report["errors"].append({
                "skus": [row["sku"] for row in chunk],
                "uncertain": False,
                "detail": str(exc),
            })
            return {"changed": changed}

        if not result["user_errors"]:
            changed.extend(row["sku"] for row in chunk)
            return {"changed": changed}

        stale_paths = {tuple(row.get("field") or []) for row in result["stale"]}
        other = [row for row in result["user_errors"]
                 if row.get("code")
                 != shopify_service.ERROR_CHANGE_FROM_QUANTITY_STALE]
        if other:
            report["errors"].append({
                "skus": [row["sku"] for row in chunk],
                "uncertain": False,
                "detail": "; ".join(
                    f"{row.get('code')}: {row.get('message')}"
                    for row in other)[:500],
            })
        if not result["stale"] or attempt >= STALE_RETRIES:
            if result["stale"]:
                report["stale_after_retry"].extend(
                    row["sku"] for row in chunk)
            return {"changed": changed}

        attempt += 1
        logger.warning(
            "Shopify quantity push %s: %s stale compare(s) on %s item(s); "
            "reading again and setting from the fresh value (attempt %s). "
            "Paths: %s", run_id, len(result["stale"]), len(chunk), attempt,
            sorted(stale_paths),
        )
        fresh = shopify_publish_service.read_variant_stock(
            [row["sku"] for row in chunk], location_gid=location,
            client=client)
        rebuilt = []
        for row in chunk:
            live = fresh.get(row["sku"])
            if live is None or live.get("available_at_location") is None:
                report["absent_from_shopify"].append(row["sku"])
                continue
            rebuilt.append({**row,
                            "changeFromQuantity": int(
                                live["available_at_location"])})
        if not rebuilt:
            return {"changed": changed}
        chunk = rebuilt


# --- the hook every sale path reaches ------------------------------------

def push_for_cards(session: Session, cards) -> dict:
    """Best-effort Shopify quantity push for these cards' identities.

    Called from the END of manapool_quantity_push_service.push_for_cards,
    so every sale and sellability path in the app reaches it without a
    single call site knowing about Shopify. Returns a small report for
    logging; raises nothing the caller has to handle, and does nothing at
    all while SHOPIFY_QTY_SYNC_ENABLED is off.

    Caller commits. Nothing here writes a local row.
    """
    if not sync_enabled(session):
        return {"skipped": "SHOPIFY_QTY_SYNC_ENABLED is off", "pushed": 0}
    bindings = bindings_for_cards(session, cards)
    if not bindings:
        logger.info(
            "Shopify quantity push: %s card(s) resolved to no Shopify "
            "binding; nothing to push.", len(cards),
        )
        return {"skipped": "no Shopify binding for these cards", "pushed": 0}
    report = push_bindings(session, bindings, confirm=True,
                           run_id=f"sale-{datetime.now():%Y%m%dT%H%M%S%f}")
    return {"pushed": len(report["changed"]), "report": report}


# --- the narrow hook, for paths Mana Pool's push does not reach -----------
#
# ★ WHY A SECOND HOOK AT ALL. push_for_cards (above) rides on
# manapool_quantity_push_service.push_for_cards, which fires on the three
# SELLABILITY transitions -- sold, removed, not-for-sale. That covers
# every path Mana Pool needs, because Mana Pool is told about those by us.
# It does NOT cover the commonest real event on this shop: a Mana Pool
# ORDER arriving and reserving a card. Mana Pool never needed telling --
# the sale happened there and its own quantity dropped by itself -- so
# order_service.allocate_order has never called a push, and before this
# existed Shopify went on advertising reserved cards until someone ran a
# reconciliation. Measured 2026-11-03: two identities drifted that way in
# thirty-five minutes.
#
# ★ AND WHY IT ONLY EVER LOWERS. These paths run INSIDE a transaction the
# caller has not committed yet -- order_service, pick_wave_service and
# both fulfillment-exception services all leave the commit to their
# caller. A push that RAISED a quantity from uncommitted state could
# advertise stock that a rollback then took away, which is an oversell:
# the exact thing this whole feature exists to prevent. A push that
# LOWERS cannot: the worst a rollback can leave behind is Shopify
# offering fewer cards than the shop holds, which is a lost sale that the
# next reconciliation corrects. So this hook writes only where the local
# count is BELOW what Shopify advertises, and that single rule is what
# makes it safe to call from anywhere, committed or not.
#
# ★ THE RAISES ARE DELIBERATELY LEFT. A cancellation or a reopened wave
# gives stock back, and Shopify learns about it at the next
# reconciliation rather than immediately. See the ticket 5c report: the
# honest fix for raises is a scheduled reconciliation, not a push from
# inside a transaction that might not survive.

RAISE_DEFERRED = "would_raise_deferred"


def skus_for_cards(cards) -> list[str]:
    """The Shopify SKUs these cards belong to, computed locally.

    ★ CAPTURE THESE BEFORE A DELETE. A deleted InventoryCard cannot be
    asked for its set code, so a caller that removes rows -- batch
    delete -- has to take the identities first and hand them over. Found
    by a test: passing the card objects after the flush resolved to no
    bindings at all and the push silently did nothing.
    """
    skus = []
    for card in cards or []:
        key = variant_key(card)
        if not key:
            continue
        set_code, collector_number, language_id, condition_id, finish_id = key
        sku = variant_sku(
            set_code=set_code, collector_number=collector_number,
            language_id=language_id, condition_id=condition_id,
            finish_id=finish_id,
        )
        if sku not in skus:
            skus.append(sku)
    return skus


def push_lower_only(session: Session, cards, *, origin: str) -> dict:
    """Lower Shopify's quantity for these cards' identities. Never raises
    it, and never raises an exception into the caller.

    Safe to call from inside an uncommitted transaction -- see the block
    comment above for why that is a property of lowering and not of this
    function being careful.

    `origin` names the path that called it, so a log line says which
    transition moved the stock.
    """
    return push_lower_only_for_skus(session, skus_for_cards(cards),
                                    origin=origin)


def push_lower_only_for_skus(session: Session, skus, *, origin: str) -> dict:
    """The same thing, for identities already resolved to SKUs -- for a
    caller whose cards no longer exist. See skus_for_cards."""
    try:
        return _push_lower_only(session, skus, origin=origin)
    except Exception as exc:  # noqa: BLE001 -- nothing may escape
        # ★ THE SAME CONTRACT AS THE MANA POOL HOOK. A Shopify problem
        # must never fail an allocation, an order sync or an exception
        # resolution. Logged via the cardfoundry logger, as CLAUDE.md
        # requires of every catch-and-continue.
        logger.warning(
            "shopify quantity push from %s raised and was swallowed so the "
            "local transition is unaffected: %s: %s",
            origin, type(exc).__name__, exc,
        )
        return {"origin": origin, "failed": f"{type(exc).__name__}: {exc}",
                "lowered": [], "deferred": []}


def _push_lower_only(session: Session, skus, origin: str) -> dict:
    if not sync_enabled(session):
        return {"origin": origin, "skipped": "SHOPIFY_QTY_SYNC_ENABLED is off",
                "lowered": [], "deferred": []}
    bindings = bindings_for_skus(session, skus)
    if not bindings:
        return {"origin": origin,
                "skipped": "no Shopify binding for these identities",
                "lowered": [], "deferred": []}
    location = shopify_service.location_gid()
    by_sku = {b.sku: b for b in bindings
              if b.shopify_inventory_item_gid}
    if not by_sku:
        return {"origin": origin,
                "skipped": "no binding carries a Shopify inventory item id",
                "lowered": [], "deferred": []}
    current = shopify_publish_service.read_variant_stock(
        sorted(by_sku), location_gid=location)
    wanted_by_sku = desired_quantities(session, list(by_sku.values()))
    lowering = []
    deferred = []
    for sku, binding in sorted(by_sku.items()):
        live = current.get(sku)
        if live is None or live.get("available_at_location") is None:
            continue
        have = int(live["available_at_location"])
        wanted = wanted_by_sku.get(sku, 0)
        if wanted == have:
            continue
        if wanted > have:
            # A raise. Not from here -- see the block comment.
            deferred.append({"sku": sku, "shopify_has": have,
                             "local_says": wanted})
            continue
        lowering.append(binding)
    if deferred:
        logger.info(
            "Shopify quantity push from %s: %s identity(ies) would RISE and "
            "are left for reconciliation (raising from an uncommitted "
            "transaction could advertise stock a rollback removes): %s",
            origin, len(deferred), [row["sku"] for row in deferred][:10],
        )
    if not lowering:
        return {"origin": origin, "lowered": [], "deferred": deferred,
                "skipped": "nothing to lower"}
    report = push_bindings(
        session, lowering, confirm=True,
        run_id=f"{origin}-{datetime.now():%Y%m%dT%H%M%S%f}",
        location_gid=location,
    )
    logger.warning(
        "Shopify quantity push from %s: %s identity(ies) lowered, %s left "
        "to rise at the next reconciliation.", origin,
        len(report["changed"]), len(deferred),
    )
    return {"origin": origin, "lowered": report["changed"],
            "deferred": deferred, "report": report}


# --- what the scheduled reconciliation leaves behind ----------------------

LAST_RECONCILE_SETTING_KEY = "shopify_reconcile_last_result"

# How many SKUs the stored result names. The Attention item has to be
# readable, and an item listing six thousand SKUs is an item nobody
# reads; the count is always exact even when the list is cut.
RECONCILE_SKU_SAMPLE = 25


def last_reconcile_result(session: Session, *, records: dict | None = None
                          ) -> dict | None:
    """What the last reconciliation found, or None if it has never run.

    Stored as one app_settings row rather than a new table: it is a
    single latest-value fact, which is exactly what app_settings is for.

    ★ PASS `records` FROM A PAGE. The Attention page reads this on every
    render, and a query of its own here is a statement added to every
    page in the app -- which the orders-list statement-count tests catch.
    attention_service loads this key alongside the cron stamps in one
    `IN (...)` and hands the dict in.
    """
    if records is not None:
        raw = records.get(LAST_RECONCILE_SETTING_KEY)
        if not (raw or "").strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            logger.warning(
                "%s holds unparseable JSON; treating the last reconciliation "
                "as unknown.", LAST_RECONCILE_SETTING_KEY,
            )
            return None
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == LAST_RECONCILE_SETTING_KEY).first())
    if setting is None or not (setting.value or "").strip():
        return None
    try:
        return json.loads(setting.value)
    except ValueError:
        logger.warning(
            "%s holds unparseable JSON; treating the last reconciliation as "
            "unknown.", LAST_RECONCILE_SETTING_KEY,
        )
        return None


def _store_reconcile_result(session: Session, result: dict) -> dict:
    over = result.get("advertising_more_than_we_hold") or []
    under = [row for row in (result.get("different") or [])
             if row["local_says"] > row["shopify_has"]]
    stored = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "bindings": result.get("bindings") or 0,
        "matched": len(result.get("matched") or []),
        "over_count": len(over),
        # ★ THE UNITS, NOT JUST THE ROWS. Nine variants each advertising
        # one card too many and one variant advertising nine are the same
        # row count and very different problems.
        "over_units": sum(row["shopify_has"] - row["local_says"]
                          for row in over),
        "over_skus": sorted(row["sku"] for row in over)[:RECONCILE_SKU_SAMPLE],
        "under_count": len(under),
        "under_units": sum(row["local_says"] - row["shopify_has"]
                           for row in under),
        "absent": len(result.get("absent_from_shopify") or []),
        "unbound": len(result.get("unbound") or []),
    }
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == LAST_RECONCILE_SETTING_KEY).first())
    value = json.dumps(stored, sort_keys=True)
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=LAST_RECONCILE_SETTING_KEY, value=value,
                               updated_at=datetime.now()))
    # ★ NO AppSettingChangeLog ROW. Every other setting in that table is a
    # POLICY someone chose, and its history is who chose what. This is a
    # measurement that overwrites itself three times a day; auditing it
    # would be six hundred rows a year of a number nobody decided.
    return stored


def reconcile_and_record(session: Session, *, client=None) -> dict:
    """The scheduled reconciliation. READS ONLY, and stamps its own run.

    ★ IT CANNOT WRITE TO SHOPIFY. It calls reconcile(), which reads, and
    then writes two local rows: the result, and the success stamp. There
    is no path from here to a quantity write -- not behind a flag, not
    behind a parameter. A cron that could correct drift by itself would
    be a cron that could overwrite a quantity nobody reviewed, and the
    whole value of this job is that its output is a report.

    ★ THE STAMP IS WRITTEN ONLY ON THE SUCCESS PATH, so a reconciliation
    that could not reach Shopify does not look like one that found
    nothing. A refused gate returns before the stamp.

    Caller commits.
    """
    result = reconcile(session, client=client)
    if result.get("stopped_because"):
        logger.error(
            "Shopify reconciliation did not run: %s", result["stopped_because"],
        )
        return {"ran": False, "stopped_because": result["stopped_because"],
                "stored": None, "result": result}
    stored = _store_reconcile_result(session, result)
    scheduled_job_runs.record_success(session,
                                      scheduled_job_runs.SHOPIFY_RECONCILE)
    logger.warning(
        "Shopify reconciliation recorded: %s bound, %s matched, %s "
        "advertising MORE than we hold (%s card(s) too many), %s "
        "under-advertised, %s absent, %s unbound.",
        stored["bindings"], stored["matched"], stored["over_count"],
        stored["over_units"], stored["under_count"], stored["absent"],
        stored["unbound"],
    )
    return {"ran": True, "stopped_because": None, "stored": stored,
            "result": result}


# --- reconciliation, read-only -------------------------------------------

def reconcile(session: Session, *, client=None, bindings=None,
              location_gid: str | None = None) -> dict:
    """Read Shopify's quantity for every bound variant and compare.

    READS ONLY. Nothing is written, here or on Shopify -- this is the
    report that says whether the two channels agree, and it is the thing
    to run before and after turning the sync on.

      matched     Shopify holds what local state says
      different   it does not, and by how much
      absent      Shopify has no stock record for the variant at this
                  location -- not the same as "zero"
      unbound     the binding carries no inventory item id to ask about
    """
    rows = list(bindings if bindings is not None else live_bindings(session))
    try:
        location = location_gid or shopify_service.location_gid()
    except shopify_service.ShopifyError as exc:
        return {"stopped_because": str(exc), "bindings": len(rows)}
    by_sku = {}
    unbound = []
    for binding in rows:
        if binding.shopify_inventory_item_gid:
            by_sku[binding.sku] = binding
        else:
            unbound.append(binding.sku)
    current = shopify_publish_service.read_variant_stock(
        sorted(by_sku), location_gid=location, client=client)
    wanted_by_sku = desired_quantities(session, list(by_sku.values()))
    matched = []
    different = []
    absent = []
    for sku, binding in sorted(by_sku.items()):
        live = current.get(sku)
        if live is None or live.get("available_at_location") is None:
            absent.append(sku)
            continue
        have = int(live["available_at_location"])
        wanted = wanted_by_sku.get(sku, 0)
        if have == wanted:
            matched.append(sku)
        else:
            different.append({"sku": sku, "shopify_has": have,
                              "local_says": wanted,
                              "product_status": live.get("product_status")})
    oversold = [row for row in different if row["shopify_has"] > row["local_says"]]
    logger.warning(
        "Shopify quantity reconciliation: %s binding(s), %s matched, %s "
        "different (%s advertising MORE than we hold), %s absent, %s "
        "unbound.", len(rows), len(matched), len(different), len(oversold),
        len(absent), len(unbound),
    )
    return {
        "location_gid": location,
        "bindings": len(rows),
        "matched": matched,
        "different": different,
        "advertising_more_than_we_hold": oversold,
        "absent_from_shopify": absent,
        "unbound": unbound,
        "stopped_because": None,
    }
