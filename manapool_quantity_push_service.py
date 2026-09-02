"""Best-effort Mana Pool quantity push, fired immediately after a local
transition reduces sellable stock (mark sold locally, remove from
inventory, mark Not For Sale -- see sellability_service.py's three
transition functions). Closes the latency window between "stock left
locally" and the next manual Perform Sync click, during which Mana Pool
keeps advertising stock that's already gone (the exact shape that let a
real order arrive for an Orcish Bowmasters sold two weeks earlier).

Never blocks or reverses the local transition it follows. Copies
_push_fulfillment_status's exact shape (main.py): the local write
commits first and unconditionally; this call happens after, wrapped so
it never raises; failure is recorded on RemoteProductBinding, not
surfaced as an error to the operator.

Zero Mana Pool reads. decrease_quantity/zero_candidate are self-
correcting by construction (see inventory_reconciliation_service.py's
own module docstring) -- there is no oversell risk in writing a lower
number than Mana Pool currently has, so the fresh local sellable count
is always written directly, without first reading Mana Pool's current
quantity to decide whether it's "still" a decrease. A redundant write
that reasserts a number Mana Pool already has is harmless -- one wasted
POST, not a correctness problem. This is a deliberate difference from
apply_reconciliation_preview, which DOES read fresh remote quantity
first -- but only as a skip-if-unneeded optimization for a batch job,
not a safety requirement, and that batch job's real cost (full order
re-ingestion, full seller-inventory pagination) doesn't shrink no matter
how few rows you hand it, so it was never reusable for a single-card
push in the first place.

product_id is resolved from RemoteProductBinding, purely locally, never
from a live remote scan -- the same trust sellability_service.
sellable_remote_product_ids already places in a validated binding.
Checked live against production for the danger case (two validated
bindings racing for one identity): 918 validated bindings, zero real
collisions on (mtgjson_id, language_id, condition_id, finish_id) --
picking the most-recently-validated one on the vanishingly unlikely
chance of a future collision is a defensive tie-break, not evidence one
is needed today.

A card with no mtgjson_id (the MTGJSON-override path -- see
RemoteProductBinding.mtgjson_override_confirmed_at) can't be matched by
the four-field identity at all; product_id is the only stable grouping
key for it, mirroring inventory_sync_workflow.py's own override
resolution (same local_card_ids_json membership check), just scoped
here to one card instead of the whole inventory.

v1.108.0: a decrease with no resolvable binding at all is now recorded
too, not just silently dropped. Checked live at v1.107.0's launch: only
918 of 6,647 currently-listed identities have a validated binding (14%)
-- the other 86% would have hit this exact silent-skip path with
nothing anywhere indicating it, the same failure class as the Orcish
Bowmasters incident this feature exists to close. See
UnresolvedQuantityPush -- distinct from a RemoteProductBinding push
failure (there IS a binding, the write to Mana Pool itself failed) since
here there's no product_id to have attempted a write against at all,
and the fix is different: backfill_remote_product_bindings.py, not a
retry.
"""

import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from import_service import normalized_language_id
from inventory_mirror_service import SELLABLE_STATUS, canonical_key
from manapool_service import update_inventory_prices_by_product
from models import Batch, InventoryCard, RemoteProductBinding, UnresolvedQuantityPush


