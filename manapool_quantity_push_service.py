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

import logging
import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from import_service import normalized_language_id
from inventory_mirror_service import SELLABLE_STATUS, canonical_key
from manapool_service import update_inventory_prices_by_product
from card_name_matching import names_equivalent
from models import Batch, InventoryCard, RemoteProductBinding, UnresolvedQuantityPush
from physical_identity import identity_predicate, is_english

logger = logging.getLogger("cardfoundry")


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


def _binding_name(binding: RemoteProductBinding) -> str:
    """The printing's name as this binding requested it."""
    try:
        requested = json.loads(binding.requested_identity_json or "{}")
    except (TypeError, ValueError):
        logger.warning(
            "Binding %s has unreadable requested_identity_json; falling back to "
            "no name, which keeps the physical-identity match strict.", binding.id,
        )
        return ""
    return str(requested.get("name") or "")


def _binding_matches_card(binding: RemoteProductBinding, card: InventoryCard) -> int | None:
    """How this binding matches this card, as a rank, or None for no match.

    0 = exact MTGJSON match, 1 = physical identity only. Used to decide
    OWNERSHIP, so the ranking has to be the same rule the SQL predicate uses.
    """
    for left, right in (
        (binding.language_id, card.language_id),
        (binding.condition_id, card.condition_id),
        (binding.finish_id, card.finish_id),
    ):
        if str(left or "").strip().upper() != str(right or "").strip().upper():
            return None
    binding_mtgjson = str(binding.mtgjson_id or "").strip().upper()
    card_mtgjson = str(card.mtgjson_id or "").strip().upper()
    if binding_mtgjson and binding_mtgjson == card_mtgjson:
        return 0
    if is_english(binding.language_id):
        return None
    set_code = str(binding.set_code or "").strip().upper()
    number = str(binding.collector_number or "").strip().upper()
    if not set_code or not number:
        return None
    if set_code != str(card.set_code or "").strip().upper():
        return None
    if number != str(card.collector_number or "").strip().upper():
        return None
    if not names_equivalent(_binding_name(binding), card.name):
        return None
    return 1


def _owning_binding_id(session: Session, card: InventoryCard) -> int | None:
    """THE binding one physical card counts toward -- exactly one, always.

    ★ THIS IS WHAT PREVENTS DOUBLE COUNTING. Mana Pool files non-English
    printings under both id conventions, so one physical card can legitimately
    match two validated bindings (one keyed on the English Scryfall object, one
    on its own language's). Counting it under both would offer the same single
    card twice.

    Ownership is decided by a total order over matching bindings, so it is
    deterministic and every binding reaches the SAME answer independently:
      1. an exact MTGJSON match beats a physical-identity-only match;
      2. ties break on the lowest binding id.
    There is no tie that both can win, and no card that neither claims.
    """
    candidates = []
    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
        RemoteProductBinding.binding_status == "validated",
        func.upper(RemoteProductBinding.language_id) == str(card.language_id or "").strip().upper(),
        func.upper(RemoteProductBinding.condition_id) == str(card.condition_id or "").strip().upper(),
        func.upper(RemoteProductBinding.finish_id) == str(card.finish_id or "").strip().upper(),
    ):
        rank = _binding_matches_card(binding, card)
        if rank is not None:
            candidates.append((rank, binding.id))
    if not candidates:
        return None
    return min(candidates)[1]


