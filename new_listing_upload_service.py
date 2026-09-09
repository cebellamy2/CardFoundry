"""Day-to-day publishing of net-new Mana Pool listings.

Scoped to the ``local_only_requires_listing`` category from an inventory
mirror preview (`inventory_mirror_service.build_inventory_mirror_preview`):
cards CardFoundry already owns and considers sellable, but Mana Pool has
never listed at all. Quantity/decrease reconciliation on already-listed
products is a separate, more cautious concern and is out of scope here.

Preview (pricing) and apply (writing) are deliberately split, matching the
Price Updates page pattern: preview does no writes, apply re-validates
freshness immediately before writing and reports Mana Pool's own per-item
result rather than building separate isolation machinery on top of it.
"""

import hashlib
import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from catalog_resolution_service import requested_variant
from inventory_mirror_service import MTGJSON_OVERRIDE_KEY_PREFIX
from models import InventoryCard, RemoteProductBinding
from new_listing_pricing_service import price_initial_bindings, price_new_listing_candidates


CANONICAL_FIELDS = ("mtgjson_id", "language_id", "condition_id", "finish_id")


class NewListingUploadError(ValueError):
    def __init__(self, message: str, excluded: list | None = None):
        super().__init__(message)
        self.excluded = excluded or []


def _representative_identity(cards: list) -> dict:
    identity = requested_variant(cards[0])
    identity["mtgjson_id"] = str(cards[0].mtgjson_id or "").strip().upper()
    return identity


def _existing_binding_for_cards(session: Session, card_ids: list[int]):
    """Look for an already-persisted validated binding covering any of these
    cards. Used only as a fallback for the rare card with no scryfall_id --
    this never resolves a *new* binding, since that requires a scryfall_id
    too (see catalog_resolution_service.requested_variant).
    """
    wanted = set(card_ids)
    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.binding_status == "validated",
    ).all():
        if wanted & set(json.loads(binding.local_card_ids_json)):
            return binding
    return None


def _card_reviewed_price_cents(cards: list) -> int | None:
    """The operator's own reviewed price for this exact group, in cents --
    current_price first (kept fresh by Flow B and manual edits), falling
    back to the original import-time price_usd. Used only as a starting
    price for a first-time listing that has no competitor/market/manual
    evidence; not a substitute for real pricing. Named to avoid colliding
    with this file's own unrelated "reviewed_price_cents" (the preview-
    time target price shown to the operator, used for drift comparison
    in apply_new_listing_preview below)."""
    for card in cards:
        price = card.current_price if card.current_price is not None else card.price_usd
        if price is not None and price > 0:
            return round(price * 100)
    return None


def _card_bought_in_price_cents(cards: list) -> int | None:
    """Cost-plus-markup fallback source, in cents -- what CardFoundry paid
    for the physical card. Used only as the very last pricing tier, when a
    first-time listing has no competitor, market, manual, or reviewed
    price either; not a substitute for real pricing."""
    for card in cards:
        price = card.bought_in_price
        if price is not None and price > 0:
            return round(price * 100)
    return None