def _resolve_binding_for_card(session: Session, card: InventoryCard) -> RemoteProductBinding | None:
    """The validated RemoteProductBinding for card's identity, or None if
    there isn't one -- an unresolved identity is nothing to push, not a
    failure (a never-listed card, or one still missing catalog data,
    correctly has nothing to decrement)."""
    key = canonical_key(card)
    if key:
        mtgjson_id, language_id, condition_id, finish_id = key
        return (
            session.query(RemoteProductBinding)
            .filter(
                RemoteProductBinding.provider == "manapool",
                RemoteProductBinding.binding_status == "validated",
                func.upper(RemoteProductBinding.mtgjson_id) == mtgjson_id,
                func.upper(RemoteProductBinding.language_id) == language_id,
                func.upper(RemoteProductBinding.condition_id) == condition_id,
                func.upper(RemoteProductBinding.finish_id) == finish_id,
            )
            .order_by(RemoteProductBinding.validated_at.desc())
            .first()
        )
    if card.mtgjson_id:
        # Has an mtgjson_id but canonical_key() still failed -- some other
        # identity field (language/condition/finish) is missing. A
        # genuinely incomplete identity, not the override case below.
        return None
    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
        RemoteProductBinding.binding_status == "validated",
        RemoteProductBinding.mtgjson_override_confirmed_at.isnot(None),
    ):
        if card.id in json.loads(binding.local_card_ids_json or "[]"):
            return binding
    return None


def _desired_quantity_for_binding(session: Session, binding: RemoteProductBinding) -> int:
    """Fresh count of currently-sellable local cards under binding's
    identity -- recomputed at push time, never cached, so a later card
    change between resolution and write is still reflected."""
    if binding.mtgjson_id:
        return (
            session.query(InventoryCard)
            .join(Batch, InventoryCard.batch_id == Batch.id)
            .filter(
                InventoryCard.status == SELLABLE_STATUS,
                Batch.is_archived == False,
                func.upper(InventoryCard.mtgjson_id) == binding.mtgjson_id.upper(),
                func.upper(InventoryCard.language_id) == binding.language_id.upper(),
                func.upper(InventoryCard.condition_id) == binding.condition_id.upper(),
                func.upper(InventoryCard.finish_id) == binding.finish_id.upper(),
            )
            .count()
        )
    bound_ids = json.loads(binding.local_card_ids_json or "[]")
    if not bound_ids:
        return 0
    return (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.id.in_(bound_ids),
            InventoryCard.status == SELLABLE_STATUS,
            Batch.is_archived == False,
        )
        .count()
    )


def _push_bindings(session: Session, bindings: list[RemoteProductBinding]) -> None:
    """Write fresh desired quantity for every binding in one batched call
    -- update_inventory_prices_by_product's own 2000-per-POST chunking,
    unchanged, same function apply_reconciliation_preview already uses.
    Never raises: a failure is recorded on every binding in this call and
    swallowed, exactly like _push_fulfillment_status. Caller commits."""
    if not bindings:
        return
    updates = [
        {
            "product_type": "mtg_single",
            "product_id": binding.product_id,
            "price_cents": None,
            "quantity": _desired_quantity_for_binding(session, binding),
        }
        for binding in bindings
    ]
    now = datetime.now(timezone.utc)
    try:
        update_inventory_prices_by_product(updates)
    except (httpx.HTTPError, RuntimeError) as exc:
        for binding in bindings:
            binding.last_quantity_push_attempted_at = now
            binding.last_quantity_push_failure_detail = str(exc)
        return
    for binding in bindings:
        binding.last_quantity_push_attempted_at = now
        binding.last_quantity_push_failure_detail = None


def _identity_key_for_card(card: InventoryCard) -> str:
    """A stable string key for UnresolvedQuantityPush, deduping repeat
    occurrences of the same unresolvable identity into one row. Prefers
    the four-field canonical identity; falls back to scryfall_id when
    there's no mtgjson_id at all (mirrors
    inventory_mirror_service._scryfall_fallback_key's own reasoning for
    this exact shape -- scryfall_id is precise enough to recognize the
    same card again even without a documented MTGJSON identity)."""
    key = canonical_key(card)
    if key:
        return "mtgjson:" + "|".join(key)
    return "scryfall:" + "|".join((
        str(card.scryfall_id or "").strip().lower(),
        normalized_language_id({"Language ID": card.language_id}),
        str(card.condition_id or "").strip().upper(),
        str(card.finish_id or "").strip().upper(),
    ))


