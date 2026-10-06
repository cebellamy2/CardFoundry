"""One safe way to change a card's listing identity.

WHY THIS EXISTS. Printing, condition, finish and language are the four
fields a Mana Pool listing is keyed on. Changing any of them locally,
without telling Mana Pool, leaves the OLD listing live at the OLD
product_id with its quantity intact and nothing backing it -- the
remote_only_unmanaged class, which nothing reconciles. That is exactly
the 2026-09-07 incident (v1.119.0's condition backfill orphaned 1,924
listings; six real orders arrived against them).

Two live paths could do it. apply_printing_correction changes all of
them at once, and the card edit form changes condition and finish. Both
now come through here, so there is one rule rather than two that can
drift.

THE RULE. If the card currently backs a Mana Pool listing, that
listing's quantity is reduced on Mana Pool FIRST -- through the existing
quantity-push machinery, recomputing from local state exactly as every
other push does, no arithmetic of our own -- and the local change only
proceeds if that write lands. If Mana Pool refuses, nothing changes at
all: the correction is abandoned, not completed-and-flagged.

The new identity is deliberately NOT listed here. Publishing is the
new-listing path's job on the next sync, and it carries a pricing
decision this function has no business making.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from actor_context import current_actor

from manapool_quantity_push_service import (
    QuantityPushFailed,
    bindings_backing_card,
    push_binding_quantity_strict,
)
from models import InventoryListingStatus

logger = logging.getLogger("cardfoundry")

IDENTITY_FIELDS = ("mtgjson_id", "language_id", "condition_id", "finish_id",
                   "scryfall_id", "set_code", "collector_number")


def identity_snapshot(card) -> dict:
    return {field: getattr(card, field, None) for field in IDENTITY_FIELDS}


def identity_would_change(card, after: dict) -> bool:
    """Whether any listing-identity field actually moves.

    Only the four Mana Pool keys on. set_code/collector_number/scryfall_id
    are carried in the snapshot for the audit trail, but a card whose
    printing is re-labelled without its mtgjson identity moving is not a
    different listing and must not pay for a remote write.
    """
    before = identity_snapshot(card)
    return any(
        str(before.get(field) or "").upper() != str(after.get(field) or "").upper()
        for field in ("mtgjson_id", "language_id", "condition_id", "finish_id")
    )


def clear_listing_status(session: Session, card_id: int) -> None:
    """Drop the card's cached listed/not_listed value.

    Deleting rather than setting "not_listed": the cache answers "is this
    card's identity live on Mana Pool", and immediately after a
    correction the honest answer is "not known until the next
    reconciliation". Writing not_listed would be a claim, and the next
    sync overwrites it either way.
    """
    deleted = (
        session.query(InventoryListingStatus)
        .filter(InventoryListingStatus.inventory_card_id == card_id)
        .delete(synchronize_session=False)
    )
    if deleted:
        logger.info(
            "identity correction: cleared cached listing status for card %s", card_id,
        )


def bindings_to_retire(session: Session, card) -> list:
    """The bindings backing the card RIGHT NOW.

    Must be called BEFORE the identity fields change. Once the card has
    moved, an identity lookup finds the NEW binding instead, and pushing
    that one would write the new listing's quantity while leaving the old
    listing exactly as it was -- the orphan this module exists to
    prevent, with an extra remote write for company.
    """
    return bindings_backing_card(session, card)


def retire_old_listings(session: Session, bindings: list, card_id: int) -> dict:
    """Reduce those listings' quantities on Mana Pool.

    Must be called AFTER the identity fields have changed: the push
    recomputes the desired quantity from current local state, so the card
    no longer matching is what makes the number drop. Called before the
    change, it would rewrite the number it already had.

    Raises QuantityPushFailed if Mana Pool will not take the write. The
    caller must let that abort the whole correction.
    """
    if not bindings:
        logger.info(
            "identity correction: card %s backed no validated binding -- "
            "no Mana Pool write needed", card_id,
        )
        return {"pushed": [], "bindings": 0}
    pushed = []
    for binding in bindings:
        quantity = push_binding_quantity_strict(session, binding)
        pushed.append({
            "binding_id": binding.id,
            "product_id": binding.product_id,
            "quantity_written": quantity,
        })

    return {"pushed": pushed, "bindings": len(bindings)}


# --- the step that was missing after a correction -----------------------
#
# retire_old_listings() reduces the OLD listing's quantity on Mana Pool,
# which is the part that matters to a buyer. It deliberately does not
# touch the binding ROW -- but nothing else did either, so the old binding
# stayed `validated` and kept claiming the card in local_card_ids_json.
# listing_integrity_service.identity_drift_rows reads exactly that
# membership, so every sync tick since has logged LISTING_IDENTITY_DRIFT
# for a card whose listing was already correctly zeroed.
#
# Confirmed live 2026-10-06: card 10997 (Hunting Grounds) was corrected
# NM -> MP by the operator at 02:02; the guard zeroed binding 7089's
# listing in the same action (quantity_written 0, audit row 18531); the
# 02:35 tick then published the card properly as binding 7596. Only the
# stale membership was left behind.

RETIREMENT_ACTION = "binding_membership_retired"


def _membership(binding) -> list:
    try:
        return list(json.loads(binding.local_card_ids_json or "[]"))
    except (TypeError, ValueError):
        logger.warning(
            "identity correction: binding %s has unreadable local_card_ids_json; "
            "treating its membership as empty rather than guessing.", binding.id,
        )
        return []


def _identity_matches(binding, card) -> bool:
    """Whether this binding ASSERTS the card's current four-key identity.

    Same rule as listing_integrity_service.identity_drift_rows: a key the
    binding leaves NULL is not a disagreement, it is an override binding
    that counts by membership instead.
    """
    for key in ("mtgjson_id", "language_id", "condition_id", "finish_id"):
        binding_value = getattr(binding, key, None)
        if not binding_value:
            continue
        if str(getattr(card, key, None) or "").upper() != str(binding_value).upper():
            return False
    return True


def superseded_memberships(session: Session) -> list[dict]:
    """Available cards still claimed by a binding that no longer describes
    them, where another VALIDATED binding does.

    ★ THE SUPERSEDING BINDING IS REQUIRED. Without one, dropping the
    membership would leave the card claimed by nothing at all, which is a
    worse state than a stale claim -- the card would look brand new to the
    new-listing path while its old listing still existed. So a stale
    membership with no replacement is reported by the drift log and left
    exactly where it is.

    Only `available` cards: a removed or sold card's stale membership
    costs nothing and clearing it would be rewriting history, which is the
    same line identity_drift_rows already draws.
    """
    from models import InventoryCard, RemoteProductBinding

    bindings = list(
        session.query(RemoteProductBinding).filter_by(provider="manapool").all()
    )
    found = []
    for binding in bindings:
        for card_id in _membership(binding):
            card = session.get(InventoryCard, card_id)
            if card is None or card.status != "available":
                continue
            if _identity_matches(binding, card):
                continue
            superseding = [
                other for other in bindings
                if other.id != binding.id
                and other.binding_status == "validated"
                and _identity_matches(other, card)
                and card_id in _membership(other)
            ]
            if not superseding:
                logger.info(
                    "identity correction: card %s is claimed by binding %s, which no "
                    "longer describes it, but no other validated binding does either "
                    "-- left alone.", card_id, binding.id,
                )
                continue
            found.append({
                "card_id": card_id,
                "card_name": card.name,
                "stale_binding_id": binding.id,
                "stale_product_id": binding.product_id,
                "stale_identity": "/".join(
                    str(getattr(binding, k, None) or "-")
                    for k in ("language_id", "condition_id", "finish_id")),
                "card_identity": "/".join(
                    str(getattr(card, k, None) or "-")
                    for k in ("language_id", "condition_id", "finish_id")),
                "superseding_binding_id": superseding[0].id,
                "superseding_product_id": superseding[0].product_id,
                "membership_before": _membership(binding),
            })
    return found


def retire_membership(session: Session, row: dict) -> dict:
    """Drop ONE card from ONE stale binding's membership list.

    ★ WHAT "RETIRED" MEANS HERE, AND WHAT IT DOES NOT.
    It removes the card id from the binding's local_card_ids_json. That is
    all. It does NOT delete the binding row, does NOT change
    binding_status, and does NOT touch Mana Pool.

    binding_status is left alone deliberately. Production has exactly ONE
    value in that column ("validated", 7,588 rows) and 28 call sites read
    it; inventing a second value to express this would put a brand-new
    state in front of all of them. It would also drop the product out of
    the mirror's bound_product_ids, demoting its listing from
    zero_candidate -- a row the system still manages and writes down to 0
    -- into remote_only_unmanaged, which nothing acts on. That is a loss,
    not a cleanup.

    ★ REFUSES IF THE DESIRED QUANTITY WOULD MOVE. For a binding with an
    mtgjson_id the quantity is counted by identity and membership is
    irrelevant, so removal is inert. For an override binding with no
    mtgjson_id it is counted by MEMBERSHIP, and removing a card there
    would drop the number -- which the next push would send to Mana Pool.
    Rather than reason about which case we are in, this measures the
    quantity before and after and refuses if it moved.

    Caller commits. Returns the before/after needed to undo it.
    """
    from manapool_quantity_push_service import _desired_quantity_for_binding
    from models import RemoteProductBinding

    binding = session.get(RemoteProductBinding, row["stale_binding_id"])
    if binding is None:
        raise ValueError(f"Binding {row['stale_binding_id']} no longer exists.")
    before = _membership(binding)
    if row["card_id"] not in before:
        return {"changed": False, "reason": "card is no longer in this membership"}

    quantity_before = _desired_quantity_for_binding(session, binding)
    after = [cid for cid in before if cid != row["card_id"]]
    binding.local_card_ids_json = json.dumps(after)
    session.flush()
    quantity_after = _desired_quantity_for_binding(session, binding)

    if quantity_after != quantity_before:
        binding.local_card_ids_json = json.dumps(before)
        session.flush()
        logger.warning(
            "identity correction: retiring card %s from binding %s would move its "
            "desired quantity %s -> %s, which the next push would send to Mana "
            "Pool; refused and left unchanged.",
            row["card_id"], binding.id, quantity_before, quantity_after,
        )
        return {
            "changed": False,
            "reason": f"desired quantity would move {quantity_before} -> {quantity_after}",
            "quantity_before": quantity_before, "quantity_after": quantity_after,
        }

    return {
        "changed": True,
        "binding_id": binding.id,
        "card_id": row["card_id"],
        "membership_before": before,
        "membership_after": after,
        "desired_quantity": quantity_before,
    }


def retire_membership_audited(session: Session, row: dict, *, script: str) -> dict:
    """retire_membership plus its audit row. THE one write both callers use.

    The one-off backfill and the live correction path reach this function
    by different routes -- see supersession_rows_for_correction for why
    the candidate TEST has to differ -- but the mutation, the quantity
    guard, the audit shape and therefore the undo are identical, which is
    the part that must not diverge.

    Caller commits.
    """
    from models import InventoryChangeLog

    outcome = retire_membership(session, row)
    if not outcome.get("changed"):
        return outcome
    session.add(InventoryChangeLog(
        actor=current_actor(),
        inventory_card_id=row["card_id"],
        change_summary=json.dumps({
            "action_type": RETIREMENT_ACTION,
            "binding_id": outcome["binding_id"],
            "product_id": row.get("stale_product_id"),
            "membership_before": outcome["membership_before"],
            "membership_after": outcome["membership_after"],
            "stale_identity": row.get("stale_identity"),
            "card_identity": row.get("card_identity"),
            "superseding_binding_id": row.get("superseding_binding_id"),
            "desired_quantity_unchanged": outcome["desired_quantity"],
            "script": script,
            "reason": (
                "This binding no longer describes the card, and its Mana Pool "
                "listing has already been reduced. Membership only: the row is "
                "not deleted, binding_status is unchanged, and nothing is "
                "written to Mana Pool."
            ),
            "undo": "identity_change_service.restore_membership(binding_id, membership_before)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, sort_keys=True),
    ))
    logger.info(
        "identity correction: card %s retired from binding %s (membership %s -> "
        "%s); desired quantity unchanged at %s.",
        row["card_id"], outcome["binding_id"], outcome["membership_before"],
        outcome["membership_after"], outcome["desired_quantity"],
    )
    return outcome


def supersession_rows_for_correction(session: Session, bindings: list, card) -> list[dict]:
    """The old bindings that no longer describe this just-corrected card.

    ★ WHY THIS DOES NOT REQUIRE A SUPERSEDING BINDING, AND THE BACKFILL
    DOES. At correction time the replacement does not exist yet: this
    module deliberately leaves publishing to the new-listing path on the
    next sync (see the module docstring), so the new binding appears
    minutes later. Card 10997 is the worked example -- corrected 02:02,
    superseded binding created 02:35. Demanding a replacement here would
    make the wiring a no-op and the drift would keep recurring, which is
    the whole thing this is meant to stop.

    What stands in for that evidence is stronger anyway: the caller has
    just moved the card's identity AWAY from these bindings and pushed
    their listings down, in the same transaction. The card being claimed
    by nothing for a few minutes is the correct state -- it is exactly
    what tells the new-listing path there is something to publish.

    The BACKFILL has no such evidence. It looks at history it did not
    make, so it keeps the stricter test.
    """
    rows = []
    for binding in bindings:
        if _identity_matches(binding, card):
            continue
        if card.id not in _membership(binding):
            continue
        rows.append({
            "card_id": card.id,
            "card_name": card.name,
            "stale_binding_id": binding.id,
            "stale_product_id": binding.product_id,
            "stale_identity": "/".join(
                str(getattr(binding, k, None) or "-")
                for k in ("language_id", "condition_id", "finish_id")),
            "card_identity": "/".join(
                str(getattr(card, k, None) or "-")
                for k in ("language_id", "condition_id", "finish_id")),
            "superseding_binding_id": None,
            "membership_before": _membership(binding),
        })
    return rows


def retire_memberships_after_correction(session: Session, bindings: list, card,
                                        *, script: str = "identity_correction") -> list[dict]:
    """Drop the just-corrected card from the bindings it has moved away from.

    ★ FOR THE CARD EDIT PATH ONLY, and deliberately NOT inside
    retire_old_listings. printing_correction_service already detaches the
    card itself immediately after that call -- updating the membership,
    re-hashing the evidence, and deleting a binding whose last card has
    left. Doing it inside the shared function pre-empts that loop, which
    then sees the card already gone and skips the binding entirely,
    leaving an empty binding behind that used to be deleted. The card edit
    form has no such loop at all, which is the real gap: card 10997 went
    through it (audit row 18531, actor cebellamy2@gmail.com) and that is
    why its membership was left stale.

    Must be called AFTER retire_old_listings, for the same reason that
    function documents: the push recomputes from current local state, and
    an override binding counts by MEMBERSHIP, so removing it first could
    change the number that was just sent.

    Never raises. The listing is already down -- the part a buyer can see
    -- and losing a whole correction over a bookkeeping step would be the
    worse trade. A failure logs and leaves the row for
    retire_superseded_bindings.py.
    """
    retired = []
    try:
        for row in supersession_rows_for_correction(session, bindings, card):
            outcome = retire_membership_audited(session, row, script=script)
            if outcome.get("changed"):
                retired.append(outcome)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "identity correction: card %s had its listings reduced, but the stale "
            "membership could not be retired (%s: %s); the correction stands and "
            "retire_superseded_bindings.py will pick it up.",
            getattr(card, "id", None), type(exc).__name__, exc,
        )
    return retired


def restore_membership(session: Session, binding_id: int, membership_before: list) -> bool:
    """The undo. Puts the recorded membership back exactly as it was.

    Takes the recorded BEFORE list rather than re-deriving it, so an undo
    restores the state that was actually captured, not a guess at it.
    Caller commits.
    """
    from models import RemoteProductBinding

    binding = session.get(RemoteProductBinding, binding_id)
    if binding is None:
        logger.warning(
            "identity correction: cannot restore membership -- binding %s is gone.",
            binding_id,
        )
        return False
    binding.local_card_ids_json = json.dumps(list(membership_before))
    session.flush()
    logger.info(
        "identity correction: restored binding %s membership to %s.",
        binding_id, membership_before,
    )
    return True


def identity_would_change_from(before: dict, card) -> bool:
    """Same test as identity_would_change, from a snapshot taken earlier.

    The card edit form mutates the card in place across a long block, so
    there is no "after" dict to compare against -- only the card itself
    and a snapshot from before it was touched.
    """
    return any(
        str(before.get(field) or "").upper()
        != str(getattr(card, field, None) or "").upper()
        for field in ("mtgjson_id", "language_id", "condition_id", "finish_id")
    )