def extract_new_listing_candidates(session: Session, mirror_preview: dict) -> tuple[list[dict], list[dict]]:
    """Build pricing candidates from a mirror preview's local_only_requires_listing rows.

    Returns (candidates, excluded). Each candidate is
    ``{"key", "identity", "desired_quantity", "card_ids", "path",
    "card_reviewed_price_cents", "product_id"?}`` where ``path`` is
    ``"scryfall_id"`` (the common case) or ``"product_id"`` (when the card
    has no scryfall_id, or when its canonical identity is an operator-
    confirmed MTGJSON override -- see below).
    ``card_reviewed_price_cents`` is the group's own current inventory
    price (see ``_card_reviewed_price_cents``), used only as a first-
    listing fallback when there's no other pricing evidence. ``excluded``
    rows carry a ``reason`` and are never written.

    An override-confirmed row (``canonical_identity["mtgjson_id"]``
    starting with ``MTGJSON_OVERRIDE_KEY_PREFIX``, see
    inventory_mirror_service._mtgjson_override_key) always uses the
    product_id path, even though the card has its own scryfall_id --
    confirmed live: Mana Pool sometimes groups every language of a
    printing under one shared catalog scryfall_id, so the card's own
    (language-specific) scryfall_id can 404 against Mana Pool's
    scryfall_id write endpoint even though a real, already-catalogued
    product exists. The override's own validated binding already proves
    that product_id is real; use it directly instead of gambling on
    scryfall_id working.
    """
    rows = [
        row for row in mirror_preview.get("rows") or []
        if row.get("category") == "local_only_requires_listing"
    ]
    candidates = []
    excluded = []
    for row in rows:
        identity_key = row.get("canonical_identity") or {}
        key = tuple(identity_key.get(field) for field in CANONICAL_FIELDS)
        cards = [
            card for card in (
                session.get(InventoryCard, card_id)
                for card_id in row.get("local_contributing_card_ids") or []
            ) if card is not None
        ]
        card_ids = [card.id for card in cards]
        if not cards:
            excluded.append({
                "key": key, "reason": "No contributing inventory cards found",
                "card_ids": card_ids,
            })
            continue
        identity = _representative_identity(cards)
        desired_quantity = int(row.get("desired_quantity") or len(cards))
        card_reviewed_price_cents = _card_reviewed_price_cents(cards)
        card_bought_in_price_cents = _card_bought_in_price_cents(cards)
        is_override = str(identity_key.get("mtgjson_id") or "").startswith(MTGJSON_OVERRIDE_KEY_PREFIX)
        if not is_override and identity.get("scryfall_id"):
            candidates.append({
                "key": key, "identity": identity, "desired_quantity": desired_quantity,
                "card_ids": card_ids, "path": "scryfall_id",
                "card_reviewed_price_cents": card_reviewed_price_cents,
                "card_bought_in_price_cents": card_bought_in_price_cents,
            })
            continue
        binding = _existing_binding_for_cards(session, card_ids)
        if not binding:
            excluded.append({
                "key": key,
                "reason": "Confirmed override binding not found" if is_override else
                    "No scryfall_id and no existing product binding",
                "card_ids": card_ids,
            })
            continue
        candidates.append({
            "key": key, "identity": identity, "desired_quantity": desired_quantity,
            "card_ids": card_ids, "path": "product_id", "product_id": binding.product_id,
            "binding_id": binding.id, "card_reviewed_price_cents": card_reviewed_price_cents,
            "card_bought_in_price_cents": card_bought_in_price_cents,
        })
    return candidates, excluded


def build_new_listing_preview(
    session: Session,
    mirror_preview: dict,
    optimizer_call, listings_call, seller_id,
    market_catalog_scryfall_call,
    market_catalog_product_call=None,
    undercut_cents=5, floor_cents=65,
    manual_overrides=(), cost_markup_multiplier=2.0,
) -> dict:
    """Price every local_only_requires_listing candidate. No writes.

    Never calls the optimizer (skip_competitor_tier=True on both pricing
    calls) -- getting a first-time listing live matters more than a
    competitively-checked price on day one, and Flow B's regular
    competitive re-pricing (competitor_pricing_service.py) picks up
    freshly-listed inventory on its own next run regardless. Market and
    manual-override pricing are unaffected; a candidate with neither
    still publishes at its own reviewed inventory price, or -- lacking
    that too -- at cost_markup_multiplier times its own bought_in_price,
    rather than holding (see price_new_listing_candidates/price_initial_bindings).
    """
    candidates, excluded = extract_new_listing_candidates(session, mirror_preview)
    scryfall_candidates = [c for c in candidates if c["path"] == "scryfall_id"]
    binding_candidates = [c for c in candidates if c["path"] == "product_id"]

    by_key = {}

    if scryfall_candidates:
        pricing = price_new_listing_candidates(
            [
                {
                    "key": c["key"], "identity": c["identity"],
                    "card_reviewed_price_cents": c.get("card_reviewed_price_cents"),
                    "card_bought_in_price_cents": c.get("card_bought_in_price_cents"),
                }
                for c in scryfall_candidates
            ],
            optimizer_call, listings_call, seller_id,
            undercut_cents=undercut_cents, floor_cents=floor_cents,
            market_catalog_call=market_catalog_scryfall_call,
            manual_overrides=manual_overrides,
            skip_competitor_tier=True, cost_markup_multiplier=cost_markup_multiplier,
        )
        by_key.update({row["key"]: row for row in pricing["results"]})

    if binding_candidates:
        bindings = [
            session.get(RemoteProductBinding, c["binding_id"]) for c in binding_candidates
        ]
        reviewed_price_by_binding_id = {
            c["binding_id"]: c.get("card_reviewed_price_cents") for c in binding_candidates
        }
        bought_in_price_by_binding_id = {
            c["binding_id"]: c.get("card_bought_in_price_cents") for c in binding_candidates
        }
        pricing = price_initial_bindings(
            bindings, optimizer_call, listings_call, seller_id,
            undercut_cents=undercut_cents, floor_cents=floor_cents,
            market_catalog_call=market_catalog_product_call,
            manual_overrides=manual_overrides,
            skip_competitor_tier=True,
            reviewed_price_by_binding_id=reviewed_price_by_binding_id,
            bought_in_price_by_binding_id=bought_in_price_by_binding_id,
            cost_markup_multiplier=cost_markup_multiplier,
        )
        binding_id_to_key = {c["binding_id"]: c["key"] for c in binding_candidates}
        for row in pricing["results"]:
            by_key[binding_id_to_key[row["binding_id"]]] = row

    rows = []
    for candidate in candidates:
        price_row = by_key.get(candidate["key"]) or {
            "status": "hold", "reason": "No pricing result", "target_price_cents": None,
        }
        rows.append({**candidate, **price_row})
    for row in excluded:
        rows.append({**row, "status": "excluded"})

    return {
        "preview_only": True,
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_local_snapshot_hash": mirror_preview.get("local_snapshot_hash"),
        "source_remote_snapshot_hash": mirror_preview.get("remote_snapshot_hash"),
        "rows": rows,
        "summary": {
            "candidates": len(candidates),
            "priced": sum(row.get("status") == "priced" for row in rows),
            "held": sum(row.get("status") == "hold" for row in rows),
            "excluded": len(excluded),
        },
    }