def _record_unresolved(session: Session, cards: list[InventoryCard]) -> None:
    """Upsert one UnresolvedQuantityPush row per distinct identity among
    `cards` -- a repeat occurrence updates last_attempted_at rather than
    accumulating duplicates. Never raises: pure local reads/writes."""
    now = datetime.now(timezone.utc)
    seen_keys: set[str] = set()
    for card in cards:
        key = _identity_key_for_card(card)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        row = session.query(UnresolvedQuantityPush).filter_by(identity_key=key).first()
        if row:
            row.last_attempted_at = now
            row.name = card.name
        else:
            session.add(UnresolvedQuantityPush(
                identity_key=key, name=card.name, set_code=card.set_code,
                collector_number=card.collector_number, mtgjson_id=card.mtgjson_id,
                language_id=card.language_id, condition_id=card.condition_id,
                finish_id=card.finish_id, last_attempted_at=now,
            ))


def _clear_unresolved(session: Session, cards: list[InventoryCard]) -> None:
    """Self-heal: a card that DID resolve to a binding this time (e.g.
    after backfill_remote_product_bindings.py ran, or a later write site
    populated one) means any stale UnresolvedQuantityPush row for its
    identity no longer reflects reality -- delete it."""
    for card in cards:
        key = _identity_key_for_card(card)
        session.query(UnresolvedQuantityPush).filter_by(identity_key=key).delete()


def push_for_cards(session: Session, cards: list[InventoryCard]) -> None:
    """Best-effort quantity push for every distinct Mana Pool product
    among `cards` -- one write per distinct binding, not one per card.
    For a single-card route, pass a one-item list; for a bulk route,
    pass every card whose local transition just committed across the
    whole loop.

    A card resolving to no binding at all is recorded on
    UnresolvedQuantityPush, not silently dropped -- see this module's
    own docstring for why that distinction matters. Never raises; caller
    must commit afterward to persist anything recorded here."""
    bindings_by_id: dict[int, RemoteProductBinding] = {}
    resolved_cards = []
    unresolved_cards = []
    for card in cards:
        binding = _resolve_binding_for_card(session, card)
        if binding:
            bindings_by_id[binding.id] = binding
            resolved_cards.append(card)
        else:
            unresolved_cards.append(card)
    _push_bindings(session, list(bindings_by_id.values()))
    if resolved_cards:
        _clear_unresolved(session, resolved_cards)
    if unresolved_cards:
        _record_unresolved(session, unresolved_cards)


def retry_quantity_push(session: Session, binding_id: int) -> bool:
    """Re-attempt one binding's quantity push, fresh. Returns True on
    success (failure_detail cleared), False if the binding doesn't exist
    or the retry also failed. Caller commits either way."""
    binding = session.get(RemoteProductBinding, binding_id)
    if not binding:
        return False
    _push_bindings(session, [binding])
    return binding.last_quantity_push_failure_detail is None


def stuck_quantity_push_bindings(session: Session) -> list[RemoteProductBinding]:
    """Bindings whose last quantity push attempt failed and hasn't since
    succeeded -- for the sync-issues page. A binding that's never had a
    push attempted at all (the overwhelming majority -- most cards never
    trigger a decrease) is not "stuck," it's simply untouched."""
    return (
        session.query(RemoteProductBinding)
        .filter(RemoteProductBinding.last_quantity_push_failure_detail.isnot(None))
        .order_by(RemoteProductBinding.last_quantity_push_attempted_at)
        .all()
    )


def unresolved_quantity_pushes(session: Session) -> list[UnresolvedQuantityPush]:
    """Identities a decrease-causing transition fired for but couldn't
    resolve a Mana Pool binding for at all -- distinct from
    stuck_quantity_push_bindings (a binding exists, the write failed).
    Every row here is fixable only by backfill_remote_product_bindings.py
    creating the missing binding (or a genuinely never-listed identity
    just staying here harmlessly) -- there is nothing to retry."""
    return (
        session.query(UnresolvedQuantityPush)
        .order_by(UnresolvedQuantityPush.last_attempted_at)
        .all()
    )