def _desired_quantity_for_binding(session: Session, binding: RemoteProductBinding) -> int:
    """Fresh count of currently-sellable local cards under binding's
    identity -- recomputed at push time, never cached, so a later card
    change between resolution and write is still reflected.

    ENGLISH BINDINGS ARE UNCHANGED (v2.11.0): one COUNT on the exact MTGJSON
    identity, or the local_card_ids_json membership fallback when the binding
    has no MTGJSON id of its own. 7,528 of 7,562 validated bindings are
    English, so this stays a single cheap query for almost every binding.

    NON-ENGLISH BINDINGS also count a card that matches on PHYSICAL IDENTITY
    when the MTGJSON ids disagree or are absent -- see physical_identity for the
    rule and the measurement behind it. Without this the listing was
    UNDER-listed: a card sitting available on the shelf was not counted, so
    Mana Pool was told we had fewer than we do.

    Only `available` cards count, exactly as before -- SELLABLE_STATUS is
    unchanged, so reserved, sold, unsellable and exception cards are still
    excluded, and an archived batch is still excluded. Nothing else about the
    definition of desired quantity changes.
    """
    if is_english(binding.language_id):
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

    condition, _fallback = identity_predicate(
        mtgjson_id=binding.mtgjson_id,
        language_id=binding.language_id,
        name=_binding_name(binding),
        set_code=binding.set_code,
        collector_number=binding.collector_number,
    )
    candidates = (
        session.query(InventoryCard)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(
            InventoryCard.status == SELLABLE_STATUS,
            Batch.is_archived == False,
            condition,
            func.upper(InventoryCard.language_id) == binding.language_id.upper(),
            func.upper(InventoryCard.condition_id) == binding.condition_id.upper(),
            func.upper(InventoryCard.finish_id) == binding.finish_id.upper(),
        )
        .all()
    )
    # One physical card, one binding. See _owning_binding_id.
    return sum(
        1 for card in candidates
        if _owning_binding_id(session, card) == binding.id
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
        # A remote WRITE. Recorded only as a string on each binding,
        # so a systematic push outage looks like a normal run --
        # exactly the silent drift that cost a week of divergence.
        logger.warning(
            "mana pool quantity push failed for %s binding(s): %s: %s",
            len(updates), type(exc).__name__, exc,
        )
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
    # The cached listed/not_listed value is now stale for every card here:
    # each one has just stopped being sellable. Found on card 9430, which
    # read "listed" on the inventory page while its Mana Pool listing had
    # been at quantity 0 for two hours. Cosmetic, but it is the cache an
    # operator looks at to decide whether something is live.
    # Imported here, not at module scope: identity_change_service imports
    # THIS module for its strict push, so a top-level import would be a
    # cycle. One function, one direction, no package gymnastics.
    from identity_change_service import clear_listing_status

    for card in cards:
        clear_listing_status(session, card.id)


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


class QuantityPushFailed(RuntimeError):
    """A quantity push that the CALLER must not step over.

    _push_bindings never raises, deliberately: it follows a local
    transition that has already committed, so failing loudly would only
    make a recorded, retryable drift look like a broken operation.

    An identity correction is the opposite case. The old listing has to
    come down BEFORE the card stops backing it, and if that write does
    not land there must be no correction -- otherwise Mana Pool keeps
    advertising stock under an identity nothing holds any more, which is
    the 2026-09-07 orphan incident exactly.
    """


def push_binding_quantity_strict(session: Session, binding: RemoteProductBinding) -> int:
    """Write this binding's freshly-recomputed desired quantity and RAISE
    if the write fails. Returns the quantity written.

    Call it AFTER the card's identity has been changed in the session and
    flushed: _desired_quantity_for_binding recomputes from current local
    state, so a card that has just moved off this identity is already
    excluded and the number written is the correct reduced one. No
    subtraction arithmetic of our own, and no second code path -- the
    same counting rule every other push uses.
    """
    session.flush()
    quantity = _desired_quantity_for_binding(session, binding)
    now = datetime.now(timezone.utc)
    try:
        update_inventory_prices_by_product([{
            "product_type": "mtg_single",
            "product_id": binding.product_id,
            "price_cents": None,
            "quantity": quantity,
        }])
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning(
            "identity correction: quantity push FAILED for binding %s "
            "(product %s, wanted %s): %s: %s",
            binding.id, binding.product_id, quantity, type(exc).__name__, exc,
        )
        raise QuantityPushFailed(
            f"Mana Pool would not accept the quantity change for the old listing "
            f"({binding.product_id}): {exc}"
        ) from exc
    binding.last_quantity_push_attempted_at = now
    binding.last_quantity_push_failure_detail = None
    logger.info(
        "identity correction: old listing quantity set to %s for binding %s (product %s)",
        quantity, binding.id, binding.product_id,
    )
    return quantity


def bindings_backing_card(session: Session, card: InventoryCard) -> list[RemoteProductBinding]:
    """Every validated binding this card currently backs -- by identity
    and by explicit membership.

    Both, because the two can disagree: the identity match is what
    _desired_quantity_for_binding counts, while local_card_ids_json is
    what apply_printing_correction detaches from. A card listed under one
    and attached to the other would otherwise have half its listing left
    standing.
    """
    found = {}
    by_identity = _resolve_binding_for_card(session, card)
    if by_identity is not None:
        found[by_identity.id] = by_identity
    for binding in session.query(RemoteProductBinding).filter(
        RemoteProductBinding.provider == "manapool",
    ):
        if card.id in json.loads(binding.local_card_ids_json or "[]"):
            found[binding.id] = binding
    return list(found.values())


# What happened to one card's return-to-sellable push. Shown to the
# operator in plain words on the confirm page -- a card that came back
# locally but is not on sale must say so, or it silently never sells.
RETURN_PUSH_OUTCOMES = {
    "pushed": "back in inventory and listed on Mana Pool",
    "no_price": "back in inventory, not listed: no price",
    "never_listed": "back in inventory, not listed: it has never been listed "
                    "-- the next sync will publish it",
    "push_failed": "back in inventory; Mana Pool could not be updated just now "
                   "-- the next sync will raise it",
}


def push_return_to_sellable(session: Session, cards: list) -> list[dict]:
    """Tell Mana Pool a card is sellable again, immediately.

    The mirror image of push_for_cards' reduction case, and deliberately
    the same machinery: reducing stock has pushed within a second since
    v1.107.0 while returning it waited for the next Perform Sync -- up to
    eight hours of a card sitting off-sale for no safety gain. The
    justification for the asymmetry (relisting is a pricing decision) has
    not held since the bulk pricing cron started repricing every listing
    three times a day.

    Never raises. The local sellability change has already committed and
    is correct -- the card IS sellable; Mana Pool just has not heard yet.
    A failure is stamped on the binding exactly as a reduction's is, and
    the next reconciliation raises it.

    Returns one outcome dict per card so the caller can say what happened.
    """
    outcomes = []
    pushable = []
    for card in cards:
        # Binding first, deliberately. A card that has never been listed
        # also usually has no price, and reporting THAT as the reason
        # would interrupt the operator over a card that was never going
        # to be listed by this path anyway. "No price" is only worth
        # saying about a card that otherwise would have gone on sale.
        if _resolve_binding_for_card(session, card) is None:
            # Publishing a first listing is the new-listing path's job --
            # it carries a pricing decision this does not.
            outcomes.append({"card_id": card.id, "name": card.name, "outcome": "never_listed"})
            continue
        if card.current_price is None:
            # Listing at no price is worse than not listing. Same rule the
            # reconciliation raise path applies to its 126 held rows.
            outcomes.append({"card_id": card.id, "name": card.name, "outcome": "no_price"})
            continue
        pushable.append(card)

    if not pushable:
        return outcomes

    before = {
        binding.id: binding.last_quantity_push_failure_detail
        for binding in {
            _resolve_binding_for_card(session, card).id: _resolve_binding_for_card(session, card)
            for card in pushable
        }.values()
    }
    push_for_cards(session, pushable)
    for card in pushable:
        binding = _resolve_binding_for_card(session, card)
        failed = binding is not None and binding.last_quantity_push_failure_detail
        outcomes.append({
            "card_id": card.id, "name": card.name,
            "outcome": "push_failed" if failed else "pushed",
            "detail": binding.last_quantity_push_failure_detail if failed else None,
        })
        if failed and before.get(binding.id) != binding.last_quantity_push_failure_detail:
            logger.warning(
                "return to sellable: card %s is available locally but Mana Pool "
                "was not updated: %s",
                card.id, binding.last_quantity_push_failure_detail,
            )
    return outcomes


# Outcomes that do NOT warrant interrupting the operator with a page.
# "pushed" is the happy path. "never_listed" is the ordinary state of a
# card that has simply never been listed -- showing a page for it would
# fire on most un-removes and train the operator to click through, which
# is exactly how the one outcome that matters (no price) would get
# missed. Both still appear per-row in bulk results, where they are a
# column rather than an interruption.
_QUIET_RETURN_OUTCOMES = {"pushed", "never_listed"}


def return_push_summary(outcomes: list[dict]) -> str:
    """One plain sentence per card the operator needs to act on.

    Silent unless a card came back into inventory and is NOT on sale for
    a reason someone can do something about."""
    notable = [o for o in outcomes
               if o.get("outcome") not in _QUIET_RETURN_OUTCOMES]
    if not notable:
        return ""
    return " ".join(
        f"{o['name']}: {RETURN_PUSH_OUTCOMES.get(o['outcome'], o['outcome'])}."
        for o in notable
    )