def _remote_indexes(remote_inventory: list[dict]) -> tuple[dict, dict]:
    remote_by_scryfall = {}
    remote_by_product = {}
    for item in remote_inventory:
        single = (item.get("product") or {}).get("single") or {}
        scryfall_id = str(single.get("scryfall_id") or "").lower()
        remote_identity = (
            scryfall_id,
            str(single.get("language_id") or "").upper(),
            str(single.get("condition_id") or "").upper(),
            str(single.get("finish_id") or "").upper(),
        )
        if scryfall_id:
            remote_by_scryfall[remote_identity] = item
        product_id = str(item.get("product_id") or "")
        if product_id:
            remote_by_product.setdefault(product_id, []).append(item)
    return remote_by_scryfall, remote_by_product


def _identity_key_from_response_item(item: dict) -> tuple[str, str, str, str]:
    """Mana Pool's scryfall_id-write response nests identity under
    product.single, matching every other inventoryItem shape this
    codebase reads (see main.py's own _new_listing_apply_detail parsing
    the same response type) -- falls back to a flat field on the item
    itself in case a given response ever omits the nested form, exactly
    as _new_listing_apply_detail already does."""
    single = (item.get("product") or {}).get("single") or {}
    return (
        str(single.get("scryfall_id") or item.get("scryfall_id") or "").lower(),
        str(single.get("language_id") or item.get("language_id") or "").upper(),
        str(single.get("condition_id") or item.get("condition_id") or "").upper(),
        str(single.get("finish_id") or item.get("finish_id") or "").upper(),
    )


def _ensure_bindings_for_scryfall_publish(
    session: Session, rows: list[dict], responses: list[dict],
) -> list[dict]:
    """A scryfall_id write needs no pre-existing binding to succeed (see
    manapool_service.create_or_update_inventory_by_scryfall_id's own
    docstring), but leaves this identity with no RemoteProductBinding at
    all -- the exact gap that later leaves manapool_quantity_push_service
    (the immediate per-transition push) nothing to resolve for it, same
    reasoning as inventory_reconciliation_service._ensure_binding_for_
    increase's identical fix for the reconciliation-increase path.
    Creates one per successfully-published row here, closing the other
    half of the same class of gap -- confirmed live: 2026-09-05's mass
    republish (job 198, 1,703 identities) created zero bindings for any
    of them.

    Matched back to each row by identity, not by response position --
    Mana Pool's response list isn't guaranteed to preserve request order
    or length (a skipped item shrinks it). Never raises: a row whose
    identity can't be found in the response, or whose product_id already
    has a conflicting binding, is reported and left alone. Caller
    commits."""
    by_identity = {}
    for response in responses:
        for item in response.get("inventory") or []:
            product_id = str(item.get("product_id") or "")
            if product_id:
                by_identity[_identity_key_from_response_item(item)] = product_id

    outcomes = []
    for row in rows:
        identity = row["identity"]
        key = (
            str(identity.get("scryfall_id") or "").lower(),
            str(identity.get("language_id") or "").upper(),
            str(identity.get("condition_id") or "").upper(),
            str(identity.get("finish_id") or "").upper(),
        )
        product_id = by_identity.get(key)
        if not product_id:
            outcomes.append({"key": row["key"], "outcome": "no_product_id_in_response"})
            continue
        mtgjson_id = str(identity.get("mtgjson_id") or "")
        existing = None
        if mtgjson_id:
            existing = session.query(RemoteProductBinding).filter(
                RemoteProductBinding.provider == "manapool",
                RemoteProductBinding.binding_status == "validated",
                func.upper(RemoteProductBinding.mtgjson_id) == mtgjson_id.upper(),
                func.upper(RemoteProductBinding.language_id) == key[1],
                func.upper(RemoteProductBinding.condition_id) == key[2],
                func.upper(RemoteProductBinding.finish_id) == key[3],
            ).first()
        if existing:
            outcomes.append({"key": row["key"], "product_id": product_id, "outcome": "existing"})
            continue
        conflict = session.query(RemoteProductBinding).filter(
            RemoteProductBinding.provider == "manapool",
            RemoteProductBinding.product_type == "mtg_single",
            RemoteProductBinding.product_id == product_id,
        ).first()
        if conflict:
            outcomes.append({"key": row["key"], "product_id": product_id, "outcome": "conflict"})
            continue
        evidence = {
            "source": "new_listing_upload_service.apply_new_listing_preview",
            "matched_via": "scryfall_id_publish",
            "requested_identity": identity,
            "product_type": "mtg_single",
            "product_id": product_id,
        }
        evidence_hash = hashlib.sha256(json.dumps(
            evidence, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        now = datetime.now(timezone.utc)
        session.add(RemoteProductBinding(
            provider="manapool", product_type="mtg_single", product_id=product_id,
            local_card_ids_json=json.dumps(sorted(row.get("reconfirmed_card_ids") or row.get("card_ids") or [])),
            requested_identity_json=json.dumps(identity, sort_keys=True),
            scryfall_id=identity.get("scryfall_id") or "", mtgjson_id=mtgjson_id or None,
            language_id=identity.get("language_id"), condition_id=identity.get("condition_id"),
            finish_id=identity.get("finish_id"), set_code=identity.get("set_code") or "",
            collector_number=identity.get("collector_number") or "",
            binding_status="validated", validated_at=now,
            catalog_as_of=None, evidence_hash=evidence_hash,
            evidence_json=json.dumps(evidence, sort_keys=True),
            remote_inventory_id=None,
        ))
        outcomes.append({"key": row["key"], "product_id": product_id, "outcome": "created"})
    return outcomes


def _identity_key(d: dict) -> tuple:
    """Same four-field identity, read off any of the three dict shapes
    that carry it: a scryfall_updates write item, a row's own
    ``identity``, or one entry of Mana Pool's 404 ``details`` list --
    all three use the same field names."""
    return (
        str(d.get("scryfall_id") or "").lower(),
        str(d.get("language_id") or "").upper(),
        str(d.get("condition_id") or "").upper(),
        str(d.get("finish_id") or "").upper(),
    )


def _not_found_keys_from_response(exc: httpx.HTTPStatusError) -> set:
    """Mana Pool's scryfall_id-write 404 names exactly which identities
    it rejected: {"status":404,"message":"Product not found","details":
    [{"scryfall_id":...,"language_id":...,"condition_id":...,
    "finish_id":...}, ...]} -- confirmed live, 2026-09-09. Returns an
    empty set for any 404 (or other status) that doesn't carry this
    exact shape, so the caller knows to treat it as unexplained and
    re-raise rather than guess."""
    response = exc.response
    if response is None or response.status_code != 404:
        return set()
    try:
        body = response.json()
    except Exception:
        return set()
    details = body.get("details") if isinstance(body, dict) else None
    if not isinstance(details, list):
        return set()
    return {
        _identity_key(item) for item in details
        if isinstance(item, dict) and item.get("scryfall_id")
    }


def _write_scryfall_updates_isolating_not_found(scryfall_writer, updates: list[dict]):
    """Write scryfall-path updates, retrying with any identity Mana Pool
    reports as "Product not found" (404) removed -- one identity Mana
    Pool's own catalog genuinely doesn't carry (e.g. a finish it
    doesn't offer for that printing) must not block every other
    identity in the same request from ever publishing. Confirmed live,
    2026-09-09: 2 such rows (both foil, Mana Pool's catalog has no foil
    SKU for either printing) blocked all 26 other legitimate new
    listings, every single scheduled run, indefinitely -- the whole
    request-level raise_for_status() in manapool_service._post_json
    took the entire chunk down over 2 bad items.

    Each retry removes only the identities the most recent response
    actually named, so a response naming previously-unseen identities
    (e.g. Mana Pool reports them a few at a time) keeps narrowing
    instead of giving up after one pass; a response that repeats
    already-known bad identities with nothing new is unexplained
    progress and re-raises rather than looping.

    Returns (responses, bad_keys): ``responses`` is exactly
    ``scryfall_writer``'s own return shape (a list, one entry per
    chunk) for whatever finally got written -- empty if every update
    turned out to be bad. ``bad_keys`` is the set of identity tuples
    (see ``_identity_key``) Mana Pool rejected, empty when nothing was.
    Any failure this can't attribute to specific identities re-raises
    unchanged -- fail closed on the whole batch, exactly as before,
    rather than silently guessing at what's safe to drop.
    """
    remaining = list(updates)
    bad_keys = set()
    while True:
        if not remaining:
            return [], bad_keys
        try:
            return scryfall_writer(remaining), bad_keys
        except httpx.HTTPStatusError as exc:
            reported = _not_found_keys_from_response(exc)
            new_bad = reported - bad_keys
            if not new_bad:
                raise
            bad_keys |= new_bad
            remaining = [item for item in remaining if _identity_key(item) not in bad_keys]


def apply_new_listing_preview(
    session: Session,
    preview: dict,
    seller_loader,
    scryfall_writer,
    product_writer,
    optimizer_call,
    listings_call,
    seller_id,
    market_catalog_scryfall_call,
    market_catalog_product_call=None,
    undercut_cents=5,
    floor_cents=65,
    price_drift_tolerance=0.10,
    manual_overrides=(),
    cost_markup_multiplier=2.0,
) -> dict:
    """Write priced rows to Mana Pool.

    Re-validates, immediately before writing, everything the preview
    reviewed:

    1. Local available quantity for each row's cards is unchanged.
    2. Mana Pool still doesn't already list that identity.
    3. The competitor/market price backing the row hasn't moved -- a live
       marketplace price is only trustworthy for a moment, and a stale
       preview can genuinely be wrong minutes later (a cheaper same-or-
       better-condition listing can appear or disappear).

    Unlike the local-availability/already-listed checks (which are a
    correctness concern -- writing on top of stale evidence there could
    double-list or misreport a quantity), a row failing (3) is not written
    but does not block its siblings: nothing is unsafe about publishing
    the other rows at their still-current prices, matching the same
    batch-isolation principle used for order-status sync. A row whose
    price moved by less than ``price_drift_tolerance`` (10% by default) is
    still published, but at the freshly re-checked price, not the stale
    reviewed one -- this is reported as "repriced," never silently. Only a
    move at or past the tolerance excludes the row entirely. Every
    excluded/repriced row is reported with why and the reviewed vs.
    current price, so the operator can re-preview just the excluded ones.

    ``manual_overrides`` must be threaded through to this fresh re-pricing
    step, not just the original preview build -- a reviewed manual price
    is a stable operator-set value, not a live quote, so re-deriving it
    here returns the identical price (zero drift) as long as the override
    is still active. Omitting it here would silently re-hold and exclude
    every manually-priced row as "no longer priceable," which is exactly
    what happened before this was wired through.

    Never calls the optimizer here either (skip_competitor_tier=True,
    matching build_new_listing_preview) -- publishing must not be blocked
    on a fresh competitive check any more than the original preview was;
    Flow B corrects the price on its own next run.

    A scryfall-path identity Mana Pool's own catalog doesn't recognize
    (a 404 naming it specifically, see
    _write_scryfall_updates_isolating_not_found) is excluded the same
    batch-isolated way, not allowed to block its siblings -- one bad
    identity previously took the whole scryfall-path batch down.
    """
    priced_rows = [row for row in preview.get("rows") or [] if row.get("status") == "priced"]
    if not priced_rows:
        raise NewListingUploadError("This preview has no priced rows to publish.")

    remote_by_scryfall, remote_by_product = _remote_indexes(seller_loader(min_quantity=0))

    still_eligible = []
    excluded = []
    for row in priced_rows:
        cards = [session.get(InventoryCard, card_id) for card_id in row.get("card_ids") or []]
        still_available = [card for card in cards if card and card.status == "available"]
        if len(still_available) != row.get("desired_quantity"):
            excluded.append({**row, "exclusion_reason": "Local availability changed since preview"})
            continue
        # Re-derived fresh from the current cards, not carried over from
        # the stale preview row -- the operator's own current_price can
        # change between preview and apply (a manual edit, Flow B) same as
        # a competitor's price can, and the reviewed-inventory-price and
        # cost-plus-markup tiers need the same freshness guarantee as
        # every other tier here.
        #
        # reconfirmed_card_ids: the exact cards whose availability was
        # just reconfirmed above, not the stale row["card_ids"] from the
        # original preview -- used only for the InventoryListingStatus
        # cache write after a successful publish (see
        # published_card_ids below); never used for pricing/quantity,
        # which stay on the preview's original card_ids/desired_quantity
        # exactly as before.
        row = {
            **row,
            "card_reviewed_price_cents": _card_reviewed_price_cents(still_available),
            "card_bought_in_price_cents": _card_bought_in_price_cents(still_available),
            "reconfirmed_card_ids": [card.id for card in still_available],
        }
        identity = row["identity"]
        if row["path"] == "scryfall_id":
            remote_identity = (
                str(identity.get("scryfall_id") or "").lower(),
                str(identity.get("language_id") or "").upper(),
                str(identity.get("condition_id") or "").upper(),
                str(identity.get("finish_id") or "").upper(),
            )
            existing = remote_by_scryfall.get(remote_identity)
        else:
            matches = remote_by_product.get(row.get("product_id"))
            existing = matches[0] if matches else None
        if existing and int(existing.get("quantity") or 0) > 0:
            excluded.append({**row, "exclusion_reason": "Mana Pool already lists this identity"})
            continue
        still_eligible.append(row)

    scryfall_eligible = [row for row in still_eligible if row["path"] == "scryfall_id"]
    product_eligible = [row for row in still_eligible if row["path"] == "product_id"]

    fresh_by_key = {}
    if scryfall_eligible:
        fresh_pricing = price_new_listing_candidates(
            [
                {
                    "key": tuple(row["key"]), "identity": row["identity"],
                    "card_reviewed_price_cents": row.get("card_reviewed_price_cents"),
                    "card_bought_in_price_cents": row.get("card_bought_in_price_cents"),
                }
                for row in scryfall_eligible
            ],
            optimizer_call, listings_call, seller_id,
            undercut_cents=undercut_cents, floor_cents=floor_cents,
            market_catalog_call=market_catalog_scryfall_call,
            manual_overrides=manual_overrides,
            skip_competitor_tier=True, cost_markup_multiplier=cost_markup_multiplier,
        )
        fresh_by_key.update({row["key"]: row for row in fresh_pricing["results"]})
    if product_eligible:
        bindings = [
            session.get(RemoteProductBinding, row["binding_id"]) for row in product_eligible
        ]
        reviewed_price_by_binding_id = {
            row["binding_id"]: row.get("card_reviewed_price_cents") for row in product_eligible
        }
        bought_in_price_by_binding_id = {
            row["binding_id"]: row.get("card_bought_in_price_cents") for row in product_eligible
        }
        fresh_pricing = price_initial_bindings(
            bindings, optimizer_call, listings_call, seller_id,
            undercut_cents=undercut_cents, floor_cents=floor_cents,
            market_catalog_call=market_catalog_product_call,
            manual_overrides=manual_overrides,
            skip_competitor_tier=True,
            reviewed_price_by_binding_id=reviewed_price_by_binding_id,
            bought_in_price_by_binding_id=bought_in_price_by_binding_id,
            cost_markup_multiplier=cost_markup_multiplier,
        )
        binding_id_to_key = {row["binding_id"]: tuple(row["key"]) for row in product_eligible}
        for fresh_row in fresh_pricing["results"]:
            fresh_by_key[binding_id_to_key[fresh_row["binding_id"]]] = fresh_row

    fresh_rows = []
    repriced = []
    for row in still_eligible:
        fresh = fresh_by_key.get(tuple(row["key"]))
        reviewed_price = int(row["target_price_cents"])
        if not fresh or fresh.get("status") != "priced":
            excluded.append({
                **row, "exclusion_reason": "No longer priceable: "
                + ((fresh or {}).get("reason") or "no current pricing evidence"),
                "reviewed_price_cents": reviewed_price,
            })
            continue
        current_price = int(fresh["target_price_cents"])
        if current_price == reviewed_price:
            fresh_rows.append(row)
            continue
        drift = abs(current_price - reviewed_price) / reviewed_price
        if drift >= price_drift_tolerance:
            excluded.append({
                **row,
                "exclusion_reason": "Price changed since preview",
                "reviewed_price_cents": reviewed_price,
                "current_price_cents": current_price,
            })
            continue
        # Within tolerance: publish, but at the fresh price, not the stale
        # reviewed one -- and say so, rather than writing it silently.
        repriced.append({
            **row,
            "reviewed_price_cents": reviewed_price,
            "current_price_cents": current_price,
        })
        fresh_rows.append({**row, "target_price_cents": current_price})

    if not fresh_rows:
        reasons = "; ".join(
            f'{(row.get("identity") or {}).get("name") or "Unknown card"}: {row.get("exclusion_reason")}'
            for row in excluded
        )
        raise NewListingUploadError(
            f"None of the {len(priced_rows)} reviewed row(s) are still valid to publish. "
            f"Reasons: {reasons}. Run a fresh preview.",
            excluded=excluded,
        )

    scryfall_updates = [
        {
            "scryfall_id": row["identity"]["scryfall_id"],
            "language_id": row["identity"]["language_id"],
            "condition_id": row["identity"]["condition_id"],
            "finish_id": row["identity"]["finish_id"],
            "price_cents": int(row["target_price_cents"]),
            "quantity": int(row["desired_quantity"]),
        }
        for row in fresh_rows if row["path"] == "scryfall_id"
    ]
    product_updates = [
        {
            "product_type": "mtg_single",
            "product_id": row["product_id"],
            "price_cents": int(row["target_price_cents"]),
            "quantity": int(row["desired_quantity"]),
        }
        for row in fresh_rows if row["path"] == "product_id"
    ]

    responses = {}
    binding_outcomes = []
    if scryfall_updates:
        written, not_found_keys = _write_scryfall_updates_isolating_not_found(
            scryfall_writer, scryfall_updates,
        )
        if not_found_keys:
            for row in fresh_rows:
                if row["path"] == "scryfall_id" and _identity_key(row["identity"]) in not_found_keys:
                    excluded.append({
                        **row,
                        "exclusion_reason": "Mana Pool does not recognize this exact identity (404 Product not found)",
                    })
            fresh_rows = [
                row for row in fresh_rows
                if row["path"] != "scryfall_id" or _identity_key(row["identity"]) not in not_found_keys
            ]
        scryfall_updates = [
            item for item in scryfall_updates if _identity_key(item) not in not_found_keys
        ]
        if written:
            responses["scryfall_id"] = written
            binding_outcomes = _ensure_bindings_for_scryfall_publish(
                session,
                [row for row in fresh_rows if row["path"] == "scryfall_id"],
                responses["scryfall_id"],
            )
    if product_updates:
        responses["product_id"] = product_writer(product_updates)

    # Every card that just got a real Mana Pool write above -- no
    # additional Mana Pool call needed, these are the same
    # reconfirmed-available cards already loaded during re-validation.
    # Used only to refresh the InventoryListingStatus cache (see
    # inventory_sync_workflow.mark_cards_listed) so the next page load
    # reads "Listed" without a manual Perform Sync/Exceptions visit.
    published_card_ids = [
        card_id for row in fresh_rows for card_id in row.get("reconfirmed_card_ids") or []
    ]

    return {
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "scryfall_updates": scryfall_updates,
        "product_updates": product_updates,
        "responses": responses,
        "published_card_ids": published_card_ids,
        "excluded": excluded,
        "repriced": repriced,
        "binding_outcomes": binding_outcomes,
    }
